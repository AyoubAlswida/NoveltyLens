"""The Chat Completions dialect each model family actually accepts.

**Every assertion here exists to catch a deterministic HTTP 400 offline, before a
paid call.** The reasoning families reject `max_tokens` outright and reject an
explicit `temperature`, so a request built for `gpt-4o` fails against
`gpt-5-mini-2025-08-07` on the first call and every call after it — which is
exactly the shape of the 2026-08-28 blocker, where a deterministic schema
rejection was discovered one paid round trip at a time.

**Declared, never probed.** These are documented properties of the provider's API.
Trying a request and adapting to the rejection would be a runtime fallback that
silently changes what was sent, which is the thing the zero-tolerance policy
forbids; a wrong dialect fails loudly with the provider naming the parameter.

No test here makes a network call.
"""

import pytest

from deployment import (
    CLASSIC_DIALECT,
    OPENAI_DEFAULT_MODEL,
    LLMTarget,
    Profile,
    Provider,
    dialect_for,
)
from llm import OPENAI_ADAPTER_BEHAVIOUR_VERSION, build_chat_payload


def target(model: str) -> LLMTarget:
    return LLMTarget(Profile.LOCAL, Provider.OPENAI, "https://api.openai.com/v1", model)


def payload(model: str, *, max_tokens: int = 1680, temperature: float = 0.0) -> dict:
    return build_chat_payload(
        target(model),
        [{"role": "user", "content": "x"}],
        max_tokens=max_tokens,
        temperature=temperature,
    )


# --- the selected model ----------------------------------------------------


def test_the_selected_local_model_is_the_owner_approved_pinned_snapshot():
    """Owner decision, 2026-08-31. A floating alias would let the provider move the
    revision under a capability artifact that still matched on every field.

    The selection has moved three times and each move is a recorded decision, not
    a search loop: gpt-4o, then gpt-5-mini, then gpt-5.1, now gpt-5.4-mini. Every
    superseded measurement stays on disk and qualifies nothing."""
    assert OPENAI_DEFAULT_MODEL == "gpt-5.4-mini-2026-03-17"
    assert target(OPENAI_DEFAULT_MODEL).model_is_pinned


# --- reasoning families ----------------------------------------------------


@pytest.mark.parametrize(
    "model",
    ["gpt-5-mini-2025-08-07", "gpt-5", "gpt-5-nano", "o1-preview", "o3-mini", "o4-mini"],
)
def test_reasoning_models_use_max_completion_tokens(model):
    """`max_tokens` is not accepted as an alias on these families; it is rejected."""
    built = payload(model)
    assert "max_completion_tokens" in built
    assert "max_tokens" not in built


@pytest.mark.parametrize("model", ["gpt-5-mini-2025-08-07", "o3-mini"])
def test_reasoning_models_receive_no_temperature_at_all(model):
    """**Omitted, never rewritten.** Sending `temperature: 1` would be this code
    choosing a sampling setting on the caller's behalf and recording nothing about
    it; omitting leaves the provider's own default in force, and the capability
    artifact says so."""
    assert "temperature" not in payload(model, temperature=0.0)


def test_the_reasoning_allowance_is_added_to_the_callers_budget_not_substituted():
    """Reasoning tokens never appear in `content` and still consume the completion
    ceiling, so a budget sized for the answer alone truncates every call. The
    allowance may only ever increase what the caller asked for."""
    dialect = dialect_for("gpt-5-mini-2025-08-07")
    assert dialect.reasoning_token_allowance > 0
    built = payload("gpt-5-mini-2025-08-07", max_tokens=1680)
    assert built["max_completion_tokens"] == 1680 + dialect.reasoning_token_allowance
    assert built["max_completion_tokens"] > 1680


def test_reasoning_models_get_a_timeout_floor_and_never_a_reduction():
    """A timeout tuned for a non-reasoning model turns a working model into a read
    timeout, which the gate correctly classifies UNREACHABLE -- an operational
    failure that measures nothing and abandons the whole record. Waiting longer for
    a slow answer is not a degradation; recording "could not be reached" about a
    provider that was answering would be."""
    assert dialect_for("gpt-5-mini-2025-08-07").minimum_timeout_seconds == 180
    assert dialect_for("gpt-4o-2024-08-06").minimum_timeout_seconds == 0
    assert max(300, dialect_for("gpt-5-mini-2025-08-07").minimum_timeout_seconds) == 300


