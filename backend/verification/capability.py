"""Capability artifacts, and the identity they are scoped to.

**A capability qualification is about one runtime, not about a model in the
abstract.** "GPT-4o supports JSON Schema" is a fact about a model family; "this
profile, against this endpoint, with this model revision, this SDK, and this
schema version, honoured the §10 contract on this date" is the fact spec §14.2.1
asks for. Only the second one can be checked, and only the second one can be
*invalidated* by something changing.

**The rule this module exists to enforce: a successful `local` OpenAI artifact
must never qualify the `aramco` configuration.** That is not a matter of
discipline or of remembering to re-run something. `deployment_profile` is a field
of the identity, identities are compared field by field, and a mismatch on any
field is a refusal. `tests/test_capability_admission.py` pins each field
separately, because a comparison that accidentally skipped one would still pass a
test that only changed the model.

**Fail closed.** A missing artifact, an unreadable one, an ineligible one, or one
whose identity differs in any field refuses admission. There is no
`SKIP_CAPABILITY_CHECK`, no debug bypass, no "warn and continue", and no implicit
auto-approval — `tests/test_capability_admission.py` greps for all of them.

**Nothing here carries a credential.** The identity is built from the profile, the
provider, a *parsed* endpoint, the model, and version strings. `LLMTarget` has no
`api_key` field by construction, so an artifact cannot acquire one by accident.
"""

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from deployment import LLMTarget, Profile, Provider

CAPABILITY_RUNNER_VERSION = "5"
"""Bumped whenever the probe set or its scoring changes.

An artifact records the runner that produced it, so a record made by a runner
that did not yet test, say, `additionalProperties` enforcement cannot silently
satisfy a gate that now requires it.

- Version 1 classified a DNS failure as a provider rejection, so an offline run
  "qualified" five of seven probes against a gateway it never contacted.
- Version 2 fixed that and still classified an HTTP 429 as a provider rejection —
  and a rejection is a *pass* for probes 3-5. It also scored the stability probe's
  operational failures as `malformed_response_rate: 0.0` and
  `all_cases_stable: true`, both of which read as passes. A run in which every
  single call failed at the transport contributed two passing terms.
- Version 3 was the 2026-08-29 amendment: operational outcomes became a separate
  vocabulary that can never satisfy a probe, probes 3-5 began requiring a genuine
  semantically-conforming response, and stability started failing closed.
- Version 4 widens the measurement protocol. Probe 7 now runs **every** executable
  §15.1 case (A-K, eleven of them) rather than a four-case subset, records each
  run's statuses, declared and derived outcome, canonical-validation result,
  consistency-check result and served model, counts consistency failures apart
  from other validation failures, and fails closed on incomplete case coverage.
  A four-case measurement and an eleven-case measurement are different
  measurements, so a v3 record cannot stand in for a v4 one.

- Version 5 gates probe 7 on **correctness as well as stability** (owner
  decision 2026-08-29). Every run is now compared against section 15.1's own
  Expected column -- the outcome, the per-requirement statuses that section
  *names*, and the ``combination_basis`` link it requires quoted verbatim -- and
  a case passes only when all three hold on all three runs. Under version 4 a
  model that returned the *same wrong answer* three times was stable and
  therefore passed; only a model whose wrong answers disagreed with each other
  failed. That is a materially weaker measurement wearing the same name, so a v4
  record cannot stand in for a v5 one.

Nothing produced by versions 1 or 2 should ever qualify anything, and this field
is why it cannot.
"""

ARTIFACT_DIRECTORY = Path(__file__).resolve().parent.parent.parent / "docs" / "measurements"

