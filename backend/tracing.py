"""Lightweight step-by-step tracing for manual pipeline testing.

Enabled by default so a test run prints exactly what's happening at each
stage. Disable with TRACE=0 in the environment once you don't need it.

**Every string that goes through here is redacted first, and that is not
belt-and-braces.** On 2026-08-28 the section 14.2.1 capability gate printed an
HTTP 429 body to stdout carrying the OpenAI account's organization id, through a
caller that had already been careful:

    trace("llm.attempt_failed", attempt=attempt, error=redaction.safe_log(error))

The call site was right and the sanitizer was incomplete -- it recognised a
double-quoted JSON body but not the Python dict repr the `openai` SDK produces.
That is the failure mode this module now assumes: **a caller will one day pass
something it believes is safe.** `trace` is the last point before stdout, so it
redacts unconditionally, and a caller that forgets is no longer the only defence.

stdout is not a private place. A terminal is screenshotted, piped into a file,
scrolled through in a shared session, and pasted into an issue.
"""

import os
import time

_ENABLED = os.getenv("TRACE", "1") != "0"
_start = time.monotonic()


def trace(label: str, **fields: object) -> None:
    """Print a single trace line: [TRACE +12.34s] label | key=value key=value"""
    if not _ENABLED:
        return
    elapsed = time.monotonic() - _start
    extras = " ".join(f"{key}={_shorten(_scrub(value))!r}" for key, value in fields.items())
    line = f"[TRACE +{elapsed:6.2f}s] {_scrub(label)}"
    if extras:
        line += f" | {extras}"
    print(line, flush=True)


def _scrub(value: object) -> object:
    """Redact any string on its way to stdout, whatever the caller believed.

    Imported lazily: `workflow.redaction` is a leaf module with no dependencies
    of its own, but `tracing` is imported by nearly everything and a package-level
    import here would put `workflow` on every import path in the backend.

    A failure inside the sanitizer must not take down the pipeline it is only
    narrating -- but it must not print the unsanitized value either, so the value
    is dropped rather than passed through.
    """
    if not isinstance(value, str):
        return value
    try:
        from workflow.redaction import redact

        return redact(value)
    except Exception:  # pragma: no cover - defensive; see the docstring
        return "[UNREDACTABLE]"


def _shorten(value: object, limit: int = 160) -> object:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"...(+{len(value) - limit} chars)"
    return value
