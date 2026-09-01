"""``python -m evidence.lens_coverage`` -- **Phase 3A measurement only.**

Ask one question, at a bounded cost: *if Lens.org replaced EPO OPS as the
evidence (full-text) source, what would the evidence layer actually get?*

**This module changes nothing.** It is not imported by `graph/`, `workflow/`,
`api/`, `server.py` or `search/search.py`; `SOURCES` is untouched, discovery is
still Lens.org and evidence is still EPO OPS. It reads `search/lens.py`'s
parsing helpers rather than reimplementing them, so what it measures is what the
production adapter would actually produce -- a second parser would measure
itself.

**It writes nothing anywhere except one dated markdown artifact.** No database
write, no Qdrant write, no cache. The artifact is a measurement, not a cache:
nothing in the pipeline reads it and it never short-circuits a provider call.

Shaped after `evidence/coverage.py`, and for the same reasons:

* **The sample is TAKEN, never GENERATED.** A file of publication numbers, or
  `patents.id` rows already stored. Never a fresh discovery search -- "go and
  find 30 real publications to measure" is exactly how a trial allowance
  disappears.
* **`--dry-run` performs zero HTTP calls.** Run it first, every time.
* **Duplicates are collapsed before querying**, and the collapse is reported.
* **One call per distinct publication, sequential.** `search/lens.py` holds a
  module-level `_request_lock` because the trial tier cut a connection
  mid-response under a fan-out of five; concurrency here is 1 by construction
  and there is no flag that could raise it.
* **`--max-calls` is a hard ceiling** checked *before* every request, so a
  mis-typed sample file cannot spend more than was authorised. A ceiling
  enforced afterwards has already spent the call.

**No credential means NOT RUN, and NOT RUN writes no coverage artifact.** An
absent key is not a measurement of Lens, and a file recording 0% because nobody
could authenticate would be indistinguishable from one recording that Lens holds
nothing -- the same substitution `EnrichmentStatus.FAILED` exists to prevent.

**Nothing here prints a credential or a provider response body.** The token is
read from `settings.lens_api_key`, placed straight into an Authorization header,
and never rendered; every failure is passed through `workflow/redaction.py`
before it reaches stdout or the artifact, and an error body is inspected only to
select a category and is then discarded.
`tests/test_lens_coverage_secrets.py` pins all of that.
"""

import argparse
import json
import logging
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Callable, Optional

import requests

from config import settings
from evidence.normalize import PublicationNumber, deduplicate
from evidence.segmenter import SEGMENTER_VERSION, segment_description
from search.lens import (
    INCLUDE_FIELDS,
    LENS_SEARCH_URL,
    ClaimNumberingError,
    _first_text,
    _parse_claims,
)
from workflow import redaction

logger = logging.getLogger(__name__)

ARTIFACT_DIRECTORY = (
    Path(__file__).resolve().parent.parent.parent / "docs" / "measurements"
)
DEFAULT_SAMPLE = (
    Path(__file__).resolve().parent
    / "samples"
    / "epo-coverage-2026-08-28.publications.txt"
)

DEFAULT_MAX_CALLS = 40
"""Hard ceiling on live Lens requests in one run.

Slightly above the 34-publication reference sample and far below anything that
could exhaust a trial allowance by accident. Checked before every request.
"""

REQUEST_TIMEOUT = (10, 45)
"""(connect, read), copied from `search/lens.py`'s reasoning verbatim: a single
timeout value only resets on each received byte, which is what let one
trial-tier call run past 200s. This is a hard per-phase cap."""

REQUESTS_PER_MINUTE = 10
"""The trial tier's per-minute ceiling, **measured, not assumed** (2026-08-31).

Lens reports it in `x-rate-limit-remaining-request-per-minute`. The first
unpaced run of this tool spent 34 requests and got 23 back as HTTP 429: ten
answered, then the bucket was empty. A rate limit is not a finding about
coverage, and 23 publications with no data is not a measurement -- so the tool
paces itself rather than reporting the provider's throttle as absent text.
"""

DEFAULT_DELAY_SECONDS = round(60.0 / REQUESTS_PER_MINUTE + 0.5, 1)
"""Derived from the measured ceiling, with half a second of headroom, so a
reviewer raising `REQUESTS_PER_MINUTE` after a tier change does not have to
remember a second number."""

MAX_ATTEMPTS = 3
"""Total attempts per publication, including the first.

Bounded, and exhaustion is `FAILED` -- the same shape `search/epo_fulltext.py`
uses, and for the same reason: an unbounded retry against a metered provider is
how a month's quota disappears, and a retry loop that eventually gives up and
reports "no text" is worse than one that fails.
"""

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
"""Transient by definition. A 401 or 403 is never retried: bad credentials do
not become good ones, and hammering an auth endpoint is how a key gets blocked."""

