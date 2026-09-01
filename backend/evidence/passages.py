"""`EnrichmentResult` → `verification.Passage`, with **real** locators.

**Evidence comes from the enrichment provider, never from discovery.** These
functions take an `EnrichmentResult` and nothing else -- no `PatentRecord` -- and
that signature is the rule rather than a description of it. An earlier version
took the record and built claim passages from `PatentRecord.claims`, which is
*Lens* discovery metadata, then stamped them `source="EPO OPS"` with an
`epo:` document id: a citation to an EPO document attached to text EPO never
served.

**A locator is a citation into a legal document.** It has to be checkable by a
human holding that document, which is why there are exactly three kinds and no
"approximate" one:

===================  ===========================  ==========================
Kind                 Source                       Rendered
===================  ===========================  ==========================
`CLAIM_NUMBER`       the claim's own number       ``claim 4``
`EPO_PARAGRAPH`      a provider `<p>` id or num   ``paragraph [0031]``
`SOURCE_RANGE`       derived character offsets    ``description characters
                                                  4120-5233``
===================  ===========================  ==========================

**A `paragraph [NNNN]` the provider did not give us is never emitted.** If EPO
numbered nothing, the honest locator is a reproducible character range: a range
that says what it is beats a paragraph number that lies. Inventing one would put
a fabricated citation, with a plausible address, into a document a patent
attorney may rely on.

**Only `CLAIMS` and `DESCRIPTION` become passages.** An abstract, a title,
bibliographic metadata and a search-result snippet are refused on the *section*
-- not on a flag, because the `evidence_eligible` flag is supposed to come from a
classifier that does not exist, and a boolean nobody computes is not a safety
boundary. `verification/passages.py`'s `EVIDENCE_SECTIONS` enforces the same rule
structurally on the way into the verifier, and `evidence_passages`' CHECK
enforces it in the schema.

**Chunk IDs are pure functions of publication + section + locator**, so
re-running over the same text version yields byte-identical IDs. They are
deliberately provider-neutral (`PAT-`, not `EPO-`): a publication re-enriched
from a different provider keeps its IDs, and `source` records the provider
instead.

A `SOURCE_RANGE` id may legitimately change when the source text changes, and
that is correct rather than a stability failure: different text is a different
passage, `text_sha256` changes with it, and results citing the old one go stale.
"""

import hashlib
from dataclasses import dataclass
from typing import Optional

from models import (
    EnrichmentResult,
    EnrichmentStatus,
    LocatorType,
    TextVersionSource,
)
from evidence.segmenter import SEGMENTER_VERSION, segment_description
from verification.passages import Passage, canonicalize

CLAIMS = "CLAIMS"
DESCRIPTION = "DESCRIPTION"


class LocatorSource:
    """Where a locator's authority comes from, mirroring the CHECK on
    `evidence_passages.locator_source`."""

    PROVIDER_CLAIM_NUMBER = "PROVIDER_CLAIM_NUMBER"
    PROVIDER_ELEMENT_ID = "PROVIDER_ELEMENT_ID"
    PROVIDER_NUMBER_ATTR = "PROVIDER_NUMBER_ATTR"
    DERIVED_SOURCE_OFFSET = "DERIVED_SOURCE_OFFSET"


class PassageBuildError(RuntimeError):
    """A passage could not be built honestly.

    Raised only for a programming-level violation -- an empty required field, a
    section that is not evidence. A *publication* that simply has no producible
    passage is not an error: it is `EXCLUDED_INELIGIBLE`, decided by
    `evidence/eligibility.py` from an empty passage list.
    """


@dataclass(frozen=True, slots=True)
class BuiltPassage:
    """A `verification.Passage` plus the provenance the database stores.

    The verifier only ever sees `.passage`. Everything else exists so an
    exclusion, a staleness check, or a reviewer's "where did this come from?" can
    be answered from the row rather than reconstructed.
    """

    passage: Passage
    locator_kind: LocatorType
    locator_source: str
    section_text_sha256: str
    char_start: Optional[int] = None
    char_end: Optional[int] = None
    segmenter_version: Optional[str] = None
    text_sha256: str = ""
    """Over the **canonical** text -- the passage of record. A result whose
    `passage_text_hashes` no longer match is stale."""

    text_version_source: TextVersionSource = TextVersionSource.PROVIDER_PUBLICATION_DATE
    retrieved_at: str = ""
    text_version_original: str = ""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunk_id_for_claim(publication: str, number: int) -> str:
    """`PAT-{pub}-CLM-{number:03d}`. Stable while the claim number is stable."""
    return f"PAT-{publication}-CLM-{number:03d}"


