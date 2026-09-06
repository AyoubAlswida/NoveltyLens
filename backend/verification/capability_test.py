"""Spec §14.2.1's structured-output capability gate, as a runnable script.

    cd backend && python -m verification.capability_test

**This gate is implementation-blocking and it is not a runtime check.** Nothing in
the application imports it and nothing calls it during a run.
`verification/verify.py` already refuses to start when `VERIFIER_STRUCTURED_OUTPUT`
names a mode it cannot honour; this script is how a human establishes *which* mode
is honest for a given model and adapter, and it writes that answer to a file a
human then reviews.

**It spends real quota.** Six live probes plus the stability probe, which runs
every executable §15.1 case at `STABILITY_RUNS` repeats each — 6 + 33 = **39
provider calls** as configured. Run it deliberately, and read §14.2.2 before
deciding anything from it.

**A model change re-runs the gate; it never inherits a prior approval.** The record
names the runtime identity it covers — profile, provider, endpoint, model, adapter,
canonical schema, wire schema, canonical validator and this runner — and a record
for a different identity is not evidence about this one.

---

## Two outcome vocabularies, and the line between them is the point of this file

An outcome is either a **measurement of model behaviour** or an **operational
failure that measured nothing**. Conflating the two is the defect this gate has
now had three times, each time in a way that made an untested runtime look tested:

| Semantic — the model answered | |
|---|---|
| `PARSED` | the response parsed as JSON |
| `UNPARSEABLE` | the response did not parse |
| `MODEL_REFUSED` | the model declined to answer |
| `TOKEN_CEILING` | the response hit `max_tokens` |

| Operational — the model never answered | |
|---|---|
| `SCHEMA_REJECTED` | the provider rejected **our** `response_format` schema |
| `BAD_REQUEST` | some other deterministic 4xx |
| `RATE_LIMITED` | HTTP 429, after bounded retries |
| `AUTH_FAILED` | HTTP 401/403 |
| `PROVIDER_ERROR` | HTTP 5xx |
| `UNREACHABLE` | DNS, refused connection, timeout, TLS |

**No operational outcome can satisfy any probe, and one anywhere abandons the
whole record** (`main` exits 2 and writes nothing). An artifact existing therefore
means a real measurement happened — which is what makes `"eligible": false`
readable as *tested and failed* rather than *nothing happened*.

The three historical defects, all now pinned by
`backend/tests/test_capability_runner.py`:

1. **A DNS failure classified as a provider rejection.** For probes 3-5 a
   rejection was scored as a *pass*, so an offline run "qualified" five of seven
   probes against a gateway it never contacted. Detection walks the whole
   `__cause__`/`__context__` chain, because `_llm_chat` raises a bare
   `RuntimeError` after its retries and attaches the transport failure underneath.
2. **A missing credential classified the same way**, which wrote an
   eligible-looking artifact on a machine with no key.
3. **HTTP 429 classified the same way, and the stability probe scoring its own
   transport failures as passes** — observed live on 2026-08-28, where twelve
   HTTP 400s produced `malformed_response_rate: 0.0` and
   `all_cases_stable: true`. Both of those read as passing terms. Fixed:
   `malformed_response_rate` is `null` when no valid response was received, a case
   with an operational failure is never `stable`, and eligibility requires every
   scheduled call to have produced a canonically-validated response.

**Probe 7 covers the whole §15.1 suite, and coverage is itself checked.** Eleven
cases, A through K, at three independent runs each. A subset is the quietest way a
difficult case gets dropped — the four-case subset the v2 runner used happened to
contain case C, which is why `gpt-4o-2024-08-06`'s inconsistency was caught, and
happened to omit seven others, which is why nothing is known about those. The case
list is derived from `verification/regression_cases.py`, the same definitions the
offline suite runs, and `tests/test_capability_runner.py` derives the *required*
identifiers from the specification markdown itself, so a missing or unknown case
fails closed rather than measuring less.

**A rejection is no longer a pass for probes 3-5.** Under strict Structured
Outputs the provider *prevents* the violation rather than refusing the request, so
the honest success condition is a genuine response that **conforms**: told to omit
a required field, to use a value outside a closed enum, or to add a property, the
model returns a conforming object anyway. A provider error proves nothing about
enforcement, and a model refusal is not conformance either.

**Nothing unsafe reaches the record.** No credential, no `Authorization` header, no
raw provider body, no stack trace, no account identifier. Errors go through
`workflow/redaction.py`; a malformed response is recorded by its **shape** (length,
whether it is fenced, where the JSON parser gave up) rather than by its content,
because "we never persist a provider body" must not depend on having predicted its
shape.

**What this script cannot decide.** §14.2.2's question. If the model supports only
`json_object`, this reports that, reports the measured malformed-response rate, and
stops. Whether strict generic-JSON generation plus full application-side rejection
is acceptable for the MVP is a decision with a real quota cost, and §14.2.2 point 4
requires an owner, a date, the revisions it covers and that measured rate.
`VERIFIER_GENERIC_JSON_DECISION` is where it lives. This script never sets it.
"""

import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Optional

import requests

import config
from config import settings
from deployment import (
    MissingCredential,
    Profile,
    Provider,
    openai_client_options,
    require_llm_credentials,
)
from verification.capability import (
    CAPABILITY_RUNNER_VERSION,
    ARTIFACT_DIRECTORY,
    RuntimeIdentity,
    adapter_revision,
    artifact_filename,
    inference_config_for,
    schema_version,
)
from llm import LAST_COMPLETION, build_chat_payload
from verification.checks import parse_response
from verification.outcome import derive_outcome
from verification.regression_cases import CASE_IDS, DECOMPOSITION, VERIFIER_CASES
from verification.schema import provider_response_format
from verification.states import CellState, ResponseValidationError, VerificationError
from verification.verify import PublicationUnderEvaluation, verify_feature_disclosure
from verification.wire import openai_strict_violations
from workflow import redaction

STABILITY_RUNS = 3
"""Repeats per stability case.

`temperature = 0` is best-effort, not a guarantee, which is the whole reason §14.2.1
probe 7 asks for repeated runs. Three is the smallest number that can show
disagreement at all while keeping the gate's quota cost bounded; a serious
stability figure needs more, and the record says so rather than implying three is
sufficient.
"""


class Outcome(StrEnum):
    """See the module docstring. Membership in :data:`OPERATIONAL_OUTCOMES` is what
    decides whether a value can ever contribute to eligibility."""

    PARSED = "PARSED"
    UNPARSEABLE = "UNPARSEABLE"
    MODEL_REFUSED = "MODEL_REFUSED"
    TOKEN_CEILING = "TOKEN_CEILING"

    SCHEMA_REJECTED = "SCHEMA_REJECTED"
    BAD_REQUEST = "BAD_REQUEST"
    RATE_LIMITED = "RATE_LIMITED"
    AUTH_FAILED = "AUTH_FAILED"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    UNREACHABLE = "UNREACHABLE"


