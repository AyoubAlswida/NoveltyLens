"""The `gpt-5.4-mini` dialect, and the version-boundary rule that keeps it its own.

**This is the second time a `gpt-5.x` selection has walked into the same trap, and
the first time the trap itself was closed.** `"gpt-5.1-2025-11-13".startswith("gpt-5")`
is true; so is `"gpt-5.4-mini-2026-03-17".startswith("gpt-5")`. Under a bare prefix
match each takes the `gpt-5` family's dialect, whose `reasoning_effort` vocabulary is
`minimal|low|medium|high` -- a different vocabulary from either of theirs.

What makes it dangerous is that it does not fail. All three families agree on
`max_completion_tokens` and on omitting `temperature`, and `medium` is accepted by
all three, so the owner-selected runtime would have run under a dialect nobody
declared for it, at the wrong reasoning allowance and the wrong timeout floor, with
no error anywhere -- and the capability artifact would have recorded that as the
identity it qualified. `gpt-5.1` was fixed by adding a row; a row alone would have
left the next version to rediscover it, so matching is now at a version boundary.

**Every assertion here is offline.** Nothing constructs a client, resolves a host or
sends a request. A dialect is *declared*, never probed: trying a request and adapting
to the rejection would be the runtime fallback this codebase refuses.
"""

import pytest

from deployment import (
    CLASSIC_DIALECT,
    MODEL_DIALECTS,
    OPENAI_DEFAULT_MODEL,
    REASONING_EFFORTS,
    ConfigError,
    LLMTarget,
    Profile,
    Provider,
    dialect_for,
    dialect_version,
)
from llm import build_chat_payload, inference_config

SELECTED = "gpt-5.4-mini-2026-03-17"
"""The owner-selected `local` verifier model, 2026-08-31. Pinned snapshot."""

PRIOR_SELECTIONS = ("gpt-5.1-2025-11-13", "gpt-5-mini-2025-08-07", "gpt-4o-2024-08-06")
"""Every previously selected model, each measured ineligible. Comparisons against
these are the point: none of their artifacts may qualify this runtime."""


def target(model: str) -> LLMTarget:
    return LLMTarget(Profile.LOCAL, Provider.OPENAI, "https://api.openai.com/v1", model)


def payload(model: str, *, max_tokens: int = 1680, temperature: float = 0.0) -> dict:
    return build_chat_payload(
        target(model),
        [{"role": "user", "content": "x"}],
        max_tokens=max_tokens,
        temperature=temperature,
    )


# --- the selection is the default, and it is pinned -------------------------


def test_the_default_local_model_is_the_selected_snapshot():
    assert OPENAI_DEFAULT_MODEL == SELECTED


def test_the_selected_model_is_a_snapshot_and_not_a_floating_alias():
    """An alias can be moved to a new revision by the provider with no
    configuration change, leaving an artifact that matches on every identity field
    while describing a model that no longer exists."""
    assert target(SELECTED).model_is_pinned
    assert not target("gpt-5.4-mini").model_is_pinned


# --- the dialect is its own, and is declared rather than inherited ----------


def test_the_selected_model_has_its_own_declared_row():
    assert any(prefix == "gpt-5.4-mini" for prefix, _ in MODEL_DIALECTS)


def test_the_effort_vocabulary_is_this_family_s_own():
    """It keeps `none` (so `minimal` is still absent) and ADDS `xhigh`."""
    assert dialect_for(SELECTED).reasoning_efforts == (
        "none", "low", "medium", "high", "xhigh",
    )


def test_it_does_not_inherit_the_gpt_5_dialect():
    """The regression. A bare `startswith` handed it this one silently."""
    assert dialect_for(SELECTED) is not dialect_for("gpt-5-mini-2025-08-07")
    assert dialect_for(SELECTED).reasoning_efforts != dialect_for("gpt-5").reasoning_efforts
    assert "minimal" not in dialect_for(SELECTED).reasoning_efforts


def test_it_does_not_inherit_the_gpt_5_1_dialect_either():
    """`xhigh` is the difference, and it is a real one: an effort ABOVE `high`."""
    assert dialect_for(SELECTED) is not dialect_for("gpt-5.1-2025-11-13")
    assert "xhigh" not in dialect_for("gpt-5.1-2025-11-13").reasoning_efforts


def test_the_neighbouring_families_are_left_exactly_as_they_were():
    """Closing the boundary must not move any other family."""
    assert dialect_for("gpt-5.1-2025-11-13").reasoning_efforts == (
        "none", "low", "medium", "high",
    )
    for model in ("gpt-5-mini-2025-08-07", "gpt-5", "gpt-5-nano", "o3-mini"):
        assert dialect_for(model).reasoning_efforts == (
            "minimal", "low", "medium", "high",
        ), model


