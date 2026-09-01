"""EPO OPS full-text retrieval for the **evidence layer**: section-aware, with a
three-state outcome and a served-identity check.

Deliberately a separate module from `search/epo.py`, which stays exactly as it
is. That file is EPO's **discovery** provider -- `search_epo` plus the flat
`enrich_with_full_text` the search pipeline uses -- and this file is EPO's
**enrichment** role. A discovery source and an enrichment source are different
roles, and the defect §17.2 names came precisely from a function that assumed
they were the same. Keeping them apart means the search pipeline is untouched by
this gate and cannot regress.

**Three outcomes, because two of them used to be one value.**
`get_patent_full_text` caught `Exception`, logged, and returned `""`, so a
timeout and a genuine empty response were indistinguishable. One is a
publication with no text -- an exclusion, a real answer about the document. The
other is a provider outage -- a failure. Recording the second as the first
understates coverage using the provider's own downtime.

**Bounded retry, and exhaustion is a failure.** Transient errors (timeouts,
5xx, 429) are retried a fixed number of times; converting exhaustion into
`NO_TEXT` is forbidden.

**Every error is redacted before it is stored.** `provider_detail` goes into
`publication_exclusions` and can reach an operator; EPO authenticates with OAuth
client credentials and its URLs are logged, so the same
`workflow/redaction.py` that guards the terminal error guards this.

**Nothing here is called by the running application yet.** Gate C registers it as
a graph node; these are pure functions over explicit inputs.
"""

import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

import requests

from config import settings
from evidence.normalize import PublicationNumber, normalize
from models import (
    EnrichmentResult,
    EnrichmentStatus,
    ProviderClaim,
    SectionText,
    TextVersionSource,
)
from search.lens import ClaimNumberingError
from workflow import redaction

logger = logging.getLogger(__name__)

AUTH_URL = "https://ops.epo.org/3.2/auth/accesstoken"
PUBLISHED_DATA_URL = "https://ops.epo.org/3.2/rest-services/published-data/publication/epodoc"

FULLTEXT_NS = {"ftxt": "http://www.epo.org/fulltext", "ops": "http://ops.epo.org"}
EXCHANGE_NS = {"exchange": "http://www.epo.org/exchange", "ops": "http://ops.epo.org"}

MAX_ATTEMPTS = 3
"""Total attempts per section, including the first.

Bounded, and exhaustion is `FAILED`. An unbounded retry against a metered
provider is how a month's quota disappears in an afternoon, and a retry loop that
eventually gives up and reports "no text" is worse than one that fails.
"""

RETRY_BACKOFF_SECONDS = 1.0
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
"""Transient by definition. A 401 or a 403 is not retried: bad credentials do not
become good ones, and hammering an auth endpoint is how a key gets blocked."""

DEFAULT_LANGUAGE = "en"


class EpoEnrichmentFailed(RuntimeError):
    """The provider never told us whether text exists.

    Fails the run. This is emphatically **not** `EXCLUDED_NO_FULL_TEXT`: that
    state requires a *successful* response that contained no eligible text.
    """


@dataclass(frozen=True, slots=True)
class _SectionResponse:
    text: str
    paragraph_ids: tuple[str, ...]
    language: str
    present: bool
    """Whether the provider actually returned this section, as distinct from
    returning it empty."""

    claims: tuple[ProviderClaim, ...] = ()
    """Structured claims, for a claims section. The ONLY permitted source of
    claim evidence -- see `evidence/passages.py`."""