IDENTITY_FIELDS = (
    "deployment_profile",
    "provider",
    "endpoint",
    "model",
    "adapter_revision",
    "schema_version",
    "wire_schema_version",
    "validator_version",
    "inference_config_fingerprint",
    "runner_version",
)
"""Every field compared at admission.

`wire_schema_version` and `validator_version` were added by the owner amendment of
2026-08-29, and they are what keeps that amendment from being a hole. Before it,
one schema was both the contract and the bytes sent to the provider, so
`schema_version` covered both. Now there are three moving parts — the canonical
contract, the transformation that renders it for a provider, and the local
validator enforcing what the provider is no longer asked to — and a capability
result is a statement about **all three together**. A record made against a
different rendering, or against a validator that enforced less, says nothing
about this runtime.

Adding fields also, deliberately, invalidates every artifact written before the
amendment: an older record has no `wire_schema_version` key, so
`_identity_from_record` cannot build an identity from it and it qualifies nothing.
The old records stay on disk and stay accurate about what they measured.

`inference_config_fingerprint` was added on 2026-08-29 after a measurement made
the gap visible. `gpt-5-mini-2025-08-07` was measured **ineligible** with
`reasoning_effort` unset; the owner then set it to `high`. Those are two different
runtimes with two different answer distributions, and without this field the
second would have inherited the first's identity — an artifact matching on every
field while describing a configuration nobody had tested. It covers reasoning
effort, whether and what temperature is sent, the token parameter and effective
ceiling, the dialect, the timeout floor, and `top_p`/`seed` should either ever be
used. **A provider default that affects behaviour is a value in this fingerprint,
never an absence from it.**

`model_revision` is deliberately **not** here and is handled separately below —
see `RuntimeIdentity.matches`.
"""


