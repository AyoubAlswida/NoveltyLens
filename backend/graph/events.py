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
    "filter",
    "enrich",
    "semantic_search",
    "fuse",
    "validate",
    "compare",
    "analyze",
    # --- Advanced Search ---
    #
    # Emitted ONLY on a run with `advanced_search`. A search-only run traverses
    # exactly the nodes it did before and emits exactly the ten phases above --
    # these names existing in the vocabulary does not put them in a search-only
    # stream.
    #
    # The phases of the withdrawn evidence layer and its gates (`normalize`,
    # `screen_dates`, `enrich_epo`, `build_passages`, `confirm_eligibility`,
    # `rank_candidates`, `verify`, `gate_b`, `gate_c`, `draft_report`, `gate_d`)
    # were removed with their nodes. Unlike `semantic_search`/`fuse` below, those
    # nodes are not coming back, so keeping their names in the vocabulary would
    # describe a checklist the product can no longer produce.
    "decompose",
    # The SEARCH STRATEGY stage. Emitted between the approved understanding and
    # the search that uses it: `strategy` while the queries are being proposed,
    # `gate_strategy` (below) while the run waits for a person to approve them.
    "strategy",
    # --- Human-in-the-Loop gates ---
    #
    # A gate phase is emitted immediately BEFORE the node calls `interrupt()`, so
    # the checklist shows what the run is waiting for rather than going quiet.
    # The pause itself is carried by the `review_required` and `paused` SSE event
    # types, not by a phase -- a phase says "this step is running", and both of
    # these steps are running: they are waiting for a person.
    "gate_a",
    "gate_strategy",
)
"""The complete progress vocabulary, in checklist order.

This tuple must stay identical to the frontend's ``PHASE_ORDER`` array
(``frontend/src/app/models/report.ts``), which drives the real progress
checklist off this stream rather than a simulated timer, **minus exactly
`semantic_search` and `fuse`** whose nodes are disabled.  **Changing this tuple
means changing both files in the same commit** - there is no longer a grace
period where the backend can extend the vocabulary on its own.  The agreement is
asserted in both directions: a phase the frontend lists that the backend cannot
emit stalls the checklist, and one the backend emits that the frontend omits
hides real work.

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
