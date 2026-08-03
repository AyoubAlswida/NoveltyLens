"""Progress-event emission for graph nodes.

The SSE contract is shared, unenforced state spanning ``backend/server.py`` and
``frontend/src/app/App.tsx``.  The frontend matches ``progress.phase`` against a
hard-coded ``PHASE_ORDER`` array and, on no match, **silently ignores the event**
(``indexOf`` returns -1 and the update is skipped) - so a typo'd or invented
phase name is invisible in backend testing and, in the UI, simply stalls the
checklist at the last phase it did recognize while the request runs on.

Every graph node emits through :func:`emit_progress`, which rejects any phase the
frontend does not know.  That converts the frontend's silent no-op into a loud
backend failure at the one place it can still be caught.

Nodes emit via LangGraph's custom stream writer, so events reach the client in
call order, interleaved with the node's work exactly as the legacy generator's
``yield`` statements were.
"""

from typing import Any

from langgraph.config import get_stream_writer

PHASES: tuple[str, ...] = (
    "understand",
    "queries",
    "search",
    "enrich",
    "semantic_search",
    "fuse",
    "validate",
    "compare",
    "analyze",
)
"""The complete progress vocabulary, in checklist order.

This tuple must stay identical to the frontend's ``PHASE_ORDER`` array
(``frontend/src/app/App.tsx``), which as of ticket P3.7 drives the real progress
checklist off this stream rather than a simulated timer.  **Changing this tuple
means changing both files in the same commit** - there is no longer a grace
period where the backend can extend the vocabulary on its own.

``semantic_search``/``fuse``/``validate`` correspond to the three Phase 3 agents
that sit between Keyword Search and Ranking; ``enrich`` covers the full-text
fetch inside Keyword Search.

Note this is the progress vocabulary, not the node list: ``index_patents``
(``graph/searching.py``) deliberately emits nothing, because indexing for later
requests is not work the user is waiting on.
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
            f"silently drop this event and stall its checklist. "
            f"Expected one of: {', '.join(PHASES)}."
        )
    get_stream_writer()({"type": "progress", "phase": phase, "message": message, **fields})
