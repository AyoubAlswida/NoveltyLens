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
                inventors=payload.get("inventors") or [],
                assignee=payload.get("assignee", ""),
                publication_date=payload.get("publication_date", ""),
                source=payload.get("source", ""),
                url=payload.get("url", ""),
                serpapi_id=payload.get("serpapi_id", ""),
                full_text=payload.get("full_text", ""),
            )
        )

    return patents