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
import re
import threading
import hashlib
import time

import requests

from config import settings
from models import PatentClaim, PatentRecord
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
    "families.simple_family",
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


_LEADING_CLAIM_NUMBER = re.compile(r"^\s*(\d{1,4})\s*[.)\]]?\s+\S")
"""Matches the number patent claims almost always open with ("1. A method...").

Requires trailing non-space so a claim that is nothing but a number does not
parse as one, and allows ``.``/``)``/``]`` because the separator varies by
jurisdiction and by how the provider transcribed it."""


def _claim_group(claims: list, lang: str = "en") -> list:
    """Pick one language's claim list.

    ``claims`` is a list of per-language groups, each holding a list of
    claim-text objects (``{claim_text: [...]}``) - see the Claims/Claims Text
    tables in the response reference. The groups are *translations of each
    other*, not additional claims, so flattening all of them (as this module
    did until the evidence layer needed real claim numbers) triplicated an
    EP patent's claims through ``full_text`` into the embedding and the
    payload, and made claim numbering meaningless. Mirrors ``_first_text``:
    prefer an explicit English group, else take the first.
    """
    for group in claims:
        if isinstance(group, dict) and str(group.get("lang", "")).lower() == lang:
            return group.get("claims", []) or []
    for group in claims:
        if isinstance(group, dict):
            return group.get("claims", []) or []
    return []


class ClaimNumberingError(RuntimeError):
    """Lens returned a claim set whose numbering cannot be resolved unambiguously.

    Raised rather than worked around, because every available workaround is a
    fabrication. Renumbering the claims discards the document's own numbering, so
    a later evidence locator saying "claim 4" would point at whichever claim this
    code decided was fourth. Emitting a duplicate number produces two claims with
    the same locator and the same ``chunk_id``
    (``PAT-{pub}-CLM-{number:03d}``), which the verifier's passage validation
    rejects as a duplicate chunk (spec section 7.3 check 4) - but only if it ever
    sees both, and deduplication upstream could equally drop one silently.
    Allocating an unused number to sidestep the collision invents a claim number
    the patent does not have.

    A locator that names the wrong claim is worse than no locator: it is
    fabricated evidence with a citation attached. The repository's zero-tolerance
    policy applies exactly here, so an ambiguous claim set fails the provider call
    and ``search_all_providers_strict`` turns it into a ``PatentSearchError``.
    """


def _parse_claims(claims: object) -> list[PatentClaim]:
    """Split Lens's claims into individually numbered :class:`PatentClaim`s.

    A claim's ``claim_text`` is a list of line fragments of that one claim, so
    they are joined; separate claims are never joined to each other. That
    boundary is the whole point - an evidence locator has to name "claim 4",
    which is unrecoverable once the claims are one space-joined string.

    The number is read from the claim's own leading number where present, since
    that is the document's own numbering and survives a provider returning
    claims out of order or omitting one. Position is only the fallback, and it
    is 1-based because patent claims are.

    **The fallback is used only where it is unambiguous.** Assigning it blindly
    was a real collision: in a set where some claims carry numbers and one does
    not, the unnumbered claim's *position* can equal a number another claim
    explicitly states. ``["3. A method.", "A further method."]`` used to produce
    claims numbered 3 and 2 with no complaint, and
    ``["2. A method.", "A further method."]`` produced two claims both numbered
    2 - two locators reading "claim 2", and two identical ``chunk_id`` values.
    Numbering is therefore resolved in two passes: every explicit number is read
    first, and a positional fallback is taken only when nothing else claims it.

    Raises:
        ClaimNumberingError: if two claims state the same number, or if an
            unnumbered claim's positional fallback collides with a number some
            other claim states. Structurally malformed input still returns ``[]``
            - a missing or non-list ``claims`` field is "this patent has no
            claims from this provider", which is a different thing from
            "numbering that cannot be trusted".
    """
    if not isinstance(claims, list):
        return []

    # Pass 1: read the text and whatever number the document itself states.
    read: list[tuple[int, str, int | None]] = []
    for position, claim in enumerate(_claim_group(claims), start=1):
        if not isinstance(claim, dict):
            continue
        text = " ".join(
            str(fragment).strip()
            for fragment in claim.get("claim_text", []) or []
            if fragment
        ).strip()
        if not text:
            continue

        match = _LEADING_CLAIM_NUMBER.match(text)
        read.append((position, text, int(match.group(1)) if match else None))

    explicit: dict[int, str] = {}
    for _, text, number in read:
        if number is None:
            continue
        if number in explicit:
            raise ClaimNumberingError(
                f"Lens returned two claims both numbered {number}: "
                f"{explicit[number][:60]!r} and {text[:60]!r}. Claim numbers are the "
                f"document's own and cannot be reassigned, so this claim set has no "
                f"usable locators."
            )
        explicit[number] = text

    # Pass 2: assign, taking a positional fallback only where nothing claims it.
    parsed: list[PatentClaim] = []
    for position, text, number in read:
        if number is None:
            if position in explicit:
                raise ClaimNumberingError(
                    f"Lens returned an unnumbered claim at position {position} "
                    f"({text[:60]!r}) while another claim explicitly states claim "
                    f"{position} ({explicit[position][:60]!r}). The positional fallback "
                    f"would duplicate that number, and inventing a different one would "
                    f"invent a claim number the patent does not have."
                )
            number = position
        parsed.append(PatentClaim(number=number, text=text))

    return parsed


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
    claims = _parse_claims(item.get("claims"))

    # full_text stays exactly what it was - description then claims, one blob -
    # because compare.py, vector_store.py and the "no full text = drop" rule in
    # graph/searching.py all read it and none of them know about sections yet.
    # `description` and `claims` are additive: the structured view for the
    # evidence layer, alongside the flat view everything else still uses.
    claims_text = " ".join(claim.text for claim in claims)
    full_text = f"{description_text} {claims_text}".strip()

    simple_family = ((item.get("families") or {}).get("simple_family") or {})
    member_ids = sorted(
        str(member.get("lens_id") or "").strip()
        for member in (simple_family.get("members") or [])
        if isinstance(member, dict) and str(member.get("lens_id") or "").strip()
    )
    family_id = (
        "lens-simple-" + hashlib.sha256("|".join(member_ids).encode("utf-8")).hexdigest()[:16]
        if member_ids
        else ""
    )
    family_size = int(simple_family.get("size") or len(member_ids) or 0)

    return PatentRecord(
        id=patent_id,
        title=title,
        abstract=abstract_text,
        publication_date=str(item.get("date_published", "") or ""),
        source="Lens.org",
        url=f"https://www.lens.org/lens/patent/{lens_id}" if lens_id else "",
        full_text=full_text,
        description=description_text,
        claims=claims,
        family_id=family_id,
        family_size=family_size,
    )


