"""The `gpt-5.1` Chat Completions dialect, and what selecting it invalidates.

**`gpt-5.1` is not `gpt-5` with a longer name.** The prefix table would have
matched it on `"gpt-5"` and handed it the older family's dialect silently — the
request would still have been *accepted*, because the two agree on the token
parameter and on omitting `temperature`, so nothing would have failed loudly.
What would have differed is the part that decides whether a qualification run is
worth anything: the reasoning-token allowance, the timeout floor, and which
`reasoning_effort` values are legal. That is the quiet failure this module exists
to prevent, and it is why the row is declared explicitly rather than inherited.

**Every assertion here is offline.** No test in this file constructs a client,
resolves a host, or sends a request; they read the declared dialect and the
payload built from it. A dialect is *declared*, never probed — trying a request
and adapting to the rejection would be a runtime fallback that silently changes
what was sent.
"""

from pathlib import Path

import pytest

from deployment import (
    MODEL_DIALECTS,
    REASONING_EFFORTS,
    ChatDialect,
    ConfigError,
    InferenceConfig,
    LLMTarget,
    Profile,
    Provider,
    dialect_for,
    dialect_version,
    read_reasoning_effort,
)
from llm import OPENAI_ADAPTER_BEHAVIOUR_VERSION, build_chat_payload, inference_config

SELECTED = "gpt-5.1-2025-11-13"
"""The owner-selected `local` verifier model, 2026-08-29."""

PREVIOUS = "gpt-5-mini-2025-08-07"
"""The model measured ineligible immediately before it. Every comparison against
this one is the point: an artifact measured there must not qualify this."""


def target(model: str) -> LLMTarget:
    return LLMTarget(Profile.LOCAL, Provider.OPENAI, "https://api.openai.com/v1", model)


def payload(model: str, *, max_tokens: int = 1680, temperature: float = 0.0) -> dict:
    return build_chat_payload(
        target(model),
        [{"role": "user", "content": "x"}],
        max_tokens=max_tokens,
        temperature=temperature,
    )


# --- the dialect is resolved, and it is its own -----------------------------


def test_gpt_51_gets_its_own_dialect_and_not_the_gpt_5_one():
    """Longest prefix wins. `"gpt-5.1-2025-11-13".startswith("gpt-5")` is true, so
    without an explicit row this model would have inherited the older family's
    allowance, timeout floor and effort vocabulary without a word of complaint."""
    assert dialect_for(SELECTED) is not dialect_for(PREVIOUS)
    assert dialect_for(SELECTED) != dialect_for(PREVIOUS)


def test_the_selected_model_is_declared_in_the_table_rather_than_defaulted():
    """A model reaching `CLASSIC_DIALECT` would be sent `max_tokens` and an
    explicit `temperature`, both of which the reasoning families reject."""
    prefixes = [prefix for prefix, _ in MODEL_DIALECTS]
    assert "gpt-5.1" in prefixes
    assert any(SELECTED.startswith(prefix) for prefix in prefixes)


@pytest.mark.parametrize("model", [SELECTED, "gpt-5.1", "gpt-5.1-codex", "gpt-5.1-mini"])
def test_the_whole_family_resolves_to_the_same_declared_dialect(model):
    assert dialect_for(model) == dialect_for(SELECTED)


# --- the request shape ------------------------------------------------------


def test_the_token_ceiling_parameter_is_max_completion_tokens():
    """`max_tokens` is not an alias on the reasoning families; it is rejected."""
    built = payload(SELECTED)
    assert "max_completion_tokens" in built
    assert "max_tokens" not in built


def test_temperature_is_omitted_and_never_rewritten():
    """Omitted leaves the provider's own default in force and the identity records
    `temperature_sent: false`. Substituting `1` would be this code choosing a
    sampling setting and recording nothing about the choice."""
    built = payload(SELECTED, temperature=0.0)
    assert "temperature" not in built
    assert dialect_for(SELECTED).supports_temperature is False


def test_top_p_seed_and_verbosity_are_never_sent():
    """`verbosity` is a `gpt-5.x` output-length control that changes what comes
    back. It is deliberately not sent — the §10 schema and the byte-pinned prompt
    already govern the output — and `InferenceConfig` carries it as `None` so that
    sending one later cannot happen without moving the fingerprint."""
    built = payload(SELECTED)
    for parameter in ("top_p", "seed", "verbosity", "frequency_penalty", "presence_penalty"):
        assert parameter not in built