@dataclass(frozen=True, slots=True)
class _BiblioResponse:
    """A successful, well-formed bibliographic response.

    Constructed only when EPO actually answered. Its absence is a **failure**
    that raises, never a value that means "no date" -- which is the whole point
    of B1: a 401, a 429, a 503, a timeout or malformed XML tells us nothing about
    the document, and turning any of them into an empty string turned a provider
    outage into `EXCLUDED_INELIGIBLE`.
    """

    publication_date: str
    """The served document's publication date, or `""` when the response was
    valid and genuinely lacked one. That empty string is the ONE honest absence,
    and it is the only path to `EXCLUDED_INELIGIBLE`."""

    served_publication: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def get_access_token(session: Optional[requests.Session] = None) -> str:
    """Fetch an OAuth token. No caching, matching `search/epo.py`.

    Raises:
        EpoEnrichmentFailed: with a **redacted** message. An OAuth failure's text
            can carry the client secret, and this string is stored.
    """
    caller = session or requests
    try:
        response = caller.post(
            AUTH_URL,
            data={"grant_type": "client_credentials"},
            auth=(settings.epo_key, settings.epo_secret),
            timeout=30,
        )
        response.raise_for_status()
        return response.json()["access_token"]
    except Exception as error:
        raise EpoEnrichmentFailed(
            f"EPO OPS authentication failed: {redaction.safe_detail(error)}"
        ) from error


def _fetch_section(
    session: requests.Session,
    headers: dict[str, str],
    epodoc: str,
    section: str,
    timeout: int,
    sleep: Callable[[float], None],
) -> tuple[_SectionResponse, int]:
    """Fetch one section with bounded retry.

    Returns the section and how many physical attempts it took -- recorded
    because external calls are at-least-once and the cost should be visible.

    A 404 means *this document has no such section*, which is a real answer and
    not a failure: EPO's full-text service holds text only for EP/WO-family
    documents, so a US-only filing legitimately 404s here.
    """
    url = f"{PUBLISHED_DATA_URL}/{epodoc}/{section}"
    last_error: Optional[Exception] = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = session.get(url, headers=headers, timeout=timeout)
            if response.status_code == 404:
                return _SectionResponse("", (), DEFAULT_LANGUAGE, present=False), attempt
            if response.status_code in RETRYABLE_STATUS:
                last_error = RuntimeError(
                    f"EPO OPS returned HTTP {response.status_code} for {section}"
                )
                if attempt < MAX_ATTEMPTS:
                    sleep(RETRY_BACKOFF_SECONDS * attempt)
                    continue
                break
            response.raise_for_status()
            return _parse_section(response.content, section), attempt
        except (EpoEnrichmentFailed, ClaimNumberingError):
            # Deterministic. Malformed XML will not parse on the second attempt,
            # and a claim set the document did not number will not acquire
            # numbers by being asked again -- retrying either spends quota to
            # learn the same thing, and swallowing them into the generic
            # "failed after 3 attempts" message would lose the specific reason.
            raise
        except Exception as error:
            last_error = error
            if attempt < MAX_ATTEMPTS:
                sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue
            break

    raise EpoEnrichmentFailed(
        f"EPO OPS {section} retrieval for {epodoc} failed after {MAX_ATTEMPTS} "
        f"attempts: {redaction.safe_detail(last_error) if last_error else 'unknown'}"
    )


def _parse_section(payload: bytes, section: str) -> _SectionResponse:
    """Parse one full-text section, keeping the provider's own paragraph ids.

    The ids are what make an `EPO_PARAGRAPH` locator possible. When the document
    carries none, the empty tuple is what forces `SOURCE_RANGE` -- which is the
    honest outcome, rather than numbering paragraphs ourselves.

    A malformed body is a **failure**, not an empty section: XML we cannot parse
    tells us nothing about whether the document has text.
    """
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as error:
        raise EpoEnrichmentFailed(
            f"EPO OPS returned a body that is not valid XML for {section}: {error}"
        ) from error

    node = root.find(f".//ftxt:{section}", FULLTEXT_NS)
    if node is None:
        return _SectionResponse("", (), DEFAULT_LANGUAGE, present=False)

    language = node.attrib.get("lang", "").lower() or DEFAULT_LANGUAGE

    if section == "claims":
        return _parse_claims_section(node, language)

    paragraphs = node.findall(".//ftxt:p", FULLTEXT_NS)
    if paragraphs:
        texts: list[str] = []
        ids: list[str] = []
        for paragraph in paragraphs:
            text = " ".join(part.strip() for part in paragraph.itertext() if part.strip())
            if not text:
                continue
            texts.append(text)
            # `num` is the document's own numbering; `id` is the element's. Both
            # are the provider's, which is the whole point -- neither is ours.
            identifier = paragraph.attrib.get("num") or paragraph.attrib.get("id") or ""
            ids.append(identifier)
        # Provider numbering is usable only when EVERY paragraph carries one. A
        # partial set would number some passages and range others.
        usable = tuple(ids) if all(ids) else ()
        return _SectionResponse("\n\n".join(texts), usable, language, present=True)

    text = " ".join(part.strip() for part in node.itertext() if part.strip())
    return _SectionResponse(text, (), language, present=True)