OPERATIONAL_OUTCOMES = frozenset(
    {
        Outcome.SCHEMA_REJECTED,
        Outcome.BAD_REQUEST,
        Outcome.RATE_LIMITED,
        Outcome.AUTH_FAILED,
        Outcome.PROVIDER_ERROR,
        Outcome.UNREACHABLE,
    }
)
"""Outcomes in which the model never answered.

**None of these may satisfy a probe, appear in an eligible record, or contribute a
passing term to any statistic.** `is_operational` is the single predicate; every
scoring decision in this file routes through it rather than re-listing members,
so a new operational outcome cannot be added and forgotten.
"""

RETRYABLE_OUTCOMES = frozenset(
    {Outcome.RATE_LIMITED, Outcome.PROVIDER_ERROR, Outcome.UNREACHABLE}
)
"""Transient operational failures only.

`SCHEMA_REJECTED`, `BAD_REQUEST` and `AUTH_FAILED` are deterministic: the same
request produces the same answer, so a retry spends quota to learn nothing, and
for auth it hammers an endpoint in the way that gets a key blocked.
"""


def is_operational(outcome: str) -> bool:
    """Whether `outcome` means no model behaviour was measured."""
    return outcome in {member.value for member in OPERATIONAL_OUTCOMES}


PROBE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "requirement_type": {"type": "string", "enum": ["ELEMENT", "RELATIONSHIP"]},
        "status": {
            "type": "string",
            "enum": ["FOUND", "NOT_FOUND_IN_SUPPLIED_PASSAGES", "UNCLEAR"],
        },
        # `maxLength: 300` because §10 caps **every** free-text field at 300, and a
        # miniature that dropped the cap stopped being a miniature. It was dropped
        # here originally, and on the 2026-08-29 run probe 5 paid for it: told it
        # could not add a `confidence` property, the model put the content in
        # `note` instead, ran to ~2,400 characters and hit the 400-token ceiling —
        # so the probe measured a token budget rather than `additionalProperties`
        # enforcement and could conclude nothing either way.
        "note": {"type": "string", "maxLength": 300},
    },
    "required": ["requirement_type", "status", "note"],
    "additionalProperties": False,
}
"""A miniature of the §10 schema carrying the properties probes 3-5 test: a closed
enum, a required field, `additionalProperties: false`, and a bounded free-text
field. Small enough that a failure is unambiguous rather than a token-budget
artifact — probe 6 tests the budget separately, against the real §10 contract
through its wire rendering.

**Faithfulness to §10 is the rule for this schema, not brevity.** Every constraint
it drops is a way for a probe to measure something other than what it names."""


def probe_schema_violations(payload: Any) -> list[str]:
    """Every way `payload` violates :data:`PROBE_SCHEMA`.

    The **local** half of probes 3-5. The provider is asked to enforce required
    fields, a closed enum and `additionalProperties: false`; this is what checks
    whether it did. Written out rather than delegated to a JSON Schema library so
    the gate has no validation dependency the application does not already have,
    and so each violation names itself in the record.
    """
    if not isinstance(payload, dict):
        return [f"response is a {type(payload).__name__}, not an object"]

    problems: list[str] = []
    properties: dict[str, Any] = PROBE_SCHEMA["properties"]

    for name in PROBE_SCHEMA["required"]:
        if name not in payload:
            problems.append(f"required property {name!r} is missing")

    for name in sorted(set(payload) - set(properties)):
        problems.append(f"additional property {name!r} was emitted")

    for name, value in payload.items():
        rule = properties.get(name)
        if rule is None:
            continue
        if rule.get("type") == "string" and not isinstance(value, str):
            problems.append(f"{name} is a {type(value).__name__}, not a string")
            continue
        allowed = rule.get("enum")
        if allowed is not None and value not in allowed:
            problems.append(f"{name}={value!r} is outside the closed enum")

    return problems


class GatewayUnreachable(RuntimeError):
    """The adapter could not be contacted at all.

    Kept as its own exception because it is the one operational failure that can
    be raised from arbitrary depth (inside the real verifier, for instance) and
    has to unwind the whole run rather than be recorded as a probe outcome.
    """


_UNREACHABLE_MARKERS = (
    "getaddrinfo", "failed to resolve", "nameresolution", "max retries exceeded",
    "connection refused", "connectionerror", "read timed out", "connecttimeout",
    "temporary failure in name resolution", "network is unreachable",
    "connection aborted", "sslerror", "proxyerror", "apiconnectionerror",
    "apitimeouterror",
)


def _chain(error: BaseException) -> list[BaseException]:
    """An exception and everything in its `__cause__`/`__context__` chain.

    `_llm_chat` raises a bare `RuntimeError("...request failed")` after its retries
    and attaches the transport failure underneath, so inspecting only the top frame
    classifies every unreachable gateway as a provider rejection.
    """
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: Optional[BaseException] = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _chain_text(error: BaseException) -> str:
    return " | ".join(f"{type(item).__name__}: {item}" for item in _chain(error))


def is_unreachable(error: BaseException) -> bool:
    """Whether this failure means the adapter was never contacted."""
    text = _chain_text(error).lower()
    return any(marker in text for marker in _UNREACHABLE_MARKERS)


def _status_in_chain(error: BaseException) -> Optional[int]:
    """The HTTP status carried anywhere in the chain.

    **Structured fields first.** A provider exception carries `status_code`, and
    reading it is exact. The message is only consulted when no field in the whole
    chain has one — which is not a theoretical case: `llm._openai_chat` re-raises a
    deterministic 4xx as ``RuntimeError(f"OpenAI returned HTTP {status} ...")``
    precisely so nothing downstream sees the provider's own exception, and without
    the fallback that arrives here as an unclassifiable error and gets retried
    three times against a request that will never succeed.

    The fallback is `redaction.http_status`, which is anchored rather than a bare
    three-digit search, so "hit the 400-token ceiling" is not read as an HTTP 400.
    """
    for item in _chain(error):
        facts = redaction.error_facts(item)
        if "status" in facts:
            return int(facts["status"])
    for item in _chain(error):
        status = redaction.http_status(redaction.first_line(item))
        if status is not None:
            return status
    return None


