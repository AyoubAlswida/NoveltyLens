"""Result Fusion node.

Combines the Keyword Search and Semantic Search branches with Reciprocal Rank
Fusion (``backend/ranking.py``, unchanged - it is already pure and correct).
RRF dedupes by ``patent.id`` as it accumulates scores, so a patent both branches
found is merged rather than duplicated, and a patent only Semantic Search found
is carried through - which is what lets the fused ordering differ from a
keyword-only ordering.
"""

from graph.events import emit_progress
from graph.state import GraphState
from ranking import reciprocal_rank_fusion


def fuse_node(state: GraphState) -> dict:
    """Fuse the keyword and semantic result sets into one candidate list."""
    keyword_patents = state["keyword_patents"]
    semantic_patents = state["semantic_patents"]

    fused = reciprocal_rank_fusion([keyword_patents, semantic_patents])

    emit_progress(
        "fuse",
        f"Combining {len(keyword_patents)} keyword + {len(semantic_patents)} "
        f"semantic results into {len(fused)} candidates…",
    )

    return {"fused_patents": fused, "candidates": fused}