def chunk_id_for_paragraph(publication: str, paragraph_id: str) -> str:
    """`PAT-{pub}-DESC-P{paragraph_id}`, using the provider's own identifier."""
    return f"PAT-{publication}-DESC-P{paragraph_id}"


def chunk_id_for_range(publication: str, start: int, end: int) -> str:
    """`PAT-{pub}-DESC-R{start:06d}-{end:06d}`.

    Changes when the source text changes, which is honest: different offsets
    address different text, and pretending otherwise would let a stored citation
    silently point somewhere new.
    """
    return f"PAT-{publication}-DESC-R{start:06d}-{end:06d}"


def _passage(
    *,
    chunk_id: str,
    publication: str,
    enrichment: EnrichmentResult,
    section: str,
    locator: str,
    language: str,
    text: str,
) -> Passage:
    """Build one validated passage, refusing to emit an incomplete one.

    Every field `verification/passages.py`'s check 5 requires must be non-empty
    *here*, not later: a passage assembled with a blank locator would be rejected
    at the verifier boundary, which is the right outcome reached far too late to
    say anything useful about why.
    """
    canonical = canonicalize(text)
    missing = [
        name
        for name, value in (
            ("chunk_id", chunk_id),
            ("publication_number", publication),
            ("source_document_id", enrichment.source_document_id),
            ("source", "EPO OPS"),
            ("section", section),
            ("locator", locator),
            ("language", language),
            ("text_version_date", enrichment.text_version_normalized),
            ("text", canonical),
        )
        if not value
    ]
    if missing:
        raise PassageBuildError(
            f"cannot build a passage for {publication}: {', '.join(missing)} "
            "would be empty, and a passage with a blank identity or locator is "
            "not a citation"
        )
    if section not in (CLAIMS, DESCRIPTION):
        raise PassageBuildError(
            f"{section} is not evidence: only CLAIMS and DESCRIPTION ever are"
        )
    return Passage(
        chunk_id=chunk_id,
        publication_number=publication,
        source_document_id=enrichment.source_document_id,
        source="EPO OPS",
        section=section,
        locator=locator,
        language=language,
        text_version_date=enrichment.text_version_normalized,
        evidence_eligible=True,
        text=canonical,
    )


def build_claim_passages(enrichment: EnrichmentResult) -> list[BuiltPassage]:
    """One passage per claim, **from the EPO claims section and nowhere else**.

    An earlier version iterated `PatentRecord.claims` -- the claims *Lens*
    returned during discovery -- and stamped the resulting passages with
    ``source="EPO OPS"`` and ``source_document_id="epo:..."``. That is fabricated
    provenance: a citation to an EPO document, attached to text EPO never served.
    It also fired when EPO returned **no claims section at all**, so a
    description-only EPO document silently grew claim passages out of discovery
    metadata.

    Discovery claim text is not evidence. It has not been served by the
    document's authoritative full-text source, it may differ from what that
    source holds, and no approved evidence source in this gate covers it. So this
    function takes no `PatentRecord` at all -- the parameter is gone, which is the
    strongest form the rule can take.

    Numbering comes from the provider's own document structure
    (`ProviderClaim.number`), never from position. `search/epo_fulltext.py`
    raises `ClaimNumberingError` rather than serving a claim it could not number
    or two claims sharing a number, so anything reaching here is already
    resolvable; the duplicate check below is the second line of that defence.

    Returns an empty list when EPO returned no claims section, or returned one
    with no resolvable per-claim structure. That is not an error here --
    `eligibility.py` turns "text retrieved, nothing citable" into
    `EXCLUDED_INELIGIBLE` with a reason.
    """
    section = enrichment.section(CLAIMS)
    if section is None or not section.claims:
        return []

    publication = enrichment.normalized_publication
    built: list[BuiltPassage] = []
    seen: set[str] = set()

    for claim in section.claims:
        if not claim.text or not claim.text.strip():
            continue
        chunk_id = chunk_id_for_claim(publication, claim.number)
        if chunk_id in seen:
            raise PassageBuildError(
                f"duplicate claim number {claim.number} for {publication}: two "
                "passages would share a chunk_id and one would be silently dropped"
            )
        seen.add(chunk_id)
        canonical = canonicalize(claim.text)
        built.append(
            BuiltPassage(
                passage=_passage(
                    chunk_id=chunk_id,
                    publication=publication,
                    enrichment=enrichment,
                    section=CLAIMS,
                    locator=f"claim {claim.number}",
                    language=section.language,
                    text=claim.text,
                ),
                locator_kind=LocatorType.CLAIM_NUMBER,
                locator_source=LocatorSource.PROVIDER_CLAIM_NUMBER,
                section_text_sha256=section.text_sha256,
                # Offsets into the exact retrieved claims section, so every claim
                # passage is traceable back into the string that hash covers.
                char_start=claim.char_start,
                char_end=claim.char_end,
                text_sha256=_sha256(canonical),
                text_version_source=enrichment.text_version_source,
                retrieved_at=enrichment.retrieved_at,
                text_version_original=enrichment.text_version_original,
            )
        )
    return built