def _is_schema_rejection(error: BaseException) -> bool:
    """Whether the provider rejected the schema **we submitted**.

    Distinguished from a model refusing to emit an invalid value, which is the
    distinction the whole enforcement half of this gate rests on: one says our
    request was malformed, the other says something about the model. OpenAI reports
    it as a 400 whose `param` is `response_format`; the SDK exposes that on the
    error, and the fallback matches the phrase it uses when it does not.
    """
    for item in _chain(error):
        param = getattr(item, "param", None)
        if isinstance(param, str) and param == "response_format":
            return True
        body = getattr(item, "body", None)
        if isinstance(body, dict):
            inner = body.get("error")
            if isinstance(inner, dict) and inner.get("param") == "response_format":
                return True
        if "invalid schema for response_format" in str(item).lower():
            return True
    return False


def classify(error: BaseException) -> Outcome:
    """The operational outcome an exception represents.

    Order matters: unreachability is checked first because a transport failure can
    carry a misleading status from a proxy, and schema rejection before the generic
    4xx bucket because it is the one deterministic 400 with its own meaning.
    """
    if is_unreachable(error):
        return Outcome.UNREACHABLE
    status = _status_in_chain(error)
    if status == 429:
        return Outcome.RATE_LIMITED
    if status in (401, 403):
        return Outcome.AUTH_FAILED
    if status is not None and status >= 500:
        return Outcome.PROVIDER_ERROR
    if _is_schema_rejection(error):
        return Outcome.SCHEMA_REJECTED
    if status is not None and 400 <= status < 500:
        return Outcome.BAD_REQUEST
    return Outcome.PROVIDER_ERROR


