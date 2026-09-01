"""Graph assembly and the SSE generator ``server.py`` consumes.

**TEMP: single-branch only.** ``semantic_search``, ``index_patents``, and
``fuse`` are all disabled together (see their commented-out registrations
below) - ``keyword_search`` feeds ``validate`` directly. With semantic_search
off, fuse had nothing to fuse and index_patents had nothing reading Qdrant to
justify writing to it. Re-enable all three together: uncomment the three node
registrations, the ``search_orchestrator -> semantic_search``,
``keyword_search -> index_patents``, ``semantic_search -> index_patents``, and
``index_patents -> fuse -> validate`` edges, and remove the direct
``keyword_search -> validate`` edge. ``graph/validation.py`` needs no change
either way - it already reads ``candidates``, which every arrangement writes.

Progress events reach the client through LangGraph's custom stream writer, which
preserves the order nodes wrote them in.  The final ``result`` event is built
here, from the finished state, because assembling it is a statement about the
SSE contract rather than about any one agent's work.

**Failures are not caught here.**  A node that raises propagates straight out of
:func:`run_graph_pipeline`; ``server.py``'s existing wrapper turns it into a
single ``error`` event.  That is the whole design - there is no point at which a
partial result can be assembled and sent.

That extends to persistence: :func:`persist_run` writes the finished report to
Postgres *before* the ``result`` event is yielded, and is not wrapped in a
``try``.  A database that cannot store the run fails the request, rather than
telling the user the search succeeded and then dropping the report on the floor
where History would never show it.
"""

from functools import lru_cache
from typing import Iterator, Optional

from langgraph.graph import END, START, StateGraph

import db
from reports import save_report

# from graph.fusion import fuse_node  # TEMP: disabled - see module docstring above
from graph.ranking import explain_node, format_matches, rank_node, verdict_for
from graph.searching import (
    # index_patents_node,  # TEMP: disabled - see module docstring above
    keyword_search_node,
    search_orchestrator_node,
    # semantic_search_node,  # TEMP: disabled - see module docstring above
)
from graph.evidence_nodes import (
    build_passages_node,
    confirm_eligibility_node,
    enrich_epo_node,
    normalize_publications_node,
    rank_and_reduce_node,
    screen_dates_node,
)
from graph.state import GraphState
from graph.understanding import expand_queries_node, understand_node
from graph.validation import validate_node
from graph.gates import (
    gate_a_node,
    gate_b_node,
    gate_c_node,
    gate_d_node,
    gate_strategy_node,
)
from graph.report import draft_report_node
from graph.strategy import propose_strategy_node
from graph.work_requests import (
    absorb_manual_candidates_node,
    regenerate_strategy_node,
    regenerate_understanding_node,
    rerun_cells_node,
    route_after_gate_a,
    route_after_gate_b,
    route_after_gate_c,
    route_after_gate_strategy,
)
from graph.verification_nodes import decompose_features_node, verify_features_node


def _route_after_validate(state: GraphState) -> str:
    """Skip ranking and explanation when nothing survived search, fusion, and
    validation.

    Running them anyway would emit ``Scoring 0 candidates…`` and ``Writing
    detailed analysis for 0 matches…`` - progress messages describing work that
    is not happening. An empty ``candidates`` here is a genuine answer (nothing
    found, or nothing judged relevant), not a failure.
    """
    return "rank" if state["candidates"] else END


def _route_after_understanding(state: GraphState) -> str:
    """The one branch that separates a search-only run from a verified one.

    A search-only run goes straight to `search_orchestrator` and traverses
    **exactly** the nodes it did before this gate existed, emitting exactly the
    phases it emitted before. That is asserted rather than assumed: the SSE
    contract is live production behaviour that a browser depends on, and this
    gate's whole job was to add a branch beside it, not through it.
    """
    return "decompose" if state.get("verification_requested") else "search_orchestrator"


def _route_after_explain(state: GraphState) -> str:
    """Verified runs continue into the evidence layer; search-only runs end.

    Ranking and explanation run for both, because a verified run's report still
    shows the similarity scores and rationales a search-only run shows -- the
    verification matrix is an addition to that report, not a replacement for it.
    """
    return "normalize_publications" if state.get("verification_requested") else END


