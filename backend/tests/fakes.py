"""Deterministic stand-ins for the network-backed semantic layer (embeddings and
Qdrant) and, as of ticket P2.2, Postgres -- shared by every test module so the
suite keeps making zero real network/database calls even though ``compare.py``
and semantic search are unconditionally on the happy path for both pipelines,
and the legacy pipeline now opens a Postgres connection as its first action.
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


class _FakeTransaction:
    def __enter__(self) -> "_FakeTransaction":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


class _FakePGCursor:
    """Recognizes exactly the statements cache.py/reports.py issue -- not a
    general SQL engine, the same scope FakeQdrantClient uses above. Matching
    is by substring on the normalized SQL text rather than full parsing,
    since this module owns both sides (the fake and the real queries) and
    can keep them in sync directly."""

    def __init__(self, db: "FakePostgresDB") -> None:
        self._db = db
        self._result: list[tuple] = []

    def __enter__(self) -> "_FakePGCursor":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple = ()) -> None:
        sql_norm = " ".join(sql.split())
        self._result = []

        if sql_norm.startswith("SELECT patent_ids FROM query_cache"):
            row = self._db.query_cache.get(params[0])
            self._result = [(row,)] if row is not None else []

        elif sql_norm.startswith("SELECT id, title, abstract, inventors, assignee, publication_date, source, url, serpapi_id, full_text"):
            self._result = [
                (p["id"], p["title"], p["abstract"], p["inventors"], p["assignee"],
                 p["publication_date"], p["source"], p["url"], p["serpapi_id"], p["full_text"])
                for p in (self._db.patents.get(pid) for pid in params[0]) if p is not None
            ]

        elif sql_norm.startswith("INSERT INTO patents"):
            (id_, title, abstract, inventors, assignee, publication_date,
             source, url, serpapi_id, full_text) = params
            existing = self._db.patents.get(id_, {})
            self._db.patents[id_] = {
                "id": id_, "title": title, "abstract": abstract, "inventors": inventors,
                "assignee": assignee, "publication_date": publication_date, "source": source,
                "url": url, "serpapi_id": serpapi_id,
                "full_text": full_text or existing.get("full_text", ""),
            }

        elif sql_norm.startswith("INSERT INTO query_cache"):
            query, patent_ids = params[0], params[1]
            self._db.query_cache[query] = list(patent_ids)

        elif sql_norm.startswith("INSERT INTO search_runs"):
            run_id = f"fake-run-{len(self._db.search_runs)}"
            self._db.search_runs.append({"id": run_id, "params": params})
            self._result = [(run_id,)]

        elif sql_norm.startswith("INSERT INTO search_run_matches"):
            self._db.search_run_matches.append(params)

        elif sql_norm.startswith("SELECT id, description, concepts"):
            self._result = []  # list_history: not exercised by pipeline tests today.

        else:
            raise AssertionError(f"FakePostgresDB got an unrecognized statement: {sql_norm[:100]!r}")

    def fetchone(self) -> tuple | None:
        return self._result[0] if self._result else None

    def fetchall(self) -> list[tuple]:
        return self._result


class FakePostgresDB:
    """In-memory stand-in for the schema ticket P2.1 introduced, scoped to
    exactly the statements cache.py/reports.py issue.

    A fresh instance is created per ``connect()`` call (see conftest.py),
    matching that a cache miss/empty history is the correct default for
    every existing test -- none of them test P2.2's cache behavior directly,
    they just need `_run_pipeline`'s new persistence calls to not require a
    real Postgres.
    """

    def __init__(self) -> None:
        self.patents: dict[str, dict] = {}
        self.query_cache: dict[str, list[str]] = {}
        self.search_runs: list[dict] = []
        self.search_run_matches: list[tuple] = []

    def cursor(self) -> _FakePGCursor:
        return _FakePGCursor(self)

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction()

    def __enter__(self) -> "FakePostgresDB":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


def fake_connect() -> FakePostgresDB:
    """Stand-in for db.connect(), which is itself a context manager
    (``@contextmanager``-decorated) -- FakePostgresDB implements
    __enter__/__exit__ directly instead, so calling this plays the same role
    without needing a generator wrapper."""
    return FakePostgresDB()