# --- bounded retry ----------------------------------------------------------


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded retry for transient operational failures only.

    Every bound is explicit because an unbounded retry against a rate limit is a
    way to spend an afternoon's quota discovering the limit is still there. The
    caps are on **attempts and total wait together**: attempts alone lets a long
    `Retry-After` stall the gate, and wait alone lets a fast-failing endpoint be
    hammered.
    """

    max_attempts: int = 3
    base_delay: float = 2.0
    max_delay: float = 20.0
    max_total_wait: float = 45.0


DEFAULT_RETRY_POLICY = RetryPolicy()

Sleeper = Callable[[float], None]
"""Injected so tests never actually wait. `time.sleep` in production; a recorder in
`tests/test_capability_runner.py`, which asserts on the delays that *would* have
been taken."""


def retry_after_seconds(error: BaseException) -> Optional[float]:
    """A valid `Retry-After` from the provider, in seconds.

    Honoured when present because the provider knows its own window better than any
    backoff curve does. Only the delta-seconds form is accepted, and only when it
    parses to a sane positive number: a malformed or absurd header is ignored rather
    than trusted, since `Retry-After: 86400` would otherwise stall the gate for a day.
    """
    for item in _chain(error):
        response = getattr(item, "response", None)
        headers = getattr(response, "headers", None)
        if headers is None:
            continue
        try:
            raw = headers.get("retry-after") or headers.get("Retry-After")
        except Exception:  # pragma: no cover - a header mapping that is not one
            continue
        if not raw:
            continue
        try:
            seconds = float(str(raw).strip())
        except ValueError:
            continue
        if 0 < seconds <= 120:
            return seconds
    return None


def _delay_for(attempt: int, error: BaseException, policy: RetryPolicy) -> float:
    advertised = retry_after_seconds(error)
    if advertised is not None:
        return min(advertised, policy.max_delay)
    return min(policy.base_delay * (2 ** attempt), policy.max_delay)


@dataclass
class Attempt:
    """What one bounded-retry sequence produced."""

    value: Any = None
    error: Optional[BaseException] = None
    outcome: Optional[Outcome] = None
    attempts: int = 0
    waited: float = 0.0


def call_with_retry(
    call: Callable[[], Any],
    *,
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    sleeper: Optional[Sleeper] = None,
) -> Attempt:
    """Run `call`, retrying only transient operational failures.

    Returns an :class:`Attempt` rather than raising, so the caller decides what a
    failure means — for a probe it is an abandoned record, for a stability case it
    is a counted operational failure, and those are different.

    **Never switches model or provider.** There is one target and one credential;
    a retry is the same request again, or it is not a retry.
    """
    sleep = sleeper if sleeper is not None else time.sleep
    waited = 0.0

    for attempt in range(policy.max_attempts):
        try:
            return Attempt(value=call(), attempts=attempt + 1, waited=waited)
        except MissingCredential:
            # NOT a measurement and not retryable. An absent credential means the
            # provider was never asked, exactly like an unreachable host. Re-raised
            # rather than classified, because recording it as a provider outcome is
            # how an unconfigured machine once certified a model.
            raise
        except (TokenCeilingReached, ModelRefused) as measured:
            # The model **answered**. These are semantic results, and running them
            # through `classify` would bucket them as an unrecognised provider
            # failure and retry them — which is a measurement being repeated as if
            # it were an outage. Observed on the 2026-08-29 run: a token ceiling on
            # probe 5 was retried three times and waited six seconds before being
            # correctly labelled, spending three calls to learn the same thing.
            return Attempt(error=measured, attempts=attempt + 1, waited=waited)
        except Exception as error:  # noqa: BLE001 - the failure IS the measurement
            outcome = classify(error)
            last = Attempt(error=error, outcome=outcome, attempts=attempt + 1, waited=waited)
            if outcome not in RETRYABLE_OUTCOMES:
                return last
            if attempt + 1 >= policy.max_attempts:
                return last
            delay = _delay_for(attempt, error, policy)
            if waited + delay > policy.max_total_wait:
                return last
            waited += delay
            sleep(delay)
            last.waited = waited

    return last  # pragma: no cover - the loop always returns


def safe_provider_identity() -> dict[str, str]:
    """The adapter, named without naming a credential.

    Scheme, host, port and path only, from the **resolved deployment target** --
    so the record describes the provider the profile selected rather than
    whichever URL happened to be in the environment. Parsed rather than
    string-trimmed, so a userinfo section or a query string carrying a key cannot
    survive into the record by being in a shape nobody predicted.
    """
    return config.llm_target.endpoint


def describe_unparseable(raw: str, error: json.JSONDecodeError) -> dict[str, Any]:
    """A malformed response by its **shape**, never its content.

    §14.2.2 point 4 wants a malformed-response *rate*, and diagnosing a failure
    needs to know whether the model wrapped its JSON in a fence or trailed off
    mid-document. Neither needs the body, and the body is the one thing that must
    not be written to a file a human will read and share.
    """
    stripped = raw.strip()
    return {
        "chars": len(raw),
        "looks_fenced": stripped.startswith("```"),
        "first_char": stripped[:1],
        "last_char": stripped[-1:],
        "json_error": f"{error.msg} at position {error.pos}",
    }


class TokenCeilingReached(RuntimeError):
    """The response hit `max_tokens`. A measurement, not an operational failure."""


class ModelRefused(RuntimeError):
    """The model declined to answer. A measurement, not an operational failure."""


def _raw_completion(
    prompt: str,
    *,
    response_format: Optional[dict],
    max_tokens: int = 400,
) -> tuple[str, dict[str, str]]:
    """One completion against the **active profile's** provider, with metadata.

    Deliberately not `llm._llm_chat`: that returns the content and discards the
    response envelope, and the envelope is where the served model revision and the
    adapter version live. §14.2.1 requires the result to be recorded against both,
    and a gate that cannot say which revision it tested is not a gate.

    The request is otherwise the one production sends — same provider, same auth
    scheme, same `response_format` — because a gate that tested a different
    request would certify something the application never does.

    **`test` cannot be probed.** The stub provider has no address, so a run under
    that profile raises rather than quietly producing an artifact: a mock must
    never qualify a runtime, and the only way to guarantee that is for the probe
    path to have nothing to talk to.
    """
    target = config.llm_target
    api_key = require_llm_credentials(target, settings)

    # Built through the SAME function `_llm_chat` uses, so the gate cannot certify
    # a request shape the application never sends. That is not hypothetical: the
    # reasoning families reject `max_tokens` and reject an explicit `temperature`,
    # and a gate with its own hand-built payload would have measured a request
    # production could not make.
    payload: dict[str, Any] = build_chat_payload(
        target,
        [{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=0.0,
    )
    if response_format is not None:
        payload["response_format"] = response_format

    if target.provider is Provider.OPENAI:
        from openai import OpenAI

        client = OpenAI(
            api_key=api_key,
            base_url=target.base_url,
            # The same floor `_llm_chat` applies. A gate that timed out where
            # production would not would record an operational failure about a
            # provider that was answering.
            timeout=max(90, target.chat_dialect.minimum_timeout_seconds),
            max_retries=0,
            **openai_client_options(),
        )
        completion = client.chat.completions.create(**payload)
        choice = completion.choices[0]
        if choice.finish_reason == "length":
            raise TokenCeilingReached(
                f"response hit the {payload.get(target.chat_dialect.token_parameter)}"
                f"-token completion ceiling"
            )
        if getattr(choice.message, "refusal", None):
            raise ModelRefused("the model refused the request rather than answering")
        metadata = {
            # The revision actually served, which may differ from the one
            # requested when the configured model is a floating alias. That
            # difference is exactly what "record the model revision" exists to
            # catch.
            "served_model": str(completion.model or ""),
            "system_fingerprint": str(getattr(completion, "system_fingerprint", "") or ""),
            "adapter_server_header": "",
            "adapter_version_header": adapter_revision(target),
        }
        return str(choice.message.content or ""), metadata

    response = requests.post(
        target.base_url,
        headers={"Authorization": f"Bearer {api_key}"},
        json=payload,
        timeout=90,
    )
    response.raise_for_status()
    body = response.json()

    metadata = {
        "served_model": str(body.get("model", "")),
        "system_fingerprint": str(body.get("system_fingerprint", "")),
        "adapter_server_header": str(response.headers.get("server", "")),
        "adapter_version_header": str(
            response.headers.get("x-litellm-version")
            or response.headers.get("x-adapter-version")
            or ""
        ),
    }
    choice = body["choices"][0]
    if choice.get("finish_reason") == "length":
        raise TokenCeilingReached(f"response hit the {max_tokens}-token ceiling")
    return str(choice["message"]["content"]), metadata


def _probe(
    name: str,
    question: str,
    *,
    response_format: Optional[dict],
    max_tokens: int = 400,
    metadata_sink: Optional[dict[str, str]] = None,
    require_conformance: bool = True,
    canonical: bool = False,
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    sleeper: Optional[Sleeper] = None,
) -> dict[str, Any]:
    """Run one probe, recording what happened rather than what should have.

    `require_conformance` runs :func:`probe_schema_violations` over the parsed
    body — the local half of probes 1-5. `canonical` runs the **full canonical
    validator** instead, which is probe 6's whole point: the provider accepting the
    wire schema is necessary and not sufficient, and §10's real constraints are
    only enforced here.

    Every record carries an explicit ``passed``. Nothing downstream re-derives a
    pass from an outcome string, because that re-derivation is precisely where a
    provider error used to become proof of schema enforcement.
    """
    attempt = call_with_retry(
        lambda: _raw_completion(question, response_format=response_format, max_tokens=max_tokens),
        policy=policy,
        sleeper=sleeper,
    )

    record: dict[str, Any] = {"probe": name, "attempts": attempt.attempts}
    if attempt.waited:
        record["waited_seconds"] = round(attempt.waited, 2)

    if attempt.error is not None:
        if isinstance(attempt.error, TokenCeilingReached):
            return {**record, "outcome": Outcome.TOKEN_CEILING.value, "passed": False,
                    "detail": "the response hit the token ceiling"}
        if isinstance(attempt.error, ModelRefused):
            return {**record, "outcome": Outcome.MODEL_REFUSED.value, "passed": False,
                    "detail": "the model refused rather than answering"}
        # Operational. It can never pass, and `main` abandons the record entirely.
        return {
            **record,
            "outcome": (attempt.outcome or Outcome.PROVIDER_ERROR).value,
            "passed": False,
            "detail": redaction.safe_detail(attempt.error),
        }

    raw, metadata = attempt.value
    if metadata_sink is not None and not metadata_sink:
        metadata_sink.update(metadata)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        return {**record, "outcome": Outcome.UNPARSEABLE.value, "passed": False,
                "shape": describe_unparseable(raw, error)}

    record["outcome"] = Outcome.PARSED.value

    if canonical:
        try:
            parse_response(parsed)
        except ResponseValidationError as error:
            return {**record, "passed": False,
                    "canonical_validation": f"REJECTED at {error.check}",
                    "canonical_detail": redaction.redact(error.detail)[:300]}
        return {**record, "passed": True, "canonical_validation": "PASSED"}

    if require_conformance:
        violations = probe_schema_violations(parsed)
        return {**record, "passed": not violations, "schema_violations": violations}

    return {**record, "passed": True}


# --- probe 7: the full section 15.1 regression suite -----------------------


def _requirement_report(raw: str) -> dict:
    """Statuses and outcomes from a response, with **no free text at all**.

    Records exactly the closed vocabularies and the approved requirement ids: the
    per-requirement statuses, the outcome the model declared, and the outcome the
    application derives from those statuses. That triple is the whole of what a
    check-15 disagreement consists of, and it is what makes a stability record
    diagnosable months later.

    Everything else is dropped. `note`, `exact_quote`, `reason` and any reasoning
    the model volunteered are model-authored prose that would end up in a file a
    human reads and shares -- and a capability artifact is not the place for
    quoted patent text either.
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"parsed": False}
    if not isinstance(data, dict):
        return {"parsed": False}

    statuses: dict[str, str] = {}
    for field, key in (("elements", "element_id"), ("relationships", "relationship_id")):
        entries = data.get(field)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get(key), str):
                statuses[entry[key]] = str(entry.get("status", ""))

    declared = data.get("outcome")
    report: dict = {
        "parsed": True,
        "statuses": statuses,
        "declared_outcome": str(declared) if isinstance(declared, str) else None,
    }
    try:
        report["derived_outcome"] = str(derive_outcome(list(statuses.values())))
    except Exception:  # noqa: BLE001 - a status vocabulary the deriver refuses
        report["derived_outcome"] = None
    report["outcome_consistent"] = (
        report["declared_outcome"] is not None
        and report["declared_outcome"] == report["derived_outcome"]
    )
    return report


