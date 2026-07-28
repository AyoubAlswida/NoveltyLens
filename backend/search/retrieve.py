"""Semantic retrieval from Qdrant."""

from models import PatentRecord

from .vector_store import semantic_search


def retrieve_similar_patents(
    invention_text: str,
    limit: int = 20,
) -> list[PatentRecord]:
    """Retrieve semantically similar patents from Qdrant."""

    hits = semantic_search(
        invention_text,
        limit=limit,
    )

    patents: list[PatentRecord] = []

    for hit in hits:
        payload = hit.payload

        patents.append(
            PatentRecord(
                id=payload["id"],
                title=payload["title"],
                abstract=payload.get("abstract", ""),
                source=payload.get("source", ""),
            )
        )

    return patents