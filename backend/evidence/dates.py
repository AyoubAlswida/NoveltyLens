"""Date screening: which date decides eligibility, where the cutoff edge sits,
and what happens when a date cannot be trusted.

**These are legal decisions, not engineering ones**, and §3.6 of the
implementation prompt forbids choosing them silently. They were put to the user
and answered; the answers are recorded in `DATE_SCREENING_POLICY` and
`TEXT_VERSION_POLICY`, which have **no working default**. Admission refuses a run
whose policy is unset or unapproved, so a run can never be screened by a rule
nobody chose.

The approved policy:

===========================  ==================================================
Decision                     Answer
===========================  ==================================================
Which date screens           The evaluated publication's **own publication
                             date**. The passages come from that exact document,
                             so its own date is the one that can be defended
                             per-document.
Cutoff edge                  **Exclusive**: ``date < cutoff``. A publication
                             dated exactly on the cutoff is `EXCLUDED_DATE`.
Uncertain dates              **Never silently pass.** Missing, malformed or
                             self-contradicting dates are `EXCLUDED_DATE` with
                             ``date_certainty = UNCERTAIN`` and a reason naming
                             the conflict, surfaced at Workflow Gate B so a
                             human can challenge it. The run continues.
===========================  ==================================================

**Patent dates carry no timezone and none is applied.** They are date-only
values in the document, and shifting one by a timezone can move a publication
across the cutoff by a day -- changing a legal answer to satisfy a datatype.
`publication_dates.date_normalized` is a plain `DATE` for the same reason.

**The priority date is stored and never screens.** It is displayed beside the
publication date for the reviewer, because it is what a patent attorney will
want to see, but the approved policy does not let it decide anything. Storing it
without using it is deliberate: silently substituting one date type for another
is the failure this module is shaped to prevent.

**Nothing here invents a date.** A publication whose date cannot be read is
excluded, never defaulted to today, to the epoch, or to the run's cutoff.
"""

import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Optional

# --- the approved policy vocabulary ---------------------------------------

PUBLICATION_LT_CUTOFF = "publication_lt_cutoff"
"""The approved `DATE_SCREENING_POLICY`: the evaluated publication's own
publication date, strictly before the cutoff."""

PUBLICATION_LTE_CUTOFF = "publication_lte_cutoff"
"""The inclusive variant. Implemented and tested, **not** the approved policy --
it exists so changing the decision is a config change with a test already
covering it, rather than a code change under time pressure."""

SUPPORTED_DATE_POLICIES = (PUBLICATION_LT_CUTOFF, PUBLICATION_LTE_CUTOFF)


class DateCertainty(StrEnum):
    """Mirrors the `date_certainty` enum in migration 0011."""

    CERTAIN = "CERTAIN"
    UNCERTAIN = "UNCERTAIN"


class ScreeningStage(StrEnum):
    """Where the screen happened, recorded per publication.

    Screening runs at `DISCOVERY` whenever the discovery metadata already carries
    a trustworthy date -- Lens supplies `date_published` -- because that is
    *before* the expensive EPO enrichment call and excluding early saves it. When
    discovery has no date, screening waits for EPO biblio and is recorded as
    `ENRICHMENT`.
    """

    DISCOVERY = "DISCOVERY"
    ENRICHMENT = "ENRICHMENT"


