"""Capability artifacts are scoped to one runtime, and admission fails closed.

**The headline rule: a successful `local` OpenAI artifact must never qualify the
`aramco` configuration.** Not by convention, not by remembering to re-run
something — structurally, because `deployment_profile` is one of the identity
fields compared field by field.

Every identity field is tested **separately**. A comparison that accidentally
skipped one would still pass a test that only ever changed the model, and the
field it skipped would be the one nobody thinks about.

**No outbound call is made.** Every artifact here is written to `tmp_path`.
"""

import json
from pathlib import Path

import pytest

from deployment import LLMTarget, Profile, Provider
from evidence.policy import PolicyNotApproved, validate_admission_policies
from verification.capability import (
    CAPABILITY_RUNNER_VERSION,
    IDENTITY_FIELDS,
    CapabilityNotQualified,
    RuntimeIdentity,
    adapter_revision,
    artifact_filename,
    load_artifacts,
    require_capability,
    schema_version,
)

BACKEND = Path(__file__).resolve().parent.parent

LOCAL = LLMTarget(
    Profile.LOCAL, Provider.OPENAI, "https://api.openai.com/v1", "gpt-4o-2024-08-06"
)
ARAMCO = LLMTarget(
    Profile.ARAMCO,
    Provider.OPENAI_COMPATIBLE,
    "https://dev-metabrain-api.aramco.com.sa/v1/chat/completions",
    "minimaxm3",
)


def write_artifact(directory: Path, identity: RuntimeIdentity, *, eligible=True) -> Path:
    record = {
        "gate": "spec §14.2.1 structured-output capability",
        "run_at": "2026-08-28T00:00:00+00:00",
        "runtime_identity": identity.as_dict(),
        "probes": [],
        "eligible": eligible,
    }
    path = directory / artifact_filename(identity, "2026-08-28")
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return path


def _identity(target=LOCAL, **overrides) -> RuntimeIdentity:
    base = RuntimeIdentity.for_target(target)
    return RuntimeIdentity(**{**base.as_dict(), **overrides})


# --- the happy path --------------------------------------------------------


def test_a_matching_eligible_artifact_admits_the_runtime(tmp_path):
    write_artifact(tmp_path, _identity(LOCAL))
    record = require_capability(LOCAL, directory=tmp_path)
    assert record["eligible"] is True


def test_the_artifact_records_every_required_identity_field(tmp_path):
    identity = _identity(LOCAL)
    stored = json.loads(write_artifact(tmp_path, identity).read_text(encoding="utf-8"))
    recorded = stored["runtime_identity"]
    for name in IDENTITY_FIELDS:
        assert recorded[name] not in (None, ""), f"{name} must be recorded"
    assert recorded["deployment_profile"] == "local"
    assert recorded["provider"] == "openai"
    assert recorded["endpoint"]["host"] == "api.openai.com"
    assert recorded["model"] == "gpt-4o-2024-08-06"
    assert recorded["adapter_revision"].startswith("openai-python/")
    assert recorded["schema_version"].startswith("sha256:")
    assert recorded["runner_version"] == CAPABILITY_RUNNER_VERSION


# --- the artifact is not transferable --------------------------------------


def test_a_local_artifact_cannot_qualify_the_aramco_profile(tmp_path):
    """**The rule this whole module exists for.** A development qualification is
    not evidence about a production gateway, and the refusal is structural rather
    than remembered."""
    write_artifact(tmp_path, _identity(LOCAL))
    with pytest.raises(CapabilityNotQualified) as caught:
        require_capability(ARAMCO, directory=tmp_path)
    assert "deployment_profile" in str(caught.value)


def test_an_aramco_artifact_cannot_qualify_the_local_profile(tmp_path):
    """Symmetrical. Neither direction transfers."""
    write_artifact(tmp_path, _identity(ARAMCO))
    with pytest.raises(CapabilityNotQualified):
        require_capability(LOCAL, directory=tmp_path)