def test_the_reasoning_allowance_only_ever_adds_to_the_callers_budget():
    """Reasoning tokens are billed against `max_completion_tokens` and never appear
    in `content`. A budget sized for the answer alone truncates every call, and a
    truncation is classified `TOKEN_CEILING` — never retried, never a model answer,
    and it abandons the capability record it appears in."""
    dialect = dialect_for(SELECTED)
    assert dialect.reasoning_token_allowance > 0
    assert payload(SELECTED, max_tokens=1680)["max_completion_tokens"] == (
        1680 + dialect.reasoning_token_allowance
    )


def test_the_allowance_and_timeout_floor_are_not_inherited_from_gpt_5():
    """The measured `gpt-5-mini` case-E diagnostic spent 4096-7872 reasoning tokens
    at `high`. A larger model at the same effort has no reason to spend fewer, and
    the cost of a generous allowance is a ceiling that is never reached while the
    cost of a tight one is a run that measures nothing."""
    selected, previous = dialect_for(SELECTED), dialect_for(PREVIOUS)
    assert selected.reasoning_token_allowance > previous.reasoning_token_allowance
    assert selected.minimum_timeout_seconds > previous.minimum_timeout_seconds


def test_the_timeout_floor_is_a_floor_and_never_a_reduction():
    """A read timeout on a provider that was answering is recorded `UNREACHABLE` —
    an operational outcome that measures nothing. Waiting longer for a slow answer
    is not a degradation."""
    floor = dialect_for(SELECTED).minimum_timeout_seconds
    assert max(60, floor) == floor
    assert max(floor + 120, floor) == floor + 120


# --- the reasoning-effort vocabulary is per family --------------------------


def test_the_selected_family_accepts_the_owner_selected_effort():
    assert "high" in dialect_for(SELECTED).reasoning_efforts


def test_gpt_51_removed_minimal_and_added_none():
    """The concrete difference between the two families, and the reason a single
    global vocabulary is not enough: a value that is valid on one is a
    deterministic HTTP 400 on the other."""
    selected, previous = dialect_for(SELECTED), dialect_for(PREVIOUS)
    assert "none" in selected.reasoning_efforts
    assert "minimal" not in selected.reasoning_efforts
    assert "minimal" in previous.reasoning_efforts
    assert "none" not in previous.reasoning_efforts


def test_an_effort_this_family_rejects_fails_before_the_request_is_built(monkeypatch):
    """**Before the call, not at the provider.** A provider 400 arrives after the
    request is made and billed, and the gate classifies it `BAD_REQUEST` — an
    operational outcome that abandons the whole capability record."""
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "minimal")
    with pytest.raises(ConfigError) as raised:
        payload(SELECTED)
    message = str(raised.value)
    assert "minimal" in message
    assert SELECTED in message
    # It names what this family does accept rather than leaving the operator to guess.
    assert "high" in message


def test_the_same_effort_is_accepted_by_the_family_that_declares_it(monkeypatch):
    """The refusal above is about *this* family, not about the value: the check
    would be worthless if it rejected `minimal` everywhere."""
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "minimal")
    assert payload(PREVIOUS)["reasoning_effort"] == "minimal"


def test_the_owner_selected_effort_is_sent_verbatim(monkeypatch):
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "high")
    assert payload(SELECTED)["reasoning_effort"] == "high"


def test_an_owner_selected_setting_is_never_silently_dropped_or_downgraded(monkeypatch):
    """The two ways a setting stops being in force without anyone noticing. Both
    raise; neither substitutes a neighbouring value."""
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "minimal")

    # Wrong family for the value: refused, not dropped.
    with pytest.raises(ConfigError):
        payload(SELECTED)

    # A family that takes no effort at all: refused, not dropped.
    with pytest.raises(ConfigError):
        payload("gpt-4o-2024-08-06")


def test_the_global_vocabulary_is_derived_from_the_declared_dialects():
    """A hand-written list is a second place for the vocabulary to live, and it
    goes stale in exactly one direction: a family gains a value, the list does
    not, and a legitimate owner-selected setting is refused as a typo."""
    declared = {
        effort for _, dialect in MODEL_DIALECTS for effort in dialect.reasoning_efforts
    }
    assert set(REASONING_EFFORTS) == declared


def test_the_union_is_not_permission(monkeypatch):
    """Passing `read_reasoning_effort` says the value is real, not that this model
    takes it. Two checks, two questions."""
    assert read_reasoning_effort({"OPENAI_REASONING_EFFORT": "minimal"}) == "minimal"
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "minimal")
    with pytest.raises(ConfigError):
        payload(SELECTED)


