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
from graph.ranking import explain_node, format_matches, rank_node
from graph.searching import (
    # index_patents_node,  # TEMP: disabled - see module docstring above
    keyword_search_node,
    search_orchestrator_node,
    # semantic_search_node,  # TEMP: disabled - see module docstring above
)
from graph.state import GraphState
from graph.understanding import (
    expand_queries_node,
    prepare_approved_search_node,
    understand_node,
)
from graph.validation import validate_node
from graph.gates import gate_a_node, gate_limitations_node, gate_strategy_node
from graph.strategy import propose_strategy_node, select_generated_queries_node
from graph.verification_nodes import decompose_features_node, propose_limitations_node

# NOT imported, and deliberately: `graph/evidence_nodes.py`,
# `graph/verification_nodes.py::verify_features_node`, `graph/report.py` and
# `graph/work_requests.py` are retained as legacy modules (spec section 10.3)
# but no compiled product graph reaches them. Importing them here is what would
# put them back in the topology, so the import list is the enforcement point.


def _route_after_validate(state: GraphState) -> str:
    """Skip ranking and explanation when nothing survived search, fusion, and
    validation.

    Running them anyway would emit ``Scoring 0 candidates…`` and ``Writing
    detailed analysis for 0 matches…`` - progress messages describing work that
    is not happening. An empty ``candidates`` here is a genuine answer (nothing
    found, or nothing judged relevant), not a failure.
    """
    return "rank" if state["candidates"] else END


def _route_from_start(state: GraphState) -> str:
    """Send each search mode to the first piece of work it actually needs.

    Quick Search needs the one-shot understanding call. Advanced Search starts
    with the reviewable proposal instead: running Quick Search understanding and
    query expansion first generated throwaway queries and put unrelated progress
    in front of the first review gate.
    """
    return "decompose" if state.get("advanced_search") else "understand"


def _route_after_strategy_proposal(state: GraphState) -> str:
    """Keep pre-version-2 runs on their durable human-review route."""
    version = int(state.get("advanced_search_flow_version") or 1)
    return "select_generated_queries" if version >= 2 else "gate_strategy"