def _route_after_eligibility(state: GraphState) -> str:
    """Nothing citable means no matrix.

    Every publication was excluded -- on date, on missing full text, or on having
    no producible locator -- and each of those exclusions is recorded with its
    reason. Running `verify_features` over an empty approved set would produce a
    matrix of zero cells that reads as "we compared everything and found
    nothing", which is a claim about patents rather than about coverage.
    """
    return "rank_and_reduce" if state.get("candidates") else END


def _assemble() -> StateGraph:
    """The topology. **One graph, conditional routing** -- never two pipelines.

    Spec section 3.1. Every node is registered unconditionally and the
    verification branch, including its Human-in-the-Loop gates, is *reached*
    only when `verification_requested` is true. A search-only run therefore
    traverses exactly the nodes it did before any of this existed and emits
    exactly the same phases, which is what keeps today's SSE stream
    byte-identical.

    Assembly is separated from compilation because the two callers need different
    compilations and must not be allowed to drift into different *topologies*:
    :func:`build_graph` compiles without a checkpointer for the inline search
    path, and :func:`build_graph_with_checkpointer` compiles the identical
    builder with the Postgres saver the executor needs for `interrupt()`.
    """
    builder = StateGraph(GraphState)
    builder.add_node("understand", understand_node)
    builder.add_node("expand_queries", expand_queries_node)
    builder.add_node("search_orchestrator", search_orchestrator_node)
    builder.add_node("keyword_search", keyword_search_node)
    # builder.add_node("semantic_search", semantic_search_node)  # TEMP: disabled
    # builder.add_node("index_patents", index_patents_node)  # TEMP: disabled - only
    # semantic_search ever reads Qdrant, so writing to it while that's off is
    # pointless work. Re-enable together with semantic_search below.
    # builder.add_node("fuse", fuse_node)  # TEMP: disabled - with only one branch
    # (keyword_search) live, fusion is a no-op re-sort of a single list.
    # validate_node reads `candidates` directly instead; see graph/validation.py.
    builder.add_node("validate", validate_node)
    builder.add_node("rank", rank_node)
    builder.add_node("explain", explain_node)

    builder.add_edge(START, "understand")
    builder.add_edge("understand", "expand_queries")
    builder.add_conditional_edges(
        "expand_queries",
        _route_after_understanding,
        {"decompose": "decompose", "search_orchestrator": "search_orchestrator"},
    )
    builder.add_edge("search_orchestrator", "keyword_search")
    # builder.add_edge("search_orchestrator", "semantic_search")  # TEMP: disabled
    # TEMP: disabled along with semantic_search and fuse (see node registrations
    # above). Re-enable this whole join - index_patents, fuse, and their edges -
    # together; index_patents has to sit behind the keyword/semantic join for the
    # same race-condition reason documented on index_patents_node itself.
    # builder.add_edge("keyword_search", "index_patents")
    # builder.add_edge("semantic_search", "index_patents")
    # builder.add_edge("index_patents", "fuse")
    # builder.add_edge("fuse", "validate")
    builder.add_edge("keyword_search", "validate")
    builder.add_conditional_edges(
        "validate", _route_after_validate, {"rank": "rank", END: END}
    )
    builder.add_edge("rank", "explain")

    # --- Verification branch (Implementation Gate C) ---------------------
    #
    # Registered unconditionally; REACHED only when `verification_requested` is
    # true. A search-only run never enters any node below, which is what keeps
    # today's SSE stream byte-identical.
    #
    # The four Human-in-the-Loop gates are wired below (Implementation Gate D).
    # `verify_features` reads only approved artifacts and REFUSES to run without
    # them, so there is no auto-approval and no path that reaches the verifier on
    # machine output alone -- with or without the gates in the graph.
    builder.add_node("decompose", decompose_features_node)
    builder.add_node("normalize_publications", normalize_publications_node)
    builder.add_node("screen_dates", screen_dates_node)
    builder.add_node("enrich_epo", enrich_epo_node)
    builder.add_node("build_passages", build_passages_node)
    builder.add_node("confirm_eligibility", confirm_eligibility_node)
    builder.add_node("rank_and_reduce", rank_and_reduce_node)
    builder.add_node("verify_features", verify_features_node)

    # --- Human-in-the-Loop gates (Implementation Gate D) -----------------
    #
    # Each calls `interrupt()`. LangGraph commits a checkpoint and raises out of
    # `graph.stream()`; `workflow/executor.py` runs the section 3.3.2 pause
    # sequence and releases the lease. **No node here decides anything** -- each
    # re-reads the human decision from the database through the approved-only
    # readers, because the resume value travels through an HTTP request and a
    # checkpoint, and if it were the decision then forging it would be approving.
    builder.add_node("gate_a", gate_a_node)
    builder.add_node("propose_strategy", propose_strategy_node)
    builder.add_node("gate_strategy", gate_strategy_node)
    builder.add_node("gate_b", gate_b_node)
    builder.add_node("gate_c", gate_c_node)
    builder.add_node("draft_report", draft_report_node)
    builder.add_node("gate_d", gate_d_node)

    # The two human work-request consumers (spec cases T and U). The API records
    # a request and returns 202; these carry it out, under the lease, in the
    # graph.
    builder.add_node("absorb_manual_candidates", absorb_manual_candidates_node)
    builder.add_node("rerun_cells", rerun_cells_node)

    # Gate A sits between the machine proposing a decomposition and the search
    # that uses it, so nothing is searched for against requirements nobody
    # approved.
    builder.add_edge("decompose", "gate_a")

    # **A rejected analysis goes back to the analyst, not on to the search.**
    # If the inventor asked for a re-analysis rather than approving, the graph
    # returns to `decompose`, which proposes a NEW version and pauses again.
    # Nothing about the rejected version is edited, and no query is ever built
    # from an understanding nobody approved.
    builder.add_node("regenerate_understanding", regenerate_understanding_node)
    builder.add_conditional_edges(
        "gate_a",
        route_after_gate_a,
        {
            "regenerate_understanding": "regenerate_understanding",
            # **Not the search.** The approved understanding now leads to the
            # SEARCH STRATEGY stage, which proposes the queries and pauses again;
            # the search is reached only from there. The invariant this edge used
            # to carry -- nothing is searched for that a person did not approve --
            # is unchanged and now has two approvals in front of it rather than
            # one.
            "propose_strategy": "propose_strategy",
        },
    )
    builder.add_edge("regenerate_understanding", "decompose")

    # --- The SEARCH STRATEGY stage ---------------------------------------
    #
    # `propose_strategy` reads the APPROVED understanding and proposes queries;
    # `gate_strategy` pauses for a person and, on resume, writes `queries` from
    # the approved strategy's ENABLED entries. `search_orchestrator` is reachable
    # from nowhere else on this branch, which is what makes "no provider call
    # before both approvals" a property of the graph rather than a convention.
    builder.add_edge("propose_strategy", "gate_strategy")

    # **Rejected queries go back to the planning, not on to the search.** Same
    # shape as the understanding stage's regeneration and for the same reason: a
    # rejection is not an approval, so the graph returns to the proposing node,
    # which produces a NEW version and pauses again.
    builder.add_node("regenerate_strategy", regenerate_strategy_node)
    builder.add_conditional_edges(
        "gate_strategy",
        route_after_gate_strategy,
        {
            "regenerate_strategy": "regenerate_strategy",
            "search_orchestrator": "search_orchestrator",
        },
    )
    builder.add_edge("regenerate_strategy", "propose_strategy")
    builder.add_edge("normalize_publications", "screen_dates")
    builder.add_edge("screen_dates", "enrich_epo")
    builder.add_edge("enrich_epo", "build_passages")
    builder.add_edge("build_passages", "confirm_eligibility")
    builder.add_conditional_edges(
        "confirm_eligibility",
        _route_after_eligibility,
        {"rank_and_reduce": "rank_and_reduce", END: END},
    )
    # Gate B sits between the ranked candidate list (machine output) and the
    # verifier, so no publication is verified against that a person did not put
    # in the set. `verify_features_node` reads `approved_candidate_set` and has
    # no code path to `ranked_candidates`.
    builder.add_edge("rank_and_reduce", "gate_b")

    # **A manually added publication re-enters the pipeline; it does not skip
    # it.** Case T: it "passes normalization, date screening, enrichment, and
    # full-text eligibility IDENTICALLY to a discovered candidate", so the loop
    # goes back to `normalize_publications` -- the real nodes, not a second
    # implementation of their rules. A manual candidate that fails screening
    # lands in `publication_exclusions` exactly like a discovered one, and the
    # tail returns to Gate B so a human approves the widened set. Approving it is
    # still a separate act; nothing here shortens that path.
    builder.add_conditional_edges(
        "gate_b",
        route_after_gate_b,
        {
            "absorb_manual_candidates": "absorb_manual_candidates",
            "verify_features": "verify_features",
        },
    )
    builder.add_edge("absorb_manual_candidates", "normalize_publications")

    # Gate C reviews the matrix, the draft is assembled from the reviewed matrix,
    # and Gate D approves that draft. The draft is built BETWEEN the two gates
    # because case AC forbids approving a report into existence: Gate D requires
    # a draft that already exists and was presented.
    builder.add_edge("verify_features", "gate_c")

    # **A rerun returns to the reviewer, not past them.** Case U: only the named
    # cell re-runs, every other cell's result stays byte-identical, and the new
    # result links back to the prior version. Continuing to the draft would march
    # past the answer to the question the reviewer just asked.
    builder.add_conditional_edges(
        "gate_c",
        route_after_gate_c,
        {"rerun_cells": "rerun_cells", "draft_report": "draft_report"},
    )
    builder.add_edge("rerun_cells", "gate_c")
    builder.add_edge("draft_report", "gate_d")
    builder.add_edge("gate_d", END)

    builder.add_conditional_edges(
        "explain",
        _route_after_explain,
        {"normalize_publications": "normalize_publications", END: END},
    )
    return builder


