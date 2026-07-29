"""Semantic vector storage using Qdrant."""

from uuid import uuid5, NAMESPACE_DNS


from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    PointStruct,
    VectorParams,
)

from config import settings
from embeddings import embed_texts
from models import PatentRecord

COLLECTION_NAME = "patents"

MIN_SIMILARITY_SCORE = 0.6
"""Floor on cosine similarity for a Qdrant hit to be returned at all.

Without this, ``query_points`` always returns its ``limit`` nearest neighbors
regardless of how far away they actually are - on a small or unrelated corpus
that means "the least-bad options available," not "genuinely similar," and
those would flow into validation and ranking looking like real candidates.

Calibrated against one real probe (BAAI/bge-large-en-v1.5, live Qdrant, ~80
water-bottle/UV-sterilization patents indexed from this project's own manual
testing): a genuinely unrelated query ("tractor gearbox") scored 0.51-0.56;
closely related queries scored 0.68-0.76. There is no exact-zero baseline for
this model - any two pieces of English text land somewhere above 0, so a
threshold has to clear that floor, not sit at 0. 0.6 sits in the gap between
the two observed clusters. This is one data point on one narrow corpus, not a
rigorous calibration - revisit once there's real cross-domain usage data."""

client = QdrantClient(
    url=settings.qdrant_url,
    api_key=settings.qdrant_api_key or None,
)


def ensure_collection(vector_size: int) -> None:
    """Create the patent collection if it doesn't already exist."""

    collections = client.get_collections().collections

    if any(c.name == COLLECTION_NAME for c in collections):
        return

    client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=VectorParams(
            size=vector_size,
            distance=Distance.COSINE,
        ),
    )


def store_patents(patents: list[PatentRecord]) -> None:
    """Embed patents and store them in Qdrant."""

    if not patents:
        return

    texts = [
        f"{patent.title}\n\n{patent.full_text or patent.abstract}"
        for patent in patents
    ]

    vectors = embed_texts(texts)

    ensure_collection(len(vectors[0]))

    points = []

    for patent, vector in zip(patents, vectors, strict=True):
        points.append(
            PointStruct(
                id=str(uuid5(NAMESPACE_DNS, patent.id)),
                vector=vector,
                payload={
                    "id": patent.id,
                    "title": patent.title,
                    "abstract": patent.abstract,
                    "inventors": patent.inventors,
                    "assignee": patent.assignee,
                    "publication_date": patent.publication_date,
                    "source": patent.source,
                    "url": patent.url,
                    "serpapi_id": patent.serpapi_id,
                    "full_text": patent.full_text,
                },
            )
        )

    client.upsert(
        collection_name=COLLECTION_NAME,
        points=points,
    )


def semantic_search(
    invention_text: str,
    limit: int = 20,
):
    """Return the most semantically similar patents.

    A collection that does not exist yet means nothing has been indexed by any
    earlier request - a legitimate empty state, not an outage - so this returns
    an empty list rather than letting the client's "not found" error surface as
    a failure. ``client.get_collections()`` still raises if Qdrant itself is
    unreachable, and that failure is not caught here.
    """

    collections = client.get_collections().collections
    if not any(c.name == COLLECTION_NAME for c in collections):
        return []

    vector = embed_texts([invention_text])[0]

    # QdrantClient.search() was removed in qdrant-client 1.10+; query_points()
    # is the replacement and wraps the same ScoredPoint list in `.points`.
    return client.query_points(
        collection_name=COLLECTION_NAME,
        query=vector,
        limit=limit,
        with_payload=True,
        score_threshold=MIN_SIMILARITY_SCORE,
    ).points