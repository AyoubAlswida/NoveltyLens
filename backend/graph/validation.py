"""Validation node.

LLM relevance check over the fused candidate set, backed by
``llm.validate_relevance``. Same zero-tolerance policy as every other LLM step
in this package: a failed or malformed validation call raises and fails the
request rather than passing every candidate through unchecked.
"""

from graph.events import emit_progress
from graph.state import GraphState
from llm import validate_relevance

MAX_CANDIDATES = 30
"""Bounds the batched validation prompt to a sane size, mirroring
``graph/ranking.py``'s ``MAX_RESULTS`` cap. Candidates arrive already ordered by
fused rank, so this keeps the strongest candidates and drops the long tail."""


def validate_node(state: GraphState) -> dict:
    """Filter the fused candidate set down to the ones an LLM judges relevant.

    An empty input is not sent to the LLM - there is nothing to check, and a
    fused set that survives validation as empty is a genuine answer, the same
    as an empty search.
    """
    candidates = state["fused_patents"][:MAX_CANDIDATES]
    if not candidates:
        return {"validated_patents": [], "candidates": []}

    emit_progress("validate", f"Checking relevance of {len(candidates)} candidates…")
    keep = validate_relevance(state["invention_text"], candidates)
    validated = [patent for patent, relevant in zip(candidates, keep, strict=True) if relevant]

    emit_progress(
        "validate",
        f"Kept {len(validated)}/{len(candidates)} relevant candidates",
        current=len(validated),
        total=len(candidates),
    )

    return {"validated_patents": validated, "candidates": validated}