@lru_cache(maxsize=1)
def build_graph():
    """The graph compiled **without** a checkpointer, for the inline search path.

    Cached: the topology is a constant, so recompiling it per request was pure
    per-request work with an identical result. The compiled graph holds no
    request state - all of that lives in the ``GraphState`` passed to
    ``stream()`` - so one instance is safely shared across concurrent requests.

    Nodes are referenced by function object, and each node resolves its
    collaborators (``understand_invention``, ``search_all_providers_strict``, …)
    from module globals at call time, so ``unittest.mock.patch`` on those names
    still takes effect against a graph compiled before the patch.

    **No checkpointer is needed and none is used.** `interrupt()` requires one,
    and the only nodes that call it are the four gates -- which a search-only run
    never reaches, because `_route_after_understanding` sends it straight to
    `search_orchestrator`. A verified run on this compilation would fail at the
    first gate rather than pause silently, which is the correct direction to
    fail: `POST /api/analyze/stream` cannot admit a verified run at all.
    """
    return _assemble().compile()


def build_graph_with_checkpointer(checkpointer):
    """The identical topology compiled **with** a checkpointer, for the executor.

    Not cached, and not sharing `build_graph`'s cache: a compiled graph is bound
    to the checkpointer it was compiled with, and the executor builds one per
    claimed run inside the `checkpointer()` context manager that owns the
    connection pool. Caching it would outlive the pool it points at.
    """
    return _assemble().compile(checkpointer=checkpointer)


