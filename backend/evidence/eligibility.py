"""Eligibility is **derived** from passages, never asserted.

**The defect this exists to prevent.** Spec §1.2.3 calls a publication eligible
when it has claims or a substantive description. Read as a *classifier*, that
lets a publication be declared eligible, enter the matrix, and then produce cells
that all come back `NOT_EVALUATED` because no passage was ever built for it --
an eligibility claim the evidence layer cannot honour, and exactly the kind of
unverifiable assertion the specification exists to prevent.

So the rule is inverted: **the classifier proposes, passage construction
disposes.** A publication is eligible exactly when at least one valid passage was
actually built for it. The heuristic below can therefore only ever *reject*; it
can never assert eligibility on its own.

**Three exclusion states, and they are not interchangeable:**

``EXCLUDED_NO_FULL_TEXT``
    The provider answered and had no eligible text -- an abstract only, a title
    only, or nothing. A real answer about the document.

``EXCLUDED_INELIGIBLE``
    Text exists but nothing citable could be built from it: borrowed family
    text, no trustworthy text version, or no producible locator.

``EXCLUDED_DATE``
    Decided earlier, by `evidence/dates.py`.

A **failed** provider call is none of these. It is a failure, it fails the run,
and recording it as an exclusion would understate coverage using the provider's
own outage while quietly dropping a candidate nobody decided to drop.

**None of the three is a verdict**, and an excluded publication creates **no
verification cells at all** -- not empty, not greyed. It appears only on
exclusion surfaces.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Optional

from evidence.normalize import same_publication
from evidence.passages import BuiltPassage, CLAIMS, DESCRIPTION
from models import EnrichmentResult, EnrichmentStatus, TextVersionSource

MIN_SUBSTANTIVE_DESCRIPTION_CHARS = 400
"""Below this a description is treated as non-substantive.

