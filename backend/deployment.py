"""Deployment profiles: which environment this process believes it is running in.

**The profile is declared, never inferred.** Not from the endpoint URL, not from
the hostname, not from which API key happens to be set, not from the model name.
Inference is precisely how a process ends up believing it is somewhere it is not
— and the thing that belief gates here is whether a *capability qualification*
performed somewhere else may be reused. A URL that looks like OpenAI is not
evidence that this is the development machine.

**One profile selects one provider.** There is deliberately no separate
`LLM_PROVIDER` setting to drift out of step with the profile: a second knob is a
second thing to get inconsistent, and "profile says local, provider says
gateway" is exactly the internal inconsistency this module has to refuse.

| Profile | Provider | Settings it reads | Status |
|---|---|---|---|
| `local` | `openai` | `OPENAI_*` | development (amendment 2026-08-28) |
| `huggingface` | `huggingface` | `LLM_API_URL`, `LLM_MODEL`, `HF_TOKEN` | HF Router; explicit inference provider |
| `aramco` | `openai_compatible` | `LLM_API_*`, `LLM_MODEL` | **deferred and blocked** |
| `test` | `stub` | none | fixtures only |

**Fail closed, at import.** An unset profile, an unknown profile, or a profile
whose required settings are missing raises `ConfigError` while the module is
being imported. The alternative — defaulting to something — is how a machine
runs a whole afternoon against a provider nobody chose.

**Credentials are checked where they are used, not at import.** A missing
`OPENAI_API_KEY` is a real failure, but raising it at import would stop
`migrate.py`, `checkpointer.py setup` and the test suite from running at all, none
of which touch an LLM. `require_llm_credentials()` is called on the LLM call path
and at verified-run admission, which are the two places the absence actually
matters.
"""

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Optional
from urllib.parse import urlparse

OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"

OPENAI_DEFAULT_MODEL = "gpt-5.4-mini-2026-03-17"
"""The default development model, pinned to a **snapshot** rather than a floating
alias.

A capability artifact records the revision it qualified. An alias can be moved to
a new snapshot by the provider at any time, which silently invalidates that
record without changing a single line of configuration — the artifact would still
match on every field while describing a model that no longer exists. A pinned
snapshot closes that window, and `capability.py` records whether the configured
model is pinned so an unpinned one is a visible, stated limitation rather than an
invisible hole.

**Selected by owner decision on 2026-08-29**, recorded in
`docs/decisions/2026-08-29-verifier-model-selection.md`. It is the **third**
explicitly selected model for the `local` profile, and the two before it were
each measured and found ineligible — for different reasons, neither of them
structured output:

| Model | Measured | Why it is ineligible |
|---|---|---|
| `gpt-4o-2024-08-06` | 2026-08-29 | probes 1-6 pass; probe 7 measured a 0.25 malformed-response rate — every §15.1 case C run returned per-requirement statuses contradicting its own declared `outcome`, which §9 check 15 rejects |
| `gpt-5-mini-2025-08-07`, effort unset | 2026-08-29 | probes 1-6 pass, 33/33 calls valid, zero operational/validation/consistency failures; §15.1 case E returned `UNCLEAR`, `PARTIALLY`, `PARTIALLY` — unstable on the conservative-ambiguity case |
| `gpt-5-mini-2025-08-07`, `reasoning_effort=high` | 2026-08-29 | case-E diagnostic passed **0 of 5**; four runs judged E3 `NOT_FOUND` where §15.1 says `UNCLEAR`, and the one run with correct statuses failed §9 check 14 |

Every artifact and diagnostic record from those three stays on disk, stays
accurate about what it measured, and qualifies nothing. **§15.1 case E was
reviewed and is correctly specified**: the byte-pinned §3 prompt states that a
passage depending on an unsupplied referenced figure is an `UNCLEAR` case, so the
expectation stands and the model was wrong. Nothing about the case, the schema,
the prompt or any §9 check was changed to accommodate a measurement.

**This selection is explicit and scoped to `local`.** It says nothing about the
`aramco` profile's model, which remains deferred and unqualified. There is no
automatic fallback and no runtime model-selection chain: a configured model that
fails the gate fails, and choosing another is another recorded decision (spec
§14.2.2 point 5). Three selections in a row is three decisions, not a search
loop — nothing in this codebase may pick the next one.

**Every `gpt-5.x` version speaks its own dialect and each is declared
separately.** `gpt-5.1` is not `gpt-5` under a new name: it removed `minimal`
from the `reasoning_effort` vocabulary and added `none`. `gpt-5.4-mini` is not
`gpt-5.1` under a new name either: it keeps `none` and **adds `xhigh`**. Each has
its own reasoning-token allowance and timeout floor. See :data:`MODEL_DIALECTS`,
and :func:`dialect_for` for why a version suffix can never be inherited.

Overridable with `OPENAI_MODEL`; a different model re-runs the capability gate,
because an artifact never transfers across models.
"""