def test_a_test_profile_fixture_cannot_qualify_local_or_aramco(tmp_path):
    """Spec-amendment §6: `test` may use explicit fixtures, and they must be
    impossible to use elsewhere. `deployment_profile` is why."""
    stub = LLMTarget(Profile.TEST, Provider.STUB, "", "stub")
    write_artifact(tmp_path, _identity(stub))
    for target in (LOCAL, ARAMCO):
        with pytest.raises(CapabilityNotQualified) as caught:
            require_capability(target, directory=tmp_path)
        assert "deployment_profile" in str(caught.value)


def test_a_changed_model_invalidates_the_artifact(tmp_path):
    write_artifact(tmp_path, _identity(LOCAL, model="gpt-4o-mini-2024-07-18"))
    with pytest.raises(CapabilityNotQualified) as caught:
        require_capability(LOCAL, directory=tmp_path)
    assert "model:" in str(caught.value)


def test_a_changed_provider_invalidates_the_artifact(tmp_path):
    write_artifact(tmp_path, _identity(LOCAL, provider="openai_compatible"))
    with pytest.raises(CapabilityNotQualified) as caught:
        require_capability(LOCAL, directory=tmp_path)
    assert "provider:" in str(caught.value)


def test_a_changed_endpoint_invalidates_the_artifact(tmp_path):
    """A proxy in front of the same model is a different runtime: it can rewrite
    the request, strip `response_format`, or serve a different revision."""
    moved = _identity(LOCAL, endpoint={"scheme": "https", "host": "proxy.internal", "port": "", "path": "/v1"})
    write_artifact(tmp_path, moved)
    with pytest.raises(CapabilityNotQualified) as caught:
        require_capability(LOCAL, directory=tmp_path)
    assert "endpoint:" in str(caught.value)


def test_a_changed_adapter_revision_invalidates_the_artifact(tmp_path):
    """The SDK decides how `response_format` is serialized and how a refusal is
    surfaced. An upgrade can change both without the model changing at all."""
    write_artifact(tmp_path, _identity(LOCAL, adapter_revision="openai-python/0.28.0"))
    with pytest.raises(CapabilityNotQualified) as caught:
        require_capability(LOCAL, directory=tmp_path)
    assert "adapter_revision:" in str(caught.value)


def test_a_changed_schema_version_invalidates_the_artifact(tmp_path):
    """A record cannot outlive the contract it qualified."""
    write_artifact(tmp_path, _identity(LOCAL, schema_version="sha256:0000000000000000"))
    with pytest.raises(CapabilityNotQualified) as caught:
        require_capability(LOCAL, directory=tmp_path)
    assert "schema_version:" in str(caught.value)


def test_a_changed_runner_version_invalidates_the_artifact(tmp_path):
    """Runner version 1 classified a DNS failure as a provider rejection. Nothing
    it produced should qualify anything, and this field is why it cannot."""
    write_artifact(tmp_path, _identity(LOCAL, runner_version="1"))
    with pytest.raises(CapabilityNotQualified) as caught:
        require_capability(LOCAL, directory=tmp_path)
    assert "runner_version:" in str(caught.value)


@pytest.mark.parametrize("field", IDENTITY_FIELDS)
def test_every_identity_field_is_actually_compared(field):
    """Each one separately. A comparison that skipped a field would pass every
    test above that happened to change a different one."""
    runtime = _identity(LOCAL)
    changed = {"endpoint": {"scheme": "https", "host": "elsewhere", "port": "", "path": "/v1"}}
    altered = RuntimeIdentity(
        **{**runtime.as_dict(), field: changed.get(field, "CHANGED")}
    )
    assert not runtime.matches(altered), f"{field} is not compared"
    assert any(item.startswith(f"{field}:") for item in runtime.mismatches(altered))


def test_all_mismatching_fields_are_reported_not_just_the_first(tmp_path):
    """An operator whose profile *and* model changed deserves both, rather than
    fixing one and being refused again."""
    runtime = _identity(LOCAL)
    altered = RuntimeIdentity(
        **{**runtime.as_dict(), "model": "other", "deployment_profile": "aramco"}
    )
    reasons = runtime.mismatches(altered)
    assert any(r.startswith("model:") for r in reasons)
    assert any(r.startswith("deployment_profile:") for r in reasons)


