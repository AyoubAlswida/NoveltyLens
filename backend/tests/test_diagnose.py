"""The case diagnostic must be structurally incapable of qualifying a runtime.

A cheap probe that could qualify something is worse than no probe: it is a
five-call shortcut past a thirty-nine-call gate, and the shortcut would be taken.

So "non-qualifying" is not a property of how it is used. It is a property of what
it produces:

1. its record carries **no `runtime_identity`**, so `_identity_from_record`
   returns `None` and no runtime can ever match it;
2. its record carries **no `eligible` key**, so there is nothing to misread as a
   verdict;
3. its filename is outside the `structured-output-capability-*.json` namespace
   `capability.load_artifacts` globs, so it is never even read;
4. the module does not import the artifact writer at all.

Each is asserted here. No test makes a network call.
"""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from verification import diagnose
from verification.capability import (
    ARTIFACT_DIRECTORY,
    CapabilityNotQualified,
    _identity_from_record,
    load_artifacts,
    require_capability,
)
from verification.regression_cases import CASE_IDS

BACKEND = Path(__file__).resolve().parent.parent


class Clock:
    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def _verified(outcome: str):
    from verification.states import CellState

    class Result:
        state = CellState.VERIFIED
        failed_check = None
        raw_response = json.dumps(
            {
                "feature_id": "F2", "feature_version": "3",
                "publication_number": "US1234567B2",
                "verification_scope": "SUPPLIED_PASSAGES_ONLY",
                "outcome": outcome,
                "elements": [
                    {"element_id": "E1", "status": "FOUND",
                     "evidence": [], "combination_basis": [], "note": ""},
                    {"element_id": "E2", "status": "FOUND",
                     "evidence": [], "combination_basis": [], "note": ""},
                    {"element_id": "E3", "status": "UNCLEAR",
                     "evidence": [], "combination_basis": [], "note": ""},
                ],
                "relationships": [
                    {"relationship_id": "R1", "status": "UNCLEAR",
                     "evidence": [], "combination_basis": [], "note": ""}
                ],
                "missing_element_ids": [], "missing_relationship_ids": [],
                "ambiguities": [], "requests_for_more_context": [],
            }
        )

        class machine_result:  # noqa: N801 - stands in for a validated result
            pass

    Result.machine_result.outcome = outcome
    return Result()


# --- it cannot qualify anything -------------------------------------------


def test_the_record_carries_no_runtime_identity():
    """Without one, `_identity_from_record` returns None and nothing can match."""
    with patch.object(diagnose, "verify_feature_disclosure", return_value=_verified("UNCLEAR")):
        record = diagnose.run_case("E", runs=2, sleeper=Clock())

    assert "runtime_identity" not in record
    assert _identity_from_record(record) is None


def test_the_record_carries_no_eligibility_verdict():
    with patch.object(diagnose, "verify_feature_disclosure", return_value=_verified("UNCLEAR")):
        record = diagnose.run_case("E", runs=2, sleeper=Clock())

    assert "eligible" not in record
    assert record["non_qualifying"] is True
    assert "DIAGNOSTIC ONLY" in record["note"]


def test_the_record_states_the_inference_settings_it_ran_under(monkeypatch):
    """A diagnostic that cannot say what it measured is worth less than one that can.

    The 2026-08-29 case-E record carried no such block, so confirming afterwards
    that it really had run at `reasoning_effort=high` meant comparing its
    reasoning-token counts against the default-effort gate run. Sound, but an
    inference rather than a record -- and the next reader would have had to redo
    it. `reasoning_effort` is the field the owner amendment turns on, so it is the
    field asserted here.
    """
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "high")
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "local")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5-mini-2025-08-07")
    import deployment

    target = deployment.resolve_target(None, environ=dict(os.environ))
    with patch.object(diagnose.config, "llm_target", target), patch.object(
        diagnose, "verify_feature_disclosure", return_value=_verified("UNCLEAR")
    ):
        record = diagnose.run_case("E", runs=1, sleeper=Clock())

    assert record["inference_config"]["reasoning_effort"] == "high"
    assert record["inference_config"]["reasoning_effort_configured"] is True
    # Sanitized: the block is settings, never a credential or an account id.
    rendered = json.dumps(record["inference_config"]).lower()
    for forbidden in ("api_key", "sk-", "authorization", "org", "project", "token="):
        assert forbidden not in rendered


