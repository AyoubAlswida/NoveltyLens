"""The records already on disk, against the runtime actually configured now.

`tests/test_capability_admission.py` proves the *rule* — each identity field is
compared, and a mismatch on any of them refuses. This module proves the
*consequence* for the files that genuinely exist in `docs/measurements/`, because
a rule that is right in the abstract and a directory whose contents are safe are
two different claims, and only the second one is what admission actually reads.

Three capability artifacts and one diagnostic record exist as of 2026-08-29, and
**every one of them was a measurement of something ineligible**:

| Record | Runtime it measured | Result |
|---|---|---|
| `…-local-d7d2541857da-2026-08-28.json` | `gpt-4o-2024-08-06`, runner v2 | deterministic schema rejection |
| `…-local-473db61ad9fb-2026-08-29.json` | `gpt-4o-2024-08-06`, runner v3 | probe 7, §9 check 15, rate 0.25 |
| `…-local-2d9d6d2ba0a6-2026-08-29.json` | `gpt-5-mini-2025-08-07`, runner v4 | probe 7, §15.1 case E unstable |
| `diagnostic-case-E-2026-08-29-101631.json` | `gpt-5-mini`, `reasoning_effort=high` | 0 of 5 |

They stay on disk and stay byte-unchanged: an ineligible artifact is the most
useful record there is, and editing one to "tidy up" would destroy the only
evidence of what was measured. **None of them may qualify anything**, and the
reasons differ per record — a missing identity key, a different model, a
different adapter, a different runner, a different inference fingerprint — which
is why each is asserted rather than covered by one blanket statement.

No test here makes a network call.
"""

import json
from pathlib import Path

import pytest

from deployment import LLMTarget, Profile, Provider
from verification.capability import (
    ARTIFACT_DIRECTORY,
    CapabilityNotQualified,
    RuntimeIdentity,
    _identity_from_record,
    load_artifacts,
    require_capability,
)

SELECTED = "gpt-5.1-2025-11-13"


@pytest.fixture
def selected_runtime(monkeypatch) -> LLMTarget:
    """The runtime the owner selected on 2026-08-29, built the way `config` does."""
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "high")
    return LLMTarget(Profile.LOCAL, Provider.OPENAI, "https://api.openai.com/v1", SELECTED)


def on_disk() -> list[dict]:
    return load_artifacts(ARTIFACT_DIRECTORY)


# --- the directory is not empty, or every test below is vacuous -------------


def test_the_measurements_directory_actually_holds_the_historical_records():
    """A guard on the guard. If someone deletes the artifacts, every "cannot
    qualify" assertion below passes for the wrong reason."""
    assert ARTIFACT_DIRECTORY.is_dir()
    assert len(on_disk()) >= 3


def test_every_record_on_disk_records_an_ineligible_or_unreadable_measurement():
    """There is no eligible artifact yet, and this is the file that would have to
    change when there is one. Until then, `eligible: true` anywhere in this
    directory is a fact somebody needs to look at."""
    for record in on_disk():
        assert record.get("eligible") is not True, record["_path"]


# --- none of them qualifies the configured runtime --------------------------


def test_no_artifact_on_disk_qualifies_the_selected_runtime(selected_runtime):
    """The whole point, stated once against the real directory."""
    with pytest.raises(CapabilityNotQualified):
        require_capability(selected_runtime, directory=ARTIFACT_DIRECTORY)


def test_every_record_on_disk_is_unreadable_as_an_identity(selected_runtime):
    """As of 2026-08-29 **all three** artifacts fail before comparison even
    begins, and the reason is worth being precise about: none carries
    `inference_config_fingerprint`, and the oldest carries neither
    `wire_schema_version` nor `validator_version` either.

    `_identity_from_record` has no `.get(..., "")` default on those keys, on
    purpose. Defaulting a missing key to the empty string would let a record
    compare equal to a runtime that also happened to render `""` — which is
    exactly how an unset-reasoning-effort measurement would have qualified a
    `high` runtime. A missing key raises and the record is skipped as what it
    is: a measurement of a runtime whose identity was not yet fully described.
    """
    for record in on_disk():
        name = Path(record["_path"]).name
        assert _identity_from_record(record) is None, name
        assert "inference_config_fingerprint" not in record["runtime_identity"], name