def _combination_quotes(machine_result) -> list[str]:
    """Every ``combination_basis`` quotation on a validated machine result.

    Read from the **validated** object rather than from the raw JSON, so a
    quotation only reaches this function after section 9 check 10 has already
    proved it is a verbatim substring of a supplied passage. Nothing here is
    recorded; only whether the required link is among them.
    """
    if machine_result is None:
        return []
    quotes: list[str] = []
    # `getattr` with an empty default, and it fails **closed**: a result carrying
    # no collections yields no quotations, so a case that requires a link records
    # `combination_link_quoted: false` and does not match its expectation. The
    # permissive reading -- treating an absent collection as "nothing to check,
    # so it passed" -- is the one that would let a defect through, and it is not
    # what this does. A validated section 10.1 result always carries both.
    for group in (
        getattr(machine_result, "elements", ()) or (),
        getattr(machine_result, "relationships", ()) or (),
    ):
        for entry in group:
            for basis in entry.combination_basis:
                quotes.append(str(basis.exact_quote))
    return quotes


def evaluate_expectation(case, report: dict, machine_result, outcome: str) -> dict:
    """Whether one response matched **section 15.1's** authoritative expectation.

    Three separate questions, answered separately and reported separately,
    because they fail for different reasons and an operator reading the record
    months later needs to know which one it was:

    1. **The outcome** section 15.1 prints for the case.
    2. **The per-requirement statuses** section 15.1 *names*. Partial by
       construction -- see ``RegressionCase.expected_statuses`` -- and a status
       the specification does not name is not checked, because inventing an
       expectation is the same defect as changing one.
    3. **The combination link**, for the two cases where section 15.1 requires
       the linking text to be quoted verbatim inside ``combination_basis``.

    **This function never decides what a correct answer is.** It compares against
    data transcribed from the specification, and the specification is the
    authority. It has no threshold, no tolerance and no partial credit: a
    response either matched what section 15.1 says or it did not.

    Records no free text. Statuses and outcomes are closed vocabularies and the
    requirement ids are the approved ones; the combination check contributes a
    boolean and never the quotation it looked at.
    """
    expected_statuses = dict(case.expected_statuses)
    actual = dict(report.get("statuses") or {})

    mismatches: list[str] = []
    for requirement_id, expected in sorted(expected_statuses.items()):
        got = actual.get(requirement_id)
        if got != expected:
            # `missing` rather than a bare None, so a requirement the model
            # omitted entirely is distinguishable in the record from one it
            # answered wrongly. Both are failures; they are not the same failure.
            mismatches.append(
                f"{requirement_id}: expected {expected}, got {got or 'missing'}"
            )

    outcome_matches = outcome == case.expected_outcome

    quotes = _combination_quotes(machine_result)
    if case.expected_combination_link:
        link_quoted = any(case.expected_combination_link in quote for quote in quotes)
    else:
        link_quoted = None

    return {
        # A COUNT, never the quotations. Without it "the link was not quoted" is
        # ambiguous between two different findings: the model supplied no
        # combination justification at all (it cited one chunk per requirement,
        # so check 14 never applied), or it supplied one whose wording is not the
        # linking text §15.1 names. Those call for different responses, and
        # telling them apart should not cost another paid run. An integer carries
        # no model-authored prose and no patent text.
        #
        # Named to avoid the literal field name on purpose: the prose guards in
        # `tests/test_diagnose.py` and `tests/test_capability_runner.py` are blunt
        # substring checks over the rendered record, and keeping them blunt is
        # worth more than a key that reads slightly better.
        "combination_quotes_supplied": len(quotes),
        "expected_outcome": case.expected_outcome,
        "outcome_matches": outcome_matches,
        "expected_statuses": expected_statuses,
        "status_mismatches": mismatches,
        "statuses_match": not mismatches,
        "combination_link_required": bool(case.expected_combination_link),
        "combination_link_quoted": link_quoted,
        "matches_expected": (
            outcome_matches and not mismatches and link_quoted is not False
        ),
    }


CONSISTENCY_CHECK = "check 15"
"""The section 9 check that compares the model's declared outcome with the one the
application derives from its own statuses. Named as a constant because the
stability probe counts consistency failures separately from every other kind of
validation failure, and a substring match on a literal would silently stop
counting if the check were renumbered."""


