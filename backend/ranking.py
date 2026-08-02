"""Ranking utilities."""

from models import PatentRecord

RRF_K = 60


def _dedup_key(patent: PatentRecord) -> str:
    """Normalize a patent id exactly as ``search.search.merge_patents`` does.

    The two must agree.  Fusion is handed keyword results that ``merge_patents``
    has already deduplicated, alongside semantic results rebuilt from Qdrant
    payloads, and a looser key here would let one patent through the graph as
    two separate cards in the final report.
    """
    return patent.id.strip().upper()


def _is_richer(candidate: PatentRecord, incumbent: PatentRecord) -> bool:
    """Whether ``candidate`` should displace an already-seen ``incumbent``.

    Only when it carries full text the incumbent lacks.  ``compare.py`` scores a
    patent on ``full_text or abstract``, so which copy survives fusion decides
    whether that patent is ranked on its full description or just its abstract.
    The semantic branch rebuilds records from Qdrant payloads that may have been
    indexed before enrichment ran, so "last list wins" would let a stale, bare
    copy silently downgrade the one this request just enriched.
    """
    return bool(candidate.full_text) and not incumbent.full_text


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

            key = _dedup_key(patent)

            incumbent = patents.get(key)
            if incumbent is None or _is_richer(patent, incumbent):
                patents[key] = patent

            scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank)

    return sorted(
        patents.values(),
        key=lambda patent: scores[_dedup_key(patent)],
        reverse=True,
    )
