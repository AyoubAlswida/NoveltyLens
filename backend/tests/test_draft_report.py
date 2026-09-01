"""The draft report says what it is obliged to say (spec §12.4).

`graph/report.py`'s module docstring states the contents as a **contract** rather
than a layout choice, and every rule in it is a rule that needs checking. The
draft is where this product's honesty about its own coverage either holds or
fails: measured EPO coverage on this project's own sample was 17.6%, and the
obligation accepted in return for shipping EPO-only enrichment is that every
exclusion is visible and reasoned, and that nothing partial is presented as
complete.

`build_draft` is pure, so none of this needs a graph, a database, or a run.
"""

from graph.report import COVERAGE_STATEMENT, build_draft


def exclusion(number: str, state: str = "EXCLUDED_NO_FULL_TEXT", **extra):
    """One exclusion **exactly as `GraphState` carries it**.

    Keyed by `requested_number` and `normalized_number`, which is what
    `graph/evidence_nodes.py::_exclusion_payload` writes. Shaping the fixture
    from the real producer is the whole point: the defect below was the draft
    reading a key that producer has never written.
    """
    payload = {
        "requested_number": number,
        "normalized_number": number,
        "state": state,
        "reason": "the provider answered and had no eligible text",
        "sections_retrieved": [],
        "passages_built": 0,
    }
    payload.update(extra)
    return payload


def cell(cell_id: str, **extra):
    payload = {
        "cell_id": cell_id,
        "feature_id": "F1",
        "publication_number": "US11002233",
        "state": "VERIFIED",
        "outcome": "PARTIALLY_DISCLOSED_IN_SUPPLIED_PASSAGES",
        "result_version": 1,
    }
    payload.update(extra)
    return payload


# --- an excluded publication is NAMED ---------------------------------------


def test_the_draft_names_every_publication_it_excluded():
    """The blank that mattered most.

    The number was read as `publication_number`, a key the exclusion payload has
    never carried, so **every draft ever built recorded `null`** and the report
    listed a count of publications it could not name. A reason attached to no
    identifier discharges neither half of "visible and reasoned".
    """
    draft = build_draft(
        {"cells": [], "publication_exclusions": [exclusion("KR20110066580")]},
        reviews={},
        waivers={},
    )
    entry = draft["excluded_publications"][0]
    assert entry["publication_number"] == "KR20110066580"
    # And under the key it is stored by, so the report can be matched back to
    # `publication_exclusions` without a translation step.
    assert entry["requested_number"] == "KR20110066580"


def test_an_unnormalizable_number_is_shown_as_what_was_asked_for():
    """A publication whose number could not be normalized still has to appear.

    It is excluded precisely *because* the number could not be resolved, so there
    is no normalized form to show and the requested one is the only honest
    identifier available. Falling back to an empty string would drop the one
    publication whose exclusion is hardest to explain.
    """
    draft = build_draft(
        {
            "cells": [],
            "publication_exclusions": [
                exclusion("not-a-number", state="EXCLUDED_INELIGIBLE", normalized_number=""),
            ],
        },
        reviews={},
        waivers={},
    )
    assert draft["excluded_publications"][0]["publication_number"] == "not-a-number"


def test_an_excluded_publication_is_stated_not_to_have_been_evaluated():
    """It is not "no prior art found". §1.2.1."""
    draft = build_draft(
        {"cells": [], "publication_exclusions": [exclusion("KR20110066580")]},
        reviews={},
        waivers={},
    )
    entry = draft["excluded_publications"][0]
    assert entry["evaluated"] is False
    assert "NOT EVALUATED" in entry["note"]


def test_an_excluded_publication_never_becomes_a_matrix_entry():
    """Case AH. An exclusion means no comparison was attempted."""
    draft = build_draft(
        {"cells": [], "publication_exclusions": [exclusion("KR20110066580")]},
        reviews={},
        waivers={},
    )
    assert draft["matrix"] == []
    assert draft["counts"]["excluded_publications"] == 1


def test_the_exclusion_carries_what_makes_it_auditable():
    """`sections_retrieved` with `passages_built = 0` says exactly why a
    publication with text was still excluded: text was there, a citable locator
    was not. Dropping them leaves the reader with a state name and no way to
    challenge it."""
    draft = build_draft(
        {
            "cells": [],
            "publication_exclusions": [
                exclusion(
                    "EP3218319",
                    state="EXCLUDED_INELIGIBLE",
                    sections_retrieved=["DESCRIPTION"],
                    passages_built=0,
                )
            ],
        },
        reviews={},
        waivers={},
    )
    entry = draft["excluded_publications"][0]
    assert entry["sections_retrieved"] == ["DESCRIPTION"]
    assert entry["passages_built"] == 0


# --- a waiver discloses a gap, and never fills one --------------------------


def test_every_waiver_appears_with_its_unresolved_limitation():
    """Printing the comparison without the limitation would turn a disclosure
    into an omission."""
    waiver = {
        "reason": "Re-running twice produced the same rejection.",
        "unresolved_limitation": "F2 was never compared against US7654321.",
    }
    draft = build_draft(
        {"cells": [cell("C1", state="VALIDATION_FAILURE", failed_check="check 15")],
         "publication_exclusions": []},
        reviews={},
        waivers={"C1": [waiver]},
    )
    assert draft["matrix"][0]["waivers"] == [waiver]
    assert draft["unresolved"][0]["unresolved_limitations"] == [
        "F2 was never compared against US7654321."
    ]


def test_a_waived_comparison_keeps_its_own_state_and_class():
    """Cases AJ and AZ. A class that changed on waiving would hide the gap in
    exactly the view a reviewer scans fastest."""
    draft = build_draft(
        {"cells": [cell("C1", state="VALIDATION_FAILURE", failed_check="check 15")],
         "publication_exclusions": []},
        reviews={},
        waivers={"C1": [{"reason": "r", "unresolved_limitation": "l"}]},
    )
    entry = draft["matrix"][0]
    assert entry["state"] == "VALIDATION_FAILURE"
    assert entry["class"] == "validation-failure"
    assert draft["unresolved"][0]["waived"] is True


def test_a_validation_failure_carries_its_failing_check():
    """§9.1: recorded *with* its failing check, so the report can print it
    beside the comparison rather than blanking the row."""
    draft = build_draft(
        {"cells": [cell("C1", state="VALIDATION_FAILURE", failed_check="check 15")],
         "publication_exclusions": []},
        reviews={},
        waivers={},
    )
    assert draft["matrix"][0]["failed_check"] == "check 15"
    assert draft["unresolved"][0]["failed_check"] == "check 15"


def test_a_not_evaluated_comparison_renders_as_itself():
    """Case AZ: never as `NO_DISCLOSURE_FOUND`."""
    draft = build_draft(
        {"cells": [cell("C1", state="NOT_EVALUATED", outcome=None,
                        not_evaluated_reason="no eligible passage")],
         "publication_exclusions": []},
        reviews={},
        waivers={},
    )
    entry = draft["matrix"][0]
    assert entry["state"] == "NOT_EVALUATED"
    assert entry["class"] == "unevaluated"
    assert entry["machine_outcome"] is None
    assert draft["unresolved"][0]["reason"] == "no eligible passage"


# --- the machine result and the human overlay stay apart --------------------


def test_a_review_never_edits_the_machine_result():
    """Constraint 7 and §13.1. The overlay sits beside it under its own key."""
    review = {"status": "OVERRIDDEN", "reviewed_outcome": "NO_DISCLOSURE_FOUND_IN_SUPPLIED_PASSAGES"}
    draft = build_draft(
        {"cells": [cell("C1")], "publication_exclusions": []},
        reviews={"C1": review},
        waivers={},
    )
    entry = draft["matrix"][0]
    assert entry["machine_outcome"] == "PARTIALLY_DISCLOSED_IN_SUPPLIED_PASSAGES"
    assert entry["reviewed_outcome"] == "NO_DISCLOSURE_FOUND_IN_SUPPLIED_PASSAGES"
    assert entry["class"] == "human-overridden"


# --- nothing partial is presented as complete -------------------------------


def test_every_draft_carries_the_coverage_statement():
    """Not a disclaimer bolted on at the end: it is the difference between "we
    found no prior art" and "we compared this much"."""
    draft = build_draft({"cells": [], "publication_exclusions": []}, reviews={}, waivers={})
    assert draft["coverage_statement"] == COVERAGE_STATEMENT
    assert "NOT" in COVERAGE_STATEMENT and "EVALUATED" in COVERAGE_STATEMENT
    assert "not an exhaustive novelty search" in COVERAGE_STATEMENT
    assert "not a legal opinion" in COVERAGE_STATEMENT


def test_a_draft_says_it_is_a_draft():
    draft = build_draft({"cells": [], "publication_exclusions": []}, reviews={}, waivers={})
    assert draft["status"] == "DRAFT — NOT APPROVED"


def test_a_run_that_produced_nothing_still_produces_an_honest_draft():
    """Zero comparisons and zero exclusions is a real result, not a failure.
    The draft states it rather than being empty or absent."""
    draft = build_draft({"cells": [], "publication_exclusions": []}, reviews={}, waivers={})
    assert draft["matrix"] == []
    assert draft["unresolved"] == []
    assert draft["excluded_publications"] == []
    assert draft["counts"] == {
        "cells_total": 0,
        "unresolved": 0,
        "excluded_publications": 0,
    }
    assert draft["coverage_statement"]