@dataclass(frozen=True, slots=True)
class ChatDialect:
    """The Chat Completions parameters one model family actually accepts.

    **Declared, never probed.** These are documented properties of the provider's
    API, and the alternative — trying a request and adapting to the 400 — is a
    runtime fallback that would silently change what was sent. A model whose
    dialect is wrong fails deterministically and loudly with the provider naming
    the parameter, which is the signal to add a row to :data:`MODEL_DIALECTS`.

    It is part of the adapter, so a change here changes `adapter_revision` and
    invalidates every capability artifact — the shape of the request is half of
    what §14.2.1 measures.
    """

    token_parameter: str = "max_tokens"
    """`max_tokens` classically; `max_completion_tokens` on the reasoning
    families, which reject the old name outright rather than accepting it as an
    alias."""

    supports_temperature: bool = True
    """The reasoning families accept only their default sampling temperature and
    reject an explicit `temperature` of 0.0 with HTTP 400. Where this is False the
    parameter is **omitted**, never substituted with a different value: sending
    `temperature: 1` would be this code choosing a sampling setting, while omitting
    it leaves the provider's own default in force and says so in the record."""

    reasoning_token_allowance: int = 0
    """Extra output budget for models that bill their own reasoning against the
    completion ceiling.

    Without it every call to a reasoning model truncates: the verifier sizes its
    budget from the requirement count (`900 + 260 x n`, spec §14), which assumes
    the whole budget is available for the answer. Reasoning tokens are invisible in
    `content` and still consume it. This is an allowance, not a limit — it is added
    to whatever the caller asked for, so a caller's own sizing is never reduced."""

    supports_reasoning_effort: bool = False
    """Whether this family accepts the `reasoning_effort` parameter.

    Declared per family rather than assumed from "it is a reasoning model":
    sending it to a family that does not accept it is a deterministic 400, and
    guessing would be the runtime adaptation this module exists to refuse."""

    reasoning_efforts: tuple[str, ...] = ()
    """Every `reasoning_effort` value **this family** accepts, closed and declared.

    Empty means the family accepts none, which is the same statement
    :attr:`supports_reasoning_effort` makes and is checked alongside it.

    Per family rather than global, because the families do **not** agree. The
    `gpt-5` family takes `minimal|low|medium|high`; `gpt-5.1` **replaced
    `minimal` with `none`**, so a value that is valid on one is a deterministic
    HTTP 400 on the other. A single global vocabulary would have let an
    owner-selected `minimal` reach `gpt-5.1` and fail at the provider, after the
    call was made and billed — which is precisely the "assume the previous
    family's dialect" mistake this table exists to prevent.

    Validated in `llm.build_chat_payload` **before** the request is built, and a
    value this family does not accept **raises** rather than being dropped or
    downgraded: a silently-dropped setting runs at an effort nobody chose."""

    minimum_timeout_seconds: int = 0
    """A **floor** under the caller's read timeout, never a reduction of it.

    Spec §14 sets the verifier's timeout at 60s and marks it provisional, to be
    benchmarked against the selected model. A reasoning model spends real wall
    clock producing tokens nobody sees, and a timeout tuned for a non-reasoning
    model turns a working model into a read timeout — which this gate correctly
    classifies `UNREACHABLE`, an operational failure that measures nothing and
    abandons the whole record. Waiting longer for a slow answer is not a
    degradation; recording "the provider could not be reached" about a provider
    that was answering would be."""


