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

The embedding similarity score and excerpt-grounded limitation coverage are
**transient execution data**. Similarity orders the matches; coverage returned
by the existing explanation call helps select the High/Medium/Low novelty risk
before both are discarded. Neither reaches the ``result`` event nor the stored
row.
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

MAX_RESULTS = 8
"""Detailed analysis is limited to the eight strongest distinct families."""

HIGH_SCORE_THRESHOLD = 0.75
MEDIUM_SCORE_THRESHOLD = 0.45
HIGH_COVERAGE_RATIO = 0.75
MEDIUM_COVERAGE_RATIO = 0.40
SUBSTANTIVE_UNIT_SIMILARITY = 0.55
"""Embedding evidence for one independently meaningful invention unit.

Used only with at least one excerpt-grounded covered limitation. Semantic
similarity alone cannot claim that a patent discloses a mechanism.
"""


def _risk_for(
    result: ComparisonResult,
    covered_limitation_ids: list[str],
    total_limitations: int,
) -> str:
    """Rate a patent from similarity plus excerpt-grounded limitation coverage.

    A High rating requires at least three quarters of the independently
    approved limitations to have a substantive match somewhere in this same
    patent.  The rule is domain-neutral: it uses the reviewed limitations, not
    smoke-detector vocabulary or a model-authored coverage claim.
    """
    if total_limitations <= 0:
        raise ValueError("novelty risk requires reviewed technical limitations")
    unique_covered = set(covered_limitation_ids)
    if len(unique_covered) != len(covered_limitation_ids):
        raise ValueError("novelty risk coverage contains duplicate limitation ids")
    if len(unique_covered) > total_limitations:
        raise ValueError("novelty risk coverage exceeds reviewed technical limitations")
    coverage_ratio = len(unique_covered) / total_limitations
    broad_coverage = coverage_ratio >= HIGH_COVERAGE_RATIO
    if result.similarity_score >= HIGH_SCORE_THRESHOLD and broad_coverage:
        return "High"
    # A strong hit on one isolated subsystem is still narrow prior art. Medium
    # requires meaningful aggregate similarity plus either direct coverage of
    # two fifths of the reviewed units or evidence across at least two
    # independently meaningful units with one directly excerpt-supported unit.
    # The latter prevents a granularity cliff: several new additions should not
    # make a substantial core mechanism disclosed by prior art count as only
    # one of many units merely because its steps were merged into one line.
    if result.similarity_score < MEDIUM_SCORE_THRESHOLD:
        return "Low"
    if coverage_ratio >= MEDIUM_COVERAGE_RATIO:
        return "Medium"
    substantive_units = sum(
        score >= SUBSTANTIVE_UNIT_SIMILARITY
        for score in result.unit_similarity_scores
    )
    if unique_covered and substantive_units >= 2:
        return "Medium"
    return "Low"


def format_matches(
    comparisons: list[ComparisonResult],
    rationales: list[str],
    key_overlaps: list[list[str]],
    covered_limitation_ids: list[list[str]],
    total_limitations: int,
) -> list[dict[str, object]]:
    """Build the ``patents[]`` entries of the ``result`` event.

    ``rationales`` and ``key_overlaps`` are positionally aligned with
    ``comparisons``; zipping is strict because a length mismatch would
    silently blank an explanation. ``analyze_all_matches`` already refuses to
    return a short list for either, so this only guards against future drift.

    The aggregate embedding score and the excerpt-grounded structured coverage
    are read here and published nowhere. High requires both ``>= 75`` and broad
    support in the same patent excerpt. Medium requires both ``>= 45`` aggregate
    similarity and either excerpt-grounded coverage of at least two fifths of
    the limitations or breadth across two meaningful embedding units with at
    least one directly covered limitation. An isolated subsystem remains Low.
    """
    scored: list[tuple[float, dict[str, object]]] = []
    for result, rationale, tags, covered_ids in zip(
        comparisons, rationales, key_overlaps, covered_limitation_ids, strict=True
    ):
        patent = result.patent
        scored.append((
            result.similarity_score,
            {
                "num": patent.id,
                "title": patent.title,
                "risk": _risk_for(result, covered_ids, total_limitations),
                "rationale": rationale,
                "key_overlap": tags,
                "source": patent.source or "live",
                "url": patent.url,
                "family_id": patent.family_id,
                "family_size": patent.family_size,
            },
        ))
    # One global descending sort on the raw float, replacing the old sort on the
    # truncated percent. Risk also considers coverage breadth, but ranking stays
    # global by aggregate relevance; grouping by label would corrupt that order.
    scored.sort(key=lambda item: item[0], reverse=True)
    return [entry for _, entry in scored]


def rank_node(state: GraphState) -> dict:
    """Score every candidate against the invention and keep the top matches."""
    candidates = state["candidates"]
    emit_progress("compare", f"Scoring {len(candidates)} candidates for similarity…")
    comparisons = compare_invention_to_patents(
        state["invention_text"], candidates, state.get("similarity_units")
    )
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
    limitations = [
        str(value).strip()
        for value in (state.get("similarity_units") or [state["description"]])
        if str(value).strip()
    ]
    match_analysis = analyze_all_matches(state["description"], patents, limitations)
    emit_progress(
        "analyze",
        f"Writing detailed analysis… {len(patents)}/{len(patents)}",
        current=len(patents),
        total=len(patents),
    )
    return {"match_analysis": match_analysis}