_LEADING_NUMBER = re.compile(r"^\s*\[?(\d{1,4})[.)\]]?\s+")


def _claim_number(element: ET.Element, text: str) -> Optional[int]:
    """The claim's **own** number, from the document, or None.

    Two sources, both the provider's:

    1. the `num` attribute EPO puts on a `<claim>` element;
    2. the claim text's own leading digits, which is how a claim reads on the
       page -- ``1. A system comprising...``.

    **Position is never a source.** A retrieved claim set can legitimately start
    at claim 7, and numbering by position would make a locator say "claim 1" for
    text that is claim 7 -- a wrong citation with a confident address. Returning
    None is how this function says "the document did not tell me", which the
    caller turns into a refusal rather than a guess.
    """
    raw = (element.attrib.get("num") or element.attrib.get("id") or "").strip()
    if raw.isdigit():
        return int(raw)

    match = _LEADING_NUMBER.match(text)
    if match:
        return int(match.group(1))
    return None


def _parse_claims_section(node: ET.Element, language: str) -> _SectionResponse:
    """Parse an EPO claims section into individually numbered claims.

    The section text is rebuilt by joining the claims in document order, and each
    claim records its offsets into that exact string -- so every claim passage is
    traceable back into the text whose SHA-256 the section carries.

    Raises:
        ClaimNumberingError: when the numbering cannot be resolved honestly --
            a claim the document did not number, or two claims stating the same
            number. Both alternatives are fabrications: numbering by position
            invents a number the document does not have, and a duplicate produces
            two passages with one `chunk_id`, one of which is silently dropped.

            This **fails the provider call** rather than emitting a bad locator,
            which is what the specification requires. The cost is that one
            malformed document fails a whole run; the alternative is a citation
            that points at the wrong claim, which is worse.
    """
    elements = node.findall(".//ftxt:claim", FULLTEXT_NS)
    if not elements:
        # No <claim> wrappers: a flat claims blob. It has no per-claim structure,
        # so no claim can be located honestly. Reported as a present-but-unusable
        # section, which becomes EXCLUDED_INELIGIBLE with a reason.
        flat = " ".join(part.strip() for part in node.itertext() if part.strip())
        return _SectionResponse(flat, (), language, present=True, claims=())

    texts: list[str] = []
    numbers: list[Optional[int]] = []
    for element in elements:
        text = " ".join(part.strip() for part in element.itertext() if part.strip())
        if not text:
            continue
        texts.append(text)
        numbers.append(_claim_number(element, text))

    if not texts:
        return _SectionResponse("", (), language, present=True, claims=())

    unnumbered = [index for index, number in enumerate(numbers) if number is None]
    if unnumbered:
        raise ClaimNumberingError(
            f"EPO served {len(unnumbered)} claim(s) the document does not number. "
            "Numbering them by position would invent claim numbers the patent does "
            "not have, and every locator built from them would be a fabricated "
            "citation."
        )

    seen: dict[int, int] = {}
    for index, number in enumerate(numbers):
        if number in seen:
            raise ClaimNumberingError(
                f"EPO served two claims both numbered {number}. Emitting both would "
                "produce two passages with one chunk_id, and one would be silently "
                "dropped; renumbering either would discard the document's own "
                "numbering."
            )
        seen[number] = index

    section_text = "\n\n".join(texts)
    claims: list[ProviderClaim] = []
    cursor = 0
    for text, number in zip(texts, numbers):
        start = section_text.index(text, cursor)
        end = start + len(text)
        cursor = end
        claims.append(
            ProviderClaim(number=int(number), text=text, char_start=start, char_end=end)
        )

    return _SectionResponse(section_text, (), language, present=True, claims=tuple(claims))