MAX_SIZE = 5
"""Hard cap on results-per-query actually sent to Lens, independent of what
the caller asks for (RESULTS_PER_QUERY=10). Each result carries full text
(description + claims) built into the same response, and those responses
were taking 30s-700+s and getting cut off mid-transfer by what looks like a
connection-lifetime limit on Lens's own infrastructure. Smaller responses
finish faster and are less likely to run into that ceiling."""

MAX_ATTEMPTS = 2
RETRY_BACKOFF_SECONDS = 3.0
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


def _retry_delay(response: requests.Response | None = None) -> float:
    if response is not None:
        raw = response.headers.get("Retry-After", "")
        try:
            return min(30.0, max(RETRY_BACKOFF_SECONDS, float(raw)))
        except (TypeError, ValueError):
            pass
    return RETRY_BACKOFF_SECONDS


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
        for attempt in range(1, MAX_ATTEMPTS + 1):
            trace("lens.search.request", query=query, size=size, attempt=attempt)
            try:
                response = requests.post(
                    LENS_SEARCH_URL,
                    headers={
                        "Authorization": f"Bearer {settings.lens_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={"query": query, "size": size, "include": INCLUDE_FIELDS},
                    timeout=(10, 45),
                )
            except (
                requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError,
            ) as error:
                if attempt < MAX_ATTEMPTS:
                    logger.warning(
                        "Lens Patent API transient transport failure; retrying same query once"
                    )
                    time.sleep(RETRY_BACKOFF_SECONDS)
                    continue
                raise RuntimeError(
                    f"Lens Patent API transport failed after {MAX_ATTEMPTS} attempts "
                    f"for query {query!r}"
                ) from error

            if response.status_code in RETRYABLE_STATUSES and attempt < MAX_ATTEMPTS:
                logger.warning(
                    "Lens Patent API returned retryable HTTP %d; retrying same query once",
                    response.status_code,
                )
                time.sleep(_retry_delay(response))
                continue
            break
        else:  # pragma: no cover - the loop always returns or breaks
            raise AssertionError("Lens retry loop exhausted without a response")

        if response.status_code >= 400:
            # Mirror search/google.py's _raise_for_status reasoning: don't let a
            # library-generated error message carry anything sensitive. The
            # Authorization header (not the URL) holds the credential here, and
            # requests' HTTPError message only echoes the URL, so this is already
            # safe - but raise deliberately rather than call raise_for_status()
            # out of habit, so that stays true if this code is ever copied.
            raise RuntimeError(f"Lens Patent API search returned HTTP {response.status_code}")

        data = response.json()
        results = [_to_patent_record(item) for item in data.get("data", [])]
        trace("lens.search.response", query=query, count=len(results), ids=[r.id for r in results])
        return results