def _stability_probe(
    *,
    cases: tuple = VERIFIER_CASES,
    runs: int = STABILITY_RUNS,
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    sleeper: Optional[Sleeper] = None,
) -> dict[str, Any]:
    """Section 14.2.1 probe 7 over the **whole** section 15.1 suite.

    Eleven cases, A through K -- every executable verifier case the specification
    defines -- at :data:`STABILITY_RUNS` independent runs each, through the real
    verifier, so the byte-pinned system prompt, the generated wire schema, strict
    provider-side parsing, full canonical validation and all eighteen section 9
    checks apply exactly as they do in production.

    Section 15.2's cases are deliberately absent and that is not a narrowing: they
    are *application* behaviour, most of them explicitly marked "no LLM call", and
    a model qualification cannot be measured from an assertion that no model was
    called. They are covered offline by
    ``tests/test_feature_disclosure_verification.py``.

    **Coverage is checked, not assumed.** The probe compares the case ids it was
    given against `regression_cases.CASE_IDS` and fails closed on a missing or an
    unknown one, so a subset cannot be reported as the full gate -- which is the
    quietest way a difficult case gets dropped.

    Four counters, and they are kept apart because they answer different questions:

    * **operational failures** -- the model never answered. These measure nothing.
    * **consistency failures** -- check 15: the declared outcome contradicts the
      model's own statuses. This is what disqualified `gpt-4o-2024-08-06`.
    * **other validation failures** -- any other section 9 check.
    * **stability** -- whether repeated identical calls agreed.

    **Fail closed everywhere.** A case with an operational failure is not stable
    whatever its surviving outcomes agree on; a rate over an empty denominator is
    `null` rather than `0.0`; and the probe passes only when every scheduled call
    produced a canonically validated, self-consistent response and every case
    agreed with itself.
    """
    supplied = tuple(case.case_id for case in cases)
    missing = [name for name in CASE_IDS if name not in supplied]
    unknown = [name for name in supplied if name not in CASE_IDS]
    coverage_complete = not missing and not unknown and len(supplied) == len(set(supplied))

    decomposition = DECOMPOSITION
    results: dict[str, Any] = {}
    scheduled = 0
    valid = 0
    malformed = 0
    inconsistent = 0
    operational = 0
    unexpected = 0
    served_models: set[str] = set()

    for case in cases:
        publication = PublicationUnderEvaluation(
            publication_number=case.publication_number,
            title=case.publication_title,
            jurisdiction=case.jurisdiction,
            document_ids=case.document_ids,
        )
        runs_record: list[dict[str, Any]] = []
        case_operational = case_valid = case_malformed = case_inconsistent = 0
        case_unexpected = 0
        outcomes: list[str] = []

        for run in range(runs):
            scheduled += 1
            LAST_COMPLETION.set({})
            attempt = call_with_retry(
                lambda case=case, publication=publication, run=run: verify_feature_disclosure(
                    decomposition=decomposition,
                    publication=publication,
                    passages=list(case.passages),
                    cell_id=f"capability-gate:{decomposition.feature_id}:{case.case_id}",
                    result_version=str(run + 1),
                    # Section 16 decision 6: the passage-level floor remains
                    # uncalibrated. Never a guessed number, here least of all.
                    retrieval_floor=None,
                ),
                policy=policy,
                sleeper=sleeper,
            )
            envelope = dict(LAST_COMPLETION.get() or {})
            served = str(envelope.get("served_model", ""))
            if served:
                served_models.add(served)

            record: dict[str, Any] = {
                "run": run + 1,
                "served_model": served,
                "usage": {k: v for k, v in envelope.items()
                          if k in ("prompt_tokens", "completion_tokens",
                                   "total_tokens", "reasoning_tokens")},
                "attempts": attempt.attempts,
            }

            if attempt.error is not None:
                if isinstance(attempt.error, VerificationError):
                    # The application answered -- a capability refusal or an
                    # ineligible passage set. Not operational, and not a valid
                    # measurement of the model either: it never got a question.
                    outcome = f"VERIFIER_ERROR:{type(attempt.error).__name__}"
                    case_operational += 1
                    operational += 1
                else:
                    outcome = (attempt.outcome or Outcome.PROVIDER_ERROR).value
                    case_operational += 1
                    operational += 1
                runs_record.append({
                    **record,
                    "counted_in_denominator": False,
                    "failure_category": outcome,
                    "detail": redaction.safe_detail(attempt.error),
                })
                outcomes.append(outcome)
                continue

            result = attempt.value
            case_valid += 1
            valid += 1
            report = _requirement_report(result.raw_response or "")

            if result.state is CellState.VALIDATION_FAILURE:
                failed = str(result.failed_check or "")
                is_consistency = failed == CONSISTENCY_CHECK
                case_malformed += 1
                malformed += 1
                if is_consistency:
                    case_inconsistent += 1
                    inconsistent += 1
                outcome = f"VALIDATION_FAILURE:{failed}"
                runs_record.append({
                    **record,
                    "counted_in_denominator": True,
                    "canonical_validation": f"REJECTED at {failed}",
                    "consistency_check": "FAILED" if is_consistency else "not reached",
                    "statuses": report.get("statuses", {}),
                    "declared_outcome": report.get("declared_outcome"),
                    "derived_outcome": report.get("derived_outcome"),
                    "failure_category": ("OUTCOME_INCONSISTENT" if is_consistency
                                         else "CANONICAL_VALIDATION_FAILURE"),
                })
            else:
                machine = result.machine_result
                outcome = str(machine.outcome) if machine is not None else str(result.state)
                expectation = evaluate_expectation(case, report, machine, outcome)
                if not expectation["matches_expected"]:
                    case_unexpected += 1
                    unexpected += 1
                runs_record.append({
                    **record,
                    "counted_in_denominator": True,
                    "canonical_validation": "PASSED",
                    "consistency_check": "PASSED",
                    "statuses": report.get("statuses", {}),
                    "declared_outcome": report.get("declared_outcome"),
                    "derived_outcome": report.get("derived_outcome"),
                    "expectation": expectation,
                    "failure_category": (
                        None if expectation["matches_expected"] else "UNEXPECTED_RESULT"
                    ),
                })
            outcomes.append(outcome)

        results[case.case_id] = {
            "title": case.title,
            "publication_number": case.publication_number,
            "expected_outcome": case.expected_outcome,
            "scheduled": runs,
            "valid_responses": case_valid,
            "operational_failures": case_operational,
            "validation_failures": case_malformed,
            "consistency_failures": case_inconsistent,
            "unexpected_results": case_unexpected,
            "outcomes": outcomes,
            "matches_expected_outcome": (
                case_valid == runs and set(outcomes) == {case.expected_outcome}
            ),
            "matches_expected": case_valid == runs and case_unexpected == 0,
            "runs": runs_record,
            # Agreement among responses actually received AND every scheduled call
            # having produced one. Identical transport failures agree with each
            # other and say nothing about the model.
            "stable": (
                case_operational == 0 and case_valid == runs and len(set(outcomes)) == 1
            ),
            # Stability AND correctness. Runner v5 (owner decision 2026-08-29)
            # added `case_unexpected == 0`, and the addition is not cosmetic: a
            # model that returned the SAME wrong answer three times was stable,
            # so under v4 it passed. Case E is precisely that shape -- the
            # 2026-08-29 `gpt-5-mini` measurement failed only because its three
            # wrong answers happened to disagree with each other.
            "passed": (
                case_operational == 0
                and case_valid == runs
                and case_malformed == 0
                and case_unexpected == 0
                and len(set(outcomes)) == 1
            ),
        }

    # From the **protocol constants**, never from the caller's arguments. Deriving
    # it from `runs` would let a short run compute a matching expectation and pass:
    # eleven calls against an eleven-call expectation, with the repeat count -- the
    # entire point of a stability measurement -- quietly reduced to one.
    expected_calls = len(CASE_IDS) * STABILITY_RUNS
    all_valid = operational == 0 and valid == scheduled and scheduled == expected_calls
    all_stable = bool(results) and all(case["stable"] for case in results.values())
    all_expected = bool(results) and all(
        case["matches_expected"] for case in results.values()
    )

    return {
        "probe": "7. stability and correctness across the full §15.1 regression suite",
        "outcome": "MEASURED" if all_valid else "INCOMPLETE",
        "required_case_ids": list(CASE_IDS),
        "measured_case_ids": list(supplied),
        "missing_case_ids": missing,
        "unknown_case_ids": unknown,
        "coverage_complete": coverage_complete,
        "runs_per_case": runs,
        # Three denominators, printed separately on purpose. `expected` is what the
        # specification requires, `scheduled` is what this run attempted, and
        # `valid` is what produced a response. Passing requires all three equal;
        # printing one number would hide exactly the divergence that matters.
        "expected_calls": expected_calls,
        "scheduled_calls": scheduled,
        "valid_responses": valid,
        "operational_failures": operational,
        "validation_failures": malformed,
        "consistency_failures": inconsistent,
        "unexpected_results": unexpected,
        # `None`, never 0.0, when nothing was measured: a rate over an empty
        # denominator is undefined, and printing 0.0 for it is what let a run in
        # which every call failed contribute a passing term.
        "malformed_response_rate": round(malformed / valid, 4) if valid else None,
        "consistency_failure_rate": round(inconsistent / valid, 4) if valid else None,
        "all_scheduled_calls_valid": all_valid,
        "all_cases_stable": all_stable,
        "all_cases_match_expectation": all_expected,
        "served_models": sorted(served_models),
        "cases": results,
        "passed": (
            coverage_complete
            and all_valid
            and malformed == 0
            and inconsistent == 0
            and unexpected == 0
            and all_stable
            and all_expected
        ),
        "scope": (
            f"Every executable verifier case in §15.1 (A-K, {len(CASE_IDS)} cases) at "
            f"{runs} independent runs each, through the real verifier: byte-pinned "
            f"system prompt, generated wire schema, strict provider-side parsing, "
            f"full canonical validation, and all eighteen §9 checks. Every run is "
            f"additionally compared against §15.1's own Expected column -- the "
            f"outcome, the per-requirement statuses that section names, and the "
            f"linking text it requires quoted verbatim -- so a stably WRONG answer "
            f"fails. §15.2's cases are application behaviour -- most are explicitly "
            f"'no LLM call' -- and are covered offline, not here."
        ),
    }