RETRY_BACKOFF_SECONDS = 8.0
"""Floor for a 429 backoff when the provider sends no `Retry-After`. Longer than
one request slot, because the thing being waited on is a per-minute bucket."""


class NoCredential(RuntimeError):
    """`LENS_API_KEY` is not configured. The probe is NOT RUN.

    Deliberately not a measurement outcome. A run that could not authenticate
    says nothing about what Lens holds, and writing an artifact from it would
    put a 0% coverage number on disk that reads exactly like a measured absence.
    """


class NoSampleAvailable(RuntimeError):
    """No usable sample. Stops, and asks -- never runs discovery to build one."""


# --------------------------------------------------------------------------
# Outcome vocabulary
# --------------------------------------------------------------------------


class Outcome:
    """What happened, split the way `EnrichmentStatus` splits it.

    `TEXT`, `NO_TEXT` and `NOT_MATCHED` are answers about the document.
    `FAILED` is an answer about the *provider* and is never folded into the
    coverage denominator -- counting an outage as absent text would understate
    coverage using the provider's own downtime.
    """

    TEXT = "TEXT"
    """Lens answered and returned description and/or claims for the publication."""

    NO_TEXT = "NO_TEXT"
    """Lens answered, matched the publication, and had no eligible section text."""

    NOT_MATCHED = "NOT_MATCHED"
    """Lens answered and returned no record under this publication number. A real
    answer about coverage, and distinct from `NO_TEXT`: one says the index does
    not hold the document, the other says it holds it without text."""

    FAILED = "FAILED"
    """Provider-side. See `failure_category`. Never counted as `NO_TEXT`."""


class FailureCategory:
    """Sanitized failure classes, mirroring the semantic/operational split
    `verification/capability.py` uses: an operational failure is not a data
    point and must never contribute a passing or a zero term."""

    NONE = ""
    AUTH_FAILED = "AUTH_FAILED"
    RATE_LIMITED = "RATE_LIMITED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    BAD_REQUEST = "BAD_REQUEST"
    NOT_AUTHORIZED_FOR_FIELD = "NOT_AUTHORIZED_FOR_FIELD"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    TIMEOUT = "TIMEOUT"
    TRUNCATED_RESPONSE = "TRUNCATED_RESPONSE"
    UNREACHABLE = "UNREACHABLE"
    MALFORMED_JSON = "MALFORMED_JSON"
    CLAIM_NUMBERING_AMBIGUOUS = "CLAIM_NUMBERING_AMBIGUOUS"


class ClaimNumbering:
    STABLE = "STABLE"
    """Every claim carried its own number, or took a positional fallback nothing
    else claimed. Locators are trustworthy."""

    AMBIGUOUS = "AMBIGUOUS"
    """`ClaimNumberingError`. Under the zero-tolerance policy this fails the
    provider call rather than producing a locator naming the wrong claim."""

    NONE = "NONE"
    """No claims returned at all -- a different statement from untrustworthy
    numbering, and recorded as one."""


@dataclass(frozen=True, slots=True)
class ProbeRow:
    """One publication's measured outcome.

    Every field is either a fact Lens stated or a computation over text Lens
    returned. Nothing here is inferred, defaulted from a sibling, or carried
    over from the EPO measurement.
    """

    requested: str
    normalized: str
    country: str

    outcome: str
    http_status: Optional[int] = None
    failure_category: str = FailureCategory.NONE
    detail: str = ""
    """REDACTED before it is set. No token, no response body, no traceback."""

    elapsed_seconds: float = 0.0

    served_publication: str = ""
    """`jurisdiction + doc_number + kind` as Lens reported it; empty when nothing
    matched. Compared against `normalized` rather than assumed equal -- borrowed
    family text is a different document with a different date."""

    served_matches_request: Optional[bool] = None
    hits: int = 0

    description_available: bool = False
    description_chars: int = 0
    claims_available: bool = False
    claims_count: int = 0
    claim_numbering: str = ClaimNumbering.NONE

    language_description: str = ""
    language_claims: str = ""

    date_published: str = ""
    text_version_normalized: str = ""
    """ISO form of `date_published`, via `evidence.dates.parse_patent_date` --
    the same parser the EPO path uses, so "what counts as a date" keeps one
    answer in this codebase."""

    source_range_locators: int = 0
    source_range_reproducible: Optional[bool] = None
    """Whether slicing the derived offsets back out of the exact retrieved text
    reproduces every segment byte-for-byte. `None` when there was no description
    text to segment -- which is not the same as a failed reproduction."""

    paragraph_ids_supplied: bool = False
    """Lens serves `description` as one `{text, lang}` blob, so this is expected
    to be False everywhere. Recorded rather than asserted: "the provider numbered
    nothing" is what forces `SOURCE_RANGE`, and it should be measured."""

    attempts: int = 1
    """Physical requests made for this publication, including retried ones.
    Recorded because external calls are metered and the cost should be visible
    -- the same reason `EnrichmentResult.attempts` exists."""

    rate_limit_remaining_minute: Optional[int] = None
    rate_limit_remaining_month: Optional[int] = None
    """From Lens's own `x-rate-limit-remaining-*` headers. Not a credential and
    not a response body: a quota reading, which is exactly what a report on a
    trial tier has to be able to state."""

    contract_fields_present: tuple[str, ...] = ()
    contract_fields_missing: tuple[str, ...] = ()
    """Against `models.EnrichmentResult` and `evidence/passages.py`'s `_passage`
    requirements. `source` and `source_document_id` are structurally
    unproducible from Lens today -- both are hardcoded to EPO in production --
    and are always reported missing. See the artifact's limitations section."""

    @property
    def answered(self) -> bool:
        return self.outcome != Outcome.FAILED


