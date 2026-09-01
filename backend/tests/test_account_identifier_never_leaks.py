"""The OpenAI account identifier must not escape through any surface.

**The observed leak.** On 2026-08-28 the §14.2.1 capability gate hit an HTTP 429
and printed this to stdout::

    [TRACE + 23.72s] llm.attempt_failed | attempt=0 error="RateLimitError: Error
    code: 429 - {'error': {'message': 'Rate limit reached for gpt-4o-2024-08-06
    (for limit gpt-4o) in organization org-XXXXXXXXXXXX...

The call site was already careful — it passed the error through
`redaction.safe_log` — and the sanitizer was incomplete: its body-cut pattern
recognised a double-quoted JSON body but not the **Python dict repr** the `openai`
SDK produces, so the body was never cut, and `org-...` matches no credential
shape any other rule looks for.

An organization id is not a credential; it authenticates nothing. It is an
*account identifier*, it is never needed to act on a failure, and stdout is not a
private place — a terminal gets screenshotted, piped to a file, scrolled through
in a shared session and pasted into an issue.

**Every surface is tested, not just the one that leaked.** The defect was that a
single sanitizer had a gap; testing only the gap that was found would leave the
next one to be found the same way.

The identifier below is a **synthetic sentinel**. The real one is not reproduced
here, because a test fixture is a file in a repository.
"""

import json
import logging

import pytest
from openai import OpenAIError as _OpenAIError

from workflow import redaction

ORG = "org-SYNTHETIC0000TESTSENTINEL"
PROJECT = "proj_SYNTHETIC0000TESTSENTINEL"
REQUEST_ID = "req_SYNTHETIC0000TESTSENTINEL"

RATE_LIMIT_BODY = (
    "Error code: 429 - {'error': {'message': 'Rate limit reached for "
    f"gpt-4o-2024-08-06 (for limit gpt-4o) in organization {ORG} on tokens per "
    f"min (TPM): Limit 30000, Used 29000. Visit your account page.', "
    "'type': 'tokens', 'param': None, 'code': 'rate_limit_exceeded'}}"
)
"""The shape the SDK actually produced, with the identifier replaced."""


class RateLimitError(_OpenAIError):
    """Shaped like the SDK's, carrying the same structured fields.

    It inherits from the real `openai.OpenAIError` for two reasons: `llm.py`
    catches the SDK's own base class, so a double that did not would exercise a
    path production never takes; and `redaction.provider_from_exception` walks the
    MRO, so this is also what proves a *subclass* is still recognised as OpenAI.
    """

    def __init__(self) -> None:
        super().__init__(RATE_LIMIT_BODY)
        self.status_code = 429
        self.code = "rate_limit_exceeded"


def _forbidden(rendered: str) -> None:
    for sentinel in (ORG, PROJECT, REQUEST_ID, "org-SYNTHETIC", "proj_SYNTHETIC"):
        assert sentinel not in rendered, f"{sentinel} escaped into: {rendered!r}"


# --- the sanitizer itself --------------------------------------------------


def test_a_python_repr_body_is_cut_like_a_json_one():
    """**The exact gap.** `_BODY_START` matched `{"` and not `{'`."""
    _forbidden(redaction.redact(RATE_LIMIT_BODY))
    assert "[REDACTED]" in redaction.redact(RATE_LIMIT_BODY)


@pytest.mark.parametrize(
    "text",
    [
        f"quota exhausted in organization {ORG}",
        f"project {PROJECT} is over its limit",
        f"request {REQUEST_ID} failed",
        f"user-SYNTHETIC0000TESTSENTINEL hit a wall",
    ],
)
def test_an_account_identifier_is_removed_even_outside_a_body(text):
    """Defence in depth behind the body cut, so a body arriving in a dialect
    nobody predicted still cannot carry one through."""
    _forbidden(redaction.redact(text))


def test_ordinary_words_are_not_destroyed_by_the_identifier_rule():
    """Over-redaction is cheap and under-redaction is not, but a rule that ate
    every hyphenated word would make failures unreadable and get switched off."""
    kept = redaction.redact("the organic-rankine cycle failed during user-facing work")
    assert "organic-rankine" in kept


# --- logs ------------------------------------------------------------------