class DateScreeningOutcome(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    EXCLUDED_DATE = "EXCLUDED_DATE"


class DatePolicyError(ValueError):
    """The configured date policy is unset, unknown, or unapproved.

    Raised at admission, before any run row exists. A run screened by a rule
    nobody chose is worse than a run that never started.
    """


# --- parsing ---------------------------------------------------------------

_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_COMPACT = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_YEAR_MONTH = re.compile(r"^(\d{4})-?(\d{2})$")
_YEAR_ONLY = re.compile(r"^(\d{4})$")


def parse_patent_date(raw: str) -> Optional[date]:
    """Parse the date formats patent providers actually emit, or return None.

    Accepts `YYYY-MM-DD` and EPO's compact `YYYYMMDD`. **Refuses** a
    year-or-month-only value: `2019` is not a date, and resolving it to
    2019-01-01 would place a publication on the eligible side of a 2019 cutoff on
    the strength of a digit nobody supplied. Refuses ambiguous regional forms
    (`03/04/2019`) for the same reason -- there is no way to know which field is
    the month, and guessing moves documents across cutoffs.

    Returns None rather than raising: the caller turns that into an `UNCERTAIN`
    exclusion carrying the original string, which is more useful than an
    exception that loses it.
    """
    if not raw:
        return None
    value = raw.strip()

    for pattern in (_ISO, _COMPACT):
        match = pattern.match(value)
        if match:
            try:
                return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            except ValueError:
                # A syntactically well-formed impossible date: 2019-02-30.
                return None

    if _YEAR_MONTH.match(value) or _YEAR_ONLY.match(value):
        # Deliberately unparsed. See the docstring.
        return None
    return None


# --- the screening record --------------------------------------------------


@dataclass(frozen=True, slots=True)
class PublicationDate:
    """One publication's date evidence, exactly as `publication_dates` stores it.

    Every field is either measured or explicitly absent. `date_original` is the
    provider's string **verbatim**, so a reviewer challenging an exclusion can
    see what the provider actually said rather than what we made of it.
    """

    publication_number: str
    date_original: str
    date_normalized: Optional[date]
    date_source: str
    """Which provider field it came from, e.g. `lens.date_published`. Named so a
    disagreement between two fields can be investigated rather than guessed at."""
    date_certainty: DateCertainty
    priority_date: Optional[date] = None
    """Displayed beside the publication date. **Never screens** under the
    approved policy."""
    screened_at_stage: ScreeningStage = ScreeningStage.DISCOVERY
    uncertainty_reason: str = ""
    """Why the date is UNCERTAIN, in words a reviewer can act on. Empty when
    CERTAIN."""


@dataclass(frozen=True, slots=True)
class DateScreeningResult:
    """The screen's verdict, and enough evidence to defend or challenge it."""

    outcome: DateScreeningOutcome
    publication_date: PublicationDate
    reason: str
    """Always populated, including on the eligible path -- an audit record that
    only explains its rejections is half an audit record."""
    policy: str
    cutoff: Optional[date]

    @property
    def excluded(self) -> bool:
        return self.outcome is DateScreeningOutcome.EXCLUDED_DATE


def validate_policy(policy: str) -> str:
    """Check a configured policy name, raising if it is unset or unknown.

    Called at admission. There is deliberately no default: a default policy is a
    legal decision made by whoever wrote the code, and §3.6 makes it the user's.
    """
    if not policy or not policy.strip():
        raise DatePolicyError(
            "DATE_SCREENING_POLICY is unset. It has no default: which date screens "
            "prior art, and whether the cutoff is inclusive, are legal decisions "
            f"that must be recorded. Supported: {', '.join(SUPPORTED_DATE_POLICIES)}"
        )
    if policy not in SUPPORTED_DATE_POLICIES:
        raise DatePolicyError(
            f"unknown DATE_SCREENING_POLICY {policy!r}. "
            f"Supported: {', '.join(SUPPORTED_DATE_POLICIES)}"
        )
    return policy


def build_publication_date(
    *,
    publication_number: str,
    date_original: str,
    date_source: str,
    stage: ScreeningStage,
    priority_date_original: str = "",
    corroborating_dates: Optional[dict[str, str]] = None,
) -> PublicationDate:
    """Turn a provider's date strings into a screening record.

    `corroborating_dates` is a second opinion: other provider fields that also
    claim to be this publication's date. **When two disagree the result is
    UNCERTAIN**, naming both. Picking the earlier or the "more likely" one would
    be resolving a factual conflict by preference, and the conflict is exactly
    what a reviewer needs to see.
    """
    parsed = parse_patent_date(date_original)
    priority = parse_patent_date(priority_date_original)

    if parsed is None:
        return PublicationDate(
            publication_number=publication_number,
            date_original=date_original,
            date_normalized=None,
            date_source=date_source,
            date_certainty=DateCertainty.UNCERTAIN,
            priority_date=priority,
            screened_at_stage=stage,
            uncertainty_reason=(
                f"no usable publication date: {date_source} gave {date_original!r}"
                if date_original
                else f"no publication date was supplied by {date_source}"
            ),
        )

    for other_source, other_raw in (corroborating_dates or {}).items():
        other = parse_patent_date(other_raw)
        if other is not None and other != parsed:
            return PublicationDate(
                publication_number=publication_number,
                date_original=date_original,
                date_normalized=None,
                date_source=date_source,
                date_certainty=DateCertainty.UNCERTAIN,
                priority_date=priority,
                screened_at_stage=stage,
                uncertainty_reason=(
                    f"provider fields disagree: {date_source} gave {parsed.isoformat()}, "
                    f"{other_source} gave {other.isoformat()}"
                ),
            )

    return PublicationDate(
        publication_number=publication_number,
        date_original=date_original,
        date_normalized=parsed,
        date_source=date_source,
        date_certainty=DateCertainty.CERTAIN,
        priority_date=priority,
        screened_at_stage=stage,
    )


def screen(
    publication_date: PublicationDate,
    *,
    cutoff: Optional[date],
    policy: str,
) -> DateScreeningResult:
    """Decide whether one publication is date-eligible.

    Three outcomes, in the order they are checked:

    1. **No cutoff** -- nothing to screen against, so nothing is excluded on
       date. The run simply is not date-limited.
    2. **UNCERTAIN** -- `EXCLUDED_DATE`, always, whatever the cutoff. An
       uncertain date never silently passes: letting one through would mean a
       publication entered the matrix without anyone establishing it was prior
       art, which is a legal claim made by an absent value.
    3. **CERTAIN** -- compared under the approved policy.

    Note the order of 1 and 2: with no cutoff there is no comparison to be
    uncertain *about*, so an unparseable date is not held against a publication
    in a run that was never date-limited.
    """
    validate_policy(policy)

    if cutoff is None:
        return DateScreeningResult(
            outcome=DateScreeningOutcome.ELIGIBLE,
            publication_date=publication_date,
            reason="the run has no cutoff date, so no publication is screened on date",
            policy=policy,
            cutoff=None,
        )

    if publication_date.date_certainty is DateCertainty.UNCERTAIN:
        return DateScreeningResult(
            outcome=DateScreeningOutcome.EXCLUDED_DATE,
            publication_date=publication_date,
            reason=(
                "the publication date could not be established, and an uncertain "
                f"date never passes screening: {publication_date.uncertainty_reason}"
            ),
            policy=policy,
            cutoff=cutoff,
        )

    published = publication_date.date_normalized
    assert published is not None  # guaranteed by DateCertainty.CERTAIN

    if policy == PUBLICATION_LT_CUTOFF:
        eligible = published < cutoff
        edge = "strictly before"
    else:
        eligible = published <= cutoff
        edge = "on or before"

    if eligible:
        return DateScreeningResult(
            outcome=DateScreeningOutcome.ELIGIBLE,
            publication_date=publication_date,
            reason=(
                f"published {published.isoformat()}, {edge} the cutoff "
                f"{cutoff.isoformat()}"
            ),
            policy=policy,
            cutoff=cutoff,
        )
    return DateScreeningResult(
        outcome=DateScreeningOutcome.EXCLUDED_DATE,
        publication_date=publication_date,
        reason=(
            f"published {published.isoformat()}, which is not {edge} the cutoff "
            f"{cutoff.isoformat()}"
        ),
        policy=policy,
        cutoff=cutoff,
    )
