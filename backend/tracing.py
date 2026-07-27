"""Lightweight step-by-step tracing for manual pipeline testing.

Enabled by default so a test run prints exactly what's happening at each
stage. Disable with TRACE=0 in the environment once you don't need it.
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
    extras = " ".join(f"{key}={_shorten(value)!r}" for key, value in fields.items())
    line = f"[TRACE +{elapsed:6.2f}s] {label}"
    if extras:
        line += f" | {extras}"
    print(line, flush=True)


def _shorten(value: object, limit: int = 160) -> object:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"...(+{len(value) - limit} chars)"
    return value