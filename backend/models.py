"""Typed models passed between the novelty-search pipeline stages."""

from dataclasses import dataclass, field
from enum import StrEnum


@dataclass(slots=True)
class PatentClaim:
    """One claim, kept separate from its neighbours so it can be cited.

    ``number`` is the claim's own number, which is what an evidence locator
    has to say ("claim 4") - a flattened claims blob cannot produce one. It is
    read from the claim text's own leading number where the provider includes
    it, and falls back to 1-based position otherwise; see
    ``search/lens.py:_parse_claims``.
    """

    number: int
    text: str


@dataclass(slots=True)
class PatentRecord:
    """A patent normalized by any search provider."""

    id: str
    title: str
    abstract: str = ""
    inventors: list[str] = field(default_factory=list)
    assignee: str = ""
    publication_date: str = ""
    source: str = ""
    url: str = ""
    serpapi_id: str = ""
    """Google Patents Details identifier (e.g. 'patent/US1234567B2/en'), used to fetch full text."""
    full_text: str = ""
    """Full description + claims text fetched from Google Patents Details, when available."""
    description: str = ""
    """Description text alone, without the claims ``full_text`` also carries.

    Populated only by providers that return the two separately (Lens.org does;
    Google's Details endpoint does not). Empty is not "no description" - check
    ``full_text`` for that."""
    claims: list[PatentClaim] = field(default_factory=list)
    """Individually numbered claims, when the provider returns them structured.

    Empty for providers that only ever return one merged text blob, so a reader
    must not treat an empty list as "this patent has no claims"."""
    family_id: str = ""
    """Provider-backed simple-family key. Empty means the provider supplied none."""
    abstract_language: str = ""
    classifications: list[str] = field(default_factory=list)
    family_size: int = 0
    """Total simple-family publications reported by the provider, when known."""


@dataclass(slots=True)
class InventionAnalysis:
    """Structured output of the LLM invention-understanding step."""

    queries: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    concepts: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ComparisonResult:
    """A patent and its transient embedding evidence for an invention."""

    patent: PatentRecord
    similarity_score: float
    unit_similarity_scores: list[float] = field(default_factory=list)
    """Best patent-chunk match for each independently approved invention unit.

    This stays inside execution just like ``similarity_score``.  It exists so a
    single excellent subsystem match cannot receive a High risk label while
    most of the invention has no support in that patent.
    """

@dataclass(slots=True)
class MatchAnalysis:
    """Per-patent rationales and key-overlap tags (both aligned 1:1 with the
    input patents) plus the shared uniqueness recommendations, from a single
    batched LLM call."""

    rationales: list[str] = field(default_factory=list)
    key_overlaps: list[list[str]] = field(default_factory=list)
    covered_limitation_ids: list[list[str]] = field(default_factory=list)
    missing_limitation_ids: list[list[str]] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# The evidence layer (Implementation Gate B)
# ---------------------------------------------------------------------------
#
# These describe ENRICHMENT -- turning a discovered candidate into trustworthy,
# section-aware, citable text, or into an honest exclusion. They are deliberately
# separate from `PatentRecord`, which is what a *discovery* provider returns: a
# discovery source and an enrichment source are different roles, and collapsing
# them is the defect that made EPO enrichment a silent no-op (spec section 17.2).


class EnrichmentStatus(StrEnum):
    """Three states, because two of them used to be one value.

    `get_patent_full_text` caught `Exception`, logged, and returned `""`, so a
    timeout and a genuine empty response were indistinguishable downstream. That
    is not a cosmetic problem: one is a publication with no text (an exclusion,
    a real answer) and the other is a provider outage (a failure). Recording an
    outage as an exclusion understates coverage using the provider's own downtime
    and quietly drops a candidate nobody decided to drop.
    """

    TEXT = "TEXT"
    """The provider answered and returned eligible sections."""

    NO_TEXT = "NO_TEXT"
    """The provider answered, and had none. -> EXCLUDED_NO_FULL_TEXT."""

    FAILED = "FAILED"
    """Timeout, 5xx, transport, or auth. -> the run FAILS.

    **Never** recorded as an exclusion. Bounded retry is permitted for transient
    errors; converting exhaustion into an exclusion is not.
    """


class LocatorType(StrEnum):
    """How a passage says where it came from.

    A locator is a citation. It has to be checkable by a human holding the
    document, which is why there is no "approximate" member here.
    """

    CLAIM_NUMBER = "CLAIM_NUMBER"
    """From `PatentClaim.number` -- the claim's own number. -> "claim 4"."""

    EPO_PARAGRAPH = "EPO_PARAGRAPH"
    """From a provider-supplied `<p>` id or num attribute, which is the
    document's own numbering. -> "paragraph [0031]"."""

    SOURCE_RANGE = "SOURCE_RANGE"
    """Derived character offsets into the exact retrieved section text.
    -> "description characters 4120-5233".

    Used only when the provider supplied no paragraph identifiers. A range that
    says what it is beats a paragraph number that lies: emitting
    "paragraph [0031]" for text the provider never numbered would be fabricating
    a citation into a legal document.
    """


class TextVersionSource(StrEnum):
    """Where a passage's `text_version_date` came from.

    Recorded rather than assumed, because the three are not equally trustworthy
    and a stored verdict has to be re-auditable against the exact text it was
    made from. **`retrieved_at` is never one of these** -- retrieval time says
    when we fetched, not which version we fetched.
    """

    PROVIDER_PUBLICATION_DATE = "PROVIDER_PUBLICATION_DATE"
    """The served document's publication date, from provider bibliographic data.
    Provider metadata, and honest that it identifies the version only as coarsely
    as the publication does. This is the approved policy (section 3.6 decision 4,
    option a)."""

    PROVIDER_REVISION_ID = "PROVIDER_REVISION_ID"
    """A provider-supplied document revision. EPO does not currently offer one;
    the member exists so a provider that does can be recorded honestly."""

    NONE = "NONE"
    """No trustworthy version. A passage can never carry this -- `Passage`
    requires a non-empty `text_version_date` -- so a publication that reaches
    this is excluded rather than cited."""


@dataclass(frozen=True, slots=True)
class ProviderClaim:
    """One claim **as the enrichment provider served it**, with its own number.

    Distinct from `PatentClaim`, which is what a *discovery* provider returns,
    and the distinction is load-bearing rather than tidy. Discovery claim text is
    metadata about a search result; it is not evidence, it was not served by the
    document's authoritative full-text source, and building an EPO-attributed
    passage out of it would put a citation to an EPO document on text EPO never
    supplied.

    `char_start` / `char_end` index the **exact retrieved claims section text**,
    so every claim passage is traceable back into the string whose SHA-256 the
    section carries.
    """

    number: int
    """The document's own claim number, from the provider's `num` attribute or
    from the claim text's own leading digits. **Never** positional: renumbering
    by position would make a locator say "claim 1" for text that is claim 7."""

    text: str
    char_start: int
    char_end: int


@dataclass(frozen=True, slots=True)
class SectionText:
    """One retrieved section, exactly as the provider returned it.

    `text` is **pre-canonicalization** and `text_sha256` is over that exact
    string, because `SOURCE_RANGE` offsets index into it. Canonicalizing first
    and hashing after would make every stored offset unreproducible.
    """

    section: str
    """`CLAIMS` | `DESCRIPTION` | `ABSTRACT` | `OTHER`. Only the first two are
    ever evidence; the others are recorded so an exclusion can say what *was*
    available."""

    text: str
    text_sha256: str
    language: str
    paragraph_ids: tuple[str, ...] = ()
    """Provider-supplied paragraph identifiers, in document order. Empty when the
    provider numbered nothing -- which is what forces `SOURCE_RANGE` locators."""

    claims: tuple[ProviderClaim, ...] = ()
    """The claims this section contains, when `section` is `CLAIMS`.

    **This is the only permitted source of claim evidence.** A claim passage is
    built from these and never from `PatentRecord.claims`, which belongs to
    discovery. Empty for any other section.
    """


@dataclass(frozen=True, slots=True)
class EnrichmentResult:
    """The whole outcome of enriching one publication.

    Carries all three publication identities on purpose. `requested` is what
    discovery gave us, `normalized` is what we asked the provider for, and
    `served` is what the provider says it answered with -- and comparing the last
    two is the only way to catch borrowed family text. A family member is a
    different document with different content and a different date; accepting its
    text because it was "close enough" would attach a citation to the wrong
    document.
    """

    requested_publication: str
    normalized_publication: str
    served_publication: str
    source_document_id: str
    status: EnrichmentStatus
    sections: tuple[SectionText, ...] = ()
    sections_available: tuple[str, ...] = ()
    sections_retrieved: tuple[str, ...] = ()
    language_attempted: str = ""
    text_version_original: str = ""
    text_version_normalized: str = ""
    text_version_source: TextVersionSource = TextVersionSource.NONE
    retrieved_at: str = ""
    """When the fetch happened. **NEVER** written into a `text_version_*` field."""
    provider_detail: str = ""
    """REDACTED before it is set. No URL carrying a key, no response body, no
    stack trace -- see `workflow/redaction.py`."""
    attempts: int = 0
    """Physical provider calls made, including retried ones. Recorded because
    external calls are at-least-once and the cost should be visible."""

    def section(self, name: str) -> "SectionText | None":
        """The named section, or None when it was not retrieved."""
        for entry in self.sections:
            if entry.section == name:
                return entry
        return None
