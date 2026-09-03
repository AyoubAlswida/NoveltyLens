"""Secrets cannot reach persistence, logs, the API, SSE, or the frontend --
tested along the **whole** failure path, not just against the helper.

**Defence in depth, on purpose.** `search/google.py`'s `_raise_for_status`
already strips the SerpAPI key from its own errors. This file assumes it did not.
A defence that lives only at the provider boundary fails the moment a provider is
added, a library is upgraded, or an exception takes a path nobody anticipated --
and the last place anything can be stopped is where the executor writes the
failure into PostgreSQL and the API reads it back out.

**The defect this pins was reproducible.** `_safe_detail` returned the first line
of `str(error)` unchanged, so an ordinary quota failure

    429 Client Error for url: https://serpapi.com/search?api_key=SECRET123&q=x

was written verbatim into the terminal `error` event and into
`verification_runs.failure_detail`, both of which are read back and rendered.
CLAUDE.md records that this exact shape reached a devtools console once already.

The end-to-end tests here drive a graph that raises a leaky exception, let the
real executor persist the failure, and then **read the PostgreSQL rows**.
"""

import logging
from pathlib import Path
from typing import TypedDict

import pytest
from langgraph.graph import END, START, StateGraph

from checkpointer import checkpointer
from workflow import events, lease, redaction, store
from workflow.executor import Executor, GraphAdapter, _safe_detail
from workflow.types import (
    CheckpointUnavailable,
    FencedError,
    RunStatus,
    WorkflowGate,
)

BACKEND = Path(__file__).resolve().parent.parent
ACTOR = "testnovelty@gmail.com"


# --- the corpus of realistic hostile exceptions ---------------------------
#
# Each entry is (label, message, the substring that must never survive).

LEAKY_MESSAGES = [
    (
        "serpapi-query-key",
        "429 Client Error for url: https://serpapi.com/search?engine=google_patents"
        "&q=widget&api_key=sk-live-SERPSECRET123",
        "sk-live-SERPSECRET123",
    ),
    (
        "serpapi-url-encoded-key",
        "402 Payment Required: https://serpapi.com/search?api_key=SEC%2FRET%2BENC111&q=x",
        "SEC%2FRET%2BENC111",
    ),
    (
        "epo-client-secret",
        "OAuth token request failed: client_secret=EPO_CLIENT_SECRET_456"
        "&grant_type=client_credentials at https://ops.epo.org/3.2/auth/accesstoken",
        "EPO_CLIENT_SECRET_456",
    ),
    (
        "epo-client-id-and-secret",
        "https://ops.epo.org/auth?client_id=EPOID999&client_secret=EPOSEC888 -> 401",
        "EPOSEC888",
    ),
    (
        "postgres-dsn",
        "connection failed: postgresql://agenticai:DBPASSWORD789@127.0.0.1:5432/agenticai",
        "DBPASSWORD789",
    ),
    (
        "postgres-dsn-query-credential",
        "could not connect: postgresql://u:p@db:5432/x?sslpassword=SSLKEYPASS42&sslmode=require",
        "SSLKEYPASS42",
    ),
    (
        "bearer-in-authorization-header",
        "401 Unauthorized -- Authorization: Bearer hf_LIVETOKEN_abcdef123456",
        "hf_LIVETOKEN_abcdef123456",
    ),
    (
        "bare-bearer-token",
        "gateway rejected Bearer sk-proj-LIVEKEY0987654321 with 403",
        "sk-proj-LIVEKEY0987654321",
    ),
    (
        "basic-authorization",
        "Authorization: Basic dXNlcjpzdXBlcnNlY3JldA==",
        "dXNlcjpzdXBlcnNlY3JldA==",
    ),
    (
        "x-api-key-header",
        "request headers: X-API-Key: LENS_TRIAL_KEY_777, Accept: application/json",
        "LENS_TRIAL_KEY_777",
    ),
    (
        "mixed-case-parameter",
        "GET https://api.lens.org/patent/search?API_Key=MiXeDcAsE999&size=10 failed with 403",
        "MiXeDcAsE999",
    ),
    (
        "uppercase-token-parameter",
        "callback https://gateway/cb?ACCESS_TOKEN=ya29.UPPERCASESECRET&state=1",
        "ya29.UPPERCASESECRET",
    ),
    (
        "password-parameter",
        "redirect to https://internal/login?username=svc&password=P%40ssw0rd%21",
        "P%40ssw0rd%21",
    ),
    (
        "multiline-provider-body",
        '502 Bad Gateway from provider\n{"error": {"message": "quota", '
        '"api_key": "BODYKEY_LEAKED_555", "trace_id": "abc"}}\n  at handler.js:42',
        "BODYKEY_LEAKED_555",
    ),
    (
        "multiline-xml-body",
        "EPO returned an error\n<?xml version=\"1.0\"?><fault><token>XMLTOKEN444</token></fault>",
        "XMLTOKEN444",
    ),
    (
        "python-traceback",
        'RuntimeError: call failed\nTraceback (most recent call last):\n'
        '  File "search/epo.py", line 88, in get_patent_full_text\n'
        '    raise RuntimeError(f"secret={SECRET_IN_TRACE_333}")',
        "SECRET_IN_TRACE_333",
    ),
    (
        "plpgsql-context",
        "run 9d1c is not leased to worker-1 at token 3\n"
        "CONTEXT:  PL/pgSQL function _lock_run_and_require_lease(uuid,text,bigint) line 12",
        "PL/pgSQL",
    ),
]


