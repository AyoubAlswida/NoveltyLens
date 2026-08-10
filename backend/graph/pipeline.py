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
from graph.state import GraphState
from graph.understanding import expand_queries_node, understand_node
from graph.validation import validate_node


def _route_after_validate(state: GraphState) -> str:
    """Skip ranking and explanation when nothing survived search, fusion, and
    validation.

    Running them anyway would emit ``Scoring 0 candidates…`` and ``Writing
    detailed analysis for 0 matches…`` - progress messages describing work that
    is not happening. An empty ``candidates`` here is a genuine answer (nothing
    found, or nothing judged relevant), not a failure.
    """
    return "rank" if state["candidates"] else END


@lru_cache(maxsize=1)
def build_graph():
    """Compile the novelty-search StateGraph.

    Cached: the topology is a constant, so recompiling it per request was pure
    per-request work with an identical result. The compiled graph holds no
    request state - all of that lives in the ``GraphState`` passed to
    ``stream()`` - so one instance is safely shared across concurrent requests.

    Nodes are referenced by function object, and each node resolves its
    collaborators (``understand_invention``, ``search_all_providers_strict``, …)
    from module globals at call time, so ``unittest.mock.patch`` on those names
    still takes effect against a graph compiled before the patch.
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
    builder.add_edge("explain", END)
    return builder.compile()


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
    description: str, claims: Optional[str]
) -> Iterator[dict[str, object]]:
    """Run the graph, yielding the SSE event sequence ``server.py`` frames.

    The only pipeline: the legacy generator this was once signature-compatible
    with, and the ``PIPELINE`` flag that chose between them, are both gone.
    """
    graph = build_graph()
    final_state: GraphState = {}

    for mode, chunk in graph.stream(
        {"description": description, "claims": claims},
        stream_mode=["custom", "values"],
    ):
        if mode == "custom":
            yield chunk
        else:
            final_state = chunk

    result = build_result_event(final_state)
    persist_run(final_state, result)
    yield result
