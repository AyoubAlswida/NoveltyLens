"""Patent ranking. This is the only module coupled to the ranking method."""

import math

from embeddings import chunk_text, embed_texts
from models import ComparisonResult, PatentRecord
from tracing import trace

CHUNK_MAX_WORDS = 180
CHUNK_OVERLAP_WORDS = 20


def _document_text(patent: PatentRecord) -> str:
    return f"{patent.title} {patent.full_text or patent.abstract}".strip()


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _norm(a: list[float]) -> float:
    return math.sqrt(_dot(a, a))


def _cosine(a: list[float], b: list[float], norm_a: float) -> float:
    norm_b = _norm(b)
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return _dot(a, b) / (norm_a * norm_b)


def _compare_semantic(invention_text: str, patents: list[PatentRecord]) -> list[ComparisonResult]:
    """Rank patents by semantic similarity using chunk-to-chunk comparison."""

    # Chunk the invention too
    invention_chunks = chunk_text(
        invention_text,
        CHUNK_MAX_WORDS,
        CHUNK_OVERLAP_WORDS,
    ) or [invention_text]

    trace("compare.invention.chunked", chunk_count=len(invention_chunks))

    invention_vectors = embed_texts(invention_chunks)
    invention_norms = [_norm(v) for v in invention_vectors]

    # Chunk patents
    patent_chunks: list[str] = []
    patent_owner: list[int] = []

    for patent_index, patent in enumerate(patents):
        chunks = chunk_text(
            _document_text(patent),
            CHUNK_MAX_WORDS,
            CHUNK_OVERLAP_WORDS,
        ) or [_document_text(patent)]

        trace(
            "compare.chunked",
            patent_id=patent.id,
            chunk_count=len(chunks),
        )

        patent_chunks.extend(chunks)
        patent_owner.extend([patent_index] * len(chunks))

    trace(
        "compare.embedding_all_chunks",
        invention_chunks=len(invention_chunks),
        patent_chunks=len(patent_chunks),
    )

    patent_vectors = embed_texts(patent_chunks)

    best_scores = [0.0] * len(patents)

    for owner, patent_vector in zip(patent_owner, patent_vectors):
        best = 0.0

        for invention_vector, invention_norm in zip(
            invention_vectors,
            invention_norms,
        ):
            sim = _cosine(invention_vector, patent_vector, invention_norm)
            if sim > best:
                best = sim

        if best > best_scores[owner]:
            best_scores[owner] = best

    for patent, score in zip(patents, best_scores):
        trace(
            "compare.patent_score",
            patent_id=patent.id,
            similarity=round(score, 4),
        )

    ranked = sorted(
        (
            ComparisonResult(
                patent=patent,
                similarity_score=score,
            )
            for patent, score in zip(patents, best_scores, strict=True)
        ),
        key=lambda result: result.similarity_score,
        reverse=True,
    )

    trace("compare.ranked", order=[r.patent.id for r in ranked])

    return ranked

def compare_invention_to_patents(
    invention_text: str, patents: list[PatentRecord]
) -> list[ComparisonResult]:
    """Rank normalized patents by semantic similarity to the invention.

    Raises whatever the embedding call raises (e.g. HF_TOKEN not configured, or
    the embedding API failed) rather than degrading to a lexical fallback - a
    similarity score computed by a different method than the one the user was
    told is being used is exactly the kind of untrue "good enough" result the
    zero-tolerance policy forbids.
    """
    if not patents:
        return []
    return _compare_semantic(invention_text, patents)