"""Exact embedding cache backed by Qdrant.

The cache key includes the embedding model, wire protocol, and normalized API
URL, so changing any part of the embedding identity cannot reuse incompatible
vectors. Source text is never stored in the payload.
"""

from hashlib import sha256
import os
from threading import Lock
from uuid import NAMESPACE_URL, uuid5

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from config import settings


_IDENTITY = "|".join(
    (settings.embedding_model, settings.embedding_api_format, settings.embedding_api_url.rstrip("/"))
)
_IDENTITY_HASH = sha256(_IDENTITY.encode("utf-8")).hexdigest()
COLLECTION_NAME = f"embedding_cache_{_IDENTITY_HASH[:16]}"

client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key or None)
_creation_lock = Lock()


def _enabled() -> bool:
    # Unit tests deliberately cannot reach paid/external boundaries. Tests for
    # this module opt in explicitly and replace the client with an in-memory fake.
    return os.environ.get("DEPLOYMENT_PROFILE") != "test"


def _text_hash(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def _point_id(text: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"{_IDENTITY_HASH}:{_text_hash(text)}"))


def _collection_exists() -> bool:
    return any(item.name == COLLECTION_NAME for item in client.get_collections().collections)


def get_many(texts: list[str]) -> dict[int, list[float]]:
    """Return cached vectors keyed by the caller's input position."""
    if not texts or not _enabled() or not _collection_exists():
        return {}

    ids = [_point_id(text) for text in texts]
    points = client.retrieve(
        collection_name=COLLECTION_NAME,
        ids=list(dict.fromkeys(ids)),
        with_payload=True,
        with_vectors=True,
    )
    vectors_by_id: dict[str, list[float]] = {}
    for point in points:
        payload = point.payload or {}
        if payload.get("identity") != _IDENTITY_HASH:
            continue
        vector = point.vector
        if not isinstance(vector, list) or not vector:
            raise RuntimeError("Embedding cache returned an invalid vector")
        vectors_by_id[str(point.id)] = vector
    return {index: vectors_by_id[point_id] for index, point_id in enumerate(ids) if point_id in vectors_by_id}


def put_many(texts: list[str], vectors: list[list[float]]) -> None:
    """Persist exact text/vector pairs without storing the source text."""
    if not texts or not _enabled():
        return
    if len(texts) != len(vectors):
        raise ValueError("Embedding cache text/vector count mismatch")
    if not vectors[0]:
        raise ValueError("Embedding cache cannot store an empty vector")

    if not _collection_exists():
        with _creation_lock:
            if not _collection_exists():
                try:
                    client.create_collection(
                        collection_name=COLLECTION_NAME,
                        vectors_config=VectorParams(size=len(vectors[0]), distance=Distance.COSINE),
                    )
                except Exception:
                    # Backend and worker can cold-start the same collection at
                    # once. Suppress only a real create race; other failures raise.
                    if not _collection_exists():
                        raise

    unique: dict[str, tuple[str, list[float]]] = {}
    for text, vector in zip(texts, vectors, strict=True):
        unique.setdefault(_point_id(text), (text, vector))
    points = [
            PointStruct(
                id=point_id,
                vector=vector,
                payload={"identity": _IDENTITY_HASH, "text_hash": _text_hash(text)},
            )
            for point_id, (text, vector) in unique.items()
        ]
    # A 4096-dimensional provider can produce a multi-megabyte JSON body for
    # one search. Keep writes below Qdrant's request limit without dropping any
    # cached vector. Repeating an idempotent upsert is safe after interruption.
    for start in range(0, len(points), 32):
        client.upsert(collection_name=COLLECTION_NAME, points=points[start:start + 32])