def _all_sinks(text: str) -> dict[str, str]:
    """Every string this module is responsible for keeping clean."""
    error = RuntimeError(text)
    return {
        "safe_detail(untrusted)": redaction.safe_detail(error, step="enrich_epo"),
        "safe_detail(trusted)": redaction.safe_detail(error, trusted=True),
        "safe_log": redaction.safe_log(error),
        "redact": redaction.redact(redaction.first_line(text)),
        "_safe_detail": _safe_detail(error),
    }


# --- the helper -----------------------------------------------------------


@pytest.mark.parametrize("label,message,secret", LEAKY_MESSAGES, ids=[c[0] for c in LEAKY_MESSAGES])
def test_no_sink_leaks_the_secret(label, message, secret):
    for sink, output in _all_sinks(message).items():
        assert secret not in output, f"{sink} leaked {secret!r}: {output}"


@pytest.mark.parametrize("label,message,secret", LEAKY_MESSAGES, ids=[c[0] for c in LEAKY_MESSAGES])
def test_no_sink_carries_a_traceback_or_a_body(label, message, secret):
    """One line only, and nothing that starts a response body."""
    for sink, output in _all_sinks(message).items():
        assert "\n" not in output, f"{sink} kept a second line"
        assert "Traceback (most recent" not in output
        assert "CONTEXT:" not in output
        assert len(output) <= redaction.MAX_DETAIL_CHARS


def test_the_originally_reported_defect_is_fixed():
    """The exact reproduction from the Gate A review."""
    error = RuntimeError(
        "429 Client Error for url: https://serpapi.com/search?api_key=SECRET123&q=x"
    )
    detail = _safe_detail(error)
    assert "SECRET123" not in detail
    assert detail == "SerpAPI returned HTTP 429."


# --- what survives, deliberately -----------------------------------------


def test_the_actionable_facts_survive():
    """A quota message is exactly what a user needs. Redaction removes the
    credential, not the diagnosis: the provider, the HTTP status and the step all
    survive, because "SerpAPI returned 429" is what sends someone to the right
    place."""
    detail = _safe_detail(
        RuntimeError("429 Client Error for url: https://serpapi.com/search?api_key=S"),
        step="keyword_search",
    )
    assert "SerpAPI" in detail and "429" in detail and "keyword_search" in detail


def test_an_unrecognised_host_is_not_named():
    """An allow-list, not a parser. An unrecognised host could be an internal
    address, and naming it is itself a small disclosure."""
    detail = _safe_detail(RuntimeError("500 from https://internal-vault.corp.local/x"))
    assert "internal-vault" not in detail
    assert "500" in detail


