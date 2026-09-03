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

The similarity score is **transient execution data**.  It is still computed - it
is how ranking works - and it is used for exactly two things before being
discarded: it orders the matches, and it selects the High/Medium/Low novelty
risk rating.  It reaches neither the ``result`` event nor the stored row.
``search_run_matches.rank`` is the only surviving record of the order it
produced, which is why nothing may reorder those rows on read.

A rating is a claim, so it is only ever attached to a patent that was actually
scored.  ``build_result_event`` skips this module entirely when nothing was
found; see ``graph/pipeline.py``.
"""

from graph.events import emit_progress
from graph.state import GraphState
from compare import compare_invention_to_patents
from llm import analyze_all_matches
from models import ComparisonResult

MAX_RESULTS = 15
"""Matches the cap the legacy pipeline applies in ``backend/server.py``."""


def format_matches(
    comparisons: list[ComparisonResult], rationales: list[str], key_overlaps: list[list[str]]
) -> list[dict[str, object]]:
    """Build the ``patents[]`` entries of the ``result`` event.

    ``rationales`` and ``key_overlaps`` are positionally aligned with
    ``comparisons``; zipping is strict because a length mismatch would
    silently blank an explanation. ``analyze_all_matches`` already refuses to
    return a short list for either, so this only guards against future drift.

    The score is read here and published nowhere.  The thresholds are the ones
    that have always applied - ``>= 75`` High, ``<= 25`` Low, Medium between -
    and their boundaries are pinned by test, so ``<= 25`` must not be tidied
    into ``< 25``: that moves a real patent from Low to Medium.
    """
    scored: list[tuple[float, dict[str, object]]] = []
    for result, rationale, tags in zip(comparisons, rationales, key_overlaps, strict=True):
        patent = result.patent
        score = max(0, min(100, int(result.similarity_score * 100)))
        scored.append((
            result.similarity_score,
            {
                "num": patent.id,
                "title": patent.title,
                "risk": "High" if score >= 75 else "Low" if score <= 25 else "Medium",
                "rationale": rationale,
                "key_overlap": tags,
                "source": patent.source or "live",
                "url": patent.url,
            },
        ))
    # One global descending sort on the raw float, replacing the old sort on the
    # truncated percent. The rating is a monotonic function of the score, so this
    # single sort already yields "strongest first" *and* leaves the ratings
    # grouped -- grouping first and sorting within groups would reach the same
    # order by a more fragile route. Sorting the float rather than the int also
    # drops the tie-breaking subtlety the truncated key used to rely on.
    scored.sort(key=lambda item: item[0], reverse=True)
    return [entry for _, entry in scored]


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
