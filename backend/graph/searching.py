"""Search Orchestrator, Keyword Search, and Semantic Search nodes.

**A single provider failure fails the request.**  Counting a run as successful
when at least one provider worked - which the retired legacy path did - means an
EPO outage halves the corpus and the user still gets a novelty verdict, one
indistinguishable from a healthy run but computed against prior art that was
never retrieved.  :func:`search_all_providers_strict` raises instead.

Providers are called concurrently, but their results are collected in query
order; see that function for why the ordering is load-bearing.

Finding nothing is *not* a failure.  A search that reaches every provider and
gets zero results has produced a genuine answer, and the pipeline reports it as
an empty report rather than an error.  What it must never do is dress that
emptiness up: legacy pairs it with a hardcoded sentence of claim-drafting
advice that no model wrote.  ``graph/pipeline.py`` sends an empty
recommendation list instead.

Keyword search also fetches full text for every result
(``enrich_with_full_text``, so embeddings downstream see more than an abstract).
It is not wrapped in ``except``.

Indexing those results into Qdrant is a *separate* node, :func:`index_patents_node`,
which the graph runs after both search branches have joined.  It used to be the
last thing ``keyword_search_node`` did, which was wrong: the two branches fan out
from ``search_orchestrator``, but LangGraph runs a superstep's sync nodes
sequentially, so one branch's writes are visible to the other and the write
reached Qdrant before Semantic Search read it - Semantic Search then retrieved
this request's own keyword hits.  See :func:`index_patents_node`.

**Do not depend on which branch runs first.**  An earlier version of this
docstring asserted keyword search always does; a timed run of the real pipeline
showed the opposite (semantic completed at 4.7s, keyword not until 18.1s), so
that claim was simply wrong.  It does not matter, and that is the point: the
ordering guarantee this package relies on comes from the graph's topology, not
from the scheduler.

Semantic Search therefore queries only what Qdrant holds from *earlier* requests.
"""

import logging

from graph.events import emit_progress
from graph.state import GraphState
from search.retrieve import retrieve_similar_patents
from search.search import enrich_with_full_text, search_all_providers_strict
from search.vector_store import known_ids, store_patents

logger = logging.getLogger(__name__)

RESULTS_PER_QUERY = 4
"""Results requested per query, per provider.

With 5 generated queries this caps the keyword branch at ~20 patents before
dedup, which in turn bounds the full-text enrichment fan-out and everything
downstream of it."""

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
    found = search_all_providers_strict(queries, results_per_query=RESULTS_PER_QUERY)
    patents = found.candidates

    emit_progress(
        "search",
        f"Searching patent databases… {len(queries)}/{len(queries)} · {len(patents)} found",
        current=len(queries),
        total=len(queries),
        found=len(patents),
    )

    if patents:
        # Skipped entirely when nothing was found. Emitting "fetching full text
        # for 0 patents" would describe work that isn't happening, and
        # store_patents([]) has nothing to index anyway.
        #
        # Only `candidates` are enriched. `index_only` deliberately is not: each
        # enrichment costs a separate provider credit, and those records exist
        # to grow the corpus, not to be reasoned about in this request.
        emit_progress("enrich", f"Fetching full text for {len(patents)} patents…")
        patents = enrich_with_full_text(patents)
        enriched_count = sum(1 for patent in patents if patent.full_text)
        emit_progress(
            "enrich",
            f"Full text retrieved for {enriched_count}/{len(patents)} patents",
            enriched=enriched_count,
            total=len(patents),
        )

    # `candidates` is what ranking reads.  Fusion and validation overwrite it
    # after this point without touching `keyword_patents`.
    #
    # Indexing deliberately does NOT happen here - it is `index_patents_node`,
    # which the graph runs after *both* search branches have read. See that node.
    return {
        "keyword_patents": patents,
        "index_only_patents": found.index_only,
        "candidates": patents,
    }


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
    sequentially, so whichever branch runs first has its side effects visible to
    the other. When keyword search happened to go first, the write landed in
    Qdrant before ``semantic_search_node`` read, and semantic search retrieved
    the patents keyword search had just found. On a cold or small corpus that
    made the semantic branch a pure echo, and Result Fusion then credited those
    echoes from *both* branches, ranking them above patents only one branch
    found (see ``ranking.reciprocal_rank_fusion``).

    Placing the write downstream of the join makes read-before-write a property
    of the graph's shape rather than of the scheduler's ordering, so it holds
    whichever branch runs first - which is not a fixed fact (see the module
    docstring) and is not something to encode an assumption about.

    Two sets are indexed, for different reasons:

    - ``keyword_patents`` — enriched, and always written. Re-writing a patent
      already in the collection is intentional here: this copy carries full
      text, so it can only improve the stored vector.
    - ``index_only_patents`` — the surplus the providers returned for free (see
      ``search.search.FETCH_PER_QUERY``), filtered to those Qdrant does *not*
      already hold. Skipping the known ones saves embedding calls and, more
      importantly, stops an abstract-only record from overwriting a richer
      enriched vector stored by an earlier request.

    ``semantic_patents`` is not indexed: it came *out* of Qdrant and is already
    there.

    Raises:
        Exception: whatever the embedding call or Qdrant client raises. No
            ``except`` - an unreachable Qdrant fails the whole request rather
            than silently shipping a result whose corpus never grew.
    """
    enriched = state["keyword_patents"]

    extras = state.get("index_only_patents") or []
    if extras:
        already_indexed = known_ids(extras)
        extras = [patent for patent in extras if patent.id not in already_indexed]

    to_index = enriched + extras
    if to_index:
        store_patents(to_index)
        logger.info(
            "Indexed %d patents into Qdrant (%d enriched candidates, %d new extras)",
            len(to_index), len(enriched), len(extras),
        )
    return {}