def test_a_workflow_error_is_trusted_and_keeps_its_message():
    """`WorkflowError` messages are built by this codebase and by the SQL
    functions, which interpolate run ids, statuses and tokens and nothing else --
    so the diagnosis survives in full (still redacted, as belt and braces)."""
    detail = _safe_detail(FencedError("run 9d1c is not leased to worker-1 at token 3"))
    assert detail == "run 9d1c is not leased to worker-1 at token 3"


def test_an_untrusted_exception_message_is_rebuilt_not_passed_through():
    """The second layer. Redaction is pattern-matching and patterns are never
    complete, so an exception this codebase did not raise contributes only the
    facts we extracted on purpose."""
    detail = _safe_detail(RuntimeError("mystery failure carrying UNKNOWNSHAPE_9f8e7d"))
    assert "UNKNOWNSHAPE_9f8e7d" not in detail
    assert detail == "an unexpected RuntimeError occurred."


def test_redaction_is_idempotent():
    """Running it twice must not mangle its own output -- the bug that left a
    stray `]` and, worse, let an Authorization token survive."""
    once = redaction.redact("Authorization: Bearer TOKEN123 and api_key=KEY456")
    twice = redaction.redact(once)
    assert once == twice
    assert "TOKEN123" not in once and "KEY456" not in once


# --- the checkpointer -----------------------------------------------------


def test_the_checkpointer_failure_message_is_redacted():
    from dataclasses import replace
    from unittest.mock import patch

    import checkpointer as checkpointer_module

    dsn = "postgresql://agenticai:CHECKPOINTPW321@127.0.0.1:5432/nope?password=ALSO_SECRET"
    unreachable = replace(checkpointer_module.settings, database_url=dsn)
    with patch.object(checkpointer_module, "settings", unreachable):
        with pytest.raises(CheckpointUnavailable) as caught:
            with checkpointer():
                pass
    message = str(caught.value)
    assert "CHECKPOINTPW321" not in message
    assert "ALSO_SECRET" not in message
    assert "postgresql://" not in message
    assert "127.0.0.1:5432/nope" in message, "the safe target still survives"


# --- the executor's log ---------------------------------------------------


def test_the_persistence_failure_log_is_redacted(caplog):
    """A log file is a place credentials get archived and shipped to an
    aggregator. The executor's one persistence-failure log line goes through
    `safe_log`."""
    import psycopg

    leaky = psycopg.OperationalError(
        "connection failed: postgresql://agenticai:LOGLEAKPW999@127.0.0.1:5432/agenticai"
    )
    assert "LOGLEAKPW999" not in redaction.safe_log(leaky)

    source = (BACKEND / "workflow" / "executor.py").read_text(encoding="utf-8")
    assert 'logger.error(\n                    "executor cannot reach persistence: %s", redaction.safe_log(error)' in source


def test_every_exception_interpolated_into_a_log_is_redacted():
    """Source contract over the workflow package and the API.

    A bare `%s` on an exception object is how a DSN reaches a log file, so every
    such site has to go through `safe_log`. The list is short enough to check
    exhaustively.
    """
    offenders = []
    for path in list((BACKEND / "workflow").glob("*.py")) + [
        BACKEND / "checkpointer.py",
        BACKEND / "server.py",
    ]:
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            # A log call whose final argument is the raw exception object.
            if stripped in ("error,", "error)") and "safe_log" not in text[: text.index(line) + len(line)][-400:]:
                offenders.append(f"{path.name}:{number}: {stripped}")
    assert offenders == [], f"raw exceptions interpolated into logs: {offenders}"


# --- the complete persistence path, against real PostgreSQL ---------------


class _State(TypedDict, total=False):
    steps: list[str]


_LEAKY = RuntimeError(
    "429 Client Error for url: https://serpapi.com/search?api_key=E2E_SECRET_KEY_42&q=x\n"
    '{"error": {"client_secret": "E2E_BODY_SECRET_99"}}'
)


def _boom(state: _State) -> dict:
    raise _LEAKY


def _build(saver) -> object:
    builder = StateGraph(_State)
    builder.add_node("enrich_epo", _boom)
    builder.add_edge(START, "enrich_epo")
    builder.add_edge("enrich_epo", END)
    return builder.compile(checkpointer=saver)


