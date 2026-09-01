"""The SerpAPI key must never reach a user-visible error message.

SerpAPI authenticates by query parameter, and ``requests``' ``HTTPError``
message embeds the full request URL. Nothing in this project treats provider
errors as internal: ``search_all_providers_strict`` interpolates them into
``PatentSearchError``, and ``server.py``'s wrapper sends ``str(error)`` to the
browser as the SSE ``error`` detail. So the default ``raise_for_status()``
would print a live credential into the user's devtools console.

This is not hypothetical - it happened, on an ordinary exhausted-quota 429.
"""

import json
from unittest.mock import patch

import requests

from search.google import _raise_for_status

SECRET = "sk-live-DO-NOT-LEAK-abc123"
SERPAPI_ERROR_URL = (
    f"https://serpapi.com/search?engine=google_patents&q=widget&api_key={SECRET}"
)


def _response(status_code: int) -> requests.Response:
    response = requests.Response()
    response.status_code = status_code
    response.url = SERPAPI_ERROR_URL
    return response


def test_http_error_message_excludes_the_api_key():
    try:
        _raise_for_status(_response(429), "search for 'widget'")
    except RuntimeError as error:
        assert SECRET not in str(error)
        assert "429" in str(error), "the status code is what makes this actionable"
    else:
        raise AssertionError("a 4xx response must raise")


def test_requests_own_raise_for_status_would_have_leaked_it():
    """Pins *why* the helper exists. If this ever stops leaking, the helper can
    be reconsidered - until then it is load-bearing, not defensive clutter."""
    response = _response(429)
    response.reason = "Too Many Requests"
    try:
        response.raise_for_status()
    except requests.HTTPError as error:
        assert SECRET in str(error), (
            "requests no longer embeds the URL; re-evaluate search.google._raise_for_status"
        )


def test_success_status_does_not_raise():
    _raise_for_status(_response(200), "search for 'widget'")


def test_the_key_does_not_reach_the_sse_error_frame():
    """End to end through the real transport: a failing provider must produce an
    error frame the user can act on and nothing they shouldn't see."""
    from fastapi.testclient import TestClient

    import server
    from auth import issue_token

    def broken(query: str, size: int):
        _raise_for_status(_response(429), f"search for {query!r}")

    analysis = json.dumps({
        "concepts": ["c"], "keywords": ["k"], "queries": ["q1"],
    })

    with patch("llm._llm_chat", return_value=analysis), \
         patch("search.search.SOURCES", (("Google Patents", broken),)):
        response = TestClient(
            server.app,
            headers={"Authorization": f"Bearer {issue_token('testnovelty@gmail.com')}"},
        ).post("/api/analyze/stream", json={"description": "a widget", "claims": None})

    body = response.text
    assert "error" in body, "expected the provider failure to surface"
    assert SECRET not in body


# ---------------------------------------------------------------------------
# The durable-workflow surfaces
# ---------------------------------------------------------------------------
#
# The same rule, applied to everything the executor persists. A terminal `error`
# event and a run's `failure_detail` are read back by `GET /api/runs/{id}` and
# rendered verbatim, exactly as the SSE `error` detail is -- which is the point (a
# 429 or a depleted-credits 402 is what the user needs to see) and the danger.
#
# Two new leak paths arrived with Gate A: the checkpointer's DSN, and whatever a
# node raises on its way into `finalize_run_failure`.


def test_the_checkpointer_error_carries_no_dsn_or_password():
    """A connection string carries a password in its userinfo and can carry more
    in its query string. `CheckpointUnavailable` can reach a stored
    `failure_detail`, so its message is built from the parsed host/port/database
    rather than echoed from the driver."""
    from dataclasses import replace

    import checkpointer as checkpointer_module
    from workflow.types import CheckpointUnavailable

    dsn = f"postgresql://agenticai:{SECRET}@127.0.0.1:5432/nope?password={SECRET}"
    unreachable = replace(checkpointer_module.settings, database_url=dsn)
    with patch.object(checkpointer_module, "settings", unreachable):
        try:
            with checkpointer_module.checkpointer():
                pass
        except CheckpointUnavailable as error:
            assert SECRET not in str(error)
            assert "postgresql://" not in str(error)
        else:
            raise AssertionError("an unreachable checkpointer must raise")


def test_a_leaky_provider_error_is_reduced_to_one_safe_line_by_the_executor():
    """`_safe_detail` is what stands between a raised provider error and the
    stored terminal `error` payload.

    **This assertion changed, and the change is the fix.** It used to expect the
    exception's first line to survive verbatim -- which is exactly the behaviour
    that leaked, because a SerpAPI failure's first line *is* the request URL and
    SerpAPI authenticates by query parameter. The old version also said the
    executor may rely on the provider layer having sanitized its own error. It may
    not: a defence that exists only at the provider boundary fails the moment a
    provider is added, a library is upgraded, or an exception takes a path nobody
    anticipated.

    An untrusted exception is now **rebuilt** from allow-listed facts -- the
    provider, the HTTP status, the step. The diagnosis survives; the prose does
    not.
    """
    from workflow.executor import _safe_detail

    error = RuntimeError(
        "SerpAPI returned 429\n"
        '  File "search/google.py", line 88, in _raise_for_status\n'
        "    raise RuntimeError(...)\n"
        "  CONTEXT: PL/pgSQL function ..."
    )
    detail = _safe_detail(error)

    assert detail == "SerpAPI returned HTTP 429."
    assert "\n" not in detail
    assert "File \"" not in detail, "no traceback frames"
    assert "CONTEXT:" not in detail, "no plpgsql stack"


