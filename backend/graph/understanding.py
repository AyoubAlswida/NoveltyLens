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
from models import InventionAnalysis
from workflow.types import UnapprovedArtifactError


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
        # Quick Search has no reviewed limitation list, but its existing LLM
        # call already returns technical concepts. Preserve those boundaries so
        # breadth-aware risk is generic across both search modes.
        "similarity_units": analysis.concepts,
    }


def expand_queries_node(state: GraphState) -> dict:
    """Promote the understood queries to the set the search stage dispatches.

    Today this is a straight hand-off; the split exists so a future expansion
    step (synonyms, CPC codes) has somewhere to live that is not the
    understanding call.  ``understand_node`` has already guaranteed the list is
    non-empty, so there is nothing left to validate here.
    """
    queries = state["analysis"].queries
    emit_progress("queries", f"Generated {len(queries)} AI search queries", count=len(queries), queries=queries)
    return {"queries": queries}


def prepare_approved_search_node(state: GraphState) -> dict:
    """Build the shared search inputs from the two human-approved artifacts.

    Advanced Search has already paid for a reviewed technical summary, reviewed
    limitations, and reviewed queries. Running ``understand_invention`` before
    those reviews duplicated the work, generated queries nobody would use, and
    made the progress screen claim the search plan existed before the reviewer
    approved what the invention meant.

    This node performs no model call and emits no progress phase. It preserves
    the ordinary downstream contract by constructing ``InventionAnalysis`` and
    ``invention_text`` from approved text only. Missing approved input fails
    loudly; raw or candidate text is never substituted for an approval.
    """
    summary = str(state.get("approved_summary") or "").strip()
    approved = state.get("approved_decomposition") or {}
    queries = [str(query).strip() for query in (state.get("queries") or ()) if str(query).strip()]

    limitations = [
        str(element.get("text") or "").strip()
        for feature in (approved.get("features") or ())
        for element in (feature.get("elements") or ())
        if str(element.get("text") or "").strip()
    ]
    if not summary:
        raise UnapprovedArtifactError(
            "Advanced Search cannot start without the approved technical summary"
        )
    if not limitations:
        raise UnapprovedArtifactError(
            "Advanced Search cannot start without approved technical limitations"
        )
    if not queries:
        raise UnapprovedArtifactError(
            "Advanced Search cannot start without approved search queries"
        )

    description = str(state.get("description") or "").strip()
    claims = str(state.get("claims") or "").strip()
    invention_text = "\n\n".join(
        part for part in (description, claims, summary, *limitations) if part
    )
    return {
        "analysis": InventionAnalysis(
            queries=queries,
            keywords=[],
            concepts=limitations,
        ),
        "invention_text": invention_text,
        # Preserve the reviewer-approved boundaries. Ranking can now reward a
        # patent for covering the invention broadly instead of letting one
        # strongly matching paragraph stand in for every limitation.
        "similarity_units": limitations,
    }


__all__ = ["expand_queries_node", "prepare_approved_search_node", "understand_node"]
