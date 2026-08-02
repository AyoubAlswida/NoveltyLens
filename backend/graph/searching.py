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

Keyword search also fetches full text for every result
(``enrich_with_full_text``, so embeddings downstream see more than an abstract),
as legacy's ``_run_pipeline`` already does.  It is not wrapped in ``except``.

Indexing those results into Qdrant is a *separate* node, :func:`index_patents_node`,
which the graph runs after both search branches have joined.  It used to be the
last thing ``keyword_search_node`` did, which was wrong: the two branches fan out
from ``search_orchestrator`` but LangGraph runs a superstep's sync nodes
sequentially, keyword first, so the write reached Qdrant before Semantic Search
read it and Semantic Search retrieved this request's own keyword hits.  See
:func:`index_patents_node`.

Semantic Search therefore queries only what Qdrant holds from *earlier* requests.
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

    # `candidates` is what ranking reads.  Phase 3's fusion and validation nodes
    # will overwrite it after this point without touching `keyword_patents`.
    #
    # Indexing these into Qdrant deliberately does NOT happen here - it is
    # `index_patents_node`, which the graph runs after *both* search branches
    # have read. See that node for why.
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


def index_patents_node(state: GraphState) -> dict:
    """Index this request's keyword hits into Qdrant, for *later* requests.

    This runs after both search branches, and that ordering is the entire point.
    It used to sit at the end of ``keyword_search_node``, on the assumption that
    the two branches fanning out from ``search_orchestrator`` were isolated from
    each other. They are not: LangGraph runs a superstep's sync nodes
    sequentially, and ``keyword_search`` always goes first - so the write landed
    in Qdrant before ``semantic_search_node`` read, and semantic search
    retrieved the patents keyword search had just found. On a cold or small
    corpus that made the semantic branch a pure echo, and Result Fusion then
    credited those echoes from *both* branches, ranking them above patents only
    one branch found (see ``ranking.reciprocal_rank_fusion``).

    Placing the write downstream of the join makes read-before-write a property
    of the graph's shape rather than of the scheduler's ordering, so it cannot
    silently regress if node ordering or LangGraph's execution model changes.

    Only ``keyword_patents`` is indexed: ``semantic_patents`` came *out* of
    Qdrant and is already there.

    Raises:
        Exception: whatever the embedding call or Qdrant client raises. No
            ``except`` - an unreachable Qdrant fails the whole request rather
            than silently shipping a result whose corpus never grew.
    """
    patents = state["keyword_patents"]
    if patents:
        store_patents(patents)
    return {}