def test_safe_log_is_built_from_allow_listed_fields_only():
    """It no longer passes a redacted provider message through at all: redaction is
    pattern-matching and patterns are never complete."""
    line = redaction.safe_log(RateLimitError())
    _forbidden(line)
    assert "RateLimitError" in line
    assert "status=429" in line
    assert "code=rate_limit_exceeded" in line
    assert "Rate limit reached" not in line, "no provider prose, redacted or not"


def test_safe_log_survives_an_exception_with_no_structured_fields():
    line = redaction.safe_log(RuntimeError(f"boom in organization {ORG}"))
    _forbidden(line)
    assert "RuntimeError" in line


def test_a_logger_call_through_safe_log_writes_nothing_identifying(caplog):
    with caplog.at_level(logging.WARNING):
        logging.getLogger("leak-test").warning(
            "call failed: %s", redaction.safe_log(RateLimitError())
        )
    _forbidden(caplog.text)


# --- stdout and stderr -----------------------------------------------------


def test_the_trace_boundary_redacts_whatever_a_caller_passes(capsys):
    """**The last line of defence, and the one that was missing.** The call site
    that leaked was already calling a sanitizer; `trace` now assumes a caller will
    one day pass something it believes is safe."""
    from tracing import trace

    trace("llm.attempt_failed", attempt=0, error=RATE_LIMIT_BODY)
    _forbidden(capsys.readouterr().out)


def test_the_trace_boundary_redacts_the_label_too(capsys):
    from tracing import trace

    trace(f"failure in organization {ORG}")
    _forbidden(capsys.readouterr().out)


def test_the_openai_adapter_traces_nothing_identifying(capsys, monkeypatch):
    """End to end through the real adapter: a rate-limited call, traced and then
    raised, with the sentinel nowhere in either."""
    import llm

    class Client:
        class chat:  # noqa: N801 - mirrors the SDK's shape
            class completions:
                @staticmethod
                def create(**_):
                    raise RateLimitError()

    monkeypatch.setattr(llm, "_openai_client", lambda *a, **k: Client())
    monkeypatch.setattr(llm.time, "sleep", lambda _: None)

    with pytest.raises(RuntimeError) as raised:
        llm._openai_chat({"model": "m", "messages": []}, api_key="k", timeout=1,
                         retries=1, max_tokens=10)

    captured = capsys.readouterr()
    _forbidden(captured.out)
    _forbidden(captured.err)
    _forbidden(str(raised.value))


# --- exceptions exposed to callers, SSE, and persistence -------------------


def test_the_detail_persisted_and_served_carries_no_identifier():
    """`workflow/executor.py` writes this string into the terminal `error` event
    and into `verification_runs.failure_detail`, and the API reads both back."""
    from workflow.executor import _safe_detail

    detail = _safe_detail(RateLimitError())
    _forbidden(detail)
    assert detail == "OpenAI returned HTTP 429."


def test_the_sse_error_detail_carries_no_identifier():
    """The frontend's ErrorScreen renders this verbatim in a mono block, so it is
    a browser-visible surface. It used to be `str(error)`."""
    detail = redaction.safe_detail(RateLimitError())
    _forbidden(detail)
    assert "429" in detail, "the actionable half must survive"


def test_the_sse_and_http_error_paths_do_not_stringify_the_exception():
    """Pinned at the source, because this is a one-word regression: `safe_detail`
    back to `str` is a single edit and the tests above would still pass if a
    *different* error type were used."""
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "server.py").read_text(encoding="utf-8")
    assert "'detail': str(error)" not in source
    assert "detail=str(error)" not in source
    assert "'detail': safe_detail(error)" in source
    assert "detail=safe_detail(error)" in source


# --- the capability artifact -----------------------------------------------


def test_a_rate_limited_probe_record_carries_no_identifier():
    from unittest.mock import patch

    from verification import capability_test as runner

    with patch.object(runner, "_raw_completion", side_effect=RateLimitError()):
        record = runner._probe("4. enums", "ask", response_format=None, sleeper=lambda _: None)

    _forbidden(json.dumps(record))
    assert record["outcome"] == "RATE_LIMITED"
    assert record["passed"] is False


def test_a_rate_limited_stability_case_carries_no_identifier():
    from unittest.mock import patch

    from verification import capability_test as runner

    with patch.object(runner, "verify_feature_disclosure", side_effect=RateLimitError()):
        probe = runner._stability_probe(sleeper=lambda _: None)

    _forbidden(json.dumps(probe))
    assert probe["passed"] is False
    assert probe["operational_failures"] == probe["scheduled_calls"]