def test_the_refusal_names_each_unreadable_record(selected_runtime):
    """Every refusal names a configuration fact and a file. An operator is told
    which record was skipped and why, never just "not qualified"."""
    with pytest.raises(CapabilityNotQualified) as refused:
        require_capability(selected_runtime, directory=ARTIFACT_DIRECTORY)
    reasons = " ".join(refused.value.reasons)
    for record in on_disk():
        assert Path(record["_path"]).name in reasons
    assert "no readable runtime_identity" in reasons


def test_the_two_gpt_4o_records_measured_a_different_model_entirely():
    """Recorded so the directory's contents stay legible after the identity keys
    they lack are long forgotten."""
    models = {
        record["runtime_identity"]["model"]
        for record in on_disk()
        if record["runtime_identity"]["model"].startswith("gpt-4o")
    }
    assert models == {"gpt-4o-2024-08-06"}
    for record in on_disk():
        assert record["runtime_identity"]["model"] != SELECTED


def test_the_gpt_5_mini_record_is_the_nearest_miss_and_still_qualifies_nothing():
    """The nearest thing on disk to the configured runtime: same profile, same
    provider, same endpoint, same canonical schema, same wire schema, same
    validator. It is still refused, and it stays refused even when the key it is
    missing is supplied — see the forgery test below."""
    nearest = [
        record
        for record in on_disk()
        if record["runtime_identity"]["model"].startswith("gpt-5-mini")
    ]
    assert nearest, "the gpt-5-mini artifact is missing; this test is vacuous without it"
    for record in nearest:
        identity = record["runtime_identity"]
        assert identity["model"] != SELECTED
        assert identity["adapter_revision"].endswith("behaviour/3")
        assert identity["runner_version"] == "4"
        assert record["eligible"] is False


def test_a_pre_amendment_artifact_lacks_the_schema_and_validator_keys_too():
    """The 2026-08-28 record predates `wire_schema_version` as well. Two separate
    reasons it cannot be read, and neither is a matter of remembering."""
    oldest = [
        record
        for record in on_disk()
        if "wire_schema_version" not in record["runtime_identity"]
    ]
    assert oldest, "expected at least one pre-amendment record on disk"
    for record in oldest:
        assert "validator_version" not in record["runtime_identity"]


# --- a previous artifact cannot be promoted by editing fields --------------


def test_completing_and_relabelling_a_previous_record_still_does_not_qualify(
    tmp_path, selected_runtime
):
    """**The strongest form of the shortcut, refused.** Take the real gpt-5-mini
    artifact, supply the identity key it is missing, rename the model to the
    selected one, and set `eligible: true`. It is still refused, because the
    identity is compared **field by field** and the adapter revision, the runner
    version and the inference fingerprint all still describe the runtime that was
    actually measured. Renaming a measurement does not make it one."""
    source = next(
        record
        for record in on_disk()
        if record["runtime_identity"]["model"].startswith("gpt-5-mini")
    )
    forged = json.loads(Path(source["_path"]).read_text(encoding="utf-8"))
    forged["runtime_identity"]["model"] = SELECTED
    # The key the real record lacks, filled in with the PREVIOUS runtime's own
    # value — the most favourable thing a forger could supply while still being
    # honest about what was measured.
    previous = LLMTarget(
        Profile.LOCAL, Provider.OPENAI, "https://api.openai.com/v1", "gpt-5-mini-2025-08-07"
    )
    forged["runtime_identity"]["inference_config_fingerprint"] = RuntimeIdentity.for_target(
        previous
    ).inference_config_fingerprint
    forged["eligible"] = True
    (tmp_path / "structured-output-capability-local-forged-2026-08-29.json").write_text(
        json.dumps(forged), encoding="utf-8"
    )

    with pytest.raises(CapabilityNotQualified) as refused:
        require_capability(selected_runtime, directory=tmp_path)
    reasons = " ".join(refused.value.reasons)
    assert "inference_config_fingerprint" in reasons
    assert "adapter_revision" in reasons
    assert "runner_version" in reasons


def test_a_diagnostic_record_is_never_even_read(selected_runtime):
    """`load_artifacts` globs `structured-output-capability-*.json`. A diagnostic
    is named `diagnostic-case-*.json`, so it is not rejected at admission — it is
    never opened."""
    diagnostics = sorted(ARTIFACT_DIRECTORY.glob("diagnostic-case-*.json"))
    assert diagnostics, "expected at least one diagnostic record on disk"
    loaded = {Path(record["_path"]).name for record in on_disk()}
    for path in diagnostics:
        assert path.name not in loaded


def test_a_diagnostic_renamed_into_the_capability_namespace_still_qualifies_nothing(
    tmp_path, selected_runtime
):
    """Belt and braces, because a filename is a weak boundary on its own. A
    diagnostic record carries no `runtime_identity`, so no identity can be built
    from it however it is named or whatever is bolted onto it."""
    source = sorted(ARTIFACT_DIRECTORY.glob("diagnostic-case-*.json"))[0]
    record = json.loads(source.read_text(encoding="utf-8"))
    record["eligible"] = True
    (tmp_path / "structured-output-capability-local-abc-2026-08-29.json").write_text(
        json.dumps(record), encoding="utf-8"
    )

    assert _identity_from_record(record) is None
    with pytest.raises(CapabilityNotQualified):
        require_capability(selected_runtime, directory=tmp_path)


# --- and the record that *would* qualify has to be the real runtime --------


def test_only_an_artifact_built_from_this_exact_runtime_admits_it(
    tmp_path, selected_runtime
):
    """The positive control: without it, every refusal above could be a bug in
    `require_capability` rather than a property of the records."""
    identity = RuntimeIdentity.for_target(selected_runtime)
    (tmp_path / "structured-output-capability-local-real-2026-08-29.json").write_text(
        json.dumps({"runtime_identity": identity.as_dict(), "eligible": True}),
        encoding="utf-8",
    )
    admitted = require_capability(selected_runtime, directory=tmp_path)
    assert admitted["eligible"] is True


def test_that_positive_control_stops_working_the_moment_the_dialect_changes(
    tmp_path, monkeypatch
):
    """An artifact measured at `high` cannot qualify a runtime at `medium`, and an
    artifact measured against `gpt-5.1` cannot qualify `gpt-5-mini`. Same file,
    different runtime, refused."""
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "high")
    selected = LLMTarget(Profile.LOCAL, Provider.OPENAI, "https://api.openai.com/v1", SELECTED)
    identity = RuntimeIdentity.for_target(selected)
    (tmp_path / "structured-output-capability-local-real-2026-08-29.json").write_text(
        json.dumps({"runtime_identity": identity.as_dict(), "eligible": True}),
        encoding="utf-8",
    )

    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "medium")
    with pytest.raises(CapabilityNotQualified):
        require_capability(selected, directory=tmp_path)

    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "high")
    previous = LLMTarget(
        Profile.LOCAL, Provider.OPENAI, "https://api.openai.com/v1", "gpt-5-mini-2025-08-07"
    )
    with pytest.raises(CapabilityNotQualified):
        require_capability(previous, directory=tmp_path)


def test_a_local_artifact_for_this_model_can_never_qualify_aramco(tmp_path, monkeypatch):
    """Structural, not a matter of remembering. `deployment_profile` is one of the
    ten compared fields."""
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "high")
    selected = LLMTarget(Profile.LOCAL, Provider.OPENAI, "https://api.openai.com/v1", SELECTED)
    (tmp_path / "structured-output-capability-local-real-2026-08-29.json").write_text(
        json.dumps(
            {
                "runtime_identity": RuntimeIdentity.for_target(selected).as_dict(),
                "eligible": True,
            }
        ),
        encoding="utf-8",
    )
    aramco = LLMTarget(
        Profile.ARAMCO, Provider.OPENAI_COMPATIBLE, "https://gw.example/v1", SELECTED
    )
    with pytest.raises(CapabilityNotQualified):
        require_capability(aramco, directory=tmp_path)