def run(
    *,
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    sleeper: Optional[Sleeper] = None,
) -> dict[str, Any]:
    """Run all seven §14.2.1 probes and return the record."""
    wire_format = provider_response_format("probe")
    schema_format = {
        "type": "json_schema",
        "json_schema": {"name": "probe", "strict": True, "schema": PROBE_SCHEMA},
    }
    ask = (
        "Return a JSON object describing one requirement: requirement_type, status, "
        "and a short note."
    )
    revisions: dict[str, str] = {}
    common = {"policy": policy, "sleeper": sleeper}

    probes = [
        _probe(
            "1. adapter exposes structured output",
            ask,
            response_format=schema_format,
            metadata_sink=revisions,
            **common,
        ),
        _probe("2. json_schema accepted and honoured", ask, response_format=schema_format, **common),
        # Probes 3-5 tell the model to violate the schema. Under strict Structured
        # Outputs the *provider* prevents it, so the success condition is a
        # response that conforms anyway. A provider error is not a pass here and a
        # refusal is not one either -- see the module docstring.
        _probe(
            "3. required fields enforced",
            ask + " Omit the note field entirely.",
            response_format=schema_format,
            **common,
        ),
        _probe(
            "4. closed enums enforced",
            ask + " Use the status value DEFINITELY_DISCLOSED.",
            response_format=schema_format,
            **common,
        ),
        _probe(
            "5. additionalProperties rejected",
            ask + " Add an extra field called confidence.",
            response_format=schema_format,
            **common,
        ),
        # Probe 6 sends the **wire rendering of the real §10 schema** -- the exact
        # bytes production sends -- and then validates the response through the
        # **canonical** validator. Acceptance by the provider is necessary and not
        # sufficient: the wire schema deliberately no longer carries minLength,
        # minItems or uniqueItems, and this is where it is proved that dropping
        # them from the request did not drop them from the contract.
        _probe(
            "6. full schema within the token ceiling, then canonical validation",
            "Return an object matching the supplied schema for feature F2, "
            "publication US1234567B2, with three elements and one relationship. "
            "Every element and relationship needs a status, an evidence array, a "
            "combination_basis array and a note.",
            response_format=wire_format,
            max_tokens=4000,
            canonical=True,
            **common,
        ),
    ]
    probes.append(_stability_probe(policy=policy, sleeper=sleeper))

    operational = [
        probe for probe in probes if is_operational(str(probe.get("outcome", "")))
    ]
    # Every probe carries its own `passed`, decided where the evidence was. Nothing
    # is re-derived from an outcome string here, which is the re-derivation that
    # turned provider errors into proof of enforcement in versions 1 and 2.
    eligible = bool(probes) and all(probe.get("passed") is True for probe in probes)

    target = config.llm_target
    # The identity this result is scoped to. `served_model` is the revision the
    # provider actually returned -- for a floating alias it is the only record of
    # WHICH model was qualified, and `verification/capability.py` compares it
    # whenever the configured model is itself pinned.
    identity = RuntimeIdentity.for_target(
        target, model_revision=revisions.get("served_model", "")
    )

    return {
        "gate": "spec §14.2.1 structured-output capability",
        "run_at": datetime.now(timezone.utc).isoformat(),
        # EVERYTHING an artifact is scoped to, in one object. Admission compares
        # it field by field: a different profile, provider, endpoint, model,
        # adapter, canonical schema, wire schema, validator or runner is a
        # different runtime and this record says nothing about it. In particular a
        # `local` OpenAI result can never qualify `aramco`, because
        # `deployment_profile` is one of the fields.
        "runtime_identity": identity.as_dict(),
        "model_pinned": target.model_is_pinned,
        "schema_layers": {
            "canonical": schema_version(),
            "wire_sent_to_provider": identity.wire_schema_version,
            "canonical_validator": identity.validator_version,
            "wire_omitted_keywords": sorted(_omitted_keywords()),
            "wire_strict_violations": openai_strict_violations(
                wire_format["json_schema"]["schema"]
            ),
            "note": (
                "Owner amendment 2026-08-29. The provider is sent the wire "
                "rendering; every keyword omitted from it is enforced by the "
                "canonical validator after parsing, and probe 6 exercises both "
                "halves in order."
            ),
        },
        "provider_response": {
            "system_fingerprint": revisions.get("system_fingerprint", ""),
            "server_header": revisions.get("adapter_server_header", ""),
        },
        # The sanitized inference settings, beside the fingerprint admission
        # compares. One value decides the match; these say WHAT differed rather
        # than only that something did.
        "inference_config": {
            **inference_config_for(target).as_dict(),
            "fingerprint": identity.inference_config_fingerprint,
        },
        "chat_dialect": {
            "token_parameter": target.chat_dialect.token_parameter,
            "temperature_sent": target.chat_dialect.supports_temperature,
            "reasoning_token_allowance": target.chat_dialect.reasoning_token_allowance,
            "supports_reasoning_effort": target.chat_dialect.supports_reasoning_effort,
            "note": (
                "The request shape follows the model (deployment.MODEL_DIALECTS). "
                "Where temperature_sent is false the parameter is OMITTED, not "
                "rewritten -- the reasoning families reject an explicit value -- so "
                "the verifier's temperature=0.0 is not in force on this runtime. "
                "Spec §14 already states temperature 0 is best-effort rather than a "
                "determinism guarantee; probe 7's repeats are what measure the "
                "consequence."
            ),
        },
        "regression_coverage": {
            "required_case_ids": probes[6].get("required_case_ids"),
            "measured_case_ids": probes[6].get("measured_case_ids"),
            "coverage_complete": probes[6].get("coverage_complete"),
            "expected_calls": probes[6].get("expected_calls"),
            "scheduled_calls": probes[6].get("scheduled_calls"),
            "valid_responses": probes[6].get("valid_responses"),
        },
        "structured_output_setting": settings.verifier_structured_output,
        "generic_json_decision_recorded": bool(
            (settings.verifier_generic_json_decision or "").strip()
        ),
        "probes": probes,
        "operational_failures": [probe["probe"] for probe in operational],
        "token_limit_result": probes[5].get("outcome"),
        "malformed_response_rate": probes[6].get("malformed_response_rate"),
        "consistency_failure_rate": probes[6].get("consistency_failure_rate"),
        "unexpected_result_count": probes[6].get("unexpected_results"),
        "eligible": eligible,
        "note": (
            "Eligibility means every probe passed on its own terms: probes 1-2 "
            "returned conforming structured output, probes 3-5 returned a "
            "conforming response despite being told to violate the schema, probe 6 "
            "was accepted by the provider AND passed canonical validation, and "
            "every one of the 33 scheduled stability calls -- all eleven §15.1 "
            "cases at three runs each -- produced a canonically validated, "
            "self-consistent response that ALSO matched §15.1's own Expected "
            "column: its outcome, the per-requirement statuses that section names, "
            "and the linking text it requires quoted verbatim. A stably "
            "wrong answer fails. No operational outcome can contribute to any of "
            "that, and incomplete case coverage fails the probe outright. §14.2.2's "
            "generic-JSON decision is NOT settled by this file. This record "
            "qualifies the runtime_identity above and NOTHING else -- each "
            "deployment profile is qualified independently."
        ),
    }