def _result_envelope(
    state: GraphState,
    patents: list[dict[str, object]],
    top_score: int,
    recommendations: list[str],
) -> dict[str, object]:
    """The ``result`` event's shared shape.

    Every field here is part of the contract the frontend reads; see
    ``frontend/src/app/App.tsx``.  ``title`` uses three ASCII periods, not an
    ellipsis character - the legacy pipeline does, and the two must agree.
    """
    description = state["description"]
    return {
        "type": "result",
        "data": {
            "title": description[:45] + "...",
            "date": "Today",
            "concepts": state["analysis"].concepts,
            "patents": patents,
            "top_score": top_score,
            "verdict": verdict_for(top_score),
            "query_count": len(state["queries"]),
            "recommendations": recommendations,
        },
    }


def build_result_event(state: GraphState) -> dict[str, object]:
    """Assemble the single terminal ``result`` event from the finished state."""
    if not state["candidates"]:
        # An honest empty report. The search reached every provider and found
        # nothing, which is a real answer, so it is reported rather than raised.
        #
        # Every field is either measured or absent. `concepts` and `query_count`
        # are real - the LLM did run. `top_score` is 0 because nothing was
        # scored, and `verdict` comes from the same verdict_for() every other
        # run uses rather than a value invented for this branch.
        #
        # `recommendations` is EMPTY, and that is the point of this branch.
        # Legacy fills it with a hardcoded sentence of claim-drafting advice -
        # words no model produced, sitting in the slot the user reads as model
        # output. Saying nothing is the honest option when there is nothing to
        # say. A test asserts that literal appears nowhere in this package.
        return _result_envelope(state, patents=[], top_score=0, recommendations=[])

    match_analysis = state["match_analysis"]
    patents = format_matches(state["comparisons"], match_analysis.rationales, match_analysis.key_overlaps)
    return _result_envelope(
        state,
        patents=patents,
        top_score=int(patents[0]["pct"]),
        recommendations=match_analysis.recommendations,
    )


