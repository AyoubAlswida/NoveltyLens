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
    """Return the most semantically similar patents."""

    vector = embed_texts([invention_text])[0]

    return client.search(
        collection_name=COLLECTION_NAME,
        query_vector=vector,
        limit=limit,
        with_payload=True,
    )