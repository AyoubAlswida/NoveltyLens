"""Google Patents provider implemented through SerpAPI."""

import requests

from config import settings
from models import PatentRecord

SERPAPI_URL = "https://serpapi.com/search"


def search_google(query: str, size: int = 10) -> list[PatentRecord]:
    """Return Google results normalized into ``PatentRecord`` objects."""
    if not settings.serpapi_key:
        raise RuntimeError("SERPAPI_KEY is not configured")
    response = requests.get(
        SERPAPI_URL,
        params={"engine": "google_patents", "q": query, "num": max(10, size), "api_key": settings.serpapi_key},
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    if "error" in data:
        raise RuntimeError(str(data["error"]))
    return [
        PatentRecord(
            id=item.get("publication_number") or item.get("patent_id") or "",
            title=item.get("title", ""),
            abstract=item.get("snippet", ""),
            publication_date=item.get("filing_date", ""),
            source="Google Patents",
            url=item.get("serpapi_link", ""),
        )
        for item in data.get("organic_results", [])[:size]
    ]