@pytest.mark.parametrize("model", ["gpt-5.9-unreleased", "gpt-5.2-mini-2026-01-01", "gpt-50-odd"])
def test_an_undeclared_version_matches_nothing_rather_than_a_neighbour(model):
    """**Failing loudly is the intended outcome.**

    A version this table has never heard of falls to the classic dialect, whose
    `max_tokens` and explicit `temperature` a reasoning model rejects with a
    deterministic HTTP 400. The gate classifies that `BAD_REQUEST`, writes no
    artifact and exits 2 -- which is the signal to add a row. Inheriting a
    neighbour's reasoning vocabulary quietly is strictly worse: it measures a
    runtime nobody declared and records it as qualified.
    """
    assert dialect_for(model) is CLASSIC_DIALECT


# --- the request this model actually gets -----------------------------------


def test_the_token_ceiling_uses_the_reasoning_family_parameter():
    """`max_tokens` is rejected outright by these families, not aliased."""
    built = payload(SELECTED, max_tokens=1680)
    assert "max_tokens" not in built
    assert built["max_completion_tokens"] == 1680 + 16000


def test_the_reasoning_allowance_only_ever_adds_to_the_caller_s_budget():
    """It can never shorten a response. A ceiling that is never reached costs
    nothing; a tight one is `finish_reason: "length"`, classified TOKEN_CEILING,
    never retried, and it abandons the whole record."""
    dialect = dialect_for(SELECTED)
    assert dialect.reasoning_token_allowance > 0
    for asked in (100, 1680, 9000):
        assert payload(SELECTED, max_tokens=asked)["max_completion_tokens"] > asked


def test_temperature_is_omitted_and_never_substituted():
    """Sending `1` would be this code choosing a sampling setting on the caller's
    behalf; omitting leaves the provider's default in force and records that."""
    assert "temperature" not in payload(SELECTED, temperature=0.0)
    assert inference_config(target(SELECTED), max_tokens=1680,
                            temperature=0.0).temperature_sent is False


def test_the_selected_effort_is_sent_as_chosen(monkeypatch):
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "medium")
    assert payload(SELECTED)["reasoning_effort"] == "medium"


def test_the_new_effort_is_accepted_on_this_model_only(monkeypatch):
    """`xhigh` is real on this family and is a deterministic 400 on the others, so
    it must be refused BEFORE the request is built rather than sent and rejected:
    a provider 400 arrives after the call is made and billed."""
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "xhigh")
    assert payload(SELECTED)["reasoning_effort"] == "xhigh"
    for other in ("gpt-5.1-2025-11-13", "gpt-5-mini-2025-08-07"):
        with pytest.raises(ConfigError) as raised:
            payload(other)
        assert "xhigh" in str(raised.value)


def test_an_older_family_s_effort_is_refused_on_this_model(monkeypatch):
    """`minimal` is valid on `gpt-5` and absent here. It is neither dropped nor
    downgraded to a neighbouring value: a dropped setting runs at an effort the
    operator does not believe is in force, and the artifact would then describe a
    runtime nobody tested."""
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "minimal")
    with pytest.raises(ConfigError) as raised:
        payload(SELECTED)
    message = str(raised.value)
    assert "minimal" in message and SELECTED in message
    # It names the vocabulary this family does accept, rather than failing blankly.
    assert "xhigh" in message


def test_the_declared_union_carries_every_value_some_family_accepts():
    """`REASONING_EFFORTS` is DERIVED from the table, so it cannot claim a value
    no dialect accepts, and cannot omit one that some dialect does."""
    assert "xhigh" in REASONING_EFFORTS
    for prefix, dialect in MODEL_DIALECTS:
        for effort in dialect.reasoning_efforts:
            assert effort in REASONING_EFFORTS, (prefix, effort)


# --- the model and the effort are part of qualification ---------------------


def test_the_dialect_version_carries_this_family_s_effort_vocabulary():
    """So a family gaining or losing an accepted value is itself a runtime change
    rather than a silent one."""
    rendered = dialect_version(dialect_for(SELECTED))
    assert "xhigh" in rendered
    assert rendered != dialect_version(dialect_for("gpt-5.1-2025-11-13"))
    assert rendered != dialect_version(dialect_for("gpt-5-mini-2025-08-07"))


@pytest.mark.parametrize("prior", PRIOR_SELECTIONS)
def test_no_previously_selected_model_shares_this_runtime_identity(prior):
    """An artifact is scoped to the exact runtime. Every prior selection differs
    on the model, and each of the reasoning families differs on the dialect too,
    so nothing measured there can qualify anything here."""
    assert prior != SELECTED
    assert dialect_version(dialect_for(prior)) != dialect_version(dialect_for(SELECTED))


def test_changing_the_effort_changes_the_recorded_identity(monkeypatch):
    """`inference_config_fingerprint` is one of the identity fields admission
    compares, so an artifact measured at one effort cannot qualify another."""
    seen = {}
    for effort in ("medium", "high", "xhigh"):
        monkeypatch.setenv("OPENAI_REASONING_EFFORT", effort)
        config = inference_config(target(SELECTED), max_tokens=1680, temperature=0.0)
        seen[effort] = config.reasoning_effort
    assert seen == {"medium": "medium", "high": "high", "xhigh": "xhigh"}
    assert len(set(seen.values())) == 3
