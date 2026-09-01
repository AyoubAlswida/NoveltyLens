"""Inference settings are part of the runtime a capability artifact qualifies.

**The measurement that made this necessary.** On 2026-08-29
`gpt-5-mini-2025-08-07` was measured **ineligible**: §15.1 case E returned
`UNCLEAR`, `PARTIALLY`, `PARTIALLY` across three repeats, with `reasoning_effort`
unset. The owner then set it to `high`.

Those are two different runtimes. They send different requests and the model does
different work. Before `inference_config_fingerprint` existed, an artifact from
the first would have matched the second on **every** identity field — same
profile, provider, endpoint, model, adapter, schema, validator, runner — while
describing a configuration nobody had tested. That is the exact failure mode the
whole identity mechanism exists to prevent, one field short.

So every setting capable of changing what the verifier returns is in the
fingerprint, each is mutated independently here, and each must break admission on
its own.

**No provider default is inherited silently.** `reasoning_effort=""` and
`temperature_sent=False` are *values*, not gaps: they record that the provider
chose, which is a different runtime from one where we chose.

No test here makes a network call.
"""

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from deployment import (
    CLASSIC_DIALECT,
    ConfigError,
    InferenceConfig,
    LLMTarget,
    Profile,
    Provider,
    REASONING_EFFORTS,
    REASONING_EFFORT_UNSET,
    dialect_for,
    read_reasoning_effort,
)
from llm import OPENAI_ADAPTER_BEHAVIOUR_VERSION, build_chat_payload, inference_config
from verification.capability import (
    IDENTITY_FIELDS,
    CapabilityNotQualified,
    RuntimeIdentity,
    require_capability,
)

MODEL = "gpt-5-mini-2025-08-07"


def target(model: str = MODEL) -> LLMTarget:
    return LLMTarget(Profile.LOCAL, Provider.OPENAI, "https://api.openai.com/v1", model)


@pytest.fixture
def high(monkeypatch):
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "high")


def payload(model: str = MODEL, **kwargs) -> dict:
    return build_chat_payload(
        target(model),
        [{"role": "user", "content": "x"}],
        max_tokens=kwargs.pop("max_tokens", 1940),
        temperature=kwargs.pop("temperature", 0.0),
    )


# --- the setting is validated, fail closed, before any call ---------------


def test_the_allowed_values_are_a_closed_vocabulary():
    """The union of what the declared dialects accept, derived from them.

    `none` is `gpt-5.1`'s and `gpt-5.4-mini`'s, `minimal` is `gpt-5`'s, `xhigh` is
    `gpt-5.4-mini`'s alone, and no family accepts every one of them -- which is why
    passing this check is not permission to send the value, only permission to ask
    the model's own dialect about it.

    Pinned as a literal on purpose. The tuple is *derived* from `MODEL_DIALECTS`,
    so a family added or removed moves it silently; spelling it out here makes
    that a visible change rather than an invisible one.
    """
    assert REASONING_EFFORTS == ("none", "minimal", "low", "medium", "high", "xhigh")


@pytest.mark.parametrize("value", ["turbo", "HIGHEST", "1", "true", "max"])
def test_an_unknown_value_fails_before_the_provider_call(value):
    """A typo must not become "the provider's default, silently"."""
    with pytest.raises(ConfigError) as raised:
        read_reasoning_effort({"OPENAI_REASONING_EFFORT": value})
    assert "not a supported value" in str(raised.value)


def test_an_unset_value_is_its_own_value_not_an_error():
    assert read_reasoning_effort({}) == REASONING_EFFORT_UNSET
    assert read_reasoning_effort({"OPENAI_REASONING_EFFORT": "  "}) == REASONING_EFFORT_UNSET


def test_the_value_is_normalized_but_not_guessed_at():
    assert read_reasoning_effort({"OPENAI_REASONING_EFFORT": " High "}) == "high"


def test_a_family_that_cannot_honour_it_refuses_rather_than_dropping_it(monkeypatch):
    """**Refused, not dropped.** A dropped setting runs at a different effort than
    the operator believes, and the artifact would then describe a runtime nobody
    tested."""
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "high")
    with pytest.raises(ConfigError) as raised:
        payload("gpt-4o-2024-08-06")
    assert "does not accept" in str(raised.value)


