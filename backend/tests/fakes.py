"""Deterministic stand-ins for the network-backed semantic layer (embeddings and
Qdrant), shared by every test module so the suite keeps making zero real
network calls even though ``compare.py`` and semantic search are now
unconditionally on the happy path for both pipelines.
"""

import hashlib
import re

EMBED_DIM = 32

_STOPWORDS = {
    "a", "an", "the", "and", "or", "if", "with", "by", "for", "is", "that",
    "this", "using", "system", "device", "method", "process", "of", "to",
    "in", "on", "as", "are", "be", "from", "at", "it", "its", "into",
}
"""Excluded so two texts sharing only function words still get an exact zero
dot product - what the zero-similarity test needs from this deterministic
double. A real hosted embedding model has no such guarantee (dense embeddings
rarely land on an exact zero for any two pieces of English text); this stub
optimizes for test determinism, not for simulating production model output."""


def fake_embed_texts(texts: list[str]) -> list[list[float]]:
    """A deterministic hashing-trick embedding.

    Texts sharing content words get overlapping, correlated vectors; texts
    sharing none get an exact zero dot product (barring a hash collision).
    """
    vectors: list[list[float]] = []
    for text in texts:
        vector = [0.0] * EMBED_DIM
        for word in re.findall(r"\w+", text.lower()):
            if word in _STOPWORDS:
                continue
            digest = int(hashlib.md5(word.encode()).hexdigest(), 16)
            sign = 1.0 if (digest // EMBED_DIM) % 2 == 0 else -1.0
            vector[digest % EMBED_DIM] += sign
        vectors.append(vector)
    return vectors


class _FakePoint:
    def __init__(self, payload: dict, score: float) -> None:
        self.payload = payload
        self.score = score


class _FakeCollection:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeCollectionsResult:
    def __init__(self, names: list[str]) -> None:
        self.collections = [_FakeCollection(name) for name in names]


class _FakeQueryResponse:
    def __init__(self, points: list[_FakePoint]) -> None:
        self.points = points


class FakeQdrantClient:
    """In-memory stand-in for ``qdrant_client.QdrantClient``.

    One instance persists for the lifetime of a test (see
    ``tests/conftest.py``), so calling store then search within the same test
    proves a repeated query retrieves what an earlier one indexed - the same
    property P3.3 requires of the real Qdrant collection.
    """

    def __init__(self) -> None:
        self._points: dict[str, tuple[list[float], dict]] = {}
        self._collection_created = False

    def get_collections(self) -> _FakeCollectionsResult:
        return _FakeCollectionsResult(["patents"] if self._collection_created else [])

    def create_collection(self, collection_name: str, vectors_config: object) -> None:
        self._collection_created = True

    def upsert(self, collection_name: str, points: list) -> None:
        for point in points:
            self._points[point.id] = (point.vector, point.payload)

    def query_points(
        self,
        collection_name: str,
        query: list[float],
        limit: int,
        with_payload: bool = True,
        score_threshold: float | None = None,
        **kwargs: object,
    ) -> _FakeQueryResponse:
        """Mirrors ``QdrantClient.query_points`` (the ``search()`` replacement
        as of qdrant-client 1.10+, which ``search/vector_store.py`` now calls),
        including the ``score_threshold`` filter it now passes."""

        def cosine(a: list[float], b: list[float]) -> float:
            dot = sum(x * y for x, y in zip(a, b))
            norm_a = sum(x * x for x in a) ** 0.5
            norm_b = sum(y * y for y in b) ** 0.5
            return 0.0 if norm_a == 0 or norm_b == 0 else dot / (norm_a * norm_b)

        scored = [
            _FakePoint(payload, cosine(query, vector))
            for vector, payload in self._points.values()
        ]
        if score_threshold is not None:
            scored = [point for point in scored if point.score >= score_threshold]
        scored.sort(key=lambda point: point.score, reverse=True)
        return _FakeQueryResponse(scored[:limit])