def test_a_pinned_model_whose_served_revision_differs_is_refused(tmp_path):
    """When the configured model *is* a snapshot, model and revision are the same
    statement and a difference is a real conflict."""
    write_artifact(
        tmp_path, _identity(LOCAL, model_revision="gpt-4o-2024-11-20")
    )
    runtime = RuntimeIdentity.for_target(LOCAL, model_revision="gpt-4o-2024-08-06")
    stored = RuntimeIdentity(
        **json.loads(
            (next(tmp_path.glob("*.json"))).read_text(encoding="utf-8")
        )["runtime_identity"]
    )
    assert any(r.startswith("model_revision:") for r in runtime.mismatches(stored))


# --- fail closed -----------------------------------------------------------


def test_no_artifact_at_all_refuses_admission(tmp_path):
    with pytest.raises(CapabilityNotQualified) as caught:
        require_capability(LOCAL, directory=tmp_path)
    assert "no capability artifact found" in str(caught.value)
    assert "python -m verification.capability_test" in str(caught.value)


def test_a_missing_directory_refuses_rather_than_raising_oserror(tmp_path):
    with pytest.raises(CapabilityNotQualified):
        require_capability(LOCAL, directory=tmp_path / "does-not-exist")


def test_a_matching_but_ineligible_artifact_refuses_with_the_strongest_message(tmp_path):
    """This exact runtime was tested and failed -- the strongest refusal there
    is, and it must not be confused with 'not yet tested'."""
    write_artifact(tmp_path, _identity(LOCAL), eligible=False)
    with pytest.raises(CapabilityNotQualified) as caught:
        require_capability(LOCAL, directory=tmp_path)
    message = str(caught.value)
    assert "eligible=false" in message
    assert "Do not weaken the verifier" in message


def test_a_corrupt_artifact_is_skipped_and_never_qualifies(tmp_path):
    (tmp_path / "structured-output-capability-local-broken-2026-08-28.json").write_text(
        "{not json", encoding="utf-8"
    )
    with pytest.raises(CapabilityNotQualified):
        require_capability(LOCAL, directory=tmp_path)
    assert load_artifacts(tmp_path) == []


def test_an_artifact_with_no_identity_block_never_qualifies(tmp_path):
    (tmp_path / "structured-output-capability-local-old-2026-08-28.json").write_text(
        json.dumps({"eligible": True, "model": "gpt-4o"}), encoding="utf-8"
    )
    with pytest.raises(CapabilityNotQualified) as caught:
        require_capability(LOCAL, directory=tmp_path)
    assert "no readable runtime_identity" in str(caught.value)


def test_one_matching_artifact_among_several_is_found(tmp_path):
    write_artifact(tmp_path, _identity(ARAMCO))
    write_artifact(tmp_path, _identity(LOCAL, model="gpt-4o-mini-2024-07-18"))
    write_artifact(tmp_path, _identity(LOCAL))
    assert require_capability(LOCAL, directory=tmp_path)["eligible"] is True


def test_two_runtimes_on_the_same_day_do_not_overwrite_each_other(tmp_path):
    """The filename carries a fingerprint of the whole identity. Silently
    replacing one qualification with another is how a file ends up describing a
    runtime nobody tested."""
    write_artifact(tmp_path, _identity(LOCAL))
    write_artifact(tmp_path, _identity(LOCAL, model="gpt-4o-mini-2024-07-18"))
    assert len(list(tmp_path.glob("*.json"))) == 2


# --- no bypass exists ------------------------------------------------------


