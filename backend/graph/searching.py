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

Keyword search fans out to both providers at ``RESULTS_PER_QUERY`` (10) results
each, then does two things before returning: a cheap **title/abstract
relevance pre-filter** (``compare.rank_by_title_abstract``, capped at
``FILTER_TOP_N``) to bound how many candidates reach the expensive step, then
**full-text enrichment** on only the survivors - Google's and EPO's enrichment
functions each internally skip records from the other provider, so both are
safe to call on the same mixed list. Neither the search, the filter, nor the
enrichment is wrapped in ``except`` - a failure at any of these fails the whole
request rather than silently shipping a degraded result the user cannot tell
apart from a healthy one.

Indexing survivors into Qdrant is a *separate* node, :func:`index_patents_node`,
which the graph runs after both search branches have joined - see that
function's docstring for why the write has to sit behind that join.
**Semantic Search itself is still TEMP: disabled** - not removed, see
``graph/pipeline.py``'s module docstring. ``semantic_search_node`` below is
fully intact but is not wired into the graph, so it never *reads* Qdrant right
now; the corpus grows from indexing regardless, ready for whenever it's
re-enabled.
"""

import logging

from compare import rank_by_title_abstract
from graph.events import emit_progress
from graph.state import GraphState
from search.epo import enrich_with_full_text as enrich_epo_full_text
from search.retrieve import retrieve_similar_patents
from search.search import enrich_with_full_text, search_all_providers_strict
from search.vector_store import known_ids, store_patents

logger = logging.getLogger(__name__)

RESULTS_PER_QUERY = 10
"""Results requested per query, per provider. Raised from 4: both providers'
search calls already fetch at least this many under the hood regardless (see
``search/google.py``), so this costs nothing extra - it only changes how much
of an already-fetched response gets kept, giving the filter step (below) a
bigger pool of candidates to choose the best 20 from."""

FILTER_TOP_N = 20
"""How many candidates survive the title/abstract pre-filter and go on to
full-text enrichment. Bounds the single biggest source of outbound API calls
in the pipeline (one full-text fetch per surviving candidate) regardless of
how many raw results the search fan-out produced."""

SEMANTIC_RESULTS_LIMIT = 20
"""How many semantically similar patents to pull from Qdrant per request."""


def search_orchestrator_node(state: GraphState) -> dict:
    """Announce the search before any provider is contacted.

    Emits the opening ``search`` event only; the counts it reports are the
    starting values the frontend's progress row renders until the closing event
    replaces them.
    """
    if state.get("advanced_search") and not state.get("retrieval_plan"):
        raise ValueError("This search predates the EPO abstract pipeline; start a new search")
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
    """Fan every query across every provider, pre-filter, enrich, and dedupe.

    An empty result is a legitimate outcome and is returned as one; only a
    provider that *failed* stops the request. ``graph/pipeline.py`` routes an
    empty candidate list straight to an empty report.

    Raises:
        PatentSearchError: if any single provider call fails.
    """
    queries = state["queries"]
    found = search_all_providers_strict(
        queries,
        results_per_query=RESULTS_PER_QUERY,
        on_progress=lambda completed, total, received: emit_progress(
            "search",
            f"Searching patent databases… {completed}/{total} requests completed"
            f" · {received} records received",
            current=completed,
            total=total,
            received=received,
        ),
    )
    patents = found.candidates

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
        # "filtering 0 candidates" or "fetching full text for 0 patents"
        # would describe work that isn't happening.
        emit_progress("filter", f"Filtering {len(patents)} candidates by title/abstract relevance…")
        patents = rank_by_title_abstract(state["invention_text"], patents, top_n=FILTER_TOP_N)
        emit_progress(
            "filter",
            f"Kept {len(patents)} most relevant candidates",
            kept=len(patents),
        )

        emit_progress("enrich", f"Fetching full text for {len(patents)} patents…")
        # Each provider's enrichment function only touches records from its
        # own source (matched by `patent.source`), so both are safe to run
        # on the same mixed Google+EPO list.
        patents = enrich_with_full_text(patents)
        patents = enrich_epo_full_text(patents)
        enriched_count = sum(1 for patent in patents if patent.full_text)
        dropped_count = len(patents) - enriched_count
        emit_progress(
            "enrich",
            f"Full text retrieved for {enriched_count}/{len(patents)} patents"
            + (f" — {dropped_count} dropped, no full text available" if dropped_count else ""),
            enriched=enriched_count,
            total=len(patents),
            dropped=dropped_count,
        )

        # A candidate without full text would otherwise silently fall back to
        # scoring against its abstract alone (compare.py's
        # `full_text or abstract`) - dropped instead, on request, rather than
        # letting a thinner comparison basis through unannounced. Real for
        # both providers: EPO's full-text service only covers EP/WO-family
        # documents (a US/CN/DE-only filing that shows up in EPO's search
        # index has no full text EPO can serve), and any individual Google
        # Details call can fail too.
        patents = [patent for patent in patents if patent.full_text]

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
