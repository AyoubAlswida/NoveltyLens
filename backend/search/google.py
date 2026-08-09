"""Google Patents provider implemented through SerpAPI."""

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser

import requests

from config import settings
from models import PatentRecord
from tracing import trace

logger = logging.getLogger(__name__)

SERPAPI_URL = "https://serpapi.com/search"


def _raise_for_status(response: requests.Response, context: str) -> None:
    """``raise_for_status`` without leaking the API key.

    SerpAPI takes its credential as a query parameter, and ``requests``' own
    ``HTTPError`` message embeds the full request URL. That message is not
    internal: ``search_all_providers_strict`` interpolates it into
    ``PatentSearchError``, and ``server.py`` sends ``str(error)`` straight to the
    browser as the SSE ``error`` detail - so a plain 429 or 401 would print the
    live key into the user's devtools console and any log that captured it.

    Observed for real: an exhausted free-tier quota returned 429 and put the key
    on the wire. Status and context are all a caller needs to act.
    """
    if response.status_code < 400:
        return
    raise RuntimeError(f"SerpAPI {context} returned HTTP {response.status_code}")


class _TextExtractor(HTMLParser):
    """Minimal HTML-to-text extractor for the description_link page.

    Avoids adding a bs4 dependency for what is just tag-stripping.
    """

    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self._chunks.append(text)

    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self._chunks)).strip()


def search_google(query: str, size: int = 10) -> list[PatentRecord]:
    """Return Google results normalized into ``PatentRecord`` objects."""
    if not settings.serpapi_key:
        raise RuntimeError("SERPAPI_KEY is not configured")
    trace("google.search.request", query=query, size=size)
    response = requests.get(
        SERPAPI_URL,
        params={"engine": "google_patents", "q": query, "num": max(10, size), "api_key": settings.serpapi_key},
        timeout=30,
    )
    _raise_for_status(response, f"search for {query!r}")
    data = response.json()
    if "error" in data:
        trace("google.search.error", query=query, error=str(data["error"]))
        raise RuntimeError(str(data["error"]))
    results = [
        PatentRecord(
            id=item.get("publication_number") or item.get("patent_id") or "",
            title=item.get("title", ""),
            abstract=item.get("snippet", ""),
            publication_date=item.get("filing_date", ""),
            source="Google Patents",
            url=item.get("serpapi_link", ""),
            # "patent_id" (e.g. "patent/US1234567B2/en") is the identifier the
            # Details endpoint expects; it is distinct from the human-readable
            # publication_number we use as PatentRecord.id.
            serpapi_id=item.get("patent_id", ""),
        )
        for item in data.get("organic_results", [])[:size]
    ]
    trace("google.search.response", query=query, count=len(results), ids=[r.id for r in results])
    return results


def _fetch_description(description_link: str, timeout: int) -> str:
    """Fetch and strip tags from the separate description HTML page.

    The Details endpoint doesn't inline the description text; it only
    provides a link to a hosted HTML fragment.
    """
    if not description_link:
        trace("google.description.skip", reason="no description_link")
        return ""
    response = requests.get(description_link, timeout=timeout)
    response.raise_for_status()
    parser = _TextExtractor()
    parser.feed(response.text)
    text = parser.text()
    trace("google.description.fetched", chars=len(text))
    return text


def get_patent_full_text(serpapi_id: str, timeout: int = 30, include_description: bool = True) -> str:
    """Fetch full claims (+ optionally description) text for one patent via
    the Google Patents Details API (SerpAPI ``engine=google_patents_details``).

    Note: ``claims`` comes back inline as an array of full claim strings.
    The description does NOT come back inline — only a ``description_link``
    to a separate hosted HTML page, so getting it costs a second HTTP
    request per patent. Set ``include_description=False`` to skip that and
    roughly halve the number of outbound calls.
    """
    if not serpapi_id:
        return ""
    if not settings.serpapi_key:
        raise RuntimeError("SERPAPI_KEY is not configured")
    trace("google.details.request", serpapi_id=serpapi_id, include_description=include_description)
    response = requests.get(
        SERPAPI_URL,
        params={"engine": "google_patents_details", "patent_id": serpapi_id, "api_key": settings.serpapi_key},
        timeout=timeout,
    )
    _raise_for_status(response, f"details for {serpapi_id!r}")
    data = response.json()
    if "error" in data:
        trace("google.details.error", serpapi_id=serpapi_id, error=str(data["error"]))
        raise RuntimeError(str(data["error"]))

    claims = data.get("claims", [])
    claims_text = " ".join(str(claim) for claim in claims) if isinstance(claims, list) else ""
    trace("google.details.claims", serpapi_id=serpapi_id, claim_count=len(claims) if isinstance(claims, list) else 0, claims_chars=len(claims_text))

    description_text = ""
    if include_description:
        try:
            description_text = _fetch_description(str(data.get("description_link", "") or ""), timeout)
        except Exception:
            logger.warning("Description fetch failed for %s; continuing with claims/abstract only", serpapi_id)
            trace("google.description.failed", serpapi_id=serpapi_id)

    full_text = f"{description_text} {claims_text}".strip()
    if not full_text:
        full_text = str(data.get("abstract", "") or "")
        trace("google.details.fallback_to_abstract", serpapi_id=serpapi_id, chars=len(full_text))
    return full_text


def enrich_with_full_text(patents: list[PatentRecord], max_workers: int = 8, include_description: bool = True) -> list[PatentRecord]:
    """Populate ``full_text`` on each Google-sourced patent via parallel
    Details API calls. Non-Google records, or any record whose call fails,
    are left with an empty ``full_text`` so callers fall back to title+abstract.

    A record that already has full_text is skipped too (ticket P2.2): a
    patent restored from the Postgres query cache (``search/search.py``,
    ``cache.py``) may already carry enriched text from an earlier request,
    and re-fetching it would spend a live Details API call for no new data.
    """
    targets = [patent for patent in patents if patent.serpapi_id and not patent.full_text]
    skipped = len(patents) - len(targets)
    if skipped:
        logger.info(
            "Full-text enrichment: skipping %d patent(s) (no serpapi_id, or already enriched from cache)", skipped
        )
    if not targets:
        return patents

    succeeded = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_patent = {
            executor.submit(get_patent_full_text, patent.serpapi_id, 30, include_description): patent
            for patent in targets
        }
        for future in as_completed(future_to_patent):
            patent = future_to_patent[future]
            try:
                patent.full_text = future.result()
                if patent.full_text:
                    succeeded += 1
                    logger.info("Full-text OK: %s (%s) — %d chars", patent.id, patent.serpapi_id, len(patent.full_text))
                else:
                    failed += 1
                    logger.warning("Full-text EMPTY: %s (%s) — no description/claims/abstract returned", patent.id, patent.serpapi_id)
            except Exception:
                failed += 1
                logger.exception("Full-text FAILED: %s (%s)", patent.id, patent.serpapi_id)

    logger.info("Full-text enrichment done: %d/%d patents got text (%d failed/empty)", succeeded, len(targets), failed)
    return patents