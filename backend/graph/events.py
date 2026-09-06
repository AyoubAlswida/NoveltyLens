"""Progress-event emission for graph nodes.

The SSE contract is shared, unenforced state spanning ``backend/server.py`` and
``frontend/src/app/app.ts``.  The frontend matches ``progress.phase`` against
the selected branch's phase order and, on no match, **silently ignores the event**
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

QUICK_SEARCH_PHASES: tuple[str, ...] = (
    "understand",
    "queries",
    "search",
    "filter",
    "enrich",
    "validate",
    "compare",
    "analyze",
)
"""The exact progress route for an inline Quick Search."""


ADVANCED_SEARCH_PHASES: tuple[str, ...] = (
    "decompose",
    "gate_a",
    "strategy",
    "gate_strategy",
    "search",
    "filter",
    "enrich",
    "validate",
    "compare",
    "analyze",
)
"""The exact progress route for a durable Advanced Search."""


PHASES: tuple[str, ...] = tuple(
    dict.fromkeys(
        (*QUICK_SEARCH_PHASES, *ADVANCED_SEARCH_PHASES, "semantic_search", "fuse")
    )
)
"""The complete progress vocabulary (membership, not one route's order).

The frontend declares matching ``QUICK_SEARCH_PHASE_ORDER`` and
``ADVANCED_SEARCH_PHASE_ORDER`` lists. A single checklist order is invalid:
Advanced Search pauses for two reviews before it rejoins the ordinary search,
while Quick Search never enters those review stages.

``semantic_search`` and ``fuse`` remain valid names while their nodes are
disabled, but neither route lists them until those nodes are re-enabled.

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