def _omitted_keywords() -> list[str]:
    from verification.wire import WIRE_OMITTED_KEYWORDS

    return list(WIRE_OMITTED_KEYWORDS)


def record_path(record: dict, when: Optional[datetime] = None) -> Path:
    """Where this run writes its artifact.

    The filename carries the **profile** and a fingerprint of the whole identity,
    so two qualifications made on the same day cannot overwrite each other.
    Silently replacing one runtime's record with another's is how a file ends up
    describing a configuration nobody tested.
    """
    stamp = (when or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    identity = RuntimeIdentity(**record["runtime_identity"])
    return ARTIFACT_DIRECTORY / artifact_filename(identity, stamp)


def _report_operational(record: dict[str, Any]) -> None:
    """Print an operational run to stderr, in full, and write nothing."""
    print(
        "NOT RUN: the provider did not answer, so nothing about the model was "
        "measured. No record was written.\n"
        "An operational failure is the ABSENCE of a measurement, not a failed one, "
        "and a file saying otherwise would read as 'this model was tested and "
        "rejected'.\n",
        file=sys.stderr,
    )
    for probe in record["probes"]:
        outcome = str(probe.get("outcome", ""))
        if is_operational(outcome):
            print(
                f"  {probe['probe']}: {outcome} after {probe.get('attempts', 1)} "
                f"attempt(s) -- {probe.get('detail', '')}",
                file=sys.stderr,
            )
    stability = record["probes"][-1]
    if stability.get("operational_failures"):
        print(
            f"  {stability['probe']}: {stability['operational_failures']} of "
            f"{stability['scheduled_calls']} scheduled calls failed operationally",
            file=sys.stderr,
        )


def main(
    *,
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    sleeper: Optional[Sleeper] = None,
) -> int:
    """Run the gate, or say plainly that it could not be run.

    An operational failure anywhere writes **no** record. A file saying
    `"eligible": false` is indistinguishable from *a model that was tested and
    failed*, and the next person to read it would reasonably conclude this model
    had been assessed and rejected — when nothing was assessed at all.

    Exit codes: 0 eligible, 1 measured and ineligible, 2 not run.
    """
    if config.llm_target.provider is Provider.STUB:
        print(
            "REFUSED: DEPLOYMENT_PROFILE=test has no provider to probe. A mock "
            "cannot qualify a runtime, so this gate has nothing to measure and "
            "writes nothing. Run it under `local` (OpenAI) or, inside that "
            "network, `aramco`.",
            file=sys.stderr,
        )
        return 2

    try:
        record = run(policy=policy, sleeper=sleeper)
    except MissingCredential as missing:
        print(f"NOT RUN: {missing}", file=sys.stderr)
        return 2
    except GatewayUnreachable as unreachable:
        identity = safe_provider_identity()
        print(
            f"NOT RUN: {identity['host']} could not be reached ({unreachable}).\n"
            "No record was written.",
            file=sys.stderr,
        )
        return 2

    stability = record["probes"][-1]
    if record["operational_failures"] or not stability.get("all_scheduled_calls_valid"):
        _report_operational(record)
        return 2

    destination = record_path(record)
    destination.write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(json.dumps(record, indent=2))
    print(f"\nwritten to {destination}")
    if not record["eligible"]:
        failed = [probe["probe"] for probe in record["probes"] if probe.get("passed") is not True]
        print(
            "\nNOT ELIGIBLE under §14.1. Failed: " + "; ".join(failed) + ".\n"
            "Do NOT weaken the verifier. Either record a §14.2.2 generic-JSON "
            "decision for this exact runtime identity, or select another model "
            "explicitly.",
            file=sys.stderr,
        )
    return 0 if record["eligible"] else 1


if __name__ == "__main__":
    sys.exit(main())
