"""Query Understanding and Query Expansion nodes.

Both wrap the existing ``backend/llm.py`` logic unchanged.  Neither catches
anything: ``understand_invention`` raises on a failed LLM call, on a
non-JSON response, and on malformed JSON, and every one of those must fail the
request.  There is deliberately no ``except`` in this module.

In particular there is no "use a truncated description as the query" fallback.
A query built from raw description text is not the query the model was asked
for, and prior art retrieved with it would be presented as though it were.
"""

from graph.events import emit_progress
from graph.state import GraphState
from llm import understand_invention


def understand_node(state: GraphState) -> dict:
    """Extract concepts, keywords and search queries from the invention.

    Raises:
        Exception: whatever ``understand_invention`` raises - an unreachable or
            erroring LLM, a response that is not JSON, or JSON in the wrong
            shape.  All of them fail the request.
        ValueError: if the call succeeded but produced no usable queries.  An
            empty query set cannot retrieve prior art, so continuing would
            guarantee a result built on nothing.
    """
    emit_progress("understand", "Extracting technical concepts & keywords…")

    description = state["description"]
    claims = state.get("claims")
    analysis = understand_invention(description)
    if not analysis.queries:
        raise ValueError("Invention understanding returned no search queries")

    return {
        "analysis": analysis,
        # The text ranking scores patents against.  Assembled here so the
        # ranking node never has to know how it was built.
        "invention_text": (
            f"{description} {claims or ''} "
            f"{' '.join(analysis.concepts)} {' '.join(analysis.keywords)}"
        ),
    }


def expand_queries_node(state: GraphState) -> dict:
    """Promote the understood queries to the set the search stage dispatches.

    Today this is a straight hand-off; the split exists so a future expansion
    step (synonyms, CPC codes) has somewhere to live that is not the
    understanding call.  ``understand_node`` has already guaranteed the list is
    non-empty, so there is nothing left to validate here.
    """
    queries = state["analysis"].queries
    emit_progress("queries", f"Generated {len(queries)} AI search queries", count=len(queries))
    return {"queries": queries}
