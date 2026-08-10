"""Ranking and Explanation nodes.

Ranking calls ``backend/compare.py``, which as of ticket P3.6 scores by chunked
embedding cosine similarity and has no TF-IDF fallback: it raises rather than
falling back to a different algorithm than the one the user was told is running.
This node adds no ``except`` of its own, so that propagates.

Explanation calls ``analyze_all_matches`` and catches nothing.  That function
raises on a failed LLM call, on a response with no JSON, on a rationale missing
for any patent, and on an empty recommendation list.  Every one of those must
fail the request: a match card with an invented or blank explanation is a claim
about prior art that nothing actually examined.

The score shown is the real one.  It is truncated to a whole percent and clamped
to 0-100, and that is all.  A genuinely dissimilar patent shows 0% - not a floor,
not a nudge, not a hidden row.
"""

from graph.events import emit_progress
from graph.state import GraphState
from compare import compare_invention_to_patents
from llm import analyze_all_matches
from models import ComparisonResult

MAX_RESULTS = 15
"""Matches the cap the legacy pipeline applies in ``backend/server.py``."""


def verdict_for(top_score: int) -> str:
    """Map the strongest match to the novelty verdict the frontend renders."""
    return "Weak" if top_score >= 75 else "Moderate" if top_score >= 25 else "Strong"


def format_matches(
    comparisons: list[ComparisonResult], rationales: list[str], key_overlaps: list[list[str]]
) -> list[dict[str, object]]:
    """Build the ``patents[]`` entries of the ``result`` event.

    ``rationales`` and ``key_overlaps`` are positionally aligned with
    ``comparisons``; zipping is strict because a length mismatch would
    silently blank an explanation. ``analyze_all_matches`` already refuses to
    return a short list for either, so this only guards against future drift.
    """
    formatted: list[dict[str, object]] = []
    for result, rationale, tags in zip(comparisons, rationales, key_overlaps, strict=True):
        patent = result.patent
        score = max(0, min(100, int(result.similarity_score * 100)))
        formatted.append({
            "num": patent.id,
            "title": patent.title,
            "pct": score,
            "risk": "High" if score >= 75 else "Low" if score <= 25 else "Medium",
            "rationale": rationale,
            "key_overlap": tags,
            "source": patent.source or "live",
            "url": patent.url,
        })
    # `comparisons` already arrives sorted by the underlying float score. Sorting
    # again on the truncated integer is what the legacy path does, and Python's
    # stable sort keeps the float ordering within a tie, so the two agree.
    return sorted(formatted, key=lambda item: int(item["pct"]), reverse=True)


def rank_node(state: GraphState) -> dict:
    """Score every candidate against the invention and keep the top matches."""
    candidates = state["candidates"]
    emit_progress("compare", f"Scoring {len(candidates)} candidates for similarity…")
    comparisons = compare_invention_to_patents(state["invention_text"], candidates)
    return {"comparisons": comparisons[:MAX_RESULTS]}


def explain_node(state: GraphState) -> dict:
    """Write a rationale per match and the shared drafting recommendations.

    Raises:
        Exception: whatever ``analyze_all_matches`` raises. There is no canned
            rationale and no canned recommendation list to fall back to.
    """
    comparisons = state["comparisons"]
    patents = [result.patent for result in comparisons]

    emit_progress(
        "analyze",
        f"Writing detailed analysis for {len(patents)} matches…",
        current=0,
        total=len(patents),
    )
    match_analysis = analyze_all_matches(state["description"], patents)
    emit_progress(
        "analyze",
        f"Writing detailed analysis… {len(patents)}/{len(patents)}",
        current=len(patents),
        total=len(patents),
    )
    return {"match_analysis": match_analysis}
