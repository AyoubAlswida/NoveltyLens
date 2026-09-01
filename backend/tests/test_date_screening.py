"""Date screening under the **approved** policy, and the refusal to run without
one.

The four §3.6 decisions were put to the user and answered:

===========================  ==================================================
Decision                     Answer
===========================  ==================================================
Which date screens           the evaluated publication's own publication date
Cutoff edge                  **exclusive**: ``date < cutoff``
Uncertain dates              `EXCLUDED_DATE` + `UNCERTAIN`, surfaced at Gate B
Text version when absent     the served document's publication date
===========================  ==================================================

Both cutoff edges are implemented and tested even though only one is approved --
so changing the decision is a config change with a test already covering it,
rather than a code change made under time pressure.

**Nothing here invents a date.** Every path that cannot read one excludes the
publication and says why.
"""

from datetime import date

import pytest

from evidence.dates import (
    DateCertainty,
    DatePolicyError,
    DateScreeningOutcome,
    PUBLICATION_LT_CUTOFF,
    PUBLICATION_LTE_CUTOFF,
    ScreeningStage,
    build_publication_date,
    parse_patent_date,
    screen,
    validate_policy,
)

CUTOFF = date(2020, 1, 15)


def _screened(original, *, policy=PUBLICATION_LT_CUTOFF, cutoff=CUTOFF, corroborating=None,
              priority="", stage=ScreeningStage.DISCOVERY):
    publication_date = build_publication_date(
        publication_number="US1234567",
        date_original=original,
        date_source="lens.date_published",
        stage=stage,
        priority_date_original=priority,
        corroborating_dates=corroborating,
    )
    return screen(publication_date, cutoff=cutoff, policy=policy)


# --- parsing ---------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [("2019-06-01", date(2019, 6, 1)), ("20190601", date(2019, 6, 1))],
)
def test_the_formats_providers_actually_emit_are_parsed(raw, expected):
    """ISO from Lens, compact `YYYYMMDD` from EPO."""
    assert parse_patent_date(raw) == expected


@pytest.mark.parametrize(
    "raw,why",
    [
        ("2019", "a year is not a date; resolving it to 01-01 invents two fields"),
        ("2019-06", "a month is not a date, for the same reason"),
        ("03/04/2019", "ambiguous regional order -- guessing moves documents across cutoffs"),
        ("2019-02-30", "syntactically well-formed and impossible"),
        ("2019-13-01", "impossible month"),
        ("", "absent"),
        ("sometime in 2019", "prose"),
    ],
)
def test_anything_that_would_require_a_guess_is_refused(raw, why):
    assert parse_patent_date(raw) is None, why


# --- the approved policy ---------------------------------------------------


def test_a_publication_before_the_cutoff_is_eligible():
    result = _screened("2019-06-01")
    assert result.outcome is DateScreeningOutcome.ELIGIBLE
    assert result.publication_date.date_certainty is DateCertainty.CERTAIN
    assert "strictly before" in result.reason


def test_the_boundary_date_is_excluded_under_the_approved_exclusive_policy():
    """**The approved decision.** A publication dated exactly on the cutoff is
    `EXCLUDED_DATE`: the cutoff is treated as the filing instant, so same-day art
    is not counted."""
    result = _screened("2020-01-15")
    assert result.outcome is DateScreeningOutcome.EXCLUDED_DATE
    assert result.publication_date.date_certainty is DateCertainty.CERTAIN, (
        "the date is certain -- it is the comparison that excludes it, not doubt"
    )


def test_the_boundary_date_is_eligible_under_the_inclusive_policy():
    """The unapproved variant, tested so the decision can be changed by
    configuration rather than by code."""
    result = _screened("2020-01-15", policy=PUBLICATION_LTE_CUTOFF)
    assert result.outcome is DateScreeningOutcome.ELIGIBLE


def test_a_publication_after_the_cutoff_is_excluded():
    assert _screened("2021-01-01").outcome is DateScreeningOutcome.EXCLUDED_DATE


def test_an_epo_compact_date_screens_identically():
    assert _screened("20190601").outcome is DateScreeningOutcome.ELIGIBLE


# --- uncertainty never silently passes -------------------------------------


@pytest.mark.parametrize(
    "original", ["", "not-a-date", "2019", "2019-02-30"],
    ids=["missing", "malformed", "year-only", "impossible"],
)
def test_an_unreadable_date_is_excluded_and_flagged_uncertain(original):
    """**Never silently passes.** Letting one through would mean a publication
    entered the matrix without anyone establishing it was prior art -- a legal
    claim made by an absent value."""
    result = _screened(original)
    assert result.outcome is DateScreeningOutcome.EXCLUDED_DATE
    assert result.publication_date.date_certainty is DateCertainty.UNCERTAIN
    assert result.publication_date.date_normalized is None
    assert result.publication_date.uncertainty_reason, "the reason is always named"
    assert result.publication_date.date_original == original, "verbatim, for a reviewer"


def test_two_provider_fields_disagreeing_is_uncertain_not_a_preference():
    """Picking the earlier or the "more likely" one would resolve a factual
    conflict by preference. The conflict is exactly what a reviewer needs to
    see, so both dates are named."""
    result = _screened("2019-06-01", corroborating={"epo.publication_date": "2019-07-01"})
    assert result.outcome is DateScreeningOutcome.EXCLUDED_DATE
    assert result.publication_date.date_certainty is DateCertainty.UNCERTAIN
    reason = result.publication_date.uncertainty_reason
    assert "2019-06-01" in reason and "2019-07-01" in reason
    assert "lens.date_published" in reason and "epo.publication_date" in reason


def test_two_provider_fields_agreeing_is_certain():
    result = _screened("2019-06-01", corroborating={"epo.publication_date": "20190601"})
    assert result.outcome is DateScreeningOutcome.ELIGIBLE
    assert result.publication_date.date_certainty is DateCertainty.CERTAIN


def test_an_unreadable_corroborating_field_does_not_create_doubt():
    """A second field that is itself unparseable is no evidence of conflict --
    it is simply a field that says nothing."""
    result = _screened("2019-06-01", corroborating={"epo.publication_date": ""})
    assert result.outcome is DateScreeningOutcome.ELIGIBLE


# --- the priority date is stored and never screens -------------------------


def test_the_priority_date_is_stored_but_never_decides():
    """Under the approved policy the publication date decides. The priority date
    is kept beside it for the reviewer -- storing it without using it is
    deliberate, because silently substituting one date type for another is the
    failure this module is shaped to prevent."""
    result = _screened("2019-06-01", priority="2017-01-01")
    assert result.publication_date.priority_date == date(2017, 1, 1)
    assert result.outcome is DateScreeningOutcome.ELIGIBLE

    # A priority date well before the cutoff cannot rescue a late publication.
    late = _screened("2021-06-01", priority="2015-01-01")
    assert late.outcome is DateScreeningOutcome.EXCLUDED_DATE
    assert late.publication_date.priority_date == date(2015, 1, 1)


# --- no cutoff, and the stage record ---------------------------------------


def test_with_no_cutoff_nothing_is_screened_on_date():
    """A run that is not date-limited excludes nobody -- including a publication
    whose date is unreadable, because there is no comparison to be uncertain
    about."""
    assert _screened("2021-01-01", cutoff=None).outcome is DateScreeningOutcome.ELIGIBLE
    assert _screened("garbage", cutoff=None).outcome is DateScreeningOutcome.ELIGIBLE


@pytest.mark.parametrize("stage", [ScreeningStage.DISCOVERY, ScreeningStage.ENRICHMENT])
def test_the_screening_stage_is_recorded(stage):
    """Screening runs at DISCOVERY when the discovery metadata already carries a
    trustworthy date -- Lens supplies `date_published` -- because that is before
    the expensive EPO call. Which stage screened each publication is recorded."""
    assert _screened("2019-06-01", stage=stage).publication_date.screened_at_stage is stage


def test_the_eligible_path_also_records_a_reason():
    """An audit record that only explains its rejections is half an audit
    record."""
    assert _screened("2019-06-01").reason


# --- policy validation -----------------------------------------------------


def test_an_unset_policy_is_refused():
    """No default: which date screens prior art and whether the cutoff is
    inclusive are legal decisions, and a default would answer them on the user's
    behalf."""
    with pytest.raises(DatePolicyError) as caught:
        validate_policy("")
    assert "no default" in str(caught.value)


def test_an_unknown_policy_is_refused():
    with pytest.raises(DatePolicyError):
        validate_policy("whatever_seems_reasonable")


def test_screening_validates_the_policy_before_comparing_anything():
    publication_date = build_publication_date(
        publication_number="US1",
        date_original="2019-06-01",
        date_source="x",
        stage=ScreeningStage.DISCOVERY,
    )
    with pytest.raises(DatePolicyError):
        screen(publication_date, cutoff=CUTOFF, policy="")


def test_no_timezone_shift_is_ever_applied():
    """Patent dates are date-only values in the document. Shifting one by a
    timezone can move a publication across the cutoff by a day -- changing a
    legal answer to satisfy a datatype."""
    parsed = parse_patent_date("2020-01-15")
    assert parsed == date(2020, 1, 15)
    assert not hasattr(parsed, "tzinfo"), "a plain date carries no timezone at all"
