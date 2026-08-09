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

**TEMP: Qdrant indexing and Semantic Search are disabled** - not removed, see
``graph/pipeline.py``'s module docstring. ``semantic_search_node`` below is
fully intact but is not wired into the graph, so it never runs.
"""

from compare import rank_by_title_abstract
from graph.events import emit_progress
from graph.state import GraphState
from search.epo import enrich_with_full_text as enrich_epo_full_text
from search.retrieve import retrieve_similar_patents
from search.search import enrich_with_full_text, search_all_providers_strict

# from search.vector_store import store_patents  # TEMP: disabled, see above

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

        # Index into Qdrant so a later, related request's semantic_search_node
        # can retrieve these. TEMP: disabled along with semantic search - see
        # module docstring. No try/except when re-enabled: an unreachable
        # Qdrant should fail the whole request rather than silently shipping
        # a keyword-only result.
        # store_patents(patents)

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
