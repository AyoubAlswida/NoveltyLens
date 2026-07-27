"""Progress-event emission for graph nodes.

The SSE contract is shared, unenforced state spanning ``backend/server.py`` and
``frontend/src/app/App.tsx``.  The frontend matches ``progress.phase`` against a
hard-coded ``PHASES`` array and, on no match, **silently selects the first
checklist item instead of erroring** - so a typo'd or invented phase name is
invisible in backend testing and simply wrong in the UI.

Every graph node emits through :func:`emit_progress`, which rejects any phase the
frontend does not know.  That converts the frontend's silent fallback into a loud
backend failure at the one place it can still be caught.

Nodes emit via LangGraph's custom stream writer, so events reach the client in
call order, interleaved with the node's work exactly as the legacy generator's
``yield`` statements were.
"""

from typing import Any

from langgraph.config import get_stream_writer

PHASES: tuple[str, ...] = ("understand", "queries", "search", "compare", "analyze")
"""The complete progress vocabulary, in checklist order.

This tuple must stay identical to the ``id`` values in the frontend's ``PHASES``
array (``frontend/src/app/App.tsx``).  Ticket P3.7 is the only one permitted to
change it, and it changes both files in the same commit.
"""


class SSEContractError(RuntimeError):
    """Raised when a node tries to emit an event the frontend cannot render."""


def emit_progress(phase: str, message: str, **fields: Any) -> None:
    """Emit one ``progress`` event.

    ``fields`` carries the per-phase extras the frontend reads (``count`` for
    queries; ``current``/``total``/``found`` for search and analyze).

    Raises:
        SSEContractError: if ``phase`` is not in :data:`PHASES`.
        RuntimeError: if called outside a graph run - there is no writer to emit
            to, so the event would vanish.  Drive nodes through the compiled
            graph rather than calling them directly.
    """
    if phase not in PHASES:
        raise SSEContractError(
            f"{phase!r} is not a known SSE progress phase. The frontend would "
            f"silently render this as the first checklist item. "
            f"Expected one of: {', '.join(PHASES)}."
        )
    get_stream_writer()({"type": "progress", "phase": phase, "message": message, **fields})