# --------------------------------------------------------------------------
# The probe
# --------------------------------------------------------------------------


def build_query(publication: PublicationNumber) -> str:
    """Lens's string query for one publication.

    Country and document number, never the kind code: `PublicationNumber`
    already merges `US1234567A1` and `US1234567B2` onto one identity, and the
    kind code is what Lens varies between publication stages of the same
    document. The served identity is recorded separately, so a stage mismatch is
    visible rather than silently accepted.
    """
    return f"jurisdiction:{publication.country} AND doc_number:{publication.number}"


def _classify_status(status: int, body_hint: str) -> str:
    """Map an HTTP status to a sanitized category.

    `body_hint` is inspected and then **discarded**: only the category it
    selects is ever stored, printed or written to the artifact. A provider body
    is untrusted text that can carry an echoed request, and this function is the
    one place it is looked at.
    """
    lowered = body_hint.lower()
    if status in (401, 403):
        if "quota" in lowered or "limit" in lowered:
            return FailureCategory.QUOTA_EXCEEDED
        if "not authorized" in lowered or "subscription" in lowered:
            return FailureCategory.NOT_AUTHORIZED_FOR_FIELD
        return FailureCategory.AUTH_FAILED
    if status == 429:
        return FailureCategory.RATE_LIMITED
    if status in (400, 404, 422):
        return FailureCategory.BAD_REQUEST
    return FailureCategory.PROVIDER_ERROR


def _language_of(value: object) -> str:
    """The language tag on a `{text, lang}` block.

    `search/lens.py`'s `_first_text` reads the text and discards the tag; an
    evidence passage carries a language and a wrong one is a false statement
    about the document, so it is read here rather than assumed to be English.
    """
    if isinstance(value, dict):
        return str(value.get("lang", "") or "")
    if isinstance(value, list) and value:
        for entry in value:
            if isinstance(entry, dict) and str(entry.get("lang", "")).lower() == "en":
                return "en"
        first = value[0]
        return str(first.get("lang", "") or "") if isinstance(first, dict) else ""
    return ""


def _claims_language(claims: object) -> str:
    """The language of the claim group `_parse_claims` would pick.

    Mirrors `_claim_group`'s preference order exactly -- English first, else the
    first group -- because reporting a different language from the one whose
    claims were counted would describe a group that was never parsed.
    """
    if not isinstance(claims, list):
        return ""
    for group in claims:
        if isinstance(group, dict) and str(group.get("lang", "")).lower() == "en":
            return "en"
    for group in claims:
        if isinstance(group, dict):
            return str(group.get("lang", "") or "")
    return ""


_UNPRODUCIBLE_FROM_LENS = (
    "claims_char_offsets",
    "paragraph_ids",
    "source",
    "source_document_id",
)
"""Fields the evidence contract needs that Lens structurally cannot supply today.

* `source` is hardcoded ``"EPO OPS"`` in `evidence/passages.py:_passage` and
  `source_document_id` is hardcoded ``epo:{normalized}`` in
  `evidence/normalize.py`, so a Lens-sourced passage cannot be built today
  without stamping EPO provenance on text EPO never served -- the exact defect
  `evidence/passages.py`'s docstring exists to prevent.
* `paragraph_ids` is absent because Lens serves the description as one blob,
  so every description locator would be `SOURCE_RANGE`.
* `ProviderClaim.char_start`/`char_end` have no source: `_parse_claims` returns
  `PatentClaim`, which carries no offsets, and Lens returns claims as a list of
  fragments rather than as one canonical section text to index into.

Reported as a gap, never resolved by this tool. Closing any of them is an
implementation change, and Phase 3A is measurement only.
"""


