"""Patent ranking. This is the only module coupled to the ranking method."""

import math

from embeddings import chunk_text, embed_texts
from models import ComparisonResult, PatentRecord
from tracing import trace

CHUNK_MAX_WORDS = 180
CHUNK_OVERLAP_WORDS = 20

COVERAGE_WEIGHT = 0.60
TOP_MATCHES_WEIGHT = 0.25
TITLE_ABSTRACT_WEIGHT = 0.15
TOP_MATCH_COUNT = 3
COHERENT_WINDOW_CHUNKS = 3


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


def _aggregate_similarity(
    similarities: list[list[float]], title_abstract_similarities: list[float]
) -> float:
    """Combine breadth, repeated support, and whole-document topic similarity.

    Rows are independently meaningful invention units and columns are patent
    chunks. Coverage gives every invention unit one vote, so one excellent
    paragraph cannot stand in for the rest of the invention. The top-match
    term rewards repeated strong support, while title/abstract similarity keeps
    the patent's overall subject in the score.
    """
    if not similarities or not similarities[0] or not title_abstract_similarities:
        raise ValueError("similarity aggregation requires non-empty inputs")

    coverage = sum(max(row) for row in similarities) / len(similarities)
    strongest = sorted(
        (score for row in similarities for score in row), reverse=True
    )[:TOP_MATCH_COUNT]
    repeated_support = sum(strongest) / len(strongest)
    title_abstract = max(title_abstract_similarities)
    return (
        COVERAGE_WEIGHT * coverage
        + TOP_MATCHES_WEIGHT * repeated_support
        + TITLE_ABSTRACT_WEIGHT * title_abstract
    )


def _coherent_similarity_window(
    similarities: list[list[float]], window_size: int = COHERENT_WINDOW_CHUNKS
) -> list[list[float]]:
    """Keep the adjacent patent chunks that best support the invention together.

    Taking an independent maximum across an entire long patent lets unrelated
    passages collectively impersonate one disclosure. A small adjacent window
    still tolerates a mechanism spanning chunk boundaries while requiring the
    evidence to occur in one coherent region of the document.
    """
    if not similarities or not similarities[0]:
        raise ValueError("coherent similarity requires a non-empty matrix")
    width = len(similarities[0])
    if any(len(row) != width for row in similarities):
        raise ValueError("coherent similarity requires equal-width rows")
    size = min(max(1, window_size), width)
    best_start = max(
        range(width - size + 1),
        key=lambda start: sum(max(row[start : start + size]) for row in similarities)
        / len(similarities),
    )
    return [row[best_start : best_start + size] for row in similarities]


def _compare_semantic(
    invention_text: str,
    patents: list[PatentRecord],
    similarity_units: list[str] | None = None,
) -> list[ComparisonResult]:
    """Rank patents by coverage-aware semantic similarity."""

    # Chunk the invention too
    invention_chunks = [unit.strip() for unit in (similarity_units or ()) if unit.strip()]
    if not invention_chunks:
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

    title_abstract_texts = [
        f"{patent.title} {patent.abstract}".strip() for patent in patents
    ]
    # One batched call keeps the new whole-document signal from adding another
    # sequential network round trip. The split is positional and strict below.
    document_vectors = embed_texts(patent_chunks + title_abstract_texts)
    patent_vectors = document_vectors[: len(patent_chunks)]
    title_abstract_vectors = document_vectors[len(patent_chunks) :]

    vectors_by_patent: list[list[list[float]]] = [[] for _ in patents]
    for owner, patent_vector in zip(patent_owner, patent_vectors, strict=True):
        vectors_by_patent[owner].append(patent_vector)

    score_profiles: list[tuple[float, list[float]]] = []
    for patent_vectors_for_one, title_vector in zip(
        vectors_by_patent, title_abstract_vectors, strict=True
    ):
        similarity_matrix = [
            [
                max(0.0, _cosine(invention_vector, patent_vector, invention_norm))
                for patent_vector in patent_vectors_for_one
            ]
            for invention_vector, invention_norm in zip(
                invention_vectors, invention_norms, strict=True
            )
        ]
        title_similarities = [
            max(0.0, _cosine(invention_vector, title_vector, invention_norm))
            for invention_vector, invention_norm in zip(
                invention_vectors, invention_norms, strict=True
            )
        ]
        coherent_matrix = _coherent_similarity_window(similarity_matrix)
        unit_scores = [max(row) for row in coherent_matrix]
        score_profiles.append((
            _aggregate_similarity(coherent_matrix, title_similarities),
            unit_scores,
        ))

    for patent, (score, unit_scores) in zip(patents, score_profiles, strict=True):
        trace(
            "compare.patent_score",
            patent_id=patent.id,
            similarity=round(score, 4),
            covered_units=sum(value >= 0.55 for value in unit_scores),
            total_units=len(unit_scores),
        )

    ranked = sorted(
        (
            ComparisonResult(
                patent=patent,
                similarity_score=score,
                unit_similarity_scores=unit_scores,
            )
            for patent, (score, unit_scores) in zip(patents, score_profiles, strict=True)
        ),
        key=lambda result: result.similarity_score,
        reverse=True,
    )

    trace("compare.ranked", order=[r.patent.id for r in ranked])

    return ranked

def rank_by_title_abstract(
    invention_text: str, patents: list[PatentRecord], top_n: int
) -> list[PatentRecord]:
    """Cheap relevance pre-filter: rank candidates by embedding similarity of
    title + abstract alone (no chunking, no full text) against the invention,
    and keep the top ``top_n``.

    Exists to cut the candidate set *before* the expensive step of fetching
    full text for each one - full-text enrichment is the single biggest
    source of outbound API calls in the pipeline, so filtering first on the
    cheap fields (already in hand from the search response, no extra call)
    bounds how many of those expensive calls get made.
    """
    if not patents:
        return []

    texts = [invention_text] + [f"{patent.title} {patent.abstract}".strip() for patent in patents]
    vectors = embed_texts(texts)
    invention_vector = vectors[0]
    invention_norm = _norm(invention_vector)

    scored = sorted(
        (
            (_cosine(invention_vector, vector, invention_norm), patent)
            for vector, patent in zip(vectors[1:], patents, strict=True)
        ),
        key=lambda pair: pair[0],
        reverse=True,
    )

    trace(
        "compare.title_abstract_filter",
        candidate_count=len(patents),
        kept=min(top_n, len(patents)),
    )
    return [patent for _, patent in scored[:top_n]]


def compare_invention_to_patents(
    invention_text: str,
    patents: list[PatentRecord],
    similarity_units: list[str] | None = None,
) -> list[ComparisonResult]:
    """Rank normalized patents by semantic similarity to the invention.

    Raises whatever the embedding call raises (e.g. EMBEDDING_API_URL not
    configured, or the embedding API failed) rather than degrading to a lexical fallback - a
    similarity score computed by a different method than the one the user was
    told is being used is exactly the kind of untrue "good enough" result the
    zero-tolerance policy forbids.
    """
    if not patents:
        return []
    return _compare_semantic(invention_text, patents, similarity_units)
