"""Search Orchestrator and Keyword Search nodes.

One behavior change against the legacy path: **a single provider failure fails
the request.**  ``search_patents`` counts a run as successful when at least one
provider worked.  That means an EPO outage halves the corpus and the user still
gets a novelty verdict - one indistinguishable from a healthy run, computed
against prior art that was never retrieved.
:func:`search_all_providers_strict` raises instead.

Finding nothing is *not* a failure.  A search that reaches every provider and
gets zero results has produced a genuine answer, and the pipeline reports it as
an empty report rather than an error.  What it must never do is dress that
emptiness up: legacy pairs it with a hardcoded sentence of claim-drafting
advice that no model wrote.  ``graph/pipeline.py`` sends an empty
recommendation list instead.
"""

from graph.events import emit_progress
from graph.state import GraphState
from search.search import search_all_providers_strict

RESULTS_PER_QUERY = 4
"""Matches the value the legacy pipeline passes to ``search_patents``."""


def search_orchestrator_node(state: GraphState) -> dict:
    """Announce the search before any provider is contacted.

    Emits the opening ``search`` event only; the counts it reports are the
    starting values the frontend's progress row renders until the closing event
    replaces them.
    """
    queries = state["queries"]
    emit_progress(
        "search",
        "Searching patent databases…",
        current=0,
        total=len(queries),
        found=0,
    )
    return {}


def keyword_search_node(state: GraphState) -> dict:
    """Fan every query across every provider and deduplicate the results.

    An empty result is a legitimate outcome and is returned as one; only a
    provider that *failed* stops the request. ``graph/pipeline.py`` routes an
    empty candidate list straight to an empty report.

    Raises:
        PatentSearchError: if any single provider call fails.
    """
    queries = state["queries"]
    patents = search_all_providers_strict(queries, results_per_query=RESULTS_PER_QUERY)

    emit_progress(
        "search",
        f"Searching patent databases… {len(queries)}/{len(queries)} · {len(patents)} found",
        current=len(queries),
        total=len(queries),
        found=len(patents),
    )

    # `candidates` is what ranking reads.  Phase 3's fusion and validation nodes
    # will overwrite it after this point without touching `keyword_patents`.
    return {"keyword_patents": patents, "candidates": patents}
