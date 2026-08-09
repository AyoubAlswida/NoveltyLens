"""Deterministic stand-ins for the network-backed semantic layer -- embeddings
and Qdrant -- shared by every test module so the suite keeps making zero real
network calls even though ``compare.py`` and semantic search are unconditionally
on the pipeline's happy path.

The Postgres doubles that used to live here went with the legacy pipeline and
the Postgres layer itself; Qdrant is the only persistent state left to fake.
"""

import hashlib
import re

from search.vector_store import QUERY_INSTRUCTION

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


def fake_epo_enrich_with_full_text(patents: list) -> list:
    """Deterministic stand-in for ``search.epo.enrich_with_full_text``.

    Unlike Google's enrichment (naturally skipped in tests because synthetic
    ``PatentRecord``s never carry a ``serpapi_id``), EPO's real function keys
    off ``source`` + ``id`` alone - both of which test fixtures do set, so
    without this stub it would make real calls to EPO's auth and full-text
    endpoints during the test suite.
    """
    for patent in patents:
        if patent.source == "European Patent Office" and not patent.full_text:
            patent.full_text = f"{patent.title}. {patent.abstract}".strip()
    return patents


def fake_google_enrich_with_full_text(patents: list) -> list:
    """Deterministic stand-in for Google's ``enrich_with_full_text``.

    The real function is naturally a no-op in tests (it only touches records
    with a ``serpapi_id``, which synthetic ``PatentRecord`` fixtures never
    set) - but ``keyword_search_node`` now drops any candidate that comes out
    of enrichment with no full text at all, which would silently gut every
    Google-sourced test fixture that was never meant to model real
    ``serpapi_id`` gating. This stub sets full text the same way the EPO one
    does, so test corpora survive the drop like real enriched data would.
    """
    for patent in patents:
        if patent.source == "Google Patents" and not patent.full_text:
            patent.full_text = f"{patent.title}. {patent.abstract}".strip()
    return patents


def fake_embed_texts(texts: list[str]) -> list[list[float]]:
    """A deterministic hashing-trick embedding.

    Texts sharing content words get overlapping, correlated vectors; texts
    sharing none get an exact zero dot product (barring a hash collision).

    ``QUERY_INSTRUCTION`` is stripped first for the same reason ``_STOPWORDS``
    exists: it makes this double behave like the model it stands in for. bge was
    *trained* with that prefix on queries, so the real model reads it as a
    retrieval instruction rather than as content. A bag-of-words double has no
    such notion - it would score the prefix's five content words as ordinary
    query terms, diluting the real ones and shrinking every similarity by
    roughly the ratio of the vector norms. That artifact belongs to the stub,
    not to production, so the stub does not simulate it.
    """
    vectors: list[list[float]] = []
    for text in texts:
        vector = [0.0] * EMBED_DIM
        for word in re.findall(r"\w+", text.removeprefix(QUERY_INSTRUCTION).lower()):
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


class _FakeRetrievedPoint:
    """What ``retrieve`` returns - an id and nothing else, since callers pass
    ``with_payload=False``/``with_vectors=False``."""

    def __init__(self, point_id: str) -> None:
        self.id = point_id


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

    def retrieve(
        self,
        collection_name: str,
        ids: list[str],
        with_payload: bool = True,
        with_vectors: bool = True,
        **kwargs: object,
    ) -> list:
        """Mirrors ``QdrantClient.retrieve``: returns only the ids that exist,
        which is exactly what ``vector_store.known_ids`` reads it for."""
        return [_FakeRetrievedPoint(point_id) for point_id in ids if point_id in self._points]

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