NON_RETRYABLE_STATUS = frozenset({400, 401, 403, 404, 405, 406, 410})
"""Deterministic. Retrying these changes nothing and, for 401/403, hammering an
auth endpoint is how a key gets blocked."""


def _fetch_biblio(
    session: requests.Session,
    headers: dict[str, str],
    epodoc: str,
    timeout: int,
    sleep: Callable[[float], None],
) -> tuple[_BiblioResponse, int]:
    """Fetch bibliographic data, distinguishing **three** outcomes.

    This function used to return ``("", "")`` for every non-200 and every
    exception, which quietly turned a 401, a 429, a 503, a timeout, a transport
    error and malformed XML into "no publication date" -- and from there into
    `EXCLUDED_INELIGIBLE`. That is a **provider failure converted into an
    exclusion**: the run reports a publication as ineligible when in truth nobody
    ever asked the provider successfully. It also silently understates coverage
    using the provider's own downtime.

    The three outcomes now:

    1. **200 with a valid date** -- the trusted text version. Continue.
    2. **200, well-formed, genuinely no publication date** -- the one honest
       absence. `publication_date=""`, which `eligibility.py` turns into
       `EXCLUDED_INELIGIBLE` under the approved text-version policy.
    3. **Anything else** -- raises `EpoEnrichmentFailed`. The run fails. Never an
       exclusion, never `NO_TEXT`.

    Retry is bounded and applies only to genuinely transient conditions: 429,
    approved 5xx, timeouts and transport errors. A 401/403/404 is not retried,
    and malformed XML is not retried either -- a body that did not parse will not
    parse on the second attempt, and re-requesting it spends quota to learn the
    same thing.

    Returns:
        The response and how many physical attempts it took.
    """
    url = f"{PUBLISHED_DATA_URL}/{epodoc}/biblio"
    last_error: Optional[Exception] = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = session.get(url, headers=headers, timeout=timeout)
        except Exception as error:
            # Timeout or transport failure: transient by nature.
            last_error = error
            if attempt < MAX_ATTEMPTS:
                sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue
            break

        status = response.status_code
        if status == 200:
            return _parse_biblio(response.content, epodoc), attempt

        if status in NON_RETRYABLE_STATUS:
            raise EpoEnrichmentFailed(
                f"EPO OPS bibliographic data for {epodoc} returned HTTP {status}. "
                "Without it there is no trustworthy text version, and a failed "
                "request is not an absence of one -- the run fails rather than "
                "recording an exclusion nobody established."
            )

        if status in RETRYABLE_STATUS:
            last_error = RuntimeError(f"EPO OPS returned HTTP {status} for biblio")
            if attempt < MAX_ATTEMPTS:
                sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue
            break

        raise EpoEnrichmentFailed(
            f"EPO OPS bibliographic data for {epodoc} returned an unexpected "
            f"HTTP {status}."
        )

    raise EpoEnrichmentFailed(
        f"EPO OPS bibliographic data for {epodoc} failed after {MAX_ATTEMPTS} "
        f"attempts: {redaction.safe_detail(last_error) if last_error else 'unknown'}"
    )


def _parse_biblio(payload: bytes, epodoc: str) -> _BiblioResponse:
    """Parse a 200 bibliographic body.

    Malformed XML **raises**: a body we cannot parse tells us nothing about the
    document, and treating it as "no date" would be the same
    failure-becomes-exclusion bug one layer down. It is deliberately not retried
    -- it is deterministic.
    """
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as error:
        raise EpoEnrichmentFailed(
            f"EPO OPS returned a bibliographic body for {epodoc} that is not valid "
            f"XML: {error}. A body that did not parse says nothing about the "
            "document, so this is a failure rather than a missing date."
        ) from error

    document = root.find(".//exchange:exchange-document", EXCHANGE_NS)
    served = ""
    if document is not None:
        country = document.attrib.get("country", "")
        number = document.attrib.get("doc-number", "")
        served = f"{country}{number}"

    date_text = ""
    for node in root.findall(
        ".//exchange:publication-reference//exchange:date", EXCHANGE_NS
    ):
        if node.text and node.text.strip():
            date_text = node.text.strip()
            break

    # A 200 that carries no exchange-document at all is not a well-formed answer
    # about this publication -- it is a response shape we do not understand, and
    # guessing that it means "no date" would be the same defect again.
    if document is None:
        raise EpoEnrichmentFailed(
            f"EPO OPS returned a bibliographic response for {epodoc} with no "
            "exchange-document. The response shape is unrecognised, which is a "
            "failure rather than an absent publication date."
        )

    return _BiblioResponse(publication_date=date_text, served_publication=served)