def test_no_bypass_environment_variable_exists_anywhere():
    """Spec-amendment §6: no bypass variable, no debug flag, no implicit
    auto-approval.

    An **AST** walk over environment reads and defined names, not a substring
    search: `capability.py`'s own docstring names the bypass variable in order to
    say it does not exist, and a grep test that fires on its own prose is one
    somebody eventually relaxes.
    """
    import ast

    markers = ("skip_capability", "disable_capability", "capability_bypass",
               "ignore_capability", "allow_unqualified", "bypass_capability")
    offenders = []
    for path in BACKEND.rglob("*.py"):
        if any(part in {".venv", "__pycache__"} for part in path.parts):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.append(node.name)
            elif isinstance(node, ast.Name):
                names.append(node.id)
            elif isinstance(node, ast.keyword) and node.arg:
                names.append(node.arg)
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"get", "getenv"}
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                names.append(node.args[0].value)
            for name in names:
                lowered = name.lower()
                if any(marker in lowered for marker in markers):
                    offenders.append(f"{path.relative_to(BACKEND)} -> {name}")
    assert offenders == [], f"a capability bypass exists: {offenders}"


def test_require_capability_takes_no_override_parameter():
    """A keyword argument that skipped the check would be a bypass with a
    friendlier name."""
    import inspect

    parameters = set(inspect.signature(require_capability).parameters)
    assert parameters == {"target", "directory"}


def test_admission_refuses_a_verified_run_with_no_capability_artifact(tmp_path):
    """End to end at the admission boundary: policies fully recorded, capability
    absent, run refused."""

    class FullyRecorded:
        date_screening_policy = "publication_lt_cutoff"
        date_screening_decision = "Approved 2026-08-28 by the repository owner"
        text_version_policy = "provider_publication_date"
        text_version_decision = "Approved 2026-08-28 by the repository owner"

    with pytest.raises(CapabilityNotQualified):
        validate_admission_policies(
            FullyRecorded(), target=LOCAL, capability_directory=tmp_path
        )


def test_admission_reports_policy_gaps_before_reaching_the_capability_check(tmp_path):
    """Ordering matters for the operator: an unrecorded policy is a different
    problem from an unqualified model, and they are fixed in different places."""

    class NoPolicies:
        date_screening_policy = ""
        date_screening_decision = ""
        text_version_policy = ""
        text_version_decision = ""

    with pytest.raises(PolicyNotApproved):
        validate_admission_policies(
            NoPolicies(), target=LOCAL, capability_directory=tmp_path
        )


def test_admission_passes_when_policies_and_capability_are_both_satisfied(tmp_path):
    class FullyRecorded:
        date_screening_policy = "publication_lt_cutoff"
        date_screening_decision = "Approved 2026-08-28 by the repository owner"
        text_version_policy = "provider_publication_date"
        text_version_decision = "Approved 2026-08-28 by the repository owner"

    write_artifact(tmp_path, _identity(LOCAL))
    approved = validate_admission_policies(
        FullyRecorded(), target=LOCAL, capability_directory=tmp_path
    )
    assert approved.date_screening_policy == "publication_lt_cutoff"


# --- artifacts carry no secrets --------------------------------------------


def test_an_artifact_identity_carries_no_credential(tmp_path):
    hostile = LLMTarget(
        Profile.ARAMCO,
        Provider.OPENAI_COMPATIBLE,
        "https://user:sup3rsecret@gateway.example/v1?api_key=SECRET123",
        "m",
    )
    rendered = write_artifact(tmp_path, _identity(hostile)).read_text(encoding="utf-8")
    assert "sup3rsecret" not in rendered
    assert "SECRET123" not in rendered
    assert "api_key" not in rendered


def test_the_identity_has_no_field_a_credential_could_land_in():
    import dataclasses

    names = {field.name for field in dataclasses.fields(RuntimeIdentity)}
    assert not any(
        marker in name for name in names for marker in ("key", "token", "secret", "auth")
    )


def test_the_schema_version_tracks_the_actual_schema():
    """Derived rather than hand-maintained: a version number somebody has to
    remember to bump is a version number that outlives its contract."""
    first = schema_version()
    assert first.startswith("sha256:")
    assert first == schema_version()


def test_the_adapter_revision_names_the_client_that_will_make_the_call():
    assert adapter_revision(LOCAL).startswith("openai-python/")
    assert adapter_revision(ARAMCO).startswith("requests/")
    assert adapter_revision(LLMTarget(Profile.TEST, Provider.STUB, "", "stub")) == "stub/1"