def test_the_recorded_config_is_the_one_the_gate_would_fingerprint(monkeypatch):
    """The diagnostic composes its block instead of importing `capability`, so
    that the module-level ban on importing the artifact writer's home stays
    intact. This is what stops the two compositions drifting apart: a diagnostic
    recording a configuration the gate would not fingerprint is a diagnostic
    describing a runtime nobody is about to qualify.
    """
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "high")
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "local")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5-mini-2025-08-07")
    import deployment
    from verification.capability import inference_config_for

    target = deployment.resolve_target(None, environ=dict(os.environ))
    with patch.object(diagnose.config, "llm_target", target), patch.object(
        diagnose, "verify_feature_disclosure", return_value=_verified("UNCLEAR")
    ):
        record = diagnose.run_case("E", runs=1, sleeper=Clock())

    assert record["inference_config"] == inference_config_for(target).as_dict()


def test_recording_the_inference_config_does_not_make_it_qualify(monkeypatch, tmp_path):
    """The detail is for a human reader, never for admission.

    `_identity_from_record` needs a `runtime_identity` mapping and returns `None`
    without one, so no amount of settings detail can promote a diagnostic into a
    qualification. This is the guard on the change that added the block above.
    """
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "high")
    with patch.object(diagnose, "verify_feature_disclosure", return_value=_verified("UNCLEAR")):
        record = diagnose.run_case("E", runs=1, sleeper=Clock())

    assert "inference_config" in record
    assert "runtime_identity" not in record
    assert "eligible" not in record
    assert _identity_from_record(record) is None

    # Even forged into the namespace with an eligibility verdict bolted on.
    (tmp_path / "structured-output-capability-local-forged-2026-08-29.json").write_text(
        json.dumps({**record, "eligible": True}), encoding="utf-8"
    )
    import config

    with pytest.raises(CapabilityNotQualified):
        require_capability(config.llm_target, directory=tmp_path)


def test_a_diagnostic_record_dropped_into_the_measurements_directory_is_never_read(tmp_path):
    """Belt and braces on the namespace: even a hand-copied file is not globbed."""
    with patch.object(diagnose, "verify_feature_disclosure", return_value=_verified("UNCLEAR")):
        record = diagnose.run_case("E", runs=1, sleeper=Clock())

    (tmp_path / "diagnostic-case-E-2026-08-29-120000.json").write_text(
        json.dumps({**record, "eligible": True}), encoding="utf-8"
    )
    assert load_artifacts(tmp_path) == []


def test_even_renamed_into_the_capability_namespace_it_qualifies_nothing(tmp_path):
    """The last line of defence: a file renamed into the namespace still has no
    identity, so it matches no runtime."""
    import config

    with patch.object(diagnose, "verify_feature_disclosure", return_value=_verified("UNCLEAR")):
        record = diagnose.run_case("E", runs=1, sleeper=Clock())

    (tmp_path / "structured-output-capability-local-forged-2026-08-29.json").write_text(
        json.dumps({**record, "eligible": True}), encoding="utf-8"
    )
    with pytest.raises(CapabilityNotQualified) as raised:
        require_capability(config.llm_target, directory=tmp_path)
    assert "no readable runtime_identity" in "; ".join(raised.value.reasons)


def test_the_module_does_not_import_the_artifact_writer():
    """Checked on the **import lines**, not on the whole source: the docstring
    names these symbols in order to say the module does not use them, and a
    substring test over the file would fail on the explanation itself."""
    source = (BACKEND / "verification" / "diagnose.py").read_text(encoding="utf-8")
    imports = chr(10).join(
        line for line in source.splitlines()
        if line.strip().startswith(("import ", "from "))
    )
    for symbol in ("artifact_filename", "ARTIFACT_DIRECTORY", "RuntimeIdentity",
                   "record_path as", "capability import"):
        assert symbol not in imports, f"diagnose.py imports {symbol}"

    # And nothing in the body constructs one either.
    body = source.split('"""', 2)[-1]
    assert "RuntimeIdentity(" not in body
    assert "artifact_filename(" not in body


def test_the_filename_is_outside_the_capability_namespace():
    path = diagnose.record_path("E")
    assert path.name.startswith("diagnostic-case-")
    assert not path.name.startswith("structured-output-capability-")
    assert path.parent == ARTIFACT_DIRECTORY