MODEL_DIALECTS: tuple[tuple[str, ChatDialect], ...] = (
    (
        "gpt-5.4-mini",
        ChatDialect(
            token_parameter="max_completion_tokens",
            supports_temperature=False,
            # Matched to `gpt-5.1`'s rather than reduced because this model is a
            # "mini". The allowance only ever ADDS to the caller's own budget, so
            # a wider one cannot shorten a response: the cost of being generous is
            # a ceiling that is never reached, and the cost of being tight is
            # `finish_reason: "length"` -- classified TOKEN_CEILING, never
            # retried, and it abandons the whole capability record. This model
            # also accepts `xhigh`, an effort ABOVE `high`, so sizing down from a
            # `high`-effort measurement of a different model would be sizing for
            # the wrong ceiling. **Chosen, not measured** -- no run has been made
            # against this model.
            reasoning_token_allowance=16000,
            supports_reasoning_effort=True,
            # NOT the `gpt-5` set and NOT the `gpt-5.1` set. This family keeps
            # `none` (so `minimal` is still absent) and ADDS `xhigh` above
            # `high`. Declared from the owner's selection record, never probed.
            reasoning_efforts=("none", "low", "medium", "high", "xhigh"),
            # Chosen, not measured, and for the same reason as the allowance: a
            # read timeout on a provider that was answering is recorded
            # UNREACHABLE, an operational failure that measures nothing about the
            # model and abandons the record.
            minimum_timeout_seconds=300,
        ),
    ),
    (
        "gpt-5.1",
        ChatDialect(
            token_parameter="max_completion_tokens",
            supports_temperature=False,
            # Larger than the `gpt-5` family's, and deliberately so. Reasoning
            # tokens are billed against `max_completion_tokens` and never appear
            # in `content`, so an allowance sized for a smaller model turns a
            # working model into `finish_reason: "length"` -- which this codebase
            # classifies `TOKEN_CEILING`, never retries, and correctly refuses to
            # treat as a model answer. `gpt-5-mini` was measured at 4096-7872
            # reasoning tokens on one §15.1 case at `high`; a larger model at the
            # same effort has no reason to spend fewer. The allowance only ever
            # ADDS to the caller's own budget, so a wider one cannot shorten a
            # response -- the cost of being generous is a ceiling that is never
            # reached, and the cost of being tight is a run that measures nothing.
            reasoning_token_allowance=16000,
            supports_reasoning_effort=True,
            # NOT the `gpt-5` set. `gpt-5.1` removed `minimal` and added `none`.
            reasoning_efforts=("none", "low", "medium", "high"),
            # Higher than the `gpt-5` family's 180s for the same reason the
            # allowance is higher: at `high` effort this model spends real wall
            # clock producing tokens nobody sees, and a read timeout is recorded
            # as UNREACHABLE -- an operational failure that measures nothing about
            # the model and abandons the whole capability record.
            minimum_timeout_seconds=300,
        ),
    ),
    (
        "gpt-5",
        ChatDialect(
            token_parameter="max_completion_tokens",
            supports_temperature=False,
            reasoning_token_allowance=8000,
            supports_reasoning_effort=True,
            reasoning_efforts=("minimal", "low", "medium", "high"),
            minimum_timeout_seconds=180,
        ),
    ),
    (
        "o1",
        ChatDialect(
            token_parameter="max_completion_tokens",
            supports_temperature=False,
            reasoning_token_allowance=8000,
            supports_reasoning_effort=True,
            reasoning_efforts=("minimal", "low", "medium", "high"),
            minimum_timeout_seconds=180,
        ),
    ),
    (
        "o3",
        ChatDialect(
            token_parameter="max_completion_tokens",
            supports_temperature=False,
            reasoning_token_allowance=8000,
            supports_reasoning_effort=True,
            reasoning_efforts=("minimal", "low", "medium", "high"),
            minimum_timeout_seconds=180,
        ),
    ),
    (
        "o4",
        ChatDialect(
            token_parameter="max_completion_tokens",
            supports_temperature=False,
            reasoning_token_allowance=8000,
            supports_reasoning_effort=True,
            reasoning_efforts=("minimal", "low", "medium", "high"),
            minimum_timeout_seconds=180,
        ),
    ),
)
"""Model-name prefixes mapped to the dialect they speak, longest match first.

An unlisted model gets :data:`CLASSIC_DIALECT`, which is what every pre-reasoning
OpenAI model accepts. That default is **not** a guess dressed as a fallback: if a
future model rejects it, the provider returns a deterministic 400 naming the
parameter, the capability gate classifies it `BAD_REQUEST`, writes no artifact and
exits 2. Nothing is retried, nothing is adapted at runtime, and no request is
silently reshaped.

`reasoning_effort` is deliberately **not** set anywhere here. It trades answer
quality against cost, which is a decision with an owner, and defaulting it in code
would be that decision made silently.
"""