_ADAPTER = GraphAdapter(
    build=_build,
    initial_input=lambda run: {"steps": []},
    result_payload=lambda run, state: {},
    # Explicit no-op -- executor-only adapter, no report to store. See the
    # `persist_result` docstring on `GraphAdapter`.
    persist_result=lambda conn, run, state, data: None,
    gate_of=lambda value: WorkflowGate.A,
    review_payload=lambda run, gate, value: {},
    resume_value=lambda run, resumed: None,
)


@pytest.mark.postgres
def test_a_leaky_provider_failure_reaches_no_postgresql_row(pg, make_run, real_db, caplog):
    """**The whole path**: a graph node raises an exception carrying a live-looking
    key and a response body, the real executor fails the run, and the stored rows
    are read back out of PostgreSQL.

    Testing `_safe_detail` alone would not catch a future change that stopped
    calling it.
    """
    run_id = make_run(description="secret redaction end to end")

    with caplog.at_level(logging.DEBUG):
        Executor(
            adapter=_ADAPTER,
            checkpointer_factory=checkpointer,
            owner_id="redaction-worker",
            ttl_seconds=30,
            heartbeat_seconds=5,
            poll_interval_seconds=0.05,
        ).claim_and_run_one()

    run = store.get_run(pg, run_id=run_id, actor_email=ACTOR)
    assert run["status"] is RunStatus.FAILED, "the run failed rather than degrading"
    assert run["failure_detail"], (
        "failure_detail must actually be populated -- an always-NULL column would "
        "make every assertion about it below pass vacuously (migration 0017)"
    )

    terminal = events.terminal_event(pg, run_id=run_id)
    assert terminal is not None and terminal["event_type"] == "error"

    # 1. the terminal workflow_events row, straight out of the database
    stored_event = pg.execute(
        "SELECT payload::text FROM workflow_events WHERE run_id = %s AND is_terminal",
        (run_id,),
    ).fetchone()[0]
    # 2. verification_runs.failure_detail and failure_code
    failure_code, failure_detail = pg.execute(
        "SELECT failure_code, failure_detail FROM verification_runs WHERE id = %s",
        (run_id,),
    ).fetchone()
    # 3. every attempt row's sanitized detail column
    attempt_details = " ".join(
        str(row[0])
        for row in pg.execute(
            "SELECT detail FROM node_attempts WHERE run_id = %s", (run_id,)
        )
    )

    surfaces = {
        "workflow_events.payload": stored_event,
        "verification_runs.failure_detail": str(failure_detail),
        "verification_runs.failure_code": str(failure_code),
        "node_attempts.detail": attempt_details,
        "executor log": caplog.text,
        # 4/5. what the API and SSE would serve: both read the stored payload.
        "api/sse payload": str(terminal["payload"]),
    }
    for name, surface in surfaces.items():
        assert "E2E_SECRET_KEY_42" not in surface, f"{name} leaked the api_key"
        assert "E2E_BODY_SECRET_99" not in surface, f"{name} leaked the body secret"
        assert "serpapi.com/search?api_key" not in surface, f"{name} kept a credential URL"
        assert "Traceback" not in surface, f"{name} kept a traceback"

    assert terminal["payload"]["detail"] == "SerpAPI returned HTTP 429."
    assert terminal["payload"]["code"] == "RuntimeError"


@pytest.mark.postgres
def test_the_failure_is_still_a_failure(pg, make_run, real_db):
    """Redaction removes the credential, never the failure. No silent fallback, no
    degraded output presented as success."""
    run_id = make_run(description="redaction does not soften a failure")
    Executor(
        adapter=_ADAPTER,
        checkpointer_factory=checkpointer,
        owner_id="redaction-worker-2",
        ttl_seconds=30,
        heartbeat_seconds=5,
        poll_interval_seconds=0.05,
    ).claim_and_run_one()

    run = store.get_run(pg, run_id=run_id, actor_email=ACTOR)
    assert run["status"] is RunStatus.FAILED
    assert run["failure_code"], "a FAILED run always names its failure"
    assert events.terminal_event(pg, run_id=run_id)["event_type"] == "error"
    assert lease.lease_row(pg, run_id=run_id)["owner_id"] is None, "the lease was released"
