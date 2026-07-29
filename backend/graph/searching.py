"""Search Orchestrator, Keyword Search, and Semantic Search nodes.

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

Keyword search also does two things Phase 3 needs and legacy's ``_run_pipeline``
already does: it fetches full text for every result (``enrich_with_full_text``,
so embeddings downstream see more than an abstract) and indexes the enriched
patents into Qdrant (``store_patents``) so a later, related query's Semantic
Search node can retrieve them.  Neither is wrapped in ``except`` - a Qdrant
outage here fails the whole request rather than silently shipping a keyword-only
result the user cannot tell apart from a healthy one.

Semantic Search runs as a separate node, in parallel with keyword search (both
branch off ``search_orchestrator`` in ``graph/pipeline.py``), so it queries
whatever Qdrant already holds from *earlier* requests - not this request's own
keyword hits, which have not been indexed yet when it runs.
"""

from graph.events import emit_progress
from graph.state import GraphState
from search.retrieve import retrieve_similar_patents
from search.search import enrich_with_full_text, search_all_providers_strict
from search.vector_store import store_patents

RESULTS_PER_QUERY = 4
"""Matches the value the legacy pipeline passes to ``search_patents``."""

SEMANTIC_RESULTS_LIMIT = 20
"""How many semantically similar patents to pull from Qdrant per request."""


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
    """Fan every query across every provider, enrich, index, and deduplicate.

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

    if patents:
        # Skipped entirely when nothing was found - matching legacy, which
        # returns its empty report before reaching this step. Emitting
        # "fetching full text for 0 patents" would describe work that isn't
        # happening, and store_patents([]) has nothing to index anyway.
        emit_progress("enrich", f"Fetching full text for {len(patents)} patents…")
        patents = enrich_with_full_text(patents)
        enriched_count = sum(1 for patent in patents if patent.full_text)
        emit_progress(
            "enrich",
            f"Full text retrieved for {enriched_count}/{len(patents)} patents",
            enriched=enriched_count,
            total=len(patents),
        )

        # Index into Qdrant so a later, related request's semantic_search_node
        # can retrieve these. No try/except: an unreachable Qdrant fails the
        # whole request rather than silently shipping a keyword-only result.
        store_patents(patents)

    # `candidates` is what ranking reads.  Phase 3's fusion and validation nodes
    # will overwrite it after this point without touching `keyword_patents`.
    return {"keyword_patents": patents, "candidates": patents}


def semantic_search_node(state: GraphState) -> dict:
    """Retrieve patents from Qdrant that are semantically similar to the invention.

    Runs in parallel with ``keyword_search_node``; see the module docstring for
    why it can only ever see patents indexed by *earlier* requests.

    Raises:
        Exception: whatever the embedding call or Qdrant client raises. A
            collection that simply has nothing indexed yet returns an empty
            list rather than raising - see ``search/vector_store.py``.
    """
    emit_progress("semantic_search", "Searching indexed patents by meaning…")
    patents = retrieve_similar_patents(state["invention_text"], limit=SEMANTIC_RESULTS_LIMIT)
    emit_progress(
        "semantic_search",
        f"Found {len(patents)} semantically similar patents",
        found=len(patents),
    )
    return {"semantic_patents": patents}