def build_description_passages(
    enrichment: EnrichmentResult,
) -> list[BuiltPassage]:
    """Description passages, preferring the provider's own paragraph numbering.

    Two paths, and which one is taken is recorded rather than inferred:

    * EPO supplied `<p>` identifiers → `EPO_PARAGRAPH`, rendered as the
      document's own ``paragraph [0031]``.
    * EPO supplied none → `SOURCE_RANGE`, character offsets into the exact
      retrieved text, with `segmenter_version` so the split is reproducible.

    There is deliberately no third path that guesses paragraph numbers from
    position. A description's first segment is not necessarily paragraph 1 --
    the retrieved text may start mid-document -- and numbering it as though it
    were would fabricate a citation.
    """
    section = enrichment.section(DESCRIPTION)
    if section is None or not section.text.strip():
        return []

    publication = enrichment.normalized_publication
    segments = segment_description(section.text)
    if not segments:
        return []

    # The provider's numbering is only usable when it covers every segment. A
    # partial mapping would number some passages and range others, which reads as
    # if the unnumbered ones were somehow lesser rather than simply unnumbered.
    use_provider_ids = len(section.paragraph_ids) == len(segments)

    built: list[BuiltPassage] = []
    for index, segment in enumerate(segments):
        canonical = canonicalize(segment.text)
        if not canonical:
            continue
        if use_provider_ids:
            paragraph_id = section.paragraph_ids[index]
            chunk_id = chunk_id_for_paragraph(publication, paragraph_id)
            locator = f"paragraph [{paragraph_id}]"
            kind = LocatorType.EPO_PARAGRAPH
            source = (
                LocatorSource.PROVIDER_NUMBER_ATTR
                if paragraph_id.isdigit()
                else LocatorSource.PROVIDER_ELEMENT_ID
            )
            start = end = None
            version = None
        else:
            chunk_id = chunk_id_for_range(publication, segment.start, segment.end)
            locator = (
                f"description characters {segment.start}-{segment.end}"
            )
            kind = LocatorType.SOURCE_RANGE
            source = LocatorSource.DERIVED_SOURCE_OFFSET
            start, end = segment.start, segment.end
            version = SEGMENTER_VERSION

        built.append(
            BuiltPassage(
                passage=_passage(
                    chunk_id=chunk_id,
                    publication=publication,
                    enrichment=enrichment,
                    section=DESCRIPTION,
                    locator=locator,
                    language=section.language,
                    text=segment.text,
                ),
                locator_kind=kind,
                locator_source=source,
                section_text_sha256=section.text_sha256,
                char_start=start,
                char_end=end,
                segmenter_version=version,
                text_sha256=_sha256(canonical),
                text_version_source=enrichment.text_version_source,
                retrieved_at=enrichment.retrieved_at,
                text_version_original=enrichment.text_version_original,
            )
        )
    return built


def build_passages(enrichment: EnrichmentResult) -> list[BuiltPassage]:
    """Every citable passage for one publication, claims first.

    Returns an empty list when nothing citable could be built -- which is not an
    error here. `evidence/eligibility.py` turns it into `EXCLUDED_INELIGIBLE`
    with a reason, because "eligible with nothing to cite" is the one state this
    layer exists to make impossible.

    A `FAILED` enrichment never reaches this function; it fails the run. Building
    passages from a failed call would be manufacturing evidence out of an outage.
    """
    if enrichment.status is EnrichmentStatus.FAILED:
        raise PassageBuildError(
            f"{enrichment.requested_publication} enrichment FAILED; a failed "
            "provider call is not an absence of text and never produces passages"
        )
    if enrichment.status is EnrichmentStatus.NO_TEXT:
        return []
    return build_claim_passages(enrichment) + build_description_passages(enrichment)