CLASSIC_DIALECT = ChatDialect()


_EFFORT_ORDER: tuple[str, ...] = ("none", "minimal", "low", "medium", "high", "xhigh")
"""Canonical ordering for error messages only. Not an acceptance list."""

REASONING_EFFORTS: tuple[str, ...] = tuple(
    effort
    for effort in _EFFORT_ORDER
    if any(effort in dialect.reasoning_efforts for _, dialect in MODEL_DIALECTS)
)
"""Every value `OPENAI_REASONING_EFFORT` may take **on some model**.

**Derived from :data:`MODEL_DIALECTS`, never hand-written.** A hand-maintained
list is a second place for the vocabulary to live, and it goes stale in exactly
one direction: a family gains a value, the list does not, and a legitimate
owner-selected setting is refused as a typo. Deriving it means the union can
never claim a value no declared dialect accepts, nor omit one that some dialect
does.

**It is a union, and a union is not permission.** Passing this check says only
that *some* family accepts the value; `llm.build_chat_payload` then checks it
against the **configured model's own** `reasoning_efforts` and raises before the
request is built. Two checks because they answer two questions -- "is this a real
setting?" and "does this model take it?" -- and `gpt-5.1` accepting `none` while
`gpt-5` accepts `minimal` is exactly the gap a single check would miss.

A value in neither is a configuration error and raises `ConfigError`; it is never
passed through to be rejected by the provider, and it is never silently dropped --
a dropped setting would run at the provider's default while the operator believed
otherwise, and that belief is exactly what the capability identity exists to pin
down.
"""

REASONING_EFFORT_UNSET = ""
"""A reasoning effort that was never configured.

**Distinct from every configured value, including whatever the provider's default
happens to be.** An artifact measured with the setting absent describes a runtime
where the provider chose; an artifact measured at `high` describes a runtime where
we chose. They are different runtimes and neither qualifies the other, which is
only true if "unset" is a value in the identity rather than a gap in it.
"""


def read_reasoning_effort(environ: Optional[dict] = None) -> str:
    """The configured reasoning effort, or the empty string if unset.

    Raises:
        ConfigError: the value is not in :data:`REASONING_EFFORTS`. Fail closed,
            before any provider call: a typo must not become "the provider's
            default, silently".
    """
    source = environ if environ is not None else os.environ
    raw = (source.get("OPENAI_REASONING_EFFORT") or "").strip().lower()
    if not raw:
        return REASONING_EFFORT_UNSET
    if raw not in REASONING_EFFORTS:
        raise ConfigError(
            f"OPENAI_REASONING_EFFORT={raw!r} is not a supported value. Expected "
            "one of: " + ", ".join(REASONING_EFFORTS) + ", or unset to leave the "
            "provider's own default in force. It is validated here rather than at "
            "the provider so a typo fails before a paid call, and so it can never "
            "be quietly dropped -- a dropped setting runs at a different effort "
            "than the operator believes, and the capability artifact would record "
            "a runtime nobody tested."
        )
    return raw