**Provisional, and it can only reject.** Spec §16 decision 13 leaves
"substantive" an open judgement call, and it is not a machine-checkable property:
this threshold distinguishes a cross-reference line and a list of figures from
real embodiment detail, and it will occasionally be wrong. It is safe *because*
it cannot assert eligibility -- a description that clears it still has to produce
a passage before the publication is eligible. Surfaced at Workflow Gate B so a
human can challenge it.
"""


class ExclusionState(StrEnum):
    """Mirrors the `exclusion_state` enum in migration 0006."""

    EXCLUDED_DATE = "EXCLUDED_DATE"
    EXCLUDED_NO_FULL_TEXT = "EXCLUDED_NO_FULL_TEXT"
    EXCLUDED_INELIGIBLE = "EXCLUDED_INELIGIBLE"


@dataclass(frozen=True, slots=True)
class PublicationExclusion:
    """One publication-scoped exclusion, shaped for `publication_exclusions`.

    `passages_built` is what makes derived eligibility auditable: a row with
    ``sections_retrieved={DESCRIPTION}`` and ``passages_built=0`` says exactly
    why -- text was there, a citable locator was not.
    """

    requested_number: str
    normalized_number: str
    state: ExclusionState
    reason: str
    served_number: Optional[str] = None
    failing_check: Optional[str] = None
    language_attempted: str = ""
    sections_available: tuple[str, ...] = ()
    sections_retrieved: tuple[str, ...] = ()
    passages_built: int = 0
    provider_detail: str = ""


@dataclass(frozen=True, slots=True)
class EligibilityResult:
    """Eligible with passages, or excluded with a reason. Never both, never
    neither."""

    eligible: bool
    passages: tuple[BuiltPassage, ...] = ()
    exclusion: Optional[PublicationExclusion] = None
    sections_with_passages: tuple[str, ...] = field(default=())

    def __post_init__(self) -> None:
        if self.eligible and not self.passages:
            raise ValueError(
                "a publication cannot be eligible with no passages -- that is the "
                "exact assertion this module exists to make impossible"
            )
        if not self.eligible and self.exclusion is None:
            raise ValueError("an excluded publication must carry its exclusion record")


def is_substantive_description(text: str) -> bool:
    """The provisional heuristic. **Rejects only.**

    A description that clears this is not thereby eligible -- it still has to
    yield a passage. A description that fails it is excluded, which is the one
    direction a length threshold can be trusted in: a 60-character description is
    a cross-reference, whatever else it might be.
    """
    return len(text.strip()) >= MIN_SUBSTANTIVE_DESCRIPTION_CHARS


def confirm_eligibility(
    enrichment: EnrichmentResult,
    passages: list[BuiltPassage],
) -> EligibilityResult:
    """Decide one publication's fate from what was actually built for it.

    Checked in order, because the earlier reasons are more specific and a
    reviewer deserves the most specific true reason:

    1. **`FAILED`** -- raises. Not an exclusion; the run fails.
    2. **Borrowed family text** -- the provider answered for a different
       document. `EXCLUDED_INELIGIBLE`, and the text is never used.
    3. **`NO_TEXT`, or no eligible section retrieved** --
       `EXCLUDED_NO_FULL_TEXT`. An abstract-only response lands here.
    4. **No trustworthy text version** -- `EXCLUDED_INELIGIBLE`. A passage
       requires a non-empty `text_version_date`, and retrieval time is never
       substituted for it.
    5. **No passage built** -- `EXCLUDED_INELIGIBLE`, naming what was retrieved
       so the row shows text was present and a locator was not.
    6. Otherwise **eligible**, with the passages that made it so.

    Raises:
        ValueError: when `enrichment.status` is `FAILED`. A failed call has no
            eligibility answer, and inventing one is how an outage becomes a
            coverage statistic.
    """
    if enrichment.status is EnrichmentStatus.FAILED:
        raise ValueError(
            f"{enrichment.requested_publication}: enrichment FAILED, which has no "
            "eligibility answer. A provider outage is a run failure, never an "
            "exclusion."
        )

    def excluded(state: ExclusionState, reason: str, *, check: Optional[str] = None):
        return EligibilityResult(
            eligible=False,
            exclusion=PublicationExclusion(
                requested_number=enrichment.requested_publication,
                normalized_number=enrichment.normalized_publication,
                served_number=enrichment.served_publication or None,
                state=state,
                reason=reason,
                failing_check=check,
                language_attempted=enrichment.language_attempted,
                sections_available=enrichment.sections_available,
                sections_retrieved=enrichment.sections_retrieved,
                passages_built=len(passages),
                provider_detail=enrichment.provider_detail,
            ),
        )

    # 2. Borrowed family text.
    if enrichment.served_publication and not same_publication(
        enrichment.served_publication, enrichment.normalized_publication
    ):
        return excluded(
            ExclusionState.EXCLUDED_INELIGIBLE,
            (
                f"the provider served {enrichment.served_publication} for a request "
                f"for {enrichment.normalized_publication}. A family member is a "
                "different document with different content and a different date, so "
                "its text is not borrowed."
            ),
            check="spec 7.3 check 3 (source_document_id resolves to the evaluated publication)",
        )

    # 3. No eligible full text at all.
    eligible_sections = [
        name for name in enrichment.sections_retrieved if name in (CLAIMS, DESCRIPTION)
    ]
    if enrichment.status is EnrichmentStatus.NO_TEXT or not eligible_sections:
        available = ", ".join(enrichment.sections_available) or "nothing"
        return excluded(
            ExclusionState.EXCLUDED_NO_FULL_TEXT,
            (
                "the provider answered and returned no eligible full text for this "
                f"exact publication (available: {available}). An abstract, a title, "
                "bibliographic metadata and a search snippet are never disclosure "
                "evidence."
            ),
        )

    # 4. No trustworthy text version.
    if (
        enrichment.text_version_source is TextVersionSource.NONE
        or not enrichment.text_version_normalized
    ):
        return excluded(
            ExclusionState.EXCLUDED_INELIGIBLE,
            (
                "no trustworthy text version could be established for this "
                "publication. Retrieval time is never substituted for a document's "
                "version, so no passage can be built."
            ),
            check="spec 7.3 check 5 (text_version_date must be present and non-empty)",
        )

    # 5. Text, but nothing citable.
    if not passages:
        retrieved = ", ".join(eligible_sections)
        return excluded(
            ExclusionState.EXCLUDED_INELIGIBLE,
            (
                f"{retrieved} text was retrieved but no passage with a trustworthy "
                "locator could be produced from it. A publication is eligible only "
                "when at least one citable passage exists -- never 'eligible' with "
                "an empty passage set."
            ),
            check="derived eligibility (section 3.7)",
        )

    sections = tuple(
        dict.fromkeys(built.passage.section for built in passages)
    )
    return EligibilityResult(
        eligible=True, passages=tuple(passages), sections_with_passages=sections
    )