def test_reasoning_effort_is_configuration_and_never_a_code_default(monkeypatch):
    """It trades answer quality against cost, which is a decision with an owner.

    So it is read from the environment and never defaulted in code. Unset, the
    parameter is absent and the provider's own default is in force — which is a
    *recorded* configuration (`reasoning_effort: ""` in the inference identity),
    not an unrepresented gap. The owner set it to `high` on 2026-08-29; that is a
    line in `.env`, not a constant here.
    """
    monkeypatch.delenv("OPENAI_REASONING_EFFORT", raising=False)
    assert "reasoning_effort" not in payload("gpt-5-mini-2025-08-07")

    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "high")
    assert payload("gpt-5-mini-2025-08-07")["reasoning_effort"] == "high"


def test_no_other_sampling_setting_is_ever_sent(monkeypatch):
    """`verbosity`, `top_p` and `seed` are unset and unrepresented in the request.
    They are fields of `InferenceConfig` so that sending one later cannot happen
    without moving the fingerprint."""
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "high")
    built = payload("gpt-5-mini-2025-08-07")
    for parameter in ("verbosity", "top_p", "seed", "frequency_penalty"):
        assert parameter not in built


# --- classic families ------------------------------------------------------


@pytest.mark.parametrize("model", ["gpt-4o-2024-08-06", "gpt-4o-mini", "minimaxm3"])
def test_classic_models_keep_the_classic_shape(model, monkeypatch):
    """The measured `gpt-4o` runs used this shape, so a change here would
    invalidate what those artifacts describe."""
    monkeypatch.delenv("OPENAI_REASONING_EFFORT", raising=False)
    built = payload(model, max_tokens=1680, temperature=0.0)
    assert built["max_tokens"] == 1680
    assert built["temperature"] == 0.0
    assert "max_completion_tokens" not in built


def test_an_unlisted_model_gets_the_classic_dialect_and_fails_loudly_if_wrong():
    """Not a guess dressed as a fallback: if a future model rejects this shape the
    provider returns a deterministic 400 naming the parameter, the capability gate
    classifies it `BAD_REQUEST`, writes no artifact and exits 2."""
    assert dialect_for("some-future-model-2030") is CLASSIC_DIALECT
    assert dialect_for("") is CLASSIC_DIALECT


def test_the_gateway_provider_keeps_the_classic_shape():
    """`aramco` is deferred and its model has never been probed. Nothing here may
    claim a dialect for it."""
    gateway = LLMTarget(
        Profile.ARAMCO, Provider.OPENAI_COMPATIBLE, "https://gw.example/v1", "gpt-5-lookalike"
    )
    assert gateway.chat_dialect is CLASSIC_DIALECT


# --- the dialect is part of the adapter identity --------------------------


def test_the_adapter_behaviour_version_moved_with_the_dialect():
    """The shape of the request is half of what §14.2.1 measures, so a dialect
    change has to invalidate every prior capability artifact.

    v3 introduced the dialect; v4 sends `reasoning_effort`; v5 adds the `gpt-5.1`
    dialect and checks the effort against the configured model's own vocabulary;
    v6 adds the `gpt-5.4-mini` dialect and makes prefix matching respect a version
    boundary, so a version this table has not declared can no longer inherit a
    neighbour's reasoning vocabulary, allowance and timeout floor in silence.
    """
    assert OPENAI_ADAPTER_BEHAVIOUR_VERSION == "6"


def test_the_adapter_revision_carries_the_behaviour_version():
    from verification.capability import adapter_revision

    revision = adapter_revision(target("gpt-5-mini-2025-08-07"))
    assert "openai-python/" in revision
    assert f"+behaviour/{OPENAI_ADAPTER_BEHAVIOUR_VERSION}" in revision


def test_the_capability_gate_builds_its_request_the_way_production_does():
    """A gate with its own hand-built payload would have certified a request shape
    the application cannot make — and against a reasoning model it would have been
    the *wrong* shape, failing deterministically."""
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "verification" / "capability_test.py")
    text = source.read_text(encoding="utf-8")
    assert "build_chat_payload(" in text
    assert '"max_tokens": max_tokens' not in text, "the gate must not hand-build a payload"


# --- structured output is unchanged by any of this ------------------------


def test_the_dialect_never_touches_the_response_format():
    """§14.1: no downgrade to generic JSON, no provider-specific relaxation. The
    dialect adapts parameter *names* and nothing about the output contract."""
    built = payload("gpt-5-mini-2025-08-07")
    assert "response_format" not in built  # added by the caller, never by the dialect

    from verification.schema import provider_response_format
    from verification.wire import openai_strict_violations

    sent = provider_response_format()
    assert sent["json_schema"]["strict"] is True
    assert openai_strict_violations(sent["json_schema"]["schema"]) == []