def _contract_fields(
    *,
    publication: PublicationNumber,
    served: str,
    description: str,
    claims_count: int,
    language: str,
    text_version: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Which `EnrichmentResult` / passage fields this record could fill.

    `_passage` refuses to construct a passage with any identity field empty, so
    this is the same question asked one layer earlier: could an honest passage
    have been built from what Lens returned?
    """
    has_text = bool(description.strip()) or claims_count > 0
    checks = {
        "requested_publication": bool(publication.requested),
        "normalized_publication": bool(publication.normalized),
        "served_publication": bool(served),
        "status": True,
        "section_text": has_text,
        "text_sha256": has_text,
        "language": bool(language),
        "text_version_normalized": bool(text_version),
        "text_version_source": bool(text_version),
        "retrieved_at": True,
    }
    present = tuple(sorted(name for name, ok in checks.items() if ok))
    missing = tuple(
        sorted(
            [name for name, ok in checks.items() if not ok]
            + list(_UNPRODUCIBLE_FROM_LENS)
        )
    )
    return present, missing


def _measure_record(
    publication: PublicationNumber,
    item: dict,
    elapsed: float,
    status: int,
    hits: int,
) -> ProbeRow:
    """Everything derivable from one matched Lens record, and nothing else."""
    jurisdiction = str(item.get("jurisdiction", "") or "")
    doc_number = str(item.get("doc_number", "") or "")
    kind = str(item.get("kind", "") or "")
    served = f"{jurisdiction}{doc_number}{kind}"

    description = _first_text(item.get("description"))
    raw_claims = item.get("claims")

    numbering = ClaimNumbering.NONE
    claims_count = 0
    failure = FailureCategory.NONE
    detail = ""
    try:
        parsed = _parse_claims(raw_claims)
        claims_count = len(parsed)
        numbering = ClaimNumbering.STABLE if parsed else ClaimNumbering.NONE
    except ClaimNumberingError as error:
        # Not a provider failure: Lens answered. It is a statement that this
        # document's claim locators cannot be trusted, which under the
        # zero-tolerance policy fails the provider call in production.
        numbering = ClaimNumbering.AMBIGUOUS
        failure = FailureCategory.CLAIM_NUMBERING_AMBIGUOUS
        detail = redaction.safe_detail(error, step="lens_coverage.claims")

    language_description = _language_of(item.get("description"))
    language_claims = _claims_language(raw_claims)

    from evidence.dates import parse_patent_date

    date_published = str(item.get("date_published", "") or "")
    parsed_date = parse_patent_date(date_published)
    text_version = parsed_date.isoformat() if parsed_date else ""

    segments = segment_description(description) if description.strip() else []
    reproducible: Optional[bool] = None
    if description.strip():
        reproducible = bool(segments) and all(
            description[segment.start : segment.end] == segment.text
            for segment in segments
        )

    present, missing = _contract_fields(
        publication=publication,
        served=served,
        description=description,
        claims_count=claims_count,
        language=language_description or language_claims,
        text_version=text_version,
    )

    has_text = bool(description.strip()) or claims_count > 0
    return ProbeRow(
        requested=publication.requested,
        normalized=publication.normalized,
        country=publication.country,
        outcome=Outcome.TEXT if has_text else Outcome.NO_TEXT,
        http_status=status,
        failure_category=failure,
        detail=detail,
        elapsed_seconds=round(elapsed, 2),
        served_publication=served,
        served_matches_request=(
            f"{jurisdiction}{doc_number}" == publication.normalized
        ),
        hits=hits,
        description_available=bool(description.strip()),
        description_chars=len(description),
        claims_available=claims_count > 0,
        claims_count=claims_count,
        claim_numbering=numbering,
        language_description=language_description,
        language_claims=language_claims,
        date_published=date_published,
        text_version_normalized=text_version,
        source_range_locators=len(segments),
        source_range_reproducible=reproducible,
        paragraph_ids_supplied=False,
        contract_fields_present=present,
        contract_fields_missing=missing,
    )


def _failed(
    publication: PublicationNumber,
    elapsed: float,
    category: str,
    error: BaseException,
    status: Optional[int] = None,
    attempts: int = 1,
) -> ProbeRow:
    return ProbeRow(
        requested=publication.requested,
        normalized=publication.normalized,
        country=publication.country,
        outcome=Outcome.FAILED,
        http_status=status,
        failure_category=category,
        detail=redaction.safe_detail(error, step="lens_coverage"),
        elapsed_seconds=round(elapsed, 2),
        attempts=attempts,
    )


def _rate_limits(response) -> tuple[Optional[int], Optional[int]]:
    """The two quota readings Lens returns, or `None` when it did not.

    Headers, not body -- a quota number is a fact about the account and is
    exactly what a report on a trial tier has to be able to state. `None` is
    kept distinct from `0`: "the provider did not say" and "nothing left" are
    different, and a `0` that actually meant silence would misreport the budget.
    """

    def read(name: str) -> Optional[int]:
        raw = response.headers.get(name) if hasattr(response, "headers") else None
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    return (
        read("x-rate-limit-remaining-request-per-minute"),
        read("x-rate-limit-remaining-request-per-month"),
    )


def _retry_after(response, default: float) -> float:
    """Honour `Retry-After` when the provider sends one, else back off.

    Never shorter than the default: a provider asking for less time than a
    per-minute bucket needs to refill would just produce another 429.
    """
    raw = response.headers.get("Retry-After") if hasattr(response, "headers") else None
    try:
        return max(float(raw), default) if raw is not None else default
    except (TypeError, ValueError):
        return default


def probe_publication(
    publication: PublicationNumber,
    *,
    session: requests.Session,
    api_key: str,
    timeout: tuple[int, int] = REQUEST_TIMEOUT,
    sleep: Callable[[float], None] = time.sleep,
) -> ProbeRow:
    """One publication, with bounded retry on a transient provider failure.

    **Never raises.** A provider failure is a recorded `FAILED` row, because one
    unreachable call must not abandon the measurement of the other
    thirty-three -- and because `FAILED` is exactly the state that keeps an
    outage out of the coverage denominator.

    **A 429 is retried, and exhaustion is still `FAILED`.** The first unpaced
    run of this tool turned the trial tier's per-minute bucket into 23
    publications with no data; reporting a throttle as a finding about coverage
    is the same substitution `EnrichmentStatus.FAILED` exists to prevent.
    Deterministic 4xx -- auth, bad request -- are never retried.
    """
    started = time.monotonic()
    attempts = 0
    minute = month = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        attempts = attempt
        try:
            response = session.post(
                LENS_SEARCH_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "query": build_query(publication),
                    "size": 5,
                    "include": INCLUDE_FIELDS,
                },
                timeout=timeout,
            )
        except requests.exceptions.Timeout as error:
            return _failed(
                publication,
                time.monotonic() - started,
                FailureCategory.TIMEOUT,
                error,
                attempts=attempts,
            )
        except requests.exceptions.ChunkedEncodingError as error:
            return _failed(
                publication,
                time.monotonic() - started,
                FailureCategory.TRUNCATED_RESPONSE,
                error,
                attempts=attempts,
            )
        except requests.exceptions.RequestException as error:
            return _failed(
                publication,
                time.monotonic() - started,
                FailureCategory.UNREACHABLE,
                error,
                attempts=attempts,
            )

        minute, month = _rate_limits(response)
        if response.status_code in RETRYABLE_STATUS and attempt < MAX_ATTEMPTS:
            sleep(_retry_after(response, RETRY_BACKOFF_SECONDS))
            continue
        break

    elapsed = time.monotonic() - started
    if response.status_code >= 400:
        return ProbeRow(
            requested=publication.requested,
            normalized=publication.normalized,
            country=publication.country,
            outcome=Outcome.FAILED,
            http_status=response.status_code,
            failure_category=_classify_status(
                response.status_code, response.text[:400]
            ),
            # Built from the status alone. The body selected a category above
            # and is deliberately not carried into the row.
            detail=f"Lens Patent API returned HTTP {response.status_code}",
            elapsed_seconds=round(elapsed, 2),
            attempts=attempts,
            rate_limit_remaining_minute=minute,
            rate_limit_remaining_month=month,
        )

    try:
        data = response.json()
    except (ValueError, json.JSONDecodeError) as error:
        return _failed(
            publication,
            elapsed,
            FailureCategory.MALFORMED_JSON,
            error,
            status=response.status_code,
            attempts=attempts,
        )

    items = data.get("data", []) or []
    if not items:
        return ProbeRow(
            requested=publication.requested,
            normalized=publication.normalized,
            country=publication.country,
            outcome=Outcome.NOT_MATCHED,
            http_status=response.status_code,
            elapsed_seconds=round(elapsed, 2),
            hits=0,
            attempts=attempts,
            rate_limit_remaining_minute=minute,
            rate_limit_remaining_month=month,
        )

    # Prefer the record whose own identity matches what was asked for. A
    # different document that merely came back first is borrowed family text,
    # and accepting it because it was "close enough" attaches a citation to the
    # wrong document.
    chosen = items[0]
    for item in items:
        identity = f"{item.get('jurisdiction', '')}{item.get('doc_number', '')}"
        if identity == publication.normalized:
            chosen = item
            break
    row = _measure_record(
        publication, chosen, elapsed, response.status_code, len(items)
    )
    return replace(
        row,
        attempts=attempts,
        rate_limit_remaining_minute=minute,
        rate_limit_remaining_month=month,
    )


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


@dataclass
class ProbeReport:
    sample_source: str
    raw_count: int
    distinct_count: int
    collapsed: int
    rows: list[ProbeRow] = field(default_factory=list)
    calls_made: int = 0
    measured_on: date = field(default_factory=date.today)

    @property
    def answered(self) -> int:
        return sum(1 for row in self.rows if row.answered)

    @property
    def with_text(self) -> int:
        return sum(1 for row in self.rows if row.outcome == Outcome.TEXT)

    @property
    def coverage_percent(self) -> Optional[float]:
        """Coverage over *answered* publications, or None when none were.

        None rather than 0.0, for the reason `evidence/coverage.py` gives: "we
        could not measure" and "Lens has nothing" are different findings, and a
        0% that actually means an outage would send a product decision the wrong
        way.
        """
        if not self.answered:
            return None
        return round(100.0 * self.with_text / self.answered, 1)

    @property
    def latencies(self) -> list[float]:
        return sorted(row.elapsed_seconds for row in self.rows if row.elapsed_seconds)

    def percentile(self, fraction: float) -> Optional[float]:
        values = self.latencies
        if not values:
            return None
        index = min(len(values) - 1, int(round(fraction * (len(values) - 1))))
        return values[index]

    def to_markdown(self) -> str:
        counts = Counter(row.outcome for row in self.rows)
        failures = Counter(
            row.failure_category
            for row in self.rows
            if row.failure_category and row.outcome == Outcome.FAILED
        )
        coverage = (
            f"{self.coverage_percent}%"
            if self.coverage_percent is not None
            else "not measurable (no publication was answered for)"
        )
        lines = [
            f"# Lens.org full-text probe - {self.measured_on.isoformat()}",
            "",
            "Measured by `python -m evidence.lens_coverage`. **Phase 3A measurement",
            "only** - production provider routing is unchanged: discovery is Lens.org",
            "and evidence is EPO OPS. No SerpAPI, embedding or LLM quota was spent.",
            "",
            "## Sample",
            "",
            f"- Source: **{self.sample_source}**",
            f"- Raw entries: {self.raw_count}",
            f"- Distinct publications: {self.distinct_count} "
            f"({self.collapsed} duplicate(s) collapsed)",
            f"- Live Lens requests made: **{self.calls_made}**",
            "",
            "## Outcome categories",
            "",
            "| Category | Count |",
            "|---|---:|",
            f"| `TEXT` | {counts.get(Outcome.TEXT, 0)} |",
            f"| `NO_TEXT` | {counts.get(Outcome.NO_TEXT, 0)} |",
            f"| `NOT_MATCHED` | {counts.get(Outcome.NOT_MATCHED, 0)} |",
            f"| `FAILED` | {counts.get(Outcome.FAILED, 0)} |",
            "",
            f"**Coverage: {coverage}** of the {self.answered} publication(s) Lens",
            "answered for. `FAILED` is excluded from the denominator, never counted",
            "as absent text.",
            "",
        ]
        if failures:
            lines += (
                ["### Failure categories", "", "| Category | Count |", "|---|---:|"]
                + [
                    f"| `{name}` | {count} |"
                    for name, count in sorted(failures.items())
                ]
                + [""]
            )

        lines += [
            "## Coverage by jurisdiction",
            "",
            "| Office | `TEXT` | `NO_TEXT` | `NOT_MATCHED` | `FAILED` | Coverage |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for office in sorted({row.country for row in self.rows}):
            here = [r for r in self.rows if r.country == office]
            text = sum(1 for r in here if r.outcome == Outcome.TEXT)
            no_text = sum(1 for r in here if r.outcome == Outcome.NO_TEXT)
            unmatched = sum(1 for r in here if r.outcome == Outcome.NOT_MATCHED)
            failed = sum(1 for r in here if r.outcome == Outcome.FAILED)
            answered = text + no_text + unmatched
            rate = f"{round(100.0 * text / answered)}%" if answered else "n/a"
            lines.append(
                f"| {office} | {text} | {no_text} | {unmatched} | {failed} | {rate} |"
            )

        worst = self.latencies[-1] if self.latencies else None
        lines += [
            "",
            "## Latency (sequential, concurrency 1)",
            "",
            f"- p50: {self.percentile(0.5)}s",
            f"- p95: {self.percentile(0.95)}s",
            f"- max: {worst}s",
            "",
            "## Evidence-contract findings",
            "",
            "| Publication | Outcome | Desc chars | Claims | Numbering | Lang | "
            "Text version | SOURCE_RANGE | Reproducible | Served identity |",
            "|---|---|---:|---:|---|---|---|---:|---|---|",
        ]
        for row in self.rows:
            served = row.served_publication or "-"
            if row.served_matches_request is False:
                served += " **MISMATCH**"
            repro = (
                "n/a"
                if row.source_range_reproducible is None
                else ("yes" if row.source_range_reproducible else "**no**")
            )
            lines.append(
                f"| `{row.normalized}` | {row.outcome} | {row.description_chars} | "
                f"{row.claims_count} | {row.claim_numbering} | "
                f"{row.language_description or '-'} | "
                f"{row.text_version_normalized or '-'} | "
                f"{row.source_range_locators} | {repro} | {served} |"
            )

        lines += [
            "",
            "## Provider outcomes",
            "",
            "| Publication | HTTP | Failure category | Elapsed (s) | "
            "Detail (redacted) |",
            "|---|---:|---|---:|---|",
        ]
        for row in self.rows:
            status = row.http_status if row.http_status is not None else "-"
            lines.append(
                f"| `{row.normalized}` | {status} | "
                f"{row.failure_category or '-'} | {row.elapsed_seconds} | "
                f"{row.detail or '-'} |"
            )

        missing: Counter = Counter()
        for row in self.rows:
            for name in row.contract_fields_missing:
                missing[name] += 1
        if missing:
            lines += [
                "",
                "## `EnrichmentResult` / passage fields not producible",
                "",
                "| Field | Publications affected |",
                "|---|---:|",
            ] + [f"| `{name}` | {count} |" for name, count in sorted(missing.items())]

        lines += [
            "",
            f"`segmenter_version` for every `SOURCE_RANGE` locator above: "
            f"`{SEGMENTER_VERSION}`.",
            "",
            "## What this is not",
            "",
            "A dated measurement, not a cache. Nothing in the pipeline reads this file",
            "and it never short-circuits a provider call. No credential and no provider",
            "response body appears anywhere in it.",
        ]
        return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Sample
# --------------------------------------------------------------------------


def sample_from_file(path: Path) -> tuple[list[str], str]:
    if not path.exists():
        raise NoSampleAvailable(f"the supplied sample file {path} does not exist")
    entries = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not entries:
        raise NoSampleAvailable(f"the supplied sample file {path} contains no entries")
    return entries, f"pinned sample file `{path.name}`"


def sample_from_stored_patents(limit: int) -> tuple[list[str], str]:
    """`patents.id` rows already stored from past runs. Already paid for."""
    import db

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id FROM patents ORDER BY fetched_at DESC LIMIT %s", (limit,)
        ).fetchall()
    entries = [str(row[0]) for row in rows]
    if not entries:
        raise NoSampleAvailable(
            "no publication numbers are stored in `patents`, and this tool will not "
            "run discovery to build a sample. Supply one with --sample-file. "
            "Building a sample 'just this once' is the spend this rule prevents."
        )
    return entries, f"stored `patents` rows (most recent {len(entries)})"


def load_measured(path: Path) -> dict:
    """Rows from a previous run's JSON sidecar, keyed by normalized publication.

    **Only rows the provider actually answered are carried forward.** A `FAILED`
    row is not a measurement -- it is the absence of one -- so a resumed run
    re-requests those and only those. That is what makes resuming compatible
    with "never repeat a successful request unnecessarily": a success is reused,
    a throttle is retried.
    """
    if not path.exists():
        raise NoSampleAvailable(f"the merge file {path} does not exist")
    payload = json.loads(path.read_text(encoding="utf-8"))
    carried = {}
    for entry in payload.get("rows", []):
        row = ProbeRow(**{k: _retuple(k, v) for k, v in entry.items()})
        if row.answered:
            carried[row.normalized] = row
    return carried


def _retuple(key: str, value: object) -> object:
    """JSON has no tuples; `ProbeRow` declares three. Restored on the way back
    in so a merged row is byte-identical to a freshly measured one."""
    if key in ("contract_fields_present", "contract_fields_missing") and isinstance(
        value, list
    ):
        return tuple(value)
    return value


def run(
    publications: list[PublicationNumber],
    *,
    api_key: str,
    max_calls: int,
    delay_seconds: float = DEFAULT_DELAY_SECONDS,
    already_measured: Optional[dict] = None,
    prober: Optional[Callable[..., ProbeRow]] = None,
    session: Optional[requests.Session] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[list[ProbeRow], int]:
    """Probe each publication in turn, paced under the per-minute ceiling.

    **Concurrency is 1 by construction** -- `search/lens.py`'s module-level lock
    exists because the trial tier cut a connection mid-response under a fan-out
    of five, and there is deliberately no flag here that could raise it.

    **Pacing is not politeness, it is correctness.** The unpaced first run
    turned the per-minute bucket into 23 publications with no data, and a
    coverage figure computed over the eleven that got through would have been a
    measurement of the throttle rather than of the index.

    `already_measured` carries forward rows the provider answered on an earlier
    run; those publications are **not re-requested**, and the calls they would
    have cost are not spent.
    """
    call = prober or probe_publication
    carried = already_measured or {}
    owned = session is None
    session = session or requests.Session()
    rows: list[ProbeRow] = []
    made = 0
    reused = 0
    try:
        for publication in publications:
            existing = carried.get(publication.normalized)
            if existing is not None:
                rows.append(existing)
                reused += 1
                continue
            if made >= max_calls:
                logger.warning(
                    "--max-calls ceiling of %d reached; the remaining "
                    "publication(s) were not probed and are ABSENT from the "
                    "report rather than recorded as an outcome",
                    max_calls,
                )
                break
            if made:
                sleep(delay_seconds)
            rows.append(call(publication, session=session, api_key=api_key))
            made += 1
    finally:
        if owned:
            session.close()
    if reused:
        logger.info(
            "reused %d already-answered publication(s) from a previous run; "
            "no request was spent on them",
            reused,
        )
    return rows, made


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        prog="python -m evidence.lens_coverage",
        description=(
            "Phase 3A: measure what Lens.org would supply as an evidence source, "
            "over a sample that already exists. Never runs discovery. Changes "
            "no runtime behaviour."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="perform ZERO HTTP calls and print the publications that would be "
        "queried, in order, with the exact query string. Run this first.",
    )
    parser.add_argument("--sample-file", type=Path, default=DEFAULT_SAMPLE)
    parser.add_argument(
        "--from-database",
        action="store_true",
        help="take the sample from stored `patents` rows instead of the file",
    )
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument(
        "--max-calls",
        type=int,
        default=DEFAULT_MAX_CALLS,
        help=f"hard ceiling on live Lens requests (default {DEFAULT_MAX_CALLS})",
    )
    parser.add_argument(
        "--delay-seconds",
        type=float,
        default=DEFAULT_DELAY_SECONDS,
        help=f"pause between requests (default {DEFAULT_DELAY_SECONDS}s, derived "
        f"from the measured {REQUESTS_PER_MINUTE}/minute trial ceiling)",
    )
    parser.add_argument(
        "--merge-json",
        type=Path,
        default=None,
        help="a previous run's JSON sidecar. Publications it ANSWERED for are "
        "carried forward and not re-requested; ones it FAILED on are retried, "
        "because a throttle is not a measurement.",
    )
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    try:
        if args.from_database:
            entries, source = sample_from_stored_patents(args.limit)
        else:
            entries, source = sample_from_file(args.sample_file)
    except NoSampleAvailable as error:
        logger.error("%s", error)
        return 2

    distinct, collapsed = deduplicate(entries)
    print(f"sample source     : {source}")
    print(f"raw entries       : {len(entries)}")
    print(f"distinct          : {len(distinct)} ({collapsed} duplicate(s) collapsed)")
    print(f"max calls         : {args.max_calls}")
    print(f"credential present: {bool(settings.lens_api_key)}")

    if args.dry_run:
        print("\n--dry-run: no HTTP call will be made. Would query, in order:")
        for publication in distinct:
            print(f"  {publication.normalized:<16} {build_query(publication)}")
        return 0

    if not settings.lens_api_key:
        logger.error(
            "LENS_API_KEY is not configured. The probe is NOT RUN and NO artifact "
            "is written. An absent credential is not a measurement of Lens: a 0% "
            "coverage file written from it would be indistinguishable from a "
            "measured absence of text."
        )
        return 3

    carried = {}
    if args.merge_json:
        carried = load_measured(args.merge_json)
        print(f"carried forward   : {len(carried)} already-answered publication(s)")

    rows, made = run(
        distinct,
        api_key=settings.lens_api_key,
        max_calls=args.max_calls,
        delay_seconds=args.delay_seconds,
        already_measured=carried,
    )
    report = ProbeReport(
        sample_source=source,
        raw_count=len(entries),
        distinct_count=len(distinct),
        collapsed=collapsed,
        rows=rows,
        calls_made=made,
    )
    output = args.output or (
        ARTIFACT_DIRECTORY / f"lens-fulltext-probe-{report.measured_on.isoformat()}.md"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report.to_markdown(), encoding="utf-8")

    # A machine-readable sidecar beside the markdown, so a run interrupted by
    # a rate limit can be resumed without re-spending the calls that
    # succeeded. It carries the same redacted fields the markdown does and no
    # others -- there is no place in a ProbeRow for a credential or a body.
    sidecar = output.with_suffix(".json")
    sidecar.write_text(
        json.dumps(
            {
                "measured_on": report.measured_on.isoformat(),
                "sample_source": report.sample_source,
                "calls_made": report.calls_made,
                "rows": [asdict(row) for row in report.rows],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\ncalls made        : {made}")
    print(
        f"coverage          : {report.coverage_percent}% of {report.answered} answered"
    )
    print(f"artifact          : {output}")
    print(f"sidecar           : {sidecar}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