def persist_run(state: GraphState, result: dict[str, object]) -> None:
    """Store the finished report in Postgres so History can show it later.

    Called before the ``result`` event is yielded, and deliberately not wrapped
    in a ``try`` - see the module docstring.

    A run that found nothing is stored too. "We searched and there was no prior
    art" is a real answer and belongs in the history alongside the others; the
    empty ``patents``/``matches`` lists below are that answer, not a failure to
    record one.
    """
    data: dict = result["data"]  # type: ignore[assignment]
    comparisons = state.get("comparisons") or []

    with db.connect() as conn:
        save_report(
            conn,
            description=state["description"],
            claims=state.get("claims"),
            concepts=data["concepts"],
            query_count=data["query_count"],
            top_score=data["top_score"],
            verdict=data["verdict"],
            recommendations=data["recommendations"],
            matches=data["patents"],
            patents=[comparison.patent for comparison in comparisons],
        )


def run_graph_pipeline(
    description: str,
    claims: Optional[str],
    *,
    verification: Optional[dict[str, object]] = None,
) -> Iterator[dict[str, object]]:
    """Run the graph, yielding the SSE event sequence ``server.py`` frames.

    The only pipeline: the legacy generator this was once signature-compatible
    with, and the ``PIPELINE`` flag that chose between them, are both gone.

    ``verification`` is the seed state for a verified run: ``run_id``,
    ``cutoff_date``, ``date_policy``, and the **approved** artifacts. It is
    keyword-only and defaults to ``None``, so ``server.py``'s existing two-argument
    call is byte-identical to what it was and a search-only run cannot acquire a
    verification branch by accident.

    Nothing here manufactures an approval. The approved fields arrive already
    approved -- from ``workflow/approvals.py`` via the Gate A/B endpoints
    (Implementation Gate D) or via ``tests/harness.py`` -- and
    ``verify_features_node`` raises if they are absent. A caller passing
    ``{"verification_requested": True}`` and nothing else gets a failed run, which
    is the correct outcome for asking for verification nobody approved.
    """
    graph = build_graph()
    final_state: GraphState = {}

    initial: dict[str, object] = {"description": description, "claims": claims}
    initial.update(verification or {})

    for mode, chunk in graph.stream(
        initial,
        stream_mode=["custom", "values"],
    ):
        if mode == "custom":
            yield chunk
        else:
            final_state = chunk

    result = build_result_event(final_state)
    persist_run(final_state, result)
    yield result
