"""The §14.2.1 capability runner itself.

The runner is the thing that decides whether a model may serve the verifier, so
the ways it can be wrong are the ways an ineligible model gets certified. Four of
them have already happened:

1. **A DNS failure read as a provider rejection.** For probes 3-5 a rejection used
   to be a *pass*, so collapsing the two outcomes let an unreachable gateway
   certify five of seven probes. That shipped once and is pinned here.
2. **A missing credential read the same way**, which wrote an eligible-looking
   artifact on a machine with no key.
3. **An HTTP 429 read the same way** — observed live on 2026-08-28 — together with
   a stability probe that scored its own twelve HTTP 400s as
   `malformed_response_rate: 0.0` and `all_cases_stable: true`. Both read as
   passing terms. `test_the_2026_08_28_all_failed_run_is_ineligible` reproduces
   that exact run.
4. **A record written when nothing was measured.** `"eligible": false` is
   indistinguishable from *tested and failed*.

And one that must never happen: **a provider body, credential or account
identifier in the artifact.** The artifact is a file a human reads and shares.

**No outbound call is made by any test here, and no test ever waits.** Every probe
is stubbed at `_raw_completion`, which is the runner's only network boundary, and
every retry takes an injected sleeper that records the delay instead of taking it.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

from verification import capability_test as runner

BACKEND = Path(__file__).resolve().parent.parent


class Clock:
    """A sleeper that records instead of waiting.

    Injected everywhere a retry can happen. A test suite that actually slept
    through a bounded-retry policy would add ~45s per rate-limit case and would be
    the first thing someone disabled.
    """

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)

    @property
    def total(self) -> float:
        return sum(self.delays)


def _openai_target():
    """A `local`/OpenAI target, so a test does not depend on the suite's own
    `test` profile — which has no provider and deliberately cannot be probed."""
    from deployment import LLMTarget, Profile, Provider

    return LLMTarget(
        Profile.LOCAL, Provider.OPENAI, "https://api.openai.com/v1", "gpt-4o-2024-08-06"
    )


def _status_error(status: int, *, retry_after: str | None = None, param: str | None = None):
    """An exception shaped like a provider's, carrying a status **field**.

    Classification reads fields, not prose, so a double that only put the number
    in its message would be testing the fallback rather than the rule.
    """

    class Response:
        def __init__(self) -> None:
            self.status_code = status
            self.headers = {} if retry_after is None else {"retry-after": retry_after}

    class ProviderError(Exception):
        def __init__(self) -> None:
            super().__init__(f"Error code: {status}")
            self.status_code = status
            self.response = Response()
            self.param = param

    return ProviderError()


# --- unreachable is not rejection ------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        requests.exceptions.ConnectionError(
            "HTTPSConnectionPool(host='x'): Max retries exceeded "
            "(Caused by NameResolutionError('Failed to resolve'))"
        ),
        requests.exceptions.ConnectTimeout("connect timed out"),
        requests.exceptions.ReadTimeout("Read timed out"),
        OSError("[Errno 11001] getaddrinfo failed"),
    ],
)
def test_transport_failures_are_unreachable(error):
    assert runner.is_unreachable(error)
    assert runner.classify(error) is runner.Outcome.UNREACHABLE


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("400 Client Error: Bad Request"),
        RuntimeError("response_format json_schema is not supported"),
        ValueError("the model refused the schema"),
    ],
)
def test_provider_answers_are_not_unreachable(error):
    """A provider that answered is a **measurement** of something. Classifying it
    as unreachable would abandon a real result."""
    assert not runner.is_unreachable(error)


def test_unreachability_is_detected_through_the_whole_cause_chain():
    """`_llm_chat` raises a bare `RuntimeError("LLM request failed")` and attaches
    the transport failure underneath. Reading only the top frame is exactly the
    bug this file exists to prevent."""
    cause = requests.exceptions.ConnectionError("Max retries exceeded")
    wrapped = RuntimeError("LLM request failed")
    wrapped.__cause__ = cause
    assert runner.is_unreachable(wrapped)


# --- HTTP classification: no operational failure may ever pass -------------


@pytest.mark.parametrize(
    "status,expected",
    [
        (429, runner.Outcome.RATE_LIMITED),
        (401, runner.Outcome.AUTH_FAILED),
        (403, runner.Outcome.AUTH_FAILED),
        (500, runner.Outcome.PROVIDER_ERROR),
        (503, runner.Outcome.PROVIDER_ERROR),
        (404, runner.Outcome.BAD_REQUEST),
        (422, runner.Outcome.BAD_REQUEST),
    ],
)
def test_http_failures_classify_to_their_own_operational_outcome(status, expected):
    assert runner.classify(_status_error(status)) is expected
    assert runner.is_operational(expected.value)


def test_a_rate_limit_is_never_a_rejection():
    """**The 2026-08-28 defect.** A 429 was recorded as `REJECTED`, and a
    `REJECTED` enforcement probe was scored as a pass — so a rate-limited run
    reported that the provider had enforced constraints it never evaluated."""
    outcome = runner.classify(_status_error(429))
    assert outcome is runner.Outcome.RATE_LIMITED
    assert outcome not in {runner.Outcome.PARSED, runner.Outcome.UNPARSEABLE}
    assert runner.is_operational(outcome.value)


def test_a_schema_rejection_is_told_apart_from_a_model_refusing_a_value():
    """One says our *request* was malformed; the other says something about the
    model. The whole enforcement half of the gate rests on the distinction."""
    submitted = _status_error(400, param="response_format")
    assert runner.classify(submitted) is runner.Outcome.SCHEMA_REJECTED
    assert runner.is_operational("SCHEMA_REJECTED")

    other = _status_error(400, param="messages")
    assert runner.classify(other) is runner.Outcome.BAD_REQUEST


def test_a_schema_rejection_is_recognised_from_openais_own_wording():
    error = RuntimeError(
        "OpenAI returned HTTP 400: Invalid schema for response_format "
        "'feature_disclosure_verification': schema must have a 'type' key."
    )
    assert runner.classify(error) is runner.Outcome.SCHEMA_REJECTED


def test_a_wrapped_deterministic_4xx_keeps_its_status():
    """`llm._openai_chat` re-raises a 4xx as a plain RuntimeError naming the
    status, deliberately dropping the provider exception. Without the message
    fallback that arrives unclassifiable and gets retried against a request that
    can never succeed."""
    wrapped = RuntimeError("OpenAI returned HTTP 400 for model 'gpt-4o': bad request.")
    assert runner.classify(wrapped) in {
        runner.Outcome.BAD_REQUEST, runner.Outcome.SCHEMA_REJECTED
    }
    assert runner.classify(wrapped) not in runner.RETRYABLE_OUTCOMES


def test_a_token_ceiling_is_a_measurement_not_an_operational_failure():
    assert not runner.is_operational(runner.Outcome.TOKEN_CEILING.value)
    assert not runner.is_operational(runner.Outcome.MODEL_REFUSED.value)
    assert not runner.is_operational(runner.Outcome.PARSED.value)
    assert not runner.is_operational(runner.Outcome.UNPARSEABLE.value)


def test_an_operational_probe_records_the_outcome_and_never_passes():
    clock = Clock()
    with patch.object(runner, "_raw_completion", side_effect=_status_error(429)):
        result = runner._probe("3. required fields", "ask", response_format=None, sleeper=clock)
    assert result["outcome"] == "RATE_LIMITED"
    assert result["passed"] is False
    assert runner.is_operational(result["outcome"])


def test_an_unreachable_probe_records_unreachable_and_never_passes():
    clock = Clock()
    with patch.object(
        runner, "_raw_completion",
        side_effect=requests.exceptions.ConnectionError("getaddrinfo failed"),
    ):
        result = runner._probe("1. probe", "ask", response_format=None, sleeper=clock)
    assert result["outcome"] == "UNREACHABLE"
    assert result["passed"] is False


# --- bounded retry ---------------------------------------------------------


def test_transient_failures_are_retried_within_the_attempt_cap():
    clock = Clock()
    policy = runner.RetryPolicy(max_attempts=3, base_delay=2.0, max_delay=20.0, max_total_wait=45.0)
    with patch.object(runner, "_raw_completion", side_effect=_status_error(429)) as call:
        attempt = runner.call_with_retry(
            lambda: runner._raw_completion("ask", response_format=None),
            policy=policy, sleeper=clock,
        )
    assert call.call_count == 3
    assert attempt.attempts == 3
    assert attempt.outcome is runner.Outcome.RATE_LIMITED
    assert len(clock.delays) == 2, "one sleep between each pair of attempts"


def test_retry_honours_a_valid_retry_after_header():
    clock = Clock()
    with patch.object(runner, "_raw_completion", side_effect=_status_error(429, retry_after="7")):
        runner.call_with_retry(
            lambda: runner._raw_completion("ask", response_format=None),
            policy=runner.RetryPolicy(max_attempts=2), sleeper=clock,
        )
    assert clock.delays == [7.0]


@pytest.mark.parametrize("header", ["not-a-number", "-5", "0", "86400", ""])
def test_an_unusable_retry_after_falls_back_to_backoff(header):
    """A malformed header is ignored and an absurd one is not obeyed: `Retry-After:
    86400` would otherwise stall the gate for a day."""
    clock = Clock()
    with patch.object(runner, "_raw_completion", side_effect=_status_error(429, retry_after=header)):
        runner.call_with_retry(
            lambda: runner._raw_completion("ask", response_format=None),
            policy=runner.RetryPolicy(max_attempts=2, base_delay=2.0), sleeper=clock,
        )
    assert clock.delays == [2.0]


def test_total_wait_is_capped_even_when_attempts_remain():
    clock = Clock()
    policy = runner.RetryPolicy(max_attempts=9, base_delay=10.0, max_delay=20.0, max_total_wait=25.0)
    with patch.object(runner, "_raw_completion", side_effect=_status_error(503)):
        attempt = runner.call_with_retry(
            lambda: runner._raw_completion("ask", response_format=None),
            policy=policy, sleeper=clock,
        )
    assert clock.total <= policy.max_total_wait
    assert attempt.attempts < policy.max_attempts


@pytest.mark.parametrize("status,param", [(400, "response_format"), (400, None), (401, None), (403, None)])
def test_deterministic_failures_are_never_retried(status, param):
    """A schema error and an auth error produce the same answer every time.
    Retrying spends quota to learn nothing, and for auth it hammers the endpoint
    that gets a key blocked."""
    clock = Clock()
    with patch.object(runner, "_raw_completion", side_effect=_status_error(status, param=param)) as call:
        runner.call_with_retry(
            lambda: runner._raw_completion("ask", response_format=None),
            policy=runner.RetryPolicy(max_attempts=5), sleeper=clock,
        )
    assert call.call_count == 1
    assert clock.delays == []


def test_retry_never_switches_model_or_provider():
    """The retry loop re-invokes the *same* callable and touches no configuration.
    A retry that changed the target would be a silent fallback."""
    source = (BACKEND / "verification" / "capability_test.py").read_text(encoding="utf-8")
    body = source.split("def call_with_retry", 1)[1].split("\ndef ", 1)[0]
    for forbidden in ("llm_target =", "OPENAI_MODEL", "model=", "base_url"):
        assert forbidden not in body, f"call_with_retry must not touch {forbidden}"


# --- probe semantics: only a genuine conforming response passes -------------


def test_probes_3_to_5_pass_only_on_a_conforming_response():
    """Told to violate the schema, the model returned a conforming object anyway —
    which is what strict Structured Outputs being honoured actually looks like."""
    body = json.dumps({"requirement_type": "ELEMENT", "status": "FOUND", "note": "n"})
    with patch.object(runner, "_raw_completion", return_value=(body, {})):
        result = runner._probe("4. closed enums", "ask", response_format=None)
    assert result["outcome"] == "PARSED"
    assert result["passed"] is True
    assert result["schema_violations"] == []


@pytest.mark.parametrize(
    "body,expected_violation",
    [
        ({"requirement_type": "ELEMENT", "status": "FOUND"}, "required property 'note' is missing"),
        (
            {"requirement_type": "ELEMENT", "status": "DEFINITELY_DISCLOSED", "note": "n"},
            "status='DEFINITELY_DISCLOSED' is outside the closed enum",
        ),
        (
            {"requirement_type": "ELEMENT", "status": "FOUND", "note": "n", "confidence": 0.9},
            "additional property 'confidence' was emitted",
        ),
    ],
)
def test_a_model_that_emits_the_violation_fails_the_probe(body, expected_violation):
    """The provider did not enforce the constraint. That is the finding, and it is
    a failure — not something a later `REJECTED` reading could paper over."""
    with patch.object(runner, "_raw_completion", return_value=(json.dumps(body), {})):
        result = runner._probe("3. required fields", "ask", response_format=None)
    assert result["passed"] is False
    assert expected_violation in result["schema_violations"]


def test_a_token_ceiling_is_not_retried():
    """**Observed on the 2026-08-29 run.** A token ceiling is the model *answering*
    and being cut off — a measurement. It was falling through to `classify`, which
    bucketed it as an unrecognised provider failure and retried it three times,
    spending three calls and six seconds to learn the same thing."""
    clock = Clock()
    with patch.object(
        runner, "_raw_completion", side_effect=runner.TokenCeilingReached("ceiling")
    ) as call:
        result = runner._probe("5. additionalProperties", "ask", response_format=None,
                               sleeper=clock)
    assert call.call_count == 1
    assert clock.delays == []
    assert result["outcome"] == "TOKEN_CEILING"
    assert result["passed"] is False


def test_a_model_refusal_is_not_retried():
    clock = Clock()
    with patch.object(runner, "_raw_completion", side_effect=runner.ModelRefused("no")) as call:
        runner._probe("5. additionalProperties", "ask", response_format=None, sleeper=clock)
    assert call.call_count == 1
    assert clock.delays == []


def test_the_probe_schema_is_a_faithful_miniature_of_section_10():
    """Every constraint the miniature drops is a way for a probe to measure
    something other than what it names. §10 caps every free-text field at 300, and
    dropping that cap made probe 5 measure a token budget instead of
    `additionalProperties` enforcement."""
    note = runner.PROBE_SCHEMA["properties"]["note"]
    assert note["maxLength"] == 300
    assert runner.PROBE_SCHEMA["additionalProperties"] is False
    assert set(runner.PROBE_SCHEMA["required"]) == set(runner.PROBE_SCHEMA["properties"])
    assert "enum" in runner.PROBE_SCHEMA["properties"]["status"]


def test_a_model_refusal_is_not_a_pass():
    """A refusal is a real answer and a real measurement, but it is not evidence
    that a constraint was enforced."""
    with patch.object(runner, "_raw_completion", side_effect=runner.ModelRefused("no")):
        result = runner._probe("5. additionalProperties", "ask", response_format=None)
    assert result["outcome"] == "MODEL_REFUSED"
    assert result["passed"] is False


def test_probe_6_requires_canonical_validation_not_merely_provider_acceptance():
    """The wire schema no longer carries `minItems`, so a provider can legitimately
    accept an empty `elements` array. The canonical validator is the only thing
    left that rejects it, and probe 6 is where that is proved end to end."""
    accepted_but_invalid = json.dumps(
        {
            "feature_id": "F2", "feature_version": "3",
            "publication_number": "US1234567B2",
            "verification_scope": "SUPPLIED_PASSAGES_ONLY",
            "outcome": "UNCLEAR",
            "elements": [],  # minItems: 1 in the canonical contract
            "relationships": [], "missing_element_ids": [],
            "missing_relationship_ids": [], "ambiguities": [],
            "requests_for_more_context": [],
        }
    )
    with patch.object(runner, "_raw_completion", return_value=(accepted_but_invalid, {})):
        result = runner._probe("6. full schema", "ask", response_format=None, canonical=True)
    assert result["outcome"] == "PARSED"
    assert result["passed"] is False
    assert result["canonical_validation"].startswith("REJECTED")


# --- nothing unsafe reaches the artifact -----------------------------------


def test_the_adapter_identity_carries_no_credential():
    """Parsed, not string-trimmed: a userinfo section or a query-string key must
    not survive by being in a shape nobody predicted."""
    from deployment import LLMTarget, Profile, Provider

    hostile = "https://user:sup3rsecret@gateway.example/v1/chat/completions?api_key=SECRET123"
    with patch.object(
        runner.config,
        "llm_target",
        LLMTarget(Profile.ARAMCO, Provider.OPENAI_COMPATIBLE, hostile, "m"),
    ):
        identity = runner.safe_provider_identity()
    rendered = json.dumps(identity)
    assert "sup3rsecret" not in rendered
    assert "SECRET123" not in rendered
    assert "api_key" not in rendered
    assert identity["host"] == "gateway.example"


def test_a_malformed_response_is_recorded_by_shape_and_never_by_content():
    """"We never persist a provider body" must not depend on having predicted its
    shape."""
    body = '```json\n{"secret_token": "hf_LIVEKEY", "status": "FOUND"'
    try:
        json.loads(body)
    except json.JSONDecodeError as error:
        shape = runner.describe_unparseable(body, error)

    rendered = json.dumps(shape)
    assert "hf_LIVEKEY" not in rendered
    assert "secret_token" not in rendered
    assert shape["looks_fenced"] is True
    assert shape["chars"] == len(body)
    assert "position" in shape["json_error"]


def test_a_probe_result_never_carries_the_raw_body():
    with patch.object(
        runner, "_raw_completion", return_value=("not json {hf_LIVEKEY}", {})
    ):
        result = runner._probe("2. json_schema", "ask", response_format=None)
    assert result["outcome"] == "UNPARSEABLE"
    assert "hf_LIVEKEY" not in json.dumps(result)
    assert "response" not in result


def test_no_stack_trace_reaches_a_probe_record():
    clock = Clock()
    with patch.object(runner, "_raw_completion", side_effect=_status_error(429)):
        result = runner._probe("4. enums", "ask", response_format=None, sleeper=clock)
    assert "Traceback" not in json.dumps(result)
    assert "File \"" not in json.dumps(result)


# --- the eligibility rule --------------------------------------------------


def _probe_record(name, *, passed, outcome="PARSED", **extra):
    return {"probe": name, "outcome": outcome, "passed": passed, "attempts": 1, **extra}


def _stability_record(*, passed=True, malformed_rate=0.0, operational=0,
                      scheduled=33, valid=33, inconsistent=0):
    return {
        "probe": "7. stability",
        "outcome": "MEASURED" if operational == 0 else "INCOMPLETE",
        "cases": {},
        "runs_per_case": 3,
        "required_case_ids": list("ABCDEFGHIJK"),
        "measured_case_ids": list("ABCDEFGHIJK"),
        "missing_case_ids": [],
        "unknown_case_ids": [],
        "coverage_complete": True,
        "expected_calls": 33,
        "scheduled_calls": scheduled,
        "valid_responses": valid,
        "operational_failures": operational,
        "validation_failures": 0,
        "consistency_failures": inconsistent,
        "malformed_response_rate": malformed_rate,
        "consistency_failure_rate": None,
        "all_scheduled_calls_valid": operational == 0 and valid == scheduled,
        "all_cases_stable": passed,
        "passed": passed,
    }


def _probes(*, schema=True, enforcement=True, token=True, stability=None):
    return [
        _probe_record("1", passed=schema),
        _probe_record("2", passed=schema),
        _probe_record("3", passed=enforcement),
        _probe_record("4", passed=enforcement),
        _probe_record("5", passed=enforcement),
        _probe_record("6", passed=token, canonical_validation="PASSED"),
        stability if stability is not None else _stability_record(),
    ]


def _run_with(probes):
    """Drive `run()` with a fixed probe sequence and no network."""
    ordered = list(probes)
    with patch.object(runner, "_probe", side_effect=ordered[:6]), \
         patch.object(runner, "_stability_probe", return_value=ordered[6]):
        return runner.run()


def test_all_probes_passing_is_eligible():
    assert _run_with(_probes())["eligible"] is True


@pytest.mark.parametrize("failing", ["schema", "enforcement", "token"])
def test_any_failing_probe_makes_the_run_ineligible(failing):
    assert _run_with(_probes(**{failing: False}))["eligible"] is False


def test_an_operational_enforcement_probe_is_not_a_pass():
    """**The defect the owner asked to be removed.** A `REJECTED` enforcement probe
    used to be scored as a pass on the reasoning that a provider refusing a
    violation is the constraint working. A provider *error* is not a refusal to
    emit a violation, and 2026-08-28 proved the difference: five probes 'behaved'
    against a rate-limited endpoint."""
    probes = _probes()
    probes[2] = _probe_record("3", passed=False, outcome="RATE_LIMITED")
    record = _run_with(probes)
    assert record["eligible"] is False
    assert record["operational_failures"] == ["3"]


def test_an_unparseable_probe_is_not_eligible():
    probes = _probes()
    probes[1] = _probe_record("2", passed=False, outcome="UNPARSEABLE")
    assert _run_with(probes)["eligible"] is False


def test_any_malformed_response_in_the_stability_probe_is_not_eligible():
    stability = _stability_record(passed=False, malformed_rate=0.0834)
    assert _run_with(_probes(stability=stability))["eligible"] is False


# --- the 2026-08-28 all-failed run -----------------------------------------


def test_the_2026_08_28_all_failed_run_is_ineligible():
    """**Exact reproduction of the run this fix exists for.**

    Twelve HTTP 400s, every stability call failing at the transport, and the old
    scoring reported `malformed_response_rate: 0.0` and `all_cases_stable: true` —
    two passing terms from a run that measured nothing. The corrected probe must
    report it as failed, must not report a rate of 0.0 over an empty denominator,
    and must not call four cases of identical transport failures 'stable'.
    """
    failure = RuntimeError(
        "OpenAI returned HTTP 400 for model 'gpt-4o-2024-08-06': Invalid schema "
        "for response_format 'feature_disclosure_verification'."
    )
    clock = Clock()
    with patch.object(runner, "verify_feature_disclosure", side_effect=failure):
        probe = runner._stability_probe(sleeper=clock)

    assert probe["scheduled_calls"] == 33, "eleven §15.1 cases at three runs each"
    assert probe["valid_responses"] == 0
    assert probe["operational_failures"] == 33
    assert probe["malformed_response_rate"] is None, (
        "a rate over an empty denominator is undefined, not zero"
    )
    assert probe["all_cases_stable"] is False, (
        "identical transport failures agree with each other and say nothing "
        "about the model"
    )
    assert probe["all_scheduled_calls_valid"] is False
    assert probe["passed"] is False
    assert probe["outcome"] == "INCOMPLETE"
    for case in probe["cases"].values():
        assert case["stable"] is False
        assert case["valid_responses"] == 0
        assert case["operational_failures"] == 3


def test_main_writes_no_record_when_every_stability_call_failed(tmp_path):
    """The whole point: an artifact exists only when something was measured."""
    destination = tmp_path / "capability.json"
    probes = _probes(stability=_stability_record(passed=False, operational=12, valid=0,
                                                 malformed_rate=None))
    record = {**_run_with(probes)}
    with patch.object(runner, "run", return_value=record), \
         patch.object(runner, "record_path", return_value=destination), \
         patch.object(runner.config, "llm_target", _openai_target()):
        code = runner.main()
    assert code == 2
    assert not destination.exists()


def test_main_writes_no_record_when_any_probe_is_operational(tmp_path):
    destination = tmp_path / "capability.json"
    probes = _probes()
    probes[3] = _probe_record("4", passed=False, outcome="RATE_LIMITED")
    record = _run_with(probes)
    with patch.object(runner, "run", return_value=record), \
         patch.object(runner, "record_path", return_value=destination), \
         patch.object(runner.config, "llm_target", _openai_target()):
        code = runner.main()
    assert code == 2
    assert not destination.exists()


# --- the case-C behaviour that disqualified gpt-4o -------------------------


def _inconsistent_result():
    """A response whose declared outcome contradicts its own statuses.

    This is exactly what `gpt-4o-2024-08-06` returned on §15.1 case C, three runs
    out of three, on 2026-08-29: correct per-requirement statuses, and an overall
    outcome that §6 rule 5 does not derive from them.
    """
    from verification.states import CellState

    class Result:
        state = CellState.VALIDATION_FAILURE
        failed_check = "check 15"
        machine_result = None
        raw_response = json.dumps(
            {
                "feature_id": "F2", "feature_version": "3",
                "publication_number": "US1234567B2",
                "verification_scope": "SUPPLIED_PASSAGES_ONLY",
                # F > 0 and N > 0, so §6 rule 5 derives PARTIALLY.
                "outcome": "NO_DISCLOSURE_FOUND_IN_SUPPLIED_PASSAGES",
                "elements": [
                    {"element_id": "E1", "status": "FOUND",
                     "evidence": [], "combination_basis": [], "note": ""},
                    {"element_id": "E2", "status": "NOT_FOUND_IN_SUPPLIED_PASSAGES",
                     "evidence": [], "combination_basis": [], "note": ""},
                    {"element_id": "E3", "status": "NOT_FOUND_IN_SUPPLIED_PASSAGES",
                     "evidence": [], "combination_basis": [], "note": ""},
                ],
                "relationships": [
                    {"relationship_id": "R1", "status": "NOT_FOUND_IN_SUPPLIED_PASSAGES",
                     "evidence": [], "combination_basis": [], "note": ""}
                ],
                "missing_element_ids": ["E2", "E3"],
                "missing_relationship_ids": ["R1"],
                "ambiguities": [], "requests_for_more_context": [],
            }
        )

    return Result()


def test_the_former_case_c_behaviour_is_rejected():
    """**The measured gpt-4o failure, reproduced offline.**

    It must be counted as a consistency failure specifically -- not folded into a
    generic validation failure -- and it must be reported with the declared and the
    derived outcome side by side, because that pair is the whole of what the
    disagreement consists of.
    """
    clock = Clock()
    with patch.object(runner, "verify_feature_disclosure", return_value=_inconsistent_result()):
        probe = runner._stability_probe(sleeper=clock)

    assert probe["valid_responses"] == 33, "the model DID answer; these are measurements"
    assert probe["operational_failures"] == 0
    assert probe["consistency_failures"] == 33
    assert probe["validation_failures"] == 33
    assert probe["consistency_failure_rate"] == 1.0
    assert probe["passed"] is False

    run = probe["cases"]["C"]["runs"][0]
    assert run["counted_in_denominator"] is True
    assert run["failure_category"] == "OUTCOME_INCONSISTENT"
    assert run["consistency_check"] == "FAILED"
    assert run["declared_outcome"] == "NO_DISCLOSURE_FOUND_IN_SUPPLIED_PASSAGES"
    assert run["derived_outcome"] == "PARTIALLY_DISCLOSED_IN_SUPPLIED_PASSAGES"
    assert run["statuses"]["E1"] == "FOUND"


def test_one_inconsistent_run_out_of_thirty_three_makes_the_result_ineligible():
    """**No threshold.** Thirty-two clean runs and one self-contradictory one is not
    a 3% error rate to be weighed; it is a model that contradicted itself, and the
    verifier's anti-hedging check exists precisely because that must not be
    averaged away."""
    from verification.states import CellState

    class Clean:
        state = CellState.VERIFIED
        failed_check = None
        raw_response = json.dumps(
            {
                "feature_id": "F2", "feature_version": "3",
                "publication_number": "US1234567B2",
                "verification_scope": "SUPPLIED_PASSAGES_ONLY",
                "outcome": "NO_DISCLOSURE_FOUND_IN_SUPPLIED_PASSAGES",
                "elements": [
                    {"element_id": e, "status": "NOT_FOUND_IN_SUPPLIED_PASSAGES",
                     "evidence": [], "combination_basis": [], "note": ""}
                    for e in ("E1", "E2", "E3")
                ],
                "relationships": [
                    {"relationship_id": "R1", "status": "NOT_FOUND_IN_SUPPLIED_PASSAGES",
                     "evidence": [], "combination_basis": [], "note": ""}
                ],
                "missing_element_ids": ["E1", "E2", "E3"],
                "missing_relationship_ids": ["R1"],
                "ambiguities": [], "requests_for_more_context": [],
            }
        )

        class machine_result:  # noqa: N801
            outcome = "NO_DISCLOSURE_FOUND_IN_SUPPLIED_PASSAGES"

    responses = [Clean()] * 33
    responses[17] = _inconsistent_result()

    with patch.object(runner, "verify_feature_disclosure", side_effect=responses):
        probe = runner._stability_probe(sleeper=Clock())

    assert probe["valid_responses"] == 33
    assert probe["consistency_failures"] == 1
    assert probe["all_scheduled_calls_valid"] is True, "every call did return a response"
    assert probe["passed"] is False, "and one contradiction is still disqualifying"

    record = _probes(stability=probe)
    assert _run_with(record)["eligible"] is False


def test_the_probe_records_no_model_authored_prose():
    """A capability artifact is a file a human reads and shares. Statuses and
    outcomes are closed vocabularies; notes, quotations and any reasoning the model
    volunteered are not, and quoted patent text does not belong here either."""
    with patch.object(runner, "verify_feature_disclosure", return_value=_inconsistent_result()):
        probe = runner._stability_probe(sleeper=Clock())

    rendered = json.dumps(probe)
    for forbidden in ("exact_quote", "note", "combination_basis", "heat exchanger"):
        assert forbidden not in rendered, forbidden


# --- the record describes the runtime it covers ----------------------------


def test_the_record_names_the_runtime_it_covers():
    """§14.2.1 plus the 2026-08-29 amendment: a capability result is scoped to an
    exact runtime identity, and a record that cannot say what it tested is not a
    gate."""
    from verification.capability import IDENTITY_FIELDS

    record = _run_with(_probes())
    identity = record["runtime_identity"]
    for name in IDENTITY_FIELDS:
        assert name in identity, f"{name} must be recorded"
    assert identity["deployment_profile"]
    assert identity["provider"]
    assert identity["schema_version"].startswith("sha256:")
    assert identity["wire_schema_version"].startswith("sha256:")
    assert identity["validator_version"].startswith("sha256:")
    assert identity["runner_version"]
    assert "model_revision" in identity
    assert record["structured_output_setting"]
    assert "malformed_response_rate" in record
    assert "token_limit_result" in record
    assert "model_pinned" in record
    assert record["run_at"]


def test_the_record_describes_both_schema_layers():
    """The amendment made the wire rendering part of what was measured, so the
    artifact has to say what was sent and what is enforced locally."""
    layers = _run_with(_probes())["schema_layers"]
    assert layers["canonical"].startswith("sha256:")
    assert layers["wire_sent_to_provider"].startswith("sha256:")
    assert layers["canonical_validator"].startswith("sha256:")
    assert set(layers["wire_omitted_keywords"]) == {
        "minLength", "minItems", "maxItems", "uniqueItems"
    }
    assert layers["wire_strict_violations"] == []


def test_the_recorded_endpoint_is_parsed_parts_not_a_url_string():
    identity = _run_with(_probes())["runtime_identity"]
    assert isinstance(identity["endpoint"], dict)
    assert set(identity["endpoint"]) == {"scheme", "host", "port", "path"}


def test_the_record_states_that_it_qualifies_only_this_runtime():
    record = _run_with(_probes())
    assert "qualifies the runtime_identity above and NOTHING else" in record["note"]
    assert "qualified independently" in record["note"]


def test_the_record_states_that_it_does_not_settle_the_generic_json_decision():
    assert "NOT settled" in _run_with(_probes())["note"]


# --- an untaken measurement writes nothing ---------------------------------


def test_main_writes_no_record_when_the_gateway_is_unreachable(tmp_path):
    destination = tmp_path / "structured-output-capability-2026-08-28.json"
    with patch.object(runner, "run", side_effect=runner.GatewayUnreachable("DNS")), \
         patch.object(runner, "record_path", return_value=destination):
        code = runner.main()

    assert code == 2, "a distinct exit code: not eligible-but-tested, not success"
    assert not destination.exists(), "an untaken measurement must leave no artifact"


def test_main_writes_a_record_and_signals_failure_when_a_model_is_ineligible(tmp_path):
    """Ineligible is a *result*: it is recorded, and the exit code differs from
    the not-run one so a caller can tell them apart."""
    destination = tmp_path / "capability.json"
    record = _run_with(_probes(enforcement=False))
    with patch.object(runner, "run", return_value=record), \
         patch.object(runner, "record_path", return_value=destination), \
         patch.object(runner.config, "llm_target", _openai_target()):
        code = runner.main()

    assert code == 1
    assert json.loads(destination.read_text(encoding="utf-8"))["eligible"] is False


def test_the_record_path_names_the_profile_and_is_dated():
    from verification.capability import RuntimeIdentity

    identity = RuntimeIdentity.for_target(_openai_target())
    path = runner.record_path(
        {"runtime_identity": identity.as_dict()},
        datetime(2026, 8, 28, tzinfo=timezone.utc),
    )
    assert path.name.startswith("structured-output-capability-local-")
    assert path.name.endswith("-2026-08-28.json")
    assert path.parent.name == "measurements"


def test_the_stub_profile_cannot_produce_an_artifact():
    """**A mock must never qualify a runtime.** Under `test` there is no provider
    to probe, so the gate refuses before it can measure anything."""
    from deployment import LLMTarget, Profile, Provider

    stub = LLMTarget(Profile.TEST, Provider.STUB, "", "stub")
    with patch.object(runner.config, "llm_target", stub), \
         patch.object(runner, "run") as ran:
        code = runner.main()
    assert code == 2
    ran.assert_not_called()


def test_a_missing_credential_is_never_recorded_as_a_provider_outcome():
    """The same defect class as the DNS case, found by running the gate with no
    key set: an unconfigured machine must not record six provider outcomes and
    certify a model it never contacted."""
    from deployment import MissingCredential

    with patch.object(runner, "_raw_completion", side_effect=MissingCredential("no key")):
        with pytest.raises(MissingCredential):
            runner._probe("3. required fields", "ask", response_format=None)


def test_main_writes_nothing_when_the_credential_is_absent(tmp_path):
    from deployment import MissingCredential

    destination = tmp_path / "capability.json"
    with patch.object(runner, "run", side_effect=MissingCredential("OPENAI_API_KEY is not set")), \
         patch.object(runner, "record_path", return_value=destination), \
         patch.object(runner.config, "llm_target", _openai_target()):
        code = runner.main()
    assert code == 2
    assert not destination.exists()


# --- the runner stays out of the application -------------------------------


HUMAN_RUN_GATES = {"capability_test.py", "diagnose.py"}
"""Scripts a human runs deliberately, never a request path.

