"""Ranking utilities."""

from models import PatentRecord

RRF_K = 60


def reciprocal_rank_fusion(
    ranked_lists: list[list[PatentRecord]],
) -> list[PatentRecord]:
    """
    Combine multiple ranked lists using Reciprocal Rank Fusion.

    Each list is assumed to already be ordered best→worst.
    """

    scores: dict[str, float] = {}
    patents: dict[str, PatentRecord] = {}

    for ranking in ranked_lists:
        for rank, patent in enumerate(ranking, start=1):

            patents[patent.id] = patent

            scores.setdefault(patent.id, 0.0)

            scores[patent.id] += 1.0 / (RRF_K + rank)

    return sorted(
        patents.values(),
        key=lambda patent: scores[patent.id],
        reverse=True,
    )