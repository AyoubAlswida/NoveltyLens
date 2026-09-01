"""Probe 7 must measure the whole §15.1 suite, and must fail closed if it does not.

**A narrowed stability gate is the quietest way a model gets qualified.** The v2
runner measured four of eleven cases. That subset happened to include case C, which
is the only reason `gpt-4o-2024-08-06`'s outcome inconsistency was ever seen — and
it happened to omit seven others, about which nothing was learned while the record
said "stability across the §15 regression suite". Nobody chose that; the subset was
simply what had been written down.

So the required case list is derived **from the specification markdown** here, not
from the code under test. A test that read `CASE_IDS` and asserted against
`CASE_IDS` would pass no matter how many cases were dropped.

No test here makes a network call, and none of them wait.
"""

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from verification import capability_test as runner
from verification.regression_cases import CASE_IDS, VERIFIER_CASES, case_by_id

SPEC = (
    Path(__file__).resolve().parents[2]
    / "docs" / "prompts" / "feature-disclosure-verification.md"
)


def spec_case_ids() -> list[str]:
    """The §15.1 case identifiers, read out of the specification's own table.

    §15.1 is "Verifier behaviour" — every row requires a model call. §15.2 is
    "Application behaviour", most rows explicitly marked "no LLM call", so those
    are not part of a *model* qualification and are covered offline.
    """
    text = SPEC.read_text(encoding="utf-8")
    section = text.split("### 15.1 Verifier behaviour", 1)[1].split("### 15.2", 1)[0]
    return [
        match.group(1)
        for line in section.splitlines()
        if (match := re.match(r"^\|\s*([A-Z]{1,2})\s*\|", line))
    ]


class Clock:
    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


# --- coverage is derived from the spec, not from the code -----------------


def test_the_specification_defines_eleven_executable_verifier_cases():
    """A guard on the guard: if §15.1 grows a case, this fails first and points at
    the document rather than at the runner."""
    assert spec_case_ids() == list("ABCDEFGHIJK")


def test_every_specification_case_is_measured():
    """**The anti-narrowing test.** The required list comes from the markdown."""
    assert list(CASE_IDS) == spec_case_ids()
    assert [case.case_id for case in VERIFIER_CASES] == spec_case_ids()


def test_every_case_carries_passages_and_a_stated_expectation():
    for case in VERIFIER_CASES:
        assert case.passages, case.case_id
        assert case.publication_number, case.case_id
        assert case.expected_outcome, case.case_id
        assert case.title, case.case_id


def test_case_j_is_evaluated_against_its_own_publication():
    """The translated claim belongs to EP1234567A1. Dropping it into the US set
    would be exactly the identity violation §7.3 check 1 rejects, and the case
    would then measure the exclusion rather than the model."""
    assert case_by_id("J").publication_number == "EP1234567A1"


def test_an_unknown_case_identifier_fails_closed():
    with pytest.raises(KeyError):
        case_by_id("ZZ")


# --- the probe refuses to report a subset as the whole gate ---------------


REQUIREMENT_IDS = ("E1", "E2", "E3", "R1")

FOUND = "FOUND"
NOT_FOUND = "NOT_FOUND_IN_SUPPLIED_PASSAGES"
UNCLEAR = "UNCLEAR"

FILLER_FOR_OUTCOME = {
    # A status set that §6 derives the named outcome from, used only for the
    # requirements §15.1 does not name. `PARTIALLY` needs at least one FOUND and
    # one NOT_FOUND; `UNCLEAR` needs at least one UNCLEAR and no NOT_FOUND.
    "FULLY_DISCLOSED_IN_SUPPLIED_PASSAGES": FOUND,
    "NO_DISCLOSURE_FOUND_IN_SUPPLIED_PASSAGES": NOT_FOUND,
    "PARTIALLY_DISCLOSED_IN_SUPPLIED_PASSAGES": NOT_FOUND,
    "UNCLEAR": FOUND,
}


def correct_statuses(case) -> dict[str, str]:
    """A status set that satisfies §15.1's Expected column for `case`.

    The statuses §15.1 *names* are taken verbatim; the rest are filled with a
    value consistent with the expected outcome. This is a **test double**, not an
    expectation: nothing here is compared against a model. It exists so the
    positive control below is genuinely a correct answer rather than a wrong one
    that used to pass.
    """
    statuses = {name: FILLER_FOR_OUTCOME[case.expected_outcome] for name in REQUIREMENT_IDS}
    statuses.update(dict(case.expected_statuses))
    if case.expected_outcome == "PARTIALLY_DISCLOSED_IN_SUPPLIED_PASSAGES":
        if FOUND not in statuses.values():
            statuses["E1"] = FOUND
        if NOT_FOUND not in statuses.values():
            statuses["R1"] = NOT_FOUND
    if case.expected_outcome == "UNCLEAR" and UNCLEAR not in statuses.values():
        statuses["E3"] = UNCLEAR
    return statuses


def _entry(requirement_id: str, status: str, link: str) -> dict:
    key = "relationship_id" if requirement_id.startswith("R") else "element_id"
    basis = (
        [{"chunk_id": "c", "locator": "claim 1", "exact_quote": link, "explanation": "x"}]
        if link
        else []
    )
    return {
        key: requirement_id,
        "status": status,
        "evidence": [],
        "combination_basis": basis,
        "note": "",
    }


def stub_result_for(case, *, statuses=None, outcome=None, quote_link=True):
    """A stand-in verifier result for one case, with no network call.

    `statuses`, `outcome` and `quote_link` are overridable so a test can construct
    a response that is *stably wrong* — which is the exact shape runner v5 exists
    to reject and v4 accepted.
    """
    from verification.states import CellState

    statuses = dict(statuses or correct_statuses(case))
    outcome = outcome or case.expected_outcome
    link = case.expected_combination_link if quote_link else ""

    elements = [_entry(name, statuses[name], link) for name in ("E1", "E2", "E3")]
    relationships = [_entry("R1", statuses["R1"], link)]

    class Basis:
        def __init__(self, quote: str) -> None:
            self.exact_quote = quote

    class Entry:
        def __init__(self, payload: dict) -> None:
            self.combination_basis = tuple(
                Basis(item["exact_quote"]) for item in payload["combination_basis"]
            )

    class Machine:
        pass

    machine = Machine()
    machine.outcome = outcome
    machine.elements = tuple(Entry(payload) for payload in elements)
    machine.relationships = tuple(Entry(payload) for payload in relationships)

    class Result:
        def __init__(self) -> None:
            self.state = CellState.VERIFIED
            self.failed_check = None
            self.raw_response = json.dumps(
                {
                    "feature_id": "F2", "feature_version": "3",
                    "publication_number": case.publication_number,
                    "verification_scope": "SUPPLIED_PASSAGES_ONLY",
                    "outcome": outcome,
                    "elements": elements,
                    "relationships": relationships,
                    "missing_element_ids": [
                        name for name in ("E1", "E2", "E3") if statuses[name] == NOT_FOUND
                    ],
                    "missing_relationship_ids": (
                        ["R1"] if statuses["R1"] == NOT_FOUND else []
                    ),
                    "ambiguities": [], "requests_for_more_context": [],
                }
            )
            self.machine_result = machine

    return Result()


def _stability_with(cases, **overrides):
    """Run the probe with a per-case stubbed verifier so no call is made.

    Per case, not one response for all eleven: the probe now compares every run
    against §15.1's Expected column, so a single shared response would be wrong
    for ten of the eleven cases and the positive control would be meaningless.
    """
    def fake(*_args, **kwargs):
        case_id = kwargs["cell_id"].rsplit(":", 1)[-1]
        return stub_result_for(case_by_id(case_id), **overrides)

    with patch.object(runner, "verify_feature_disclosure", side_effect=fake):
        return runner._stability_probe(cases=cases, sleeper=Clock())


def test_the_full_suite_can_pass():
    """Otherwise every negative test below would pass for the wrong reason."""
    probe = _stability_with(VERIFIER_CASES)
    assert probe["coverage_complete"] is True
    assert probe["passed"] is True
    assert probe["expected_calls"] == probe["scheduled_calls"] == probe["valid_responses"]


def test_a_partial_subset_cannot_be_reported_as_the_full_stability_gate():
    """**The v2 shape.** Four cases, every one of them clean, and the probe still
    fails — because it was asked to certify a suite it did not run."""
    subset = tuple(case for case in VERIFIER_CASES if case.case_id in {"A", "C", "F", "G"})
    probe = _stability_with(subset)

    assert probe["coverage_complete"] is False
    assert probe["passed"] is False
    assert sorted(probe["missing_case_ids"]) == ["B", "D", "E", "H", "I", "J", "K"]
    assert probe["outcome"] == "INCOMPLETE"


def test_an_omitted_case_fails_closed_even_if_it_is_the_only_one_missing():
    subset = tuple(case for case in VERIFIER_CASES if case.case_id != "C")
    probe = _stability_with(subset)
    assert probe["missing_case_ids"] == ["C"]
    assert probe["coverage_complete"] is False
    assert probe["passed"] is False


def test_an_unknown_case_in_the_suite_fails_closed():
    """A case the specification does not define is not evidence about the model,
    and silently accepting one would let the suite be padded."""
    from verification.regression_cases import RegressionCase

    invented = RegressionCase(
        case_id="ZZ", title="invented", passages=VERIFIER_CASES[0].passages,
        publication_number="US1234567B2", publication_title="t", jurisdiction="US",
        document_ids=frozenset({"epo:US1234567"}),
        expected_outcome="NO_DISCLOSURE_FOUND_IN_SUPPLIED_PASSAGES",
    )
    probe = _stability_with(VERIFIER_CASES + (invented,))
    assert probe["unknown_case_ids"] == ["ZZ"]
    assert probe["coverage_complete"] is False
    assert probe["passed"] is False


# --- denominators cannot diverge while passing ----------------------------


def test_the_three_denominators_are_reported_separately():
    """`expected` is what the specification requires, `scheduled` is what this run
    attempted, `valid` is what produced a response. One number would hide exactly
    the divergence that matters."""
    probe = _stability_with(VERIFIER_CASES)
    for key in ("expected_calls", "scheduled_calls", "valid_responses"):
        assert key in probe


def test_a_short_run_count_cannot_pass():
    """Fewer repeats than the protocol requires is a different measurement.
    `expected_calls` is computed from the spec's case count and the configured
    repeat count, so scheduling fewer diverges and fails."""
    with patch.object(runner, "verify_feature_disclosure") as call:
        call.return_value = _StubResult()
        probe = runner._stability_probe(cases=VERIFIER_CASES, runs=1, sleeper=Clock())

    assert probe["scheduled_calls"] == len(CASE_IDS)
    assert probe["expected_calls"] == len(CASE_IDS) * runner.STABILITY_RUNS
    assert probe["scheduled_calls"] != probe["expected_calls"]
    assert probe["all_scheduled_calls_valid"] is False
    assert probe["passed"] is False


class _StubResult:
    from verification.states import CellState as _CellState

    state = _CellState.VERIFIED
    failed_check = None
    raw_response = "{}"

    class machine_result:  # noqa: N801 - stands in for a validated result
        outcome = "NO_DISCLOSURE_FOUND_IN_SUPPLIED_PASSAGES"
        # A validated §10.1 result always carries both, as tuples of frozen
        # models. The probe reads them to check §15.1's combination_basis
        # requirement, so a double that omitted them would fail with an
        # AttributeError rather than with the assertion the test is about.
        elements = ()
        relationships = ()


def test_an_operational_failure_breaks_the_valid_denominator_and_fails():
    error = RuntimeError("OpenAI returned HTTP 429")
    with patch.object(runner, "verify_feature_disclosure", side_effect=error):
        probe = runner._stability_probe(sleeper=Clock())

    assert probe["valid_responses"] == 0
    assert probe["operational_failures"] == probe["scheduled_calls"] == 33
    assert probe["malformed_response_rate"] is None
    assert probe["all_cases_stable"] is False
    assert probe["passed"] is False


# --- a stably WRONG answer is not a pass (runner v5) -----------------------


def test_a_stably_wrong_outcome_fails_even_though_it_is_perfectly_stable():
    """**The v4 hole, closed.** Three identical answers agree with each other, so
    v4's `stable` term was satisfied and the case passed — correctness was
    recorded and never gated on. A model that confidently returns the same wrong
    verdict every time is exactly the one a stability-only measurement cannot
    see."""
    case_e = case_by_id("E")
    wrong = dict(correct_statuses(case_e))
    wrong["E3"] = NOT_FOUND  # §15.1 E says UNCLEAR; this is the measured failure.

    probe = _stability_with(
        VERIFIER_CASES,
        statuses=wrong,
        outcome="PARTIALLY_DISCLOSED_IN_SUPPLIED_PASSAGES",
    )

    # Stable, valid, self-consistent — and wrong.
    assert probe["operational_failures"] == 0
    assert probe["validation_failures"] == 0
    assert probe["all_scheduled_calls_valid"] is True
    assert probe["unexpected_results"] > 0
    assert probe["all_cases_match_expectation"] is False
    assert probe["passed"] is False


def test_a_named_status_that_disagrees_with_the_specification_fails():
    """§15.1 names E3 `UNCLEAR` for case E. A response that reaches the right
    outcome by a different route has not matched the Expected column."""
    case_e = case_by_id("E")
    probe = _stability_with((case_e,), statuses={**correct_statuses(case_e), "E3": FOUND})
    assert probe["cases"]["E"]["unexpected_results"] == runner.STABILITY_RUNS
    assert probe["cases"]["E"]["matches_expected"] is False
    assert probe["passed"] is False


def test_an_unquoted_combination_link_fails_the_cases_that_require_one():
    """§15.1 A and I require the linking text quoted verbatim inside
    `combination_basis`. Check 14 alone only demands *a* basis; it cannot demand
    the right one."""
    probe = _stability_with(VERIFIER_CASES, quote_link=False)
    assert probe["cases"]["A"]["matches_expected"] is False
    assert probe["cases"]["I"]["matches_expected"] is False
    # A case §15.1 states no link requirement for is unaffected.
    assert probe["cases"]["F"]["matches_expected"] is True
    assert probe["passed"] is False
