"""Validation node.

LLM relevance check over the candidate set, backed by ``llm.validate_relevance``.
Same zero-tolerance policy as every other LLM step in this package: a failed or
malformed validation call raises and fails the request rather than passing
every candidate through unchecked.

Reads ``state["candidates"]`` directly rather than ``fused_patents`` - fusion
is TEMP: disabled along with semantic_search (see ``graph/pipeline.py``), and
with only one branch (keyword_search) live, fusion was a no-op re-sort of the
same list anyway. Re-enabling fusion doesn't require touching this file:
``fuse_node`` also writes ``candidates``.
"""

from graph.events import emit_progress
from graph.state import GraphState
from llm import validate_relevance

MAX_CANDIDATES = 30
"""Bounds the batched validation prompt to a sane size, mirroring
``graph/ranking.py``'s ``MAX_RESULTS`` cap. Candidates arrive already ordered by
the upstream step, so this keeps the strongest candidates and drops the long
tail."""


def validate_node(state: GraphState) -> dict:
    """Filter the candidate set down to the ones an LLM judges relevant.

    An empty input is not sent to the LLM - there is nothing to check, and a
    candidate set that survives validation as empty is a genuine answer, the
    same as an empty search.
    """
    candidates = state["candidates"][:MAX_CANDIDATES]
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