# --- the production request carries it ------------------------------------


def test_the_production_request_contains_reasoning_effort_high(high):
    assert payload()["reasoning_effort"] == "high"


def test_it_is_sent_as_a_parameter_and_never_emulated_in_the_prompt(high):
    """Asking a model to "think harder" in its instructions is a different
    intervention with a different failure mode, and it would change a prompt that
    is byte-pinned to the specification."""
    from verification.prompt import SYSTEM_PROMPT

    for word in ("reasoning_effort", "think harder", "reason carefully", "high effort"):
        assert word not in SYSTEM_PROMPT.lower()

    built = payload()
    assert built["reasoning_effort"] == "high"
    assert built["messages"] == [{"role": "user", "content": "x"}]


def test_nothing_else_about_the_request_changed(high):
    """Temperature behaviour, the token parameter and the ceiling are untouched by
    the reasoning setting."""
    built = payload()
    assert "temperature" not in built, "this family still rejects an explicit value"
    assert built["max_completion_tokens"] == 1940 + dialect_for(MODEL).reasoning_token_allowance
    assert "top_p" not in built and "seed" not in built


def test_the_capability_runner_uses_the_production_payload_builder():
    """A gate with its own payload would certify a request the application cannot
    make — and would have missed `reasoning_effort` entirely."""
    source = (
        Path(__file__).resolve().parent.parent / "verification" / "capability_test.py"
    ).read_text(encoding="utf-8")
    assert "build_chat_payload(" in source
    assert '"reasoning_effort"' not in source, "the gate must not set it independently"


def test_the_diagnostic_uses_the_production_verifier_path():
    source = (
        Path(__file__).resolve().parent.parent / "verification" / "diagnose.py"
    ).read_text(encoding="utf-8")
    assert "verify_feature_disclosure(" in source
    assert '"reasoning_effort"' not in source


# --- the fingerprint covers every behaviour-affecting setting -------------


def active(**overrides) -> InferenceConfig:
    base = inference_config(target(), max_tokens=1940, temperature=0.0)
    return replace(base, **overrides)


def test_the_active_identity_records_high_reasoning(high):
    config_now = inference_config(target(), max_tokens=1940, temperature=0.0)
    assert config_now.reasoning_effort == "high"
    assert config_now.as_dict()["reasoning_effort_configured"] is True


def test_an_unset_effort_is_recorded_as_a_value_not_an_absence(monkeypatch):
    monkeypatch.delenv("OPENAI_REASONING_EFFORT", raising=False)
    rendered = inference_config(target(), max_tokens=1940, temperature=0.0).as_dict()
    assert rendered["reasoning_effort"] == "(unset - provider default)"
    assert rendered["reasoning_effort_configured"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("reasoning_effort", "low"),
        ("reasoning_effort", "medium"),
        ("reasoning_effort", "minimal"),
        ("reasoning_effort", ""),
        ("temperature_sent", True),
        ("temperature", 0.7),
        ("token_parameter", "max_tokens"),
        ("effective_token_ceiling", 4096),
        ("dialect_version", "{}"),
        ("timeout_floor_seconds", 60),
        ("top_p", 0.9),
        ("seed", 7),
    ],
)
def test_every_inference_field_moves_the_fingerprint_on_its_own(high, field, value):
    """Mutated **independently**: a fingerprint that only moved when two fields
    changed together would let either one change alone go unnoticed."""
    base = active()
    assert getattr(base, field) != value, f"{field} test value must differ from the active one"
    assert replace(base, **{field: value}).fingerprint != base.fingerprint


def test_the_fingerprint_is_stable_for_an_unchanged_configuration(high):
    assert active().fingerprint == active().fingerprint


# --- admission compares it ------------------------------------------------


def test_the_fingerprint_is_an_identity_field():
    assert "inference_config_fingerprint" in IDENTITY_FIELDS


def artifact(tmp_path: Path, identity: RuntimeIdentity, *, eligible: bool = True) -> Path:
    path = tmp_path / "structured-output-capability-local-abc-2026-08-29.json"
    path.write_text(
        json.dumps({"runtime_identity": identity.as_dict(), "eligible": eligible}),
        encoding="utf-8",
    )
    return path