Both spend real quota per invocation. `diagnose.py` imports `capability_test`'s
classification and retry primitives on purpose — one definition of "what is an
operational failure" is the whole point — so the exemption is a *set*, named here,
rather than a single filename that quietly stopped covering everything."""


def test_nothing_in_the_application_imports_a_human_run_gate():
    """They are gates a human runs, not runtime checks. An application that
    imported one could call it on a request path and spend quota per run.

    Both are checked, and they are allowed to import each other: sharing the
    classification vocabulary is what keeps the cheap probe and the full gate
    from disagreeing about what a failure is.
    """
    offenders = []
    for path in BACKEND.rglob("*.py"):
        if any(part in {".venv", "__pycache__", "tests"} for part in path.parts):
            continue
        if path.name in HUMAN_RUN_GATES:
            continue
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith(("import ", "from ")):
                continue
            if "capability_test" in stripped or "verification.diagnose" in stripped:
                offenders.append(f"{path.relative_to(BACKEND)}: {stripped}")
    assert offenders == [], f"a human-run gate is imported by the app: {offenders}"


def test_the_runner_does_not_import_the_test_tree():
    """It ships inside the package, so importing `tests.` from it would put the
    test tree on a production import path."""
    source = (BACKEND / "verification" / "capability_test.py").read_text(encoding="utf-8")
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            assert not stripped.startswith(("import tests", "from tests")), stripped


def test_the_gate_and_the_offline_suite_run_the_same_cases():
    """**There is one copy of the §15.1 data now, and this is what proves it.**

    The gate used to carry a trimmed four-case copy of the fixture, because nothing
    in the application package may import the test tree. Two copies drift, and the
    drift is invisible: the gate would measure a model against passages the offline
    suite never pins. The definitions moved into
    `verification/regression_cases.py`, and `tests/fixtures_verification.py` is now
    a re-export of them.
    """
    from tests import fixtures_verification as fx
    from verification.regression_cases import DECOMPOSITION, VERIFIER_CASES

    assert fx.DECOMPOSITION is DECOMPOSITION
    assert fx.CANONICAL_PASSAGES is not None
    assert fx.VERIFIER_CASES is VERIFIER_CASES

    assert DECOMPOSITION.feature_id == "F2"
    assert DECOMPOSITION.element_ids == ("E1", "E2", "E3")
    assert DECOMPOSITION.relationship_ids == ("R1",)
    # Requirement ids are unique ACROSS elements and relationships -- the same
    # rule `decomposition_requirement_ids` enforces in the database.
    assert len(set(DECOMPOSITION.element_ids + DECOMPOSITION.relationship_ids)) == 4


def test_the_stability_probe_passes_no_retrieval_floor():
    """§16 decision 6 blocks the floor on the Qdrant/Qwen3 recalibration. A
    guessed floor here would drop passages and report `NOT_EVALUATED` for an
    evaluation that never happened."""
    source = (BACKEND / "verification" / "capability_test.py").read_text(encoding="utf-8")
    body = source.split("def _stability_probe", 1)[1]
    assert "retrieval_floor=None" in body
    assert "retrieval_floor=0" not in body


def test_the_stability_probe_covers_every_specification_case():
    """Guarded here as well as in `test_stability_coverage.py`, because this file
    is where someone edits the probe."""
    from verification.regression_cases import CASE_IDS

    assert list(CASE_IDS) == list("ABCDEFGHIJK")
    assert len(CASE_IDS) * runner.STABILITY_RUNS == 33


def test_probe_6_sends_the_wire_schema_production_sends():
    """A gate that probed a different schema would certify something the
    application never does."""
    source = (BACKEND / "verification" / "capability_test.py").read_text(encoding="utf-8")
    body = source.split("def run(", 1)[1]
    assert "provider_response_format(" in source
    assert "response_format=wire_format" in body