def schema_version() -> str:
    """A content hash of the **canonical** §10 response schema.

    Derived rather than hand-maintained: a hand-written version number is a thing
    somebody forgets to bump, and the failure mode is a capability record
    outliving the contract it qualified. Canonical JSON (sorted keys) so
    formatting changes do not read as contract changes.
    """
    from verification.schema import RESPONSE_JSON_SCHEMA

    canonical = json.dumps(RESPONSE_JSON_SCHEMA, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def wire_schema_version() -> str:
    """A content hash of the bytes actually sent to the provider.

    Distinct from :func:`schema_version` because the two can move independently:
    the canonical contract can change without the rendering changing, and the
    rendering can change without the contract changing. §14.2.1 measures whether
    *this* provider honours *these* bytes, so the bytes are part of the identity.
    """
    from verification.schema import RESPONSE_JSON_SCHEMA
    from verification.wire import to_wire_schema, wire_schema_fingerprint

    return wire_schema_fingerprint(to_wire_schema(RESPONSE_JSON_SCHEMA))


def validator_version() -> str:
    """A content hash of the canonical post-parse validator.

    Load-bearing since the amendment: constraints the provider is no longer asked
    to enforce are enforced only here, so a capability result is only meaningful
    alongside the validator that was in force when it was measured.
    """
    from verification.schema import canonical_validator_version

    return canonical_validator_version()


def inference_config_for(target: LLMTarget):
    """The active inference configuration, from the same code that builds requests.

    Sized against the verifier's own workload -- the section 2 canonical
    decomposition's requirement count -- so `effective_token_ceiling` is the
    ceiling the verifier actually sends rather than a nominal default. A gate that
    fingerprinted a different ceiling than production sends would qualify a
    request nobody makes.
    """
    from llm import inference_config
    from verification.regression_cases import DECOMPOSITION
    from verification.verify import TEMPERATURE, max_tokens_for

    return inference_config(
        target,
        max_tokens=max_tokens_for(DECOMPOSITION),
        temperature=TEMPERATURE,
    )


def inference_config_fingerprint(target: LLMTarget) -> str:
    """One comparable value covering every behaviour-affecting inference setting."""
    if target.provider is Provider.STUB:
        # The stub has no address and no settings. A constant, so a `test` artifact
        # cannot accidentally collide with a real runtime's fingerprint.
        return "sha256:stub"
    return inference_config_for(target).fingerprint


def adapter_revision(target: LLMTarget) -> str:
    """The client library **and this codebase's own adapter behaviour**.

    Part of the identity because the adapter is half of what §14.2.1 tests. The
    SDK decides how `response_format` is serialized and how a refusal is surfaced,
    and an SDK upgrade can change both without the model changing at all — but the
    SDK version alone is not the adapter. What `response_format` is built *from*,
    which statuses are retried, and how truncation is classified are decided in
    `llm.py`, and any of them can change while `openai.__version__` stands still.
    Both halves are recorded.
    """
    if target.provider is Provider.OPENAI:
        from llm import OPENAI_ADAPTER_BEHAVIOUR_VERSION

        try:
            import openai

            sdk = f"openai-python/{openai.__version__}"
        except Exception:  # pragma: no cover - the SDK is a hard dependency
            sdk = "openai-python/unknown"
        return f"{sdk}+behaviour/{OPENAI_ADAPTER_BEHAVIOUR_VERSION}"
    if target.provider is Provider.OPENAI_COMPATIBLE:
        import requests

        return f"requests/{requests.__version__}"
    return "stub/1"


@dataclass(frozen=True, slots=True)
class RuntimeIdentity:
    """What a capability result is scoped to. Contains no credential."""

    deployment_profile: str
    provider: str
    endpoint: dict[str, str]
    model: str
    model_revision: str
    adapter_revision: str
    schema_version: str
    wire_schema_version: str
    validator_version: str
    inference_config_fingerprint: str
    runner_version: str

    @classmethod
    def for_target(
        cls, target: LLMTarget, *, model_revision: str = ""
    ) -> "RuntimeIdentity":
        return cls(
            deployment_profile=str(target.profile),
            provider=str(target.provider),
            endpoint=target.endpoint,
            model=target.model,
            model_revision=model_revision,
            adapter_revision=adapter_revision(target),
            schema_version=schema_version(),
            wire_schema_version=wire_schema_version(),
            validator_version=validator_version(),
            inference_config_fingerprint=inference_config_fingerprint(target),
            runner_version=CAPABILITY_RUNNER_VERSION,
        )

    def mismatches(self, other: "RuntimeIdentity") -> list[str]:
        """Every identity field on which `self` and `other` differ.

        All of them, not the first: an operator whose profile *and* model changed
        deserves to be told both rather than to fix one and be refused again.

        `model_revision` is compared **only when the configured model is itself a
        pinned snapshot**. When it is, model and revision are the same statement
        and a difference is a real conflict. When the configured model is a
        floating alias, the provider may move it under us and no static
        comparison can detect that — so the artifact records the revision it
        qualified as evidence, `model_pinned` records that the window exists, and
        the honest answer is to pin the snapshot rather than to pretend the check
        is stronger than it is.
        """
        differences = []
        for name in IDENTITY_FIELDS:
            mine, theirs = getattr(self, name), getattr(other, name)
            if mine != theirs:
                differences.append(f"{name}: artifact={theirs!r} runtime={mine!r}")
        if (
            self.model_revision
            and other.model_revision
            and self.model_revision != other.model_revision
        ):
            differences.append(
                f"model_revision: artifact={other.model_revision!r} "
                f"runtime={self.model_revision!r}"
            )
        return differences

    def matches(self, other: "RuntimeIdentity") -> bool:
        return not self.mismatches(other)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class CapabilityNotQualified(RuntimeError):
    """No eligible capability artifact matches the active runtime.

    Carries the reasons as structured text so an admission response can quote
    them; every one names a configuration fact, never a credential.
    """

    code = "CAPABILITY_NOT_QUALIFIED"

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__(
            "no eligible structured-output capability artifact matches this runtime: "
            + "; ".join(reasons)
        )


def artifact_filename(identity: RuntimeIdentity, stamp: str) -> str:
    """`structured-output-capability-<profile>-<fingerprint>-<date>.json`.

    The fingerprint is a hash of the whole identity, so two artifacts for the
    same profile on the same day — a model change, say — cannot overwrite each
    other. Silently replacing a qualification with a different one is how a
    record ends up describing a runtime nobody tested.
    """
    canonical = json.dumps(identity.as_dict(), sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return (
        f"structured-output-capability-{identity.deployment_profile}-"
        f"{fingerprint}-{stamp}.json"
    )


def _identity_from_record(record: dict[str, Any]) -> Optional[RuntimeIdentity]:
    payload = record.get("runtime_identity")
    if not isinstance(payload, dict):
        return None
    try:
        return RuntimeIdentity(
            deployment_profile=str(payload["deployment_profile"]),
            provider=str(payload["provider"]),
            endpoint=dict(payload["endpoint"]),
            model=str(payload["model"]),
            model_revision=str(payload.get("model_revision", "")),
            adapter_revision=str(payload["adapter_revision"]),
            schema_version=str(payload["schema_version"]),
            # No `.get(..., "")` default on these two. An artifact written before
            # the 2026-08-29 amendment has neither key, and defaulting them to ""
            # would let it compare equal to a runtime that also happened to render
            # "" -- so a missing key raises KeyError here and the record is
            # skipped as unreadable, which is exactly what it is: a measurement of
            # a runtime whose identity was not yet fully described.
            wire_schema_version=str(payload["wire_schema_version"]),
            validator_version=str(payload["validator_version"]),
            # No default here either. An artifact written before the inference
            # identity existed has no such key, and defaulting it to "" would let
            # it compare equal to a runtime that also rendered "" -- which is
            # exactly how an unset-reasoning-effort measurement would have
            # qualified a `high` runtime.
            inference_config_fingerprint=str(payload["inference_config_fingerprint"]),
            runner_version=str(payload["runner_version"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def load_artifacts(directory: Optional[Path] = None) -> list[dict[str, Any]]:
    """Every readable capability record on disk.

    An unreadable or non-JSON file is skipped rather than raising: a stray file in
    the measurements directory must not be able to stop admission, and it cannot
    qualify anything either, because it will never produce a matching identity.
    """
    root = directory or ARTIFACT_DIRECTORY
    records: list[dict[str, Any]] = []
    if not root.is_dir():
        return records
    for path in sorted(root.glob("structured-output-capability-*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(record, dict):
            record["_path"] = str(path)
            records.append(record)
    return records


def require_capability(
    target: LLMTarget, *, directory: Optional[Path] = None
) -> dict[str, Any]:
    """The eligible artifact qualifying `target`, or refuse.

    Called at verified-run admission. A search-only run never reaches it — the
    amendment changed which provider development talks to, not what a search
    does.

    Raises:
        CapabilityNotQualified: no artifact, none matching, or none eligible.
    """
    runtime = RuntimeIdentity.for_target(target)
    records = load_artifacts(directory)

    if not records:
        raise CapabilityNotQualified(
            [
                f"no capability artifact found in {directory or ARTIFACT_DIRECTORY}",
                f"run `python -m verification.capability_test` under "
                f"DEPLOYMENT_PROFILE={target.profile.value}",
            ]
        )

    reasons: list[str] = []
    for record in records:
        identity = _identity_from_record(record)
        if identity is None:
            reasons.append(f"{Path(record['_path']).name}: no readable runtime_identity")
            continue
        differences = runtime.mismatches(identity)
        if differences:
            reasons.append(f"{Path(record['_path']).name}: " + "; ".join(differences))
            continue
        if not record.get("eligible"):
            # A matching artifact that says NOT eligible is the strongest
            # possible refusal: this exact runtime was tested and failed.
            raise CapabilityNotQualified(
                [
                    f"{Path(record['_path']).name} matches this runtime and records "
                    "eligible=false. This exact configuration was tested and did not "
                    "meet spec §14.1. Do not weaken the verifier; record a §14.2.2 "
                    "decision or select another model explicitly."
                ]
            )
        return record

    raise CapabilityNotQualified(reasons)


def describe_runtime(target: LLMTarget) -> dict[str, Any]:
    """The active runtime identity, for an operator or a report. No credential."""
    identity = RuntimeIdentity.for_target(target)
    return {
        **identity.as_dict(),
        "model_pinned": target.model_is_pinned,
    }


__all__ = [
    "ARTIFACT_DIRECTORY",
    "CAPABILITY_RUNNER_VERSION",
    "CapabilityNotQualified",
    "IDENTITY_FIELDS",
    "RuntimeIdentity",
    "adapter_revision",
    "artifact_filename",
    "describe_runtime",
    "load_artifacts",
    "require_capability",
    "inference_config_fingerprint",
    "inference_config_for",
    "schema_version",
    "validator_version",
    "wire_schema_version",
]