def test_the_executor_does_not_rely_on_the_provider_having_sanitized_anything():
    """The defence-in-depth half, as its own test.

    A provider error reaching the executor with the key still in it -- precisely
    what a new provider or an unanticipated path would produce -- is safe by the
    time it is persisted.
    """
    from workflow.executor import _safe_detail

    detail = _safe_detail(RuntimeError(f"429 Client Error for url: {SERPAPI_ERROR_URL}"))
    assert SECRET not in detail
    assert detail == "SerpAPI returned HTTP 429."


def test_a_workflow_error_message_carries_no_payload_or_credential():
    """The SQL functions interpolate run ids, statuses and tokens into their
    messages -- never a payload, a credential or a provider body -- and
    `raise_for_sqlstate` keeps only the RAISE line, dropping plpgsql's CONTEXT
    stack."""
    from workflow.types import _first_line

    raised = (
        "run 9d1c is not leased to worker-1 at token 3\n"
        "CONTEXT:  PL/pgSQL function _lock_run_and_require_lease(uuid,text,bigint) line 12\n"
        "PL/pgSQL function append_worker_event(...) line 19 at PERFORM"
    )
    assert _first_line(raised) == "run 9d1c is not leased to worker-1 at token 3"
    assert "PL/pgSQL" not in _first_line(raised)


def test_no_workflow_module_logs_the_database_url():
    """A source contract. `settings.database_url` is passed to psycopg and to the
    checkpointer and is never formatted into a message, a log line or an event
    payload."""
    from pathlib import Path

    backend = Path(__file__).resolve().parent.parent
    offenders = []
    for path in list((backend / "workflow").glob("*.py")) + [backend / "checkpointer.py"]:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"'):
                continue
            if "database_url" not in stripped:
                continue
            # The only legitimate uses: handing it to a driver, or parsing it to
            # build the redacted target.
            if any(
                allowed in stripped
                for allowed in ("from_conn_string", "urlsplit(", "psycopg.connect")
            ):
                continue
            offenders.append(f"{path.name}:{number}: {stripped}")
    assert offenders == [], f"database_url reaches a message or log: {offenders}"


# --- the Settings object's own string form ---------------------------------
#
# `workflow/redaction.py` scrubs credentials at the persistence boundary. It
# never sees the *object's* repr, and `repr(settings)` used to print five live
# credentials -- found when an unrelated test failure printed the real HF_TOKEN,
# SERPAPI_KEY, LLM_API_KEY, EMBEDDING_API_KEY and AUTH_SECRET into the pytest
# output. Anything that renders this object carried them: a failure report, a log
# line, an f-string in an exception, a debugger session someone screenshots.

SECRET_FIELDS = (
    "hf_token",
    "serpapi_key",
    "epo_key",
    "epo_secret",
    "lens_api_key",
    "llm_api_key",
    "uspto_api_key",
    "qdrant_api_key",
    "embedding_api_key",
    "auth_secret",
    "database_url",
)
"""Every field on `Settings` that carries or embeds a credential.

`database_url` is here because a PostgreSQL DSN carries its password in the
userinfo section -- the same reason `checkpointer.py` rebuilds its error messages
from the parsed host/port/database rather than echoing the DSN.
"""


def test_no_credential_field_appears_in_the_settings_repr():
    from config import settings

    rendered = repr(settings)
    leaked = [
        name
        for name in SECRET_FIELDS
        if (value := getattr(settings, name, "")) and str(value) in rendered
    ]
    assert leaked == [], f"repr(settings) exposes: {leaked}"


def test_every_credential_field_is_declared_repr_false():
    """Asserted on the dataclass rather than on today's values, because a field
    whose value happens to be empty in this environment would pass the test above
    while still leaking in an environment where it is set."""
    import dataclasses

    from config import Settings

    fields = {field.name: field for field in dataclasses.fields(Settings)}
    missing = [
        name
        for name in SECRET_FIELDS
        if name in fields and fields[name].repr
    ]
    assert missing == [], f"credential fields still in the repr: {missing}"


def test_the_repr_still_names_the_configuration_worth_debugging():
    """The point is to remove credentials, not to make the object useless. An
    operator diagnosing a misconfiguration needs to see which model and which
    hosts are configured."""
    from config import settings

    rendered = repr(settings)
    assert "llm_model" in rendered
    assert "qdrant_url" in rendered
    assert "llm_api_url" in rendered


def test_a_formatted_settings_object_carries_no_credential():
    """`repr` is not the only path: f-strings and `str()` reach the same code for
    a dataclass, and an exception message is the usual way an object ends up in a
    log."""
    from config import settings

    for rendered in (str(settings), f"{settings}", "{}".format(settings)):
        for name in SECRET_FIELDS:
            value = getattr(settings, name, "")
            if value:
                assert str(value) not in rendered, name