def enrich_publication(
    publication_number: str,
    *,
    session: Optional[requests.Session] = None,
    timeout: int = 30,
    sleep: Callable[[float], None] = time.sleep,
) -> EnrichmentResult:
    """Retrieve one publication's claims and description from EPO OPS.

    The whole enrichment contract in one function:

    * normalizes to epodoc form **before** the lookup, because the full-text
      endpoints 404 on a kind-suffixed identifier;
    * returns sections with their exact text and SHA-256, not a concatenated
      blob, because `SOURCE_RANGE` offsets index that exact string;
    * carries back the identity EPO actually served, so borrowed family text can
      be refused downstream;
    * takes the text version from the served document's **publication date**, per
      the approved policy, and records `retrieved_at` separately -- retrieval
      time is never substituted for a document's version;
    * distinguishes `TEXT`, `NO_TEXT` and `FAILED`.

    Raises:
        EpoEnrichmentFailed: on timeout, 5xx, transport or auth failure, after
            bounded retry. Never returns `NO_TEXT` for these.
    """
    publication: PublicationNumber = normalize(publication_number)
    owned_session = session is None
    session = session or requests.Session()
    retrieved_at = _now()
    attempts = 0

    try:
        token = get_access_token(session)
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/xml"}

        claims, used = _fetch_section(
            session, headers, publication.normalized, "claims", timeout, sleep
        )
        attempts += used
        description, used = _fetch_section(
            session, headers, publication.normalized, "description", timeout, sleep
        )
        attempts += used
        biblio, used = _fetch_biblio(
            session, headers, publication.normalized, timeout, sleep
        )
        attempts += used
    finally:
        if owned_session:
            session.close()

    sections: list[SectionText] = []
    available: list[str] = []
    retrieved: list[str] = []
    for name, response in (("CLAIMS", claims), ("DESCRIPTION", description)):
        if response.present:
            available.append(name)
        if response.text.strip():
            retrieved.append(name)
            sections.append(
                SectionText(
                    section=name,
                    text=response.text,
                    text_sha256=_sha256(response.text),
                    language=response.language,
                    paragraph_ids=response.paragraph_ids,
                    claims=response.claims,
                )
            )

    normalized_version = _normalize_version_date(biblio.publication_date)
    served = biblio.served_publication
    status = EnrichmentStatus.TEXT if sections else EnrichmentStatus.NO_TEXT

    return EnrichmentResult(
        requested_publication=publication.requested,
        normalized_publication=publication.normalized,
        served_publication=served,
        source_document_id=publication.source_document_id,
        status=status,
        sections=tuple(sections),
        sections_available=tuple(available),
        sections_retrieved=tuple(retrieved),
        language_attempted=DEFAULT_LANGUAGE,
        text_version_original=biblio.publication_date,
        text_version_normalized=normalized_version,
        text_version_source=(
            TextVersionSource.PROVIDER_PUBLICATION_DATE
            if normalized_version
            else TextVersionSource.NONE
        ),
        retrieved_at=retrieved_at,
        attempts=attempts,
    )


def _normalize_version_date(raw: str) -> str:
    """EPO's `YYYYMMDD` to ISO, or `""`.

    Reuses the evidence layer's parser rather than a second one, so "what counts
    as a date" has exactly one answer in this codebase.
    """
    from evidence.dates import parse_patent_date

    parsed = parse_patent_date(raw)
    return parsed.isoformat() if parsed else ""