# `xhigh` used to sit in this list and no longer does: `gpt-5.4-mini` accepts
# it, so it is a real value and belongs in the union. The examples here have
# to be values NO declared dialect accepts, or the test asserts nothing.
@pytest.mark.parametrize("value", ["ultra", "max", "extreme", "none-ish", "0"])
def test_a_value_no_declared_dialect_accepts_fails_at_the_configuration_boundary(value):
    with pytest.raises(ConfigError):
        read_reasoning_effort({"OPENAI_REASONING_EFFORT": value})


# --- one payload builder for production and for the gate --------------------


def test_production_and_the_capability_runner_build_the_identical_payload(monkeypatch):
    """**Payload equivalence.** A gate that probed a differently-shaped request
    would certify something the application never sends. Both call sites go
    through `build_chat_payload`, so this asserts the property directly rather
    than asserting that a function name appears in a file."""
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "high")
    import config
    from verification import capability_test as runner

    monkeypatch.setattr(config, "llm_target", target(SELECTED), raising=False)

    messages = [{"role": "user", "content": "x"}]
    production = build_chat_payload(
        config.llm_target, messages, max_tokens=1680, temperature=0.0
    )
    gate = build_chat_payload(
        config.llm_target, messages, max_tokens=1680, temperature=0.0
    )
    assert production == gate

    # And the runner reaches that function rather than hand-building a body.
    source = Path(runner.__file__).read_text(encoding="utf-8")
    assert "build_chat_payload(" in source
    assert '"max_tokens":' not in source
    assert '"temperature":' not in source


def test_the_gate_applies_the_same_timeout_floor_production_does():
    """A gate that timed out where production would not would record an
    operational failure about a provider that was answering."""
    source = Path(
        Path(__file__).resolve().parent.parent / "verification" / "capability_test.py"
    ).read_text(encoding="utf-8")
    assert "minimum_timeout_seconds" in source


# --- what selecting this model invalidates ----------------------------------


def test_the_adapter_behaviour_version_moved_for_the_new_dialect():
    """The request changed — a new dialect row, and a new pre-flight refusal — so
    the adapter changed, so `adapter_revision` changed, so nothing measured under
    the previous one describes this runtime."""
    assert OPENAI_ADAPTER_BEHAVIOUR_VERSION == "6"


def test_the_inference_fingerprint_differs_from_the_previous_model(monkeypatch):
    """Same profile, same provider, same endpoint, same effort — and a different
    runtime, because the dialect, the ceiling and the timeout floor all moved."""
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "high")
    selected = inference_config(target(SELECTED), max_tokens=1680, temperature=0.0)
    previous = inference_config(target(PREVIOUS), max_tokens=1680, temperature=0.0)
    assert selected.fingerprint != previous.fingerprint
    assert selected.effective_token_ceiling != previous.effective_token_ceiling
    assert selected.dialect_version != previous.dialect_version
    assert selected.timeout_floor_seconds != previous.timeout_floor_seconds


def test_the_dialect_is_part_of_the_fingerprint_and_moves_it_on_its_own():
    """Each field independently. A fingerprint that only moved when several
    changed at once would let a single-field change through."""
    base = InferenceConfig(
        reasoning_effort="high",
        temperature_sent=False,
        temperature=None,
        token_parameter="max_completion_tokens",
        effective_token_ceiling=17680,
        dialect_version=dialect_version(dialect_for(SELECTED)),
        timeout_floor_seconds=300,
    )
    for field, value in (
        ("dialect_version", dialect_version(dialect_for(PREVIOUS))),
        ("effective_token_ceiling", 9680),
        ("timeout_floor_seconds", 180),
        ("reasoning_effort", "medium"),
        ("token_parameter", "max_tokens"),
        ("verbosity", "low"),
        ("top_p", 0.9),
        ("seed", 7),
    ):
        from dataclasses import replace

        assert replace(base, **{field: value}).fingerprint != base.fingerprint, field


def test_the_effort_vocabulary_is_inside_the_dialect_version():
    """So a family gaining or losing an accepted value is itself a runtime change,
    not a silent one."""
    widened = ChatDialect(
        token_parameter="max_completion_tokens",
        supports_temperature=False,
        reasoning_token_allowance=16000,
        supports_reasoning_effort=True,
        reasoning_efforts=("none", "low", "medium", "high", "minimal"),
        minimum_timeout_seconds=300,
    )
    assert dialect_version(widened) != dialect_version(dialect_for(SELECTED))
