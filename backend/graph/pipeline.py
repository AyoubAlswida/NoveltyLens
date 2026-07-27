"""Graph assembly and the SSE generator ``server.py`` consumes.

The graph is linear today, matching the legacy pipeline step for step.  Phase 3
makes it branch: semantic search runs parallel to keyword search and the two
converge at fusion.

Progress events reach the client through LangGraph's custom stream writer, which
preserves the order nodes wrote them in.  The final ``result`` event is built
here, from the finished state, because assembling it is a statement about the
SSE contract rather than about any one agent's work.

**Failures are not caught here.**  A node that raises propagates straight out of
:func:`run_graph_pipeline`; ``server.py``'s existing wrapper turns it into a
single ``error`` event.  That is the whole design - there is no point at which a
partial result can be assembled and sent.
"""

from typing import Iterator, Optional

from langgraph.graph import END, START, StateGraph

from graph.ranking import explain_node, format_matches, rank_node, verdict_for
from graph.searching import keyword_search_node, search_orchestrator_node
from graph.state import GraphState
from graph.understanding import expand_queries_node, understand_node


def _route_after_search(state: GraphState) -> str:
    """Skip ranking and explanation when the search found nothing.

    Running them anyway would emit ``Scoring 0 candidates…`` and ``Writing
    detailed analysis for 0 matches…`` - progress messages describing work that
    is not happening. Ending here also matches legacy's event sequence, which
    returns straight after the search phase.
    """
    return "rank" if state["candidates"] else END


def build_graph():
    """Compile the novelty-search StateGraph."""
    builder = StateGraph(GraphState)
    builder.add_node("understand", understand_node)
    builder.add_node("expand_queries", expand_queries_node)
    builder.add_node("search_orchestrator", search_orchestrator_node)
    builder.add_node("keyword_search", keyword_search_node)
    builder.add_node("rank", rank_node)
    builder.add_node("explain", explain_node)

    builder.add_edge(START, "understand")
    builder.add_edge("understand", "expand_queries")
    builder.add_edge("expand_queries", "search_orchestrator")
    builder.add_edge("search_orchestrator", "keyword_search")
    builder.add_conditional_edges(
        "keyword_search", _route_after_search, {"rank": "rank", END: END}
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
    patents = format_matches(state["comparisons"], match_analysis.rationales)
    return _result_envelope(
        state,
        patents=patents,
        top_score=int(patents[0]["pct"]),
        recommendations=match_analysis.recommendations,
    )


def run_graph_pipeline(
    description: str, claims: Optional[str]
) -> Iterator[dict[str, object]]:
    """Run the graph, yielding the same SSE event sequence as ``_run_pipeline``.

    Signature-compatible with the legacy generator so ``server.py`` can select
    between them on the ``PIPELINE`` flag alone.
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

    yield build_result_event(final_state)
