"""Lens.org Patent API provider.

Unlike Google Patents (SerpAPI) and EPO, which both need a second "Details"
call per patent to fetch full text, Lens's ``/patent/search`` endpoint
returns bibliographic data, abstract, claims, and description in a single
response when asked for via ``include``. So ``search_lens`` populates
``full_text`` directly - there is no separate ``enrich_with_full_text`` for
this provider, and ``graph/searching.py`` needs no change to pick that up
(it just checks ``patent.full_text`` after search, same as every source).

**Currently a 14-day non-commercial trial token** (``LENS_API_KEY``). Trial
access is for evaluation only - see https://about.lens.org/lens-api-terms-of-use/
before pointing this at anything beyond local testing. Continued/commercial
use requires a paid subscription (contact subscriptions@lens.org).

Response field reference: https://docs.api.lens.org/response-patent.html
"""

import logging
import threading

import requests

from config import settings
from models import PatentRecord
from tracing import trace

logger = logging.getLogger(__name__)

LENS_SEARCH_URL = "https://api.lens.org/patent/search"

# search_all_providers_strict fires every (provider, query) pair concurrently
# across a shared thread pool - fine for Google/EPO, but Lens's trial-tier
# key can't handle 5 simultaneous requests each asking for full text on 10
# patents: one request out of 5 got its connection cut mid-response
# (ChunkedEncodingError: "Response ended prematurely") under that load,
# confirmed live. This lock forces every call through this module to run one
# at a time regardless of how many worker threads try to call search_lens
# at once - scoped to this provider only, so it doesn't slow Google/EPO down
# if they're ever re-enabled alongside it.
_request_lock = threading.Lock()

# Keep the response lean and guarantee the full-text fields come back -
# Lens's default response omits abstract/claims/description unless asked for.
INCLUDE_FIELDS = [
    "lens_id",
    "jurisdiction",
    "doc_number",
    "kind",
    "date_published",
    "biblio.invention_title",
    "biblio.parties",
    "abstract",
    "claims",
    "description",
    "legal_status",
]


def _first_text(value: object, lang: str = "en") -> str:
    """Lens repeats abstract/description/title per language as a list of
    ``{text, lang}`` objects. Prefer an explicit English entry; fall back to
    whatever is first rather than assuming index 0 is English.

    Defensive against a bare dict too - the docs format the top-level
    ``description`` field ambiguously (list vs singular object depending on
    which section of the same page you read), so this handles both shapes
    rather than trusting one.
    """
    if isinstance(value, dict):
        return str(value.get("text", "") or "")
    if isinstance(value, list) and value:
        for entry in value:
            if isinstance(entry, dict) and entry.get("lang", "").lower() == lang:
                return str(entry.get("text", "") or "")
        first = value[0]
        return str(first.get("text", "") or "") if isinstance(first, dict) else ""
    return ""


def _claims_text(claims: object) -> str:
    """``claims`` is a list of per-language groups, each holding a list of
    claim-text objects (``{claim_text: [...]}}``) - see the Claims/Claims Text
    tables in the response reference. Flatten every claim string found.
    """
    if not isinstance(claims, list):
        return ""
    parts: list[str] = []
    for group in claims:
        if not isinstance(group, dict):
            continue
        for claim in group.get("claims", []) or []:
            if not isinstance(claim, dict):
                continue
            for text in claim.get("claim_text", []) or []:
                if text:
                    parts.append(str(text))
    return " ".join(parts)


def _to_patent_record(item: dict) -> PatentRecord:
    lens_id = str(item.get("lens_id", "") or "")
    jurisdiction = str(item.get("jurisdiction", "") or "")
    doc_number = str(item.get("doc_number", "") or "")
    kind = str(item.get("kind", "") or "")
    patent_id = f"{jurisdiction}{doc_number}{kind}"

    biblio = item.get("biblio", {}) or {}
    title = _first_text(biblio.get("invention_title"))

    abstract_text = _first_text(item.get("abstract"))
    description_text = _first_text(item.get("description"))
    claims_text = _claims_text(item.get("claims"))
    full_text = f"{description_text} {claims_text}".strip()

    return PatentRecord(
        id=patent_id,
        title=title,
        abstract=abstract_text,
        publication_date=str(item.get("date_published", "") or ""),
        source="Lens.org",
        url=f"https://www.lens.org/lens/patent/{lens_id}" if lens_id else "",
        full_text=full_text,
    )


MAX_SIZE = 5
"""Hard cap on results-per-query actually sent to Lens, independent of what
the caller asks for (RESULTS_PER_QUERY=10). Each result carries full text
(description + claims) built into the same response, and those responses
were taking 30s-700+s and getting cut off mid-transfer by what looks like a
connection-lifetime limit on Lens's own infrastructure. Smaller responses
finish faster and are less likely to run into that ceiling."""


def search_lens(query: str, size: int = 10) -> list[PatentRecord]:
    """Search Lens's Patent API and return results already carrying full text
    (description + claims), unlike the other providers in ``SOURCES``.

    Raises:
        RuntimeError: if ``LENS_API_KEY`` is not configured, or the API
            returns a non-2xx status, or the request times out. Mirrors the
            zero-tolerance policy the other providers follow - a provider
            that cannot answer fails the request rather than hanging
            indefinitely or returning a silently empty result.
    """
    if not settings.lens_api_key:
        raise RuntimeError("LENS_API_KEY is not configured")

    size = min(size, MAX_SIZE)

    with _request_lock:
        trace("lens.search.request", query=query, size=size)
        try:
            response = requests.post(
                LENS_SEARCH_URL,
                headers={
                    "Authorization": f"Bearer {settings.lens_api_key}",
                    "Content-Type": "application/json",
                },
                json={"query": query, "size": size, "include": INCLUDE_FIELDS},
                # (connect, read) tuple, not a single number - a single value
                # only resets on each received byte, which is what let one
                # call run past 200s (and another hang indefinitely) against
                # a slow trial-tier server. This is a hard per-phase cap:
                # if Lens goes quiet for 45s at any point, fail loudly rather
                # than hang the whole pipeline waiting on it.
                timeout=(10, 45),
            )
        except requests.exceptions.Timeout as error:
            raise RuntimeError(f"Lens Patent API timed out for query {query!r}: {error}") from error
        if response.status_code >= 400:
            # Mirror search/google.py's _raise_for_status reasoning: don't let a
            # library-generated error message carry anything sensitive. The
            # Authorization header (not the URL) holds the credential here, and
            # requests' HTTPError message only echoes the URL, so this is already
            # safe - but raise deliberately rather than call raise_for_status()
            # out of habit, so that stays true if this code is ever copied.
            raise RuntimeError(f"Lens Patent API search returned HTTP {response.status_code}: {response.text[:200]}")

        data = response.json()
        results = [_to_patent_record(item) for item in data.get("data", [])]
        trace("lens.search.response", query=query, count=len(results), ids=[r.id for r in results])
        return results