@dataclass(frozen=True, slots=True)
class InferenceConfig:
    """Every inference setting capable of changing what the verifier returns.

    **This is a runtime identity, not telemetry.** The 2026-08-29 case-E result is
    the reason it exists: `gpt-5-mini-2025-08-07` was measured ineligible with
    `reasoning_effort` unset, and the owner then set it to `high`. Those are two
    different runtimes producing two different distributions of answers, and
    without this object the second would have inherited the first's identity --
    an artifact would have matched on every field while describing a
    configuration nobody had tested.

    Every field is either explicitly configured or explicitly recorded as absent.
    **No provider default that affects behaviour is silently inherited**:
    `temperature_sent=False` and `reasoning_effort=""` are values here, not gaps,
    so an artifact says "the provider chose" rather than saying nothing.

    Contains no credential and no account metadata. It is rendered verbatim into a
    capability artifact.
    """

    reasoning_effort: str
    """The configured effort, or `""` for unset. See :data:`REASONING_EFFORT_UNSET`."""

    temperature_sent: bool
    """Whether an explicit `temperature` is sent at all."""

    temperature: Optional[float]
    """The value sent, or `None` when it is not sent. Kept separate from
    `temperature_sent` so "sent 0.0" and "not sent" cannot collapse into the same
    falsy record."""

    token_parameter: str
    effective_token_ceiling: int
    """The ceiling actually sent for the verifier's own workload -- the caller's
    budget plus the dialect's reasoning allowance. Part of the identity because a
    ceiling that truncates is a ceiling that changes the answer."""

    dialect_version: str
    """The declared dialect, rendered canonically. Moves when any field of
    `ChatDialect` moves for this model."""

    timeout_floor_seconds: int
    """The dialect's minimum read timeout. It affects completion *eligibility*: a
    floor too low turns a slow answer into a read timeout, which is an operational
    failure that measures nothing."""

    top_p: Optional[float] = None
    seed: Optional[int] = None
    verbosity: Optional[str] = None
    """None of the three is sent today. Present so that sending one later cannot
    happen without moving the fingerprint -- an unrepresented sampling or output
    setting is exactly the silent inheritance this class refuses.

    `verbosity` joined them with the `gpt-5.1` dialect: the `gpt-5.x` families
    accept a `low|medium|high` output-length control that changes what comes back
    while changing nothing else about the request. It is deliberately **not**
    sent -- the verifier's output length is governed by the §10 schema and the
    byte-pinned prompt, and adding a second lever over it would be this code
    making a choice the specification already made -- and `None` records that
    the provider's own default is in force rather than leaving the question
    unasked."""

    def as_dict(self) -> dict:
        """Sanitized, ordered, and safe to write into an artifact."""
        return {
            "reasoning_effort": self.reasoning_effort or "(unset - provider default)",
            "reasoning_effort_configured": bool(self.reasoning_effort),
            "temperature_sent": self.temperature_sent,
            "temperature": self.temperature,
            "token_parameter": self.token_parameter,
            "effective_token_ceiling": self.effective_token_ceiling,
            "dialect_version": self.dialect_version,
            "timeout_floor_seconds": self.timeout_floor_seconds,
            "top_p": self.top_p,
            "seed": self.seed,
            "verbosity": self.verbosity,
        }

    @property
    def fingerprint(self) -> str:
        """A content hash over the canonical representation of every field above.

        One value for admission to compare, and the sanitized fields stay in the
        artifact beside it so a human can see *what* differed rather than only
        *that* something did.
        """
        canonical = json.dumps(
            {
                "reasoning_effort": self.reasoning_effort,
                "temperature_sent": self.temperature_sent,
                "temperature": self.temperature,
                "token_parameter": self.token_parameter,
                "effective_token_ceiling": self.effective_token_ceiling,
                "dialect_version": self.dialect_version,
                "timeout_floor_seconds": self.timeout_floor_seconds,
                "top_p": self.top_p,
                "seed": self.seed,
                "verbosity": self.verbosity,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def dialect_version(dialect: ChatDialect) -> str:
    """The declared dialect as one canonical string."""
    return json.dumps(asdict(dialect), sort_keys=True, separators=(",", ":"))


def _matches_family(name: str, prefix: str) -> bool:
    """Whether `name` is a member of the family `prefix` names.

    **A bare `startswith` silently annexes every future version**, and that is not
    hypothetical -- it is the trap this codebase has now walked into twice.
    `"gpt-5.1-2025-11-13".startswith("gpt-5")` is true, and so is
    `"gpt-5.4-mini-2026-03-17".startswith("gpt-5")`. Each would have taken the
    `gpt-5` family's `reasoning_effort` vocabulary, which is
    `minimal|low|medium|high` -- a *different* vocabulary from either of theirs.

    What makes that dangerous rather than merely wrong is that it does not fail.
    The families agree on `max_completion_tokens` and on omitting `temperature`,
    and `medium` is accepted by all three, so the selected runtime would have run
    under a dialect nobody declared for it, at the wrong reasoning allowance and
    the wrong timeout floor, with no error anywhere -- and the capability artifact
    would have recorded that as the identity it qualified.

    So a prefix matches only at a **version boundary**: the name must be the
    prefix exactly, or continue with a character that cannot extend the version
    number. A `.` or a digit continues it (`gpt-5` -> `gpt-5.4`, `gpt-5` ->
    `gpt-50`), and a family declaration for `gpt-5` says nothing about either.
    Anything else -- in practice `-`, as in `gpt-5-mini-2025-08-07` -- is a
    variant *within* the family the prefix names.

    A version this table has never heard of therefore matches **nothing** and
    falls to :data:`CLASSIC_DIALECT`, whose `max_tokens` and explicit
    `temperature` a reasoning model rejects with a deterministic HTTP 400. That is
    the intended outcome and it is stated in :data:`MODEL_DIALECTS`: the gate
    classifies it `BAD_REQUEST`, writes no artifact and exits 2. Failing loudly on
    an undeclared model is the whole point -- it is the signal to add a row, and
    it is strictly better than inheriting a neighbour's vocabulary quietly.
    """
    if not name.startswith(prefix):
        return False
    rest = name[len(prefix):]
    if not rest:
        return True
    return not (rest[0] == "." or rest[0].isdigit())


def dialect_for(model: str) -> ChatDialect:
    """The dialect `model` speaks. Longest match wins; unlisted is classic.

    Matching is at a version boundary rather than a bare prefix; see
    :func:`_matches_family` for the silent inheritance that forbids.
    """
    name = (model or "").strip().lower()
    for prefix, dialect in sorted(MODEL_DIALECTS, key=lambda row: len(row[0]), reverse=True):
        if _matches_family(name, prefix):
            return dialect
    return CLASSIC_DIALECT


class ConfigError(RuntimeError):
    """The environment is configured incorrectly. Raised at import."""


class Profile(StrEnum):
    LOCAL = "local"
    FUSE = "fuse"
    HUGGINGFACE = "huggingface"
    ARAMCO = "aramco"
    TEST = "test"


class Provider(StrEnum):
    FUSE = "fuse"
    HUGGINGFACE = "huggingface"
    OPENAI = "openai"
    """The official OpenAI API, through the official `openai` SDK."""

    OPENAI_COMPATIBLE = "openai_compatible"
    """A self-hosted or vendor gateway speaking the OpenAI chat-completions shape,
    reached with plain `requests`. The Aramco LiteLLM gateway is one."""

    STUB = "stub"
    """No provider at all. `test` only, and a capability artifact carrying it is
    refused under every other profile."""


PROFILE_PROVIDER: dict[Profile, Provider] = {
    Profile.LOCAL: Provider.OPENAI,
    Profile.FUSE: Provider.FUSE,
    Profile.HUGGINGFACE: Provider.HUGGINGFACE,
    Profile.ARAMCO: Provider.OPENAI_COMPATIBLE,
    Profile.TEST: Provider.STUB,
}


@dataclass(frozen=True, slots=True)
class LLMTarget:
    """The active LLM provider, resolved from the profile.

    `api_key` is **not** a field. Nothing that describes a runtime identity should
    be able to carry a credential by accident, and this object is rendered into
    capability artifacts, log lines and error messages.
    """

    profile: Profile
    provider: Provider
    base_url: str = field(repr=False)
    """Kept out of the repr: a URL can carry credentials in its userinfo
    (`https://user:pass@host/`) or a query string, and this object is rendered
    into log lines, error messages and capability artifacts. `endpoint` is the
    safe rendering and is what artifacts record."""

    model: str

    @property
    def endpoint(self) -> dict[str, str]:
        """The endpoint as sanitized parts, never as a URL string.

        Parsed rather than trimmed, so userinfo (`https://user:pass@host/`) and a
        credential-bearing query string cannot survive into an artifact by being
        in a shape nobody predicted.
        """
        parsed = urlparse(self.base_url)
        return {
            "scheme": parsed.scheme,
            "host": parsed.hostname or "",
            "port": str(parsed.port) if parsed.port else "",
            "path": parsed.path,
        }

    @property
    def chat_dialect(self) -> "ChatDialect":
        """The Chat Completions dialect this target's model speaks.

        A property rather than a field so every existing construction of
        `LLMTarget` keeps working and no caller can pass a dialect that disagrees
        with the model it accompanies.
        """
        if self.provider is Provider.OPENAI:
            return dialect_for(self.model)
        if (self.provider is Provider.FUSE
                and self.model == "deepseek-ai/DeepSeek-V4.1-Flash"):
            # Fuse counts hidden reasoning against max_tokens. A live 2026-11-04
            # probe exhausted 894/1000 tokens on reasoning; a real screening
            # batch exhausted 10000 before returning its JSON. Keep the caller's
            # answer budget and add a separate reasoning allowance.
            return ChatDialect(reasoning_token_allowance=32000,
                               minimum_timeout_seconds=300)
        # The gateway path speaks the classic shape; `aramco` is deferred and its
        # model has never been probed, so nothing here claims otherwise.
        return CLASSIC_DIALECT

    @property
    def model_is_pinned(self) -> bool:
        """Whether `model` names a specific snapshot rather than a moving alias.

        Heuristic on purpose, and only ever used to *record* a limitation — never
        to relax one. A model that looks unpinned is reported as unpinned; a
        model that looks pinned is still qualified by an artifact that names it
        exactly.
        """
        import re

        return bool(re.search(r"-\d{4}-\d{2}-\d{2}$|-\d{4}$|:\w+$", self.model))


def _read_profile(environ: Optional[dict] = None) -> Profile:
    # `is None`, not truthiness: an explicitly EMPTY mapping means "there is
    # no configuration", and `environ or os.environ` quietly substituted the
    # real environment for it -- which made a test asserting the unset case
    # pass against whatever the developer happened to have exported.
    source = environ if environ is not None else os.environ
    raw = (source.get("DEPLOYMENT_PROFILE") or "").strip().lower()
    if not raw:
        raise ConfigError(
            "DEPLOYMENT_PROFILE is not set. It has no default on purpose: this "
            "setting decides which provider the application talks to and which "
            "capability qualifications may be reused, and a default would answer "
            "both on your behalf. Set one of: "
            + ", ".join(profile.value for profile in Profile)
            + " (see backend/.env.example)."
        )
    try:
        return Profile(raw)
    except ValueError:
        raise ConfigError(
            f"DEPLOYMENT_PROFILE={raw!r} is not a known profile. Expected one of: "
            + ", ".join(profile.value for profile in Profile)
            + ". The profile is never inferred from a URL, a hostname, an API key "
            "or a model name."
        ) from None


def resolve_target(settings_obj, environ: Optional[dict] = None) -> LLMTarget:
    """Resolve the active LLM target, or refuse to start.

    Raises:
        ConfigError: unknown or missing profile, or a profile whose required
            non-credential settings are absent.
    """
    env = environ if environ is not None else os.environ
    profile = _read_profile(env)
    provider = PROFILE_PROVIDER[profile]

    if provider is Provider.OPENAI:
        base_url = (env.get("OPENAI_BASE_URL") or OPENAI_DEFAULT_BASE_URL).strip()
        model = (env.get("OPENAI_MODEL") or OPENAI_DEFAULT_MODEL).strip()
        if not model:
            raise ConfigError(
                "OPENAI_MODEL is set to an empty value under DEPLOYMENT_PROFILE=local. "
                "Unset it to take the default, or name a model; an empty model is "
                "not a choice the application can act on."
            )
        return LLMTarget(profile, provider, base_url, model)

    if provider in (Provider.OPENAI_COMPATIBLE, Provider.HUGGINGFACE, Provider.FUSE):
        base_url = (getattr(settings_obj, "llm_api_url", "") or "").strip()
        model = (getattr(settings_obj, "llm_model", "") or "").strip()
        missing = [
            name
            for name, value in (("LLM_API_URL", base_url), ("LLM_MODEL", model))
            if not value
        ]
        if missing:
            raise ConfigError(
                f"DEPLOYMENT_PROFILE={profile.value} requires "
                + " and ".join(missing)
                + ". The profile selects the provider; it cannot supply its address."
            )
        if provider is Provider.FUSE:
            parsed = urlparse(base_url)
            if (parsed.scheme != "https" or parsed.netloc != "api.fuse.humainaic.com"
                    or parsed.path != "/v1/chat/completions" or parsed.query or parsed.fragment):
                raise ConfigError("The fuse profile requires https://api.fuse.humainaic.com/v1/chat/completions")
        if provider is Provider.HUGGINGFACE:
            parsed = urlparse(base_url)
            if (parsed.scheme != 'https' or parsed.hostname != 'router.huggingface.co'
                    or parsed.username or parsed.password or parsed.query or parsed.fragment
                    or parsed.path != '/v1/chat/completions'):
                raise ConfigError('The huggingface profile requires https://router.huggingface.co/v1/chat/completions')
            if ':' not in model:
                raise ConfigError('The huggingface model must explicitly name its inference provider with a :provider suffix')
        return LLMTarget(profile, provider, base_url, model)

    # test / stub: no address, no model, nothing reachable.
    return LLMTarget(profile, provider, base_url="", model="stub")


class MissingCredential(RuntimeError):
    """The active provider has no credential configured.

    Deliberately not raised at import: `migrate.py`, `checkpointer.py setup` and
    the test suite all run without touching an LLM, and stopping them would be
    fail-closed in the wrong place.
    """


def require_llm_credentials(target: LLMTarget, settings_obj) -> str:
    """The credential for `target`, or a refusal naming what to set.

    Never returns an empty string and never falls back to another provider's key:
    a request authenticated with the wrong credential is a request to the wrong
    place, and the error it produces is far harder to read than this one.
    """
    if target.provider is Provider.FUSE:
        key = (getattr(settings_obj, "fuse_api_key", "") or "").strip()
        if not key:
            raise MissingCredential("FUSE_API_KEY is required for DEPLOYMENT_PROFILE=fuse")
        return key
    if target.provider is Provider.HUGGINGFACE:
        key = (getattr(settings_obj, 'hf_token', '') or '').strip()
        if not key:
            raise MissingCredential('HF_TOKEN is required for DEPLOYMENT_PROFILE=huggingface; no other provider credential is used')
        return key
    if target.provider is Provider.OPENAI:
        key = (os.environ.get("OPENAI_API_KEY") or "").strip()
        if not key:
            raise MissingCredential(
                "OPENAI_API_KEY is not set, and DEPLOYMENT_PROFILE=local uses the "
                "official OpenAI API. Set it in backend/.env (never commit it). "
                "There is no fallback to another provider's credential."
            )
        return key

    if target.provider is Provider.OPENAI_COMPATIBLE:
        key = (getattr(settings_obj, "llm_api_key", "") or "").strip()
        if not key:
            raise MissingCredential(
                "LLM_API_KEY (or HF_TOKEN) is not set, and "
                f"DEPLOYMENT_PROFILE={target.profile.value} uses an "
                "OpenAI-compatible gateway."
            )
        return key

    raise MissingCredential(
        "DEPLOYMENT_PROFILE=test has no LLM provider. A test that reached this "
        "line was about to make a real call; stub the boundary instead."
    )


def openai_client_options() -> dict[str, str]:
    """Optional OpenAI account scoping, from the environment only.

    Organization and project ids are account identifiers rather than secrets, but
    they are still deployment-specific and are never hard-coded — same rule as
    the key.
    """
    options: dict[str, str] = {}
    for env_name, argument in (
        ("OPENAI_ORG_ID", "organization"),
        ("OPENAI_PROJECT_ID", "project"),
    ):
        value = (os.environ.get(env_name) or "").strip()
        if value:
            options[argument] = value
    return options


__all__ = [
    "CLASSIC_DIALECT",
    "ChatDialect",
    "InferenceConfig",
    "REASONING_EFFORTS",
    "REASONING_EFFORT_UNSET",
    "dialect_version",
    "read_reasoning_effort",
    "ConfigError",
    "MODEL_DIALECTS",
    "dialect_for",
    "LLMTarget",
    "MissingCredential",
    "OPENAI_DEFAULT_BASE_URL",
    "OPENAI_DEFAULT_MODEL",
    "PROFILE_PROVIDER",
    "Profile",
    "Provider",
    "openai_client_options",
    "require_llm_credentials",
    "resolve_target",
]