def test_a_matching_high_reasoning_artifact_is_admitted(high, tmp_path):
    """The positive control. Without it every refusal below could be refusing for
    an unrelated reason."""
    identity = RuntimeIdentity.for_target(target(), model_revision=MODEL)
    artifact(tmp_path, identity)
    assert require_capability(target(), directory=tmp_path)["eligible"] is True


@pytest.mark.parametrize("other_effort", ["", "low", "medium", "minimal"])
def test_an_artifact_measured_at_another_effort_cannot_qualify_high(
    high, tmp_path, monkeypatch, other_effort
):
    """**The central rule.** An artifact from an unset or lower effort describes a
    different distribution of answers, and the case-E measurement is what proves
    those distributions differ in a way that decides eligibility."""
    if other_effort:
        monkeypatch.setenv("OPENAI_REASONING_EFFORT", other_effort)
    else:
        monkeypatch.delenv("OPENAI_REASONING_EFFORT", raising=False)
    stale = RuntimeIdentity.for_target(target(), model_revision=MODEL)
    artifact(tmp_path, stale)

    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "high")
    with pytest.raises(CapabilityNotQualified) as raised:
        require_capability(target(), directory=tmp_path)
    assert "inference_config_fingerprint" in "; ".join(raised.value.reasons)


def test_a_high_reasoning_artifact_cannot_qualify_a_default_configuration(
    high, tmp_path, monkeypatch
):
    """Symmetric, and it has to be. If only one direction were checked, dropping
    the setting in production would silently inherit a qualification made with it."""
    qualified = RuntimeIdentity.for_target(target(), model_revision=MODEL)
    artifact(tmp_path, qualified)

    monkeypatch.delenv("OPENAI_REASONING_EFFORT", raising=False)
    with pytest.raises(CapabilityNotQualified) as raised:
        require_capability(target(), directory=tmp_path)
    assert "inference_config_fingerprint" in "; ".join(raised.value.reasons)


def test_an_artifact_predating_the_inference_identity_qualifies_nothing():
    """The 2026-08-29 ineligible records have no `inference_config_fingerprint`
    key. They stay on disk, stay accurate, and cannot be read as an identity."""
    from verification.capability import _identity_from_record

    older = {
        "runtime_identity": {
            "deployment_profile": "local", "provider": "openai",
            "endpoint": {"scheme": "https", "host": "api.openai.com", "port": "", "path": "/v1"},
            "model": MODEL, "model_revision": MODEL,
            "adapter_revision": "openai-python/3.5.0+behaviour/3",
            "schema_version": "sha256:cee4a3a1caa7df7e",
            "wire_schema_version": "sha256:f9c0f9f1e3747938",
            "validator_version": "sha256:a89ff8e7acd83c7c",
            "runner_version": "4",
        }
    }
    assert _identity_from_record(older) is None


def test_the_previously_measured_ineligible_artifacts_still_refuse(high):
    """Against the real measurements directory: every artifact on disk is either
    unreadable as an identity or records `eligible: false`."""
    with pytest.raises(CapabilityNotQualified):
        require_capability(target())


# --- the adapter revision moved with the behaviour ------------------------


def test_the_adapter_behaviour_version_moved():
    """`reasoning_effort` changes what is sent, so it changes the adapter."""
    assert OPENAI_ADAPTER_BEHAVIOUR_VERSION == "6"


def test_no_model_or_provider_fallback_exists():
    """Grep, because a fallback is added in one line and removes the whole
    guarantee."""
    root = Path(__file__).resolve().parent.parent
    for name in ("llm.py", "deployment.py", "verification/capability_test.py",
                 "verification/diagnose.py"):
        text = (root / name).read_text(encoding="utf-8")
        lowered = text.lower()
        for phrase in ("fallback_model", "except.*try another model", "alternate_provider"):
            assert phrase not in lowered, f"{name} mentions {phrase}"


def test_the_stub_profile_has_its_own_constant_fingerprint():
    """A `test` artifact must not collide with a real runtime's fingerprint."""
    from verification.capability import inference_config_fingerprint

    stub = LLMTarget(Profile.TEST, Provider.STUB, "", "stub")
    assert inference_config_fingerprint(stub) == "sha256:stub"
    assert inference_config_fingerprint(stub) != inference_config_fingerprint(target())