# --- it measures the right thing ------------------------------------------


def test_it_runs_only_the_named_case_and_the_configured_number_of_repeats():
    with patch.object(
        diagnose, "verify_feature_disclosure", return_value=_verified("UNCLEAR")
    ) as call:
        record = diagnose.run_case("E", sleeper=Clock())

    assert call.call_count == diagnose.DIAGNOSTIC_RUNS == 5
    assert record["case_id"] == "E"
    assert record["runs"] == 5


def test_each_diagnostic_repeat_has_a_hard_one_call_budget():
    """The advertised call count is a ceiling, not a happy-path estimate."""
    with patch.object(
        diagnose, "verify_feature_disclosure", return_value=_verified("UNCLEAR")
    ) as call:
        diagnose.run_case("E", runs=2, sleeper=Clock())

    assert diagnose.DIAGNOSTIC_RETRY_POLICY.max_attempts == 1
    assert call.call_count == 2
    for invocation in call.call_args_list:
        assert invocation.kwargs["transport_retries"] == 0
        assert invocation.kwargs["parse_attempts"] == 1


def test_it_does_not_change_the_expected_outcome():
    """§15.1 decides what a correct answer is; a diagnostic does not get to."""
    from verification.regression_cases import case_by_id

    with patch.object(diagnose, "verify_feature_disclosure", return_value=_verified("UNCLEAR")):
        record = diagnose.run_case("E", runs=1, sleeper=Clock())
    assert record["expected_outcome"] == case_by_id("E").expected_outcome == "UNCLEAR"


def test_all_five_matching_runs_pass():
    with patch.object(diagnose, "verify_feature_disclosure", return_value=_verified("UNCLEAR")):
        record = diagnose.run_case("E", sleeper=Clock())
    assert record["passing_runs"] == 5
    assert record["all_runs_expected"] is True


def test_one_wrong_substantive_outcome_fails_the_whole_diagnostic():
    """**The 2026-08-29 shape**: `UNCLEAR`, `PARTIALLY`, `PARTIALLY`. No threshold."""
    responses = [_verified("UNCLEAR")] * 5
    responses[1] = _verified("PARTIALLY_DISCLOSED_IN_SUPPLIED_PASSAGES")
    responses[2] = _verified("PARTIALLY_DISCLOSED_IN_SUPPLIED_PASSAGES")

    with patch.object(diagnose, "verify_feature_disclosure", side_effect=responses):
        record = diagnose.run_case("E", sleeper=Clock())

    assert record["all_runs_expected"] is False
    assert record["passing_runs"] == 3
    assert record["results"][1]["failure_category"] == "WRONG_OUTCOME"


def test_a_validation_failure_fails_the_diagnostic():
    from verification.states import CellState

    class Failed:
        state = CellState.VALIDATION_FAILURE
        failed_check = "check 15"
        machine_result = None
        raw_response = "{}"

    with patch.object(diagnose, "verify_feature_disclosure", return_value=Failed()):
        record = diagnose.run_case("E", runs=2, sleeper=Clock())

    assert record["all_runs_expected"] is False
    assert record["results"][0]["failure_category"] == "OUTCOME_INCONSISTENT"


def test_an_operational_failure_fails_the_diagnostic_and_is_labelled_as_such():
    error = RuntimeError("OpenAI returned HTTP 429")
    with patch.object(diagnose, "verify_feature_disclosure", side_effect=error):
        record = diagnose.run_case("E", runs=2, sleeper=Clock())

    assert record["all_runs_expected"] is False
    assert record["operational_failures"] == 2
    assert record["results"][0]["operational"] is True


def test_an_unknown_case_identifier_fails_closed():
    with pytest.raises(KeyError):
        diagnose.run_case("ZZ", runs=1, sleeper=Clock())


def test_every_specification_case_is_addressable():
    for case_id in CASE_IDS:
        assert diagnose.record_path(case_id).name.startswith(f"diagnostic-case-{case_id}-")


def test_no_diagnostic_record_carries_model_authored_prose():
    with patch.object(diagnose, "verify_feature_disclosure", return_value=_verified("UNCLEAR")):
        record = diagnose.run_case("E", runs=1, sleeper=Clock())
    rendered = json.dumps(record)
    for forbidden in ("exact_quote", "combination_basis", "heat exchanger"):
        assert forbidden not in rendered