def _assemble() -> StateGraph:
    """The topology. **One graph, conditional routing** -- never two pipelines.

    Spec section 3.1. Every node is registered unconditionally and the Advanced
    Search branch, including its two Human-in-the-Loop gates, is *reached* only
    when `advanced_search` is true. A search-only run therefore traverses
    exactly the nodes it did before any of this existed and emits exactly the
    same phases, which is what keeps today's SSE stream byte-identical.

    **An Advanced Search run rejoins the ordinary pipeline and ends with it.**
    After the two invention reviews, flow version 2 generates and validates its
    queries automatically. Legacy flow version 1 still pauses at `gate_strategy`.
    `prepare_approved_search` then converts the approved summary, requirements,
    and selected queries into ordinary search state without an LLM call. It runs
    `search_orchestrator -> keyword_search -> validate ->
    rank -> explain` and terminates. Gates B, C and D, the evidence layer and the
    draft report were removed when feature verification was withdrawn.

    They are **removed, not commented out.** The `# TEMP: disabled` style below
    is reserved for the semantic-search trio, which is genuinely coming back;
    leaving the withdrawn nodes commented would invite someone to uncomment a
    path whose API, screens and tests no longer exist. Git history is the record.

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

    builder.add_conditional_edges(
        START,
        _route_from_start,
        {"decompose": "decompose", "understand": "understand"},
    )
    builder.add_edge("understand", "expand_queries")
    builder.add_edge("expand_queries", "search_orchestrator")
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

    # **The graph ends after `explain`, for both kinds of run.** An Advanced
    # Search run rejoined the ordinary pipeline at `search_orchestrator` and
    # finishes with it; there is no evidence layer left to continue into. The
    # durable executor persists the report and the terminal result atomically
    # after the graph returns -- see `workflow/executor.py::_succeed`.
    builder.add_edge("explain", END)

    # --- Advanced Search branch ------------------------------------------
    #
    # Registered unconditionally; REACHED only when `advanced_search` is true.
    # A search-only run never enters any node below, which is what keeps today's
    # SSE stream byte-identical.
    #
    # Two Human-in-the-Loop pauses presented as two stages. They share durable
    # Workflow Gate A but approve different artifacts in sequence: summary first,
    # then requirements generated from that approved summary. `gate_strategy`
    # remains registered only so version-1 checkpoints can resume.
    builder.add_node("decompose", decompose_features_node)

    # Each gate calls `interrupt()`. LangGraph commits a checkpoint and raises
    # out of `graph.stream()`; `workflow/executor.py` runs the pause sequence and
    # releases the lease. **Neither node decides anything** -- each re-reads the
    # human decision from the database through the approved-only readers, because
    # the resume value travels through an HTTP request and a checkpoint, and if
    # it were the decision then forging it would be approving.
    builder.add_node("gate_a", gate_a_node)
    builder.add_node("propose_limitations", propose_limitations_node)
    builder.add_node("gate_limitations", gate_limitations_node)
    builder.add_node("propose_strategy", propose_strategy_node)
    builder.add_node("select_generated_queries", select_generated_queries_node)
    builder.add_node("gate_strategy", gate_strategy_node)
    builder.add_node("prepare_approved_search", prepare_approved_search_node)

    # The first Gate-A pause sits between the summary proposal and limitation
    # generation, so Stage 2 cannot be based on an unapproved interpretation.
    builder.add_edge("decompose", "gate_a")

    # **A plain edge, not a router.** Regeneration was removed from the product:
    # editing the summary and requirements replaces them, so each Gate-A pause
    # has exactly one way out. The approved summary leads to requirement
    # generation and its own pause. Approved requirements then lead to query
    # generation; only legacy flow version 1 adds another human review.
    builder.add_edge("gate_a", "propose_limitations")
    builder.add_edge("propose_limitations", "gate_limitations")
    builder.add_edge("gate_limitations", "propose_strategy")

    # --- The SEARCH STRATEGY stage ---------------------------------------
    #
    # New runs release validated machine-generated queries automatically. Runs
    # admitted before flow version 2 retain the old query-review checkpoint and
    # endpoints, including checkpoints already parked inside `gate_strategy`.
    builder.add_conditional_edges(
        "propose_strategy",
        _route_after_strategy_proposal,
        {
            "select_generated_queries": "select_generated_queries",
            "gate_strategy": "gate_strategy",
        },
    )
    builder.add_edge("select_generated_queries", "prepare_approved_search")
    builder.add_edge("gate_strategy", "prepare_approved_search")
    builder.add_edge("prepare_approved_search", "search_orchestrator")

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
    never reaches, because `_route_from_start` sends it through the Quick Search
    branch only. A verified run on this compilation would fail at the
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
    recommendations: list[str],
) -> dict[str, object]:
    """The ``result`` event's shared shape.

    Every field here is part of the contract the frontend reads; see
    ``frontend/src/app/models/report.ts``.  ``title`` uses three ASCII periods,
    not an ellipsis character - the legacy pipeline does, and the two must agree.

    There is no ``top_score`` and no ``verdict``.  The product publishes no
    numeric similarity and states no overall novelty conclusion; the per-patent
    ``risk`` rating in ``patents[]`` is the only judgement that ships, and an
    empty ``patents`` list therefore carries no rating at all.
    """
    description = state["description"]
    return {
        "type": "result",
        "data": {
            "title": description[:45] + "...",
            "date": "Today",
            "concepts": state["analysis"].concepts,
            "patents": patents,
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
        # are real - the LLM did run. Nothing was scored, so nothing is rated:
        # `patents` is empty and the payload carries no risk rating anywhere.
        # Reporting "Novelty risk: Low" over an empty result set would state a
        # finding this run did not make, which is the substitution the
        # zero-tolerance policy exists to forbid.
        #
        # `recommendations` is EMPTY, and that is the point of this branch.
        # Legacy fills it with a hardcoded sentence of claim-drafting advice -
        # words no model produced, sitting in the slot the user reads as model
        # output. Saying nothing is the honest option when there is nothing to
        # say. A test asserts that literal appears nowhere in this package.
        return _result_envelope(state, patents=[], recommendations=[])

    match_analysis = state["match_analysis"]
    limitations = [
        str(value).strip()
        for value in (state.get("similarity_units") or [state["description"]])
        if str(value).strip()
    ]
    patents = format_matches(
        state["comparisons"],
        match_analysis.rationales,
        match_analysis.key_overlaps,
        match_analysis.covered_limitation_ids,
        len(limitations),
    )
    return _result_envelope(
        state,
        patents=patents,
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
    approved -- from ``workflow/approvals.py`` via the two gate endpoints, or via
    ``tests/harness.py`` -- and each gate node raises if they are absent. A caller
    passing ``{"advanced_search": True}`` and nothing else gets a failed run,
    which is the correct outcome for asking for a guided run nobody approved.
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
