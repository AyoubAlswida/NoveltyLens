"""LLM integration for invention understanding, match explanation, and relevance validation."""

import json
import logging
import time
from collections.abc import Sequence
from contextvars import ContextVar

import requests

import config
from config import settings
from deployment import (
    ConfigError,
    InferenceConfig,
    Provider,
    dialect_version,
    openai_client_options,
    read_reasoning_effort,
    require_llm_credentials,
)
from models import InventionAnalysis, MatchAnalysis, PatentRecord
from tracing import trace
from workflow import redaction


logger = logging.getLogger(__name__)

JSON_ATTEMPTS = 2
"""How many times to ask for a JSON response before giving up.

A retry here re-*asks* the model; it never repairs a broken response. Repairing
would mean guessing at content no model produced, which is the fabrication the
zero-tolerance policy exists to prevent. Asking again is honest, and if every
attempt fails the request fails."""

RETRY_TEMPERATURE = 0.2
"""Temperature floor for retry attempts.

Re-asking at temperature 0.0 would resample the same broken output and waste the
attempt, so a retry nudges temperature up just enough to get a genuinely
different sample. Only retries are affected - the first attempt always uses the
caller's temperature."""


class LLMTruncatedError(RuntimeError):
    """The model hit ``max_tokens`` and its response was cut off mid-sentence.

    Deliberately distinct from a transport failure, and deliberately **not
    retried**: the same prompt at the same ``max_tokens`` truncates again.
    Raising immediately reports the actual problem instead of burning retries.

    This was the real cause of both JSON failures observed in production. A
    truncated response fails in one of two ways, and the second is worse:

    - ``json.loads`` chokes at the cut point with a confusing message like
      ``Expecting ',' delimiter: line 38 column 3`` — the error names a syntax
      problem in the middle of the document, giving no hint that the document is
      simply incomplete.
    - Or, if the truncated text happens to end after a complete object, a greedy
      ``\\{.*\\}`` regex matches up to the *last* closing brace and parses it
      **successfully** — silently yielding a partial result. Verified directly:
      at ``max_tokens=120`` the regex path parsed a truncated response with no
      error at all. ``understand_invention`` would have returned two search
      queries instead of five and reported success.

    Checking ``finish_reason`` catches both before parsing, which is why the
    greedy regex is gone.
    """


class LLMResponseError(RuntimeError):
    """The model returned a complete response that is not usable JSON."""


# --- The OpenAI provider ---------------------------------------------------
#
# Selected by DEPLOYMENT_PROFILE=local (amendment 2026-08-28). It sits *inside*
# `_llm_chat` rather than beside it, so every caller -- understand_invention,
# validate_relevance, analyze_all_matches, decompose_features and the
# feature-disclosure verifier -- goes through one abstraction and no workflow node
# knows which provider is live.
#
# THE CONTRACT DOES NOT CHANGE BY PROVIDER. Truncation still raises
# `LLMTruncatedError` and is still never retried at the same ceiling; a strict
# JSON Schema is still sent as `response_format`; the eighteen §9 checks still run
# on the way back. Spec §14.1 is categorical that validation strictness does not
# vary by provider, and a provider-specific relaxation would be exactly the
# unlabelled degradation the zero-tolerance policy forbids.
#
# THERE IS NO FALLBACK. Not to another model, not to another provider, not to a
# looser output mode. A configured model that fails, fails the request.

OPENAI_ADAPTER = "openai-python"
"""Named so a capability artifact can record which adapter qualified a model.
The SDK's structured-output handling is part of what the §14.2.1 gate measures,
so an SDK upgrade invalidates a capability record the same way a model change
does."""

OPENAI_ADAPTER_BEHAVIOUR_VERSION = "6"
"""This module's own behaviour as an OpenAI adapter, versioned separately from
the SDK.

The SDK version alone does not describe the adapter: what `response_format` is
built from, which statuses are retried, how a refusal is surfaced and how a
truncation is classified are all decided *here*, and any of them can change while
`openai.__version__` stands still. A capability artifact is scoped to the adapter
that produced it, so this string is part of `adapter_revision` and a bump
invalidates every prior record.

Version 2 was the 2026-08-29 schema amendment: the provider is sent the **wire**
schema (`verification/wire.py`) rather than the canonical section 10 schema.
Version 1 sent section 10 as written, which OpenAI rejected with a deterministic
HTTP 400.

Version 3 added the **chat dialect** (`deployment.ChatDialect`): the reasoning
families name their output ceiling `max_completion_tokens`, reject an explicit
`temperature`, and bill their own reasoning against that ceiling.

Version 4 sends **`reasoning_effort`** when it is configured and the family
accepts it (owner decision 2026-08-29: `high`, an explicit quality-over-cost
choice for verifier qualification). It changes what the model does, so it changes
the request and it changes the identity -- see
`deployment.InferenceConfig`, which is what admission actually compares.

Version 6 is the 2026-08-31 selection of `gpt-5.4-mini-2026-03-17`. Two things
changed and each of them changes the request:

* a **`gpt-5.4-mini` dialect row**, whose `reasoning_effort` vocabulary is
  neither `gpt-5`'s nor `gpt-5.1`'s -- it keeps `none` and adds `xhigh`;
* **version-boundary matching** in `deployment.dialect_for`. A bare prefix let
  `gpt-5.4-mini-...` match the `gpt-5` row, and because the families agree on the
  token parameter and on omitting `temperature`, and `medium` is accepted by
  both, that would have run under the wrong dialect at the wrong allowance and
  the wrong timeout floor **without failing anywhere**. An undeclared version now
  matches nothing and fails loudly at the provider instead.

Version 5 is the `gpt-5.1` dialect (owner decision 2026-08-29:
`gpt-5.1-2025-11-13` at `reasoning_effort=high`). `gpt-5.1` is **not** `gpt-5`
with a different name -- it declares its own accepted `reasoning_effort`
vocabulary (`none` in place of `minimal`), its own reasoning-token allowance and
its own timeout floor -- and the effort is now checked against the **configured
model's own** vocabulary before the payload is built rather than only against the
global one. Every one of those changes what is sent or when it is refused, so it
is a different adapter and no artifact written under version 4 describes it.

The request this adapter builds is not the same for every model or every
configuration, and the request is half of what the section 14.2.1 gate measures --
so nothing qualified under an earlier version says anything about this one.
"""


LAST_COMPLETION: ContextVar[dict] = ContextVar("LAST_COMPLETION", default={})
"""Non-content facts about the most recent completion on this context.

**Read by the section 14.2.1 capability gate and by nothing else.** The gate has to
record, per run, which model revision actually served the call and how the token
budget was spent -- and the verifier is called through
`verify_feature_disclosure`, which returns a `CellResult` and has no channel for a
response envelope. Threading one through every layer for a diagnostic would put a
gate concern into the verifier's signature.

A `ContextVar` rather than a module global: the executor runs work on threads, and
a global would let one thread's envelope be read as another's.

**It holds no content.** Served model, fingerprint, finish reason and token counts
-- never the message, never a refusal string, never a provider body. Anything that
could carry text a model produced stays out, because this dict is written into a
capability artifact.
"""


def _observe(**facts: object) -> None:
    LAST_COMPLETION.set({key: value for key, value in facts.items() if value is not None})


def _usage_facts(usage) -> dict[str, object]:
    """Token accounting, if the provider reported any.

    Reasoning models bill invisible reasoning tokens against the completion
    ceiling, so `completion_tokens` alone does not explain a truncation. When the
    provider breaks it out, the breakdown is what tells an operator whether to
    raise the budget or reduce the requirement count.
    """
    if usage is None:
        return {}
    facts: dict[str, object] = {}
    for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = getattr(usage, name, None)
        if isinstance(value, int):
            facts[name] = value
    details = getattr(usage, "completion_tokens_details", None)
    reasoning = getattr(details, "reasoning_tokens", None)
    if isinstance(reasoning, int):
        facts["reasoning_tokens"] = reasoning
    return facts


def build_chat_payload(
    target,
    messages: list[dict[str, str]],
    *,
    max_tokens: int,
    temperature: float,
) -> dict[str, object]:
    """The request body, in the dialect the configured model actually accepts.

    **One function, so there is one answer.** `_llm_chat` and the section 14.2.1
    capability gate both build their request here, because a gate that probed a
    differently-shaped request would certify something the application never sends
    -- and the parameter names are exactly what the reasoning families reject.

    Three adaptations, all declared in `deployment.MODEL_DIALECTS` rather than
    inferred, and all of them naming rather than substituting:

    - **`max_completion_tokens` instead of `max_tokens`.** The reasoning families
      reject the old name outright; it is not accepted as an alias.
    - **`temperature` omitted, never rewritten.** Those models accept only their
      own default and reject an explicit `0.0` with HTTP 400. Omitting leaves the
      provider's default in force; sending `1` would be this code choosing a
      sampling setting on the caller's behalf and recording nothing about it. The
      consequence is stated rather than hidden: on such a model the verifier's
      `temperature = 0.0` is **not** in effect, and spec section 14 already says
      temperature 0 is best-effort rather than a determinism guarantee.
    - **A reasoning allowance added to the ceiling.** Reasoning tokens never appear
      in `content` and still consume the completion budget, so a budget sized for
      the answer alone truncates every call. The allowance is *added* to what the
      caller asked for and never reduces it.

    Nothing here changes the messages, the schema, or the response contract.
    """
    dialect = target.chat_dialect
    payload: dict[str, object] = {"model": target.model, "messages": messages}
    payload[dialect.token_parameter] = max_tokens + dialect.reasoning_token_allowance
    if dialect.supports_temperature:
        payload["temperature"] = temperature

    # Sent explicitly, never emulated in the prompt. Asking a model to "think
    # harder" in its instructions is a different intervention with a different
    # failure mode, and it would change the byte-pinned system prompt.
    #
    # Read here rather than cached at import so a test can set it, and validated
    # in `read_reasoning_effort` so an unknown value fails BEFORE the call rather
    # than being dropped or rejected by the provider.
    effort = read_reasoning_effort()
    if effort and dialect.supports_reasoning_effort:
        if effort not in dialect.reasoning_efforts:
            # Accepted by *some* family (`read_reasoning_effort` established
            # that) and not by this one. `gpt-5` takes `minimal`; `gpt-5.1`
            # replaced it with `none`. Refused here, before the request is
            # built, rather than sent and rejected by the provider: the
            # provider's 400 arrives after the call is made, and the failure it
            # produces is classified BAD_REQUEST -- an operational outcome that
            # measures nothing and abandons a whole capability record.
            raise ConfigError(
                f"OPENAI_REASONING_EFFORT={effort!r} is not accepted by model "
                f"{target.model!r}. That model's family accepts: "
                + ", ".join(dialect.reasoning_efforts)
                + ". It is neither dropped nor downgraded to a neighbouring "
                "value: a setting the operator chose must either be sent as "
                "chosen or refused by name, because the capability artifact "
                "records the effort as part of the runtime identity and a "
                "substituted one would describe a runtime nobody selected."
            )
        payload["reasoning_effort"] = effort
    elif effort and not dialect.supports_reasoning_effort:
        # Configured, and this family cannot honour it. Refused rather than
        # dropped: running at the provider's default while the operator believes
        # a setting is in force is precisely the silent inheritance the inference
        # identity exists to make impossible.
        raise ConfigError(
            f"OPENAI_REASONING_EFFORT={effort!r} is set, but model "
            f"{target.model!r} does not accept `reasoning_effort`. Unset it, or "
            f"select a model whose dialect declares support. It is not dropped: a "
            f"dropped setting runs at a different effort than the operator "
            f"believes, and the capability artifact would then describe a runtime "
            f"nobody tested."
        )
    return payload


def inference_config(target, *, max_tokens: int, temperature: float) -> InferenceConfig:
    """Every inference setting that can change what the verifier returns.

    Derived from the **same** dialect and the same environment the payload builder
    uses, so the identity an artifact records and the request that was actually
    sent cannot disagree. `verification/capability.py` folds its fingerprint into
    the runtime identity; the sanitized fields go into the artifact beside it so a
    mismatch says *what* differed rather than only *that* something did.
    """
    dialect = target.chat_dialect
    sends_temperature = bool(dialect.supports_temperature)
    return InferenceConfig(
        reasoning_effort=read_reasoning_effort() if dialect.supports_reasoning_effort else "",
        temperature_sent=sends_temperature,
        temperature=temperature if sends_temperature else None,
        token_parameter=dialect.token_parameter,
        effective_token_ceiling=max_tokens + dialect.reasoning_token_allowance,
        dialect_version=dialect_version(dialect),
        timeout_floor_seconds=dialect.minimum_timeout_seconds,
    )


def _openai_client(api_key: str, timeout: int):
    """Build the official client for this call.

    Constructed per call rather than cached at import: `base_url` and the
    optional organization/project scoping come from the environment, and a client
    pinned at import would silently outlive a configuration change. The SDK
    pools connections internally, so this is not the cost it looks like.

    `max_retries=0` because `_llm_chat` owns retry policy for every provider. Two
    retry loops stacked on each other turn a bounded 3 attempts into 9 and make
    the timeout budget unreadable.
    """
    from openai import OpenAI

    return OpenAI(
        api_key=api_key,
        base_url=config.llm_target.base_url,
        timeout=timeout,
        max_retries=0,
        **openai_client_options(),
    )


def _openai_chat(
    payload: dict,
    *,
    api_key: str,
    timeout: int,
    retries: int,
    max_tokens: int,
) -> str:
    """One OpenAI chat completion, with the same retry and truncation contract.

    Raises:
        LLMTruncatedError: the response hit `max_tokens`. Never retried -- the
            same prompt at the same ceiling truncates again.
        RuntimeError: every transport attempt failed. The message is sanitized;
            an OpenAI error can carry request context and must not reach a log or
            a stored `failure_detail` unredacted.
    """
    from openai import APIStatusError, OpenAIError

    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            client = _openai_client(api_key, timeout)
            completion = client.chat.completions.create(**payload)
        except APIStatusError as error:
            # A 4xx that is not rate limiting is deterministic: a bad request, an
            # unsupported response_format, an unknown model. Retrying spends
            # quota to learn the same thing, and for 401/403 it hammers an auth
            # endpoint, which is how a key gets blocked.
            status = getattr(error, "status_code", None)
            if status is not None and 400 <= status < 500 and status != 429:
                raise RuntimeError(
                    f"OpenAI returned HTTP {status} for model "
                    f"{payload.get('model')!r}: {redaction.safe_detail(error)}"
                ) from None
            last_error = error
            trace("llm.attempt_failed", attempt=attempt, error=redaction.safe_log(error))
        except OpenAIError as error:
            last_error = error
            trace("llm.attempt_failed", attempt=attempt, error=redaction.safe_log(error))
        else:
            choice = completion.choices[0]
            content = str(choice.message.content or "")
            _observe(
                served_model=str(completion.model or ""),
                system_fingerprint=str(getattr(completion, "system_fingerprint", "") or ""),
                finish_reason=str(choice.finish_reason or ""),
                **_usage_facts(getattr(completion, "usage", None)),
            )
            trace(
                "llm.response",
                attempt=attempt,
                response_chars=len(content),
                finish_reason=choice.finish_reason,
                served_model=completion.model,
            )
            if choice.finish_reason == "length":
                # The ceiling named is the one actually sent, not the one the
                # caller asked for: on a reasoning model they differ by the
                # dialect's allowance, and a message naming the smaller number
                # would send someone to raise a limit that was never in force.
                sent = payload.get(config.llm_target.chat_dialect.token_parameter, max_tokens)
                raise LLMTruncatedError(
                    f"Model response hit the {sent}-token completion limit and was "
                    f"cut off ({len(content)} chars returned). Raise the budget for "
                    f"this call."
                )
            # A refusal is a real answer and must not be parsed as content. The
            # SDK surfaces it separately precisely so it is not mistaken for one.
            refusal = getattr(choice.message, "refusal", None)
            if refusal:
                raise LLMResponseError(
                    "OpenAI refused the request rather than answering it. This is "
                    "not a malformed response and is not retried into one."
                )
            return content

        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))

    raise RuntimeError(
        "OpenAI request failed after "
        f"{retries + 1} attempts: "
        f"{redaction.safe_detail(last_error) if last_error else 'unknown'}"
    ) from last_error


def _llm_chat(
    prompt: str,
    max_tokens: int = 400,
    temperature: float = 0.3,
    timeout: int = 30,
    retries: int = 2,
    json_object: bool = True,
    *,
    system: str | None = None,
    response_format: dict | None = None,
) -> str:
    """Send one chat completion request with bounded transient-error retries.

    ``json_object`` asks the provider for guaranteed-parseable output via
    ``response_format`` (verified supported by the HF Router + Novita endpoint
    this project targets). It constrains decoding rather than merely requesting
    JSON in the prompt, which removes the unescaped-quote and stray-prose
    failure modes at the source.

    ``system`` prepends a system message. It exists for the feature-disclosure
    verifier (``backend/verification/``), whose specification is a system/user
    split, and the split is load-bearing rather than stylistic: the untrusted
    patent passages have to sit in a *different message* from the rules that
    govern them, or the injection defence in that prompt is defending a boundary
    that does not exist. Callers that pass nothing are unaffected - the payload is
    byte-identical to before.

    ``response_format`` overrides ``json_object`` outright, for callers that need
    a stricter mode than a generic JSON object - JSON Schema-constrained
    generation, in the verifier's case, where required fields, closed enums and
    ``additionalProperties: false`` are the whole contract and ``json_object``
    enforces none of them.

    Raises:
        LLMTruncatedError: if the response hit ``max_tokens``. Not retried - see
            that class.
        RuntimeError: if every transport attempt failed.
    """
    target = config.llm_target
    api_key = require_llm_credentials(target, settings)

    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload = build_chat_payload(
        target, messages, max_tokens=max_tokens, temperature=temperature
    )
    # A floor, never a reduction: a caller that asked for longer keeps it.
    timeout = max(timeout, target.chat_dialect.minimum_timeout_seconds)
    if response_format is not None:
        payload["response_format"] = response_format
    elif json_object:
        payload["response_format"] = {"type": "json_object"}
    trace("llm.request", profile=str(target.profile), provider=str(target.provider), model=target.model, prompt_chars=len(prompt), system_chars=len(system or ""), max_tokens=max_tokens, temperature=temperature)

    if target.provider is Provider.OPENAI:
        # One provider, one code path, same contract. The official SDK is used
        # because OpenAI's structured-output dialect is its own -- `strict: true`
        # JSON Schema is enforced server-side there and is exactly what spec
        # §14.1 makes an eligibility requirement -- and because the SDK version is
        # the *adapter revision* a capability artifact is scoped to.
        return _openai_chat(payload, api_key=api_key, timeout=timeout, retries=retries, max_tokens=max_tokens)
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = requests.post(target.base_url, headers={"Authorization": f"Bearer {api_key}"}, json=payload, timeout=timeout)
            response.raise_for_status()
            choice = response.json()["choices"][0]
        except Exception as error:
            # Transport and HTTP failures only. A truncated *successful*
            # response is handled below, outside this retry loop.
            last_error = error
            trace("llm.attempt_failed", attempt=attempt, error=redaction.safe_log(error))
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
            continue

        finish_reason = choice.get("finish_reason")
        content = str(choice["message"]["content"])
        trace("llm.response", attempt=attempt, response_chars=len(content), finish_reason=finish_reason)
        if finish_reason == "length":
            raise LLMTruncatedError(
                f"Model response hit the {max_tokens}-token limit and was cut off "
                f"({len(content)} chars returned). Raise max_tokens for this call."
            )
        return content

    raise RuntimeError("LLM request failed") from last_error


def _strip_fences(raw: str) -> str:
    """Remove markdown code fences a model may wrap JSON in."""
    return raw.replace("```json", "").replace("```", "").strip()


def _llm_json(
    prompt: str,
    *,
    max_tokens: int,
    temperature: float,
    timeout: int,
    attempts: int = JSON_ATTEMPTS,
) -> dict:
    """Ask for JSON and parse it, re-asking once if the response is malformed.

    The whole cleaned response is parsed - there is no ``\\{.*\\}`` fallback.
    That regex used to turn a truncated response into either a baffling syntax
    error or, worse, a silently partial parse; see :class:`LLMTruncatedError`.
    With ``response_format`` constraining the output and ``finish_reason``
    catching truncation, the model returns a bare, complete JSON object or the
    call fails.

    Raises:
        LLMTruncatedError: propagated from :func:`_llm_chat`, never retried.
        LLMResponseError: if every attempt returned unparseable JSON.
    """
    last_error: json.JSONDecodeError | None = None
    for attempt in range(attempts):
        raw = _llm_chat(
            prompt,
            max_tokens=max_tokens,
            # A retry at temperature 0.0 would resample the identical broken
            # output; nudge it so the second attempt is actually a new sample.
            temperature=temperature if attempt == 0 else max(temperature, RETRY_TEMPERATURE),
            timeout=timeout,
        )
        try:
            return json.loads(_strip_fences(raw))
        except json.JSONDecodeError as error:
            last_error = error
            logger.warning("Malformed JSON from LLM (attempt %d/%d): %s", attempt + 1, attempts, error)
            trace("llm.json.malformed", attempt=attempt, error=str(error), response_chars=len(raw))

    raise LLMResponseError(
        f"LLM returned malformed JSON on all {attempts} attempts: {last_error}"
    ) from last_error


def understand_invention(invention: str) -> InventionAnalysis:
    """Convert the LLM's JSON response into a typed invention analysis."""
    trace("understand_invention.start", invention_chars=len(invention))
    prompt = f'''You are PatentScout AI, an AI assistant that helps users search for prior art.

Analyze the invention below.

Tasks:
1. Extract the primary technical concepts that define the invention.
2. Extract the essential engineering keywords that distinguish the invention from existing technology.
3. Generate exactly 5 high-quality patent search queries that maximize retrieval of patents describing the SAME invention.

Search query guidelines:
- Focus on the invention's primary functionality, purpose, and claimed mechanism.
- Prioritize the core invention over enabling technologies.
- Do NOT build queries around generic implementation details such as AI, cloud, encryption, Bluetooth, IoT, APIs, databases, or sensors unless they are the novel aspect of the invention.
- Use terminology that is commonly found in patent titles, abstracts, and claims - where a keyword has a more common patent-literature term or synonym, prefer that term over the user's own wording.
- Generate the 5 queries from different, deliberately distinct angles:
  1. Functional - what the invention does.
  2. Structural - what it's made of or how its components are arranged.
  3. Application/industry - the domain or use case it's deployed in.
  4. Broader technical classification - describe the invention the way a patent examiner would name its general technology category, not its specific implementation (this is the query most likely to surface prior art that solves the same problem with different vocabulary).
  5. Common patent wording - phrasing typical of how patents in this field are actually titled.
- Keep each query between 3 and 8 words.
- Avoid repeating nearly identical queries.

Return ONLY valid JSON in this exact schema:
{{"concepts": ["string"], "keywords": ["string"], "queries": ["string"]}}
Do not change the schema. Do not add explanations. Return JSON only.

Invention:
{invention}'''
    # The schema used to also request a nested `synonyms` object mapping every
    # keyword to a list of synonyms. Nothing downstream has ever read it: it was
    # pure output volume, and being the largest and most deeply nested part of
    # the response it was the most likely thing to push the response into the
    # token limit and truncate it. Removing it shrinks the response and deletes
    # a failure mode, at the cost of nothing.
    data = _llm_json(prompt, max_tokens=1200, temperature=0.0, timeout=40)
    analysis = InventionAnalysis(
        queries=[str(value) for value in data.get("queries", []) if str(value).strip()],
        keywords=[str(value) for value in data.get("keywords", []) if str(value).strip()],
        concepts=[str(value) for value in data.get("concepts", []) if str(value).strip()],
    )
    trace("understand_invention.done", queries=analysis.queries, keywords=analysis.keywords, concepts=analysis.concepts)
    return analysis


CLAIMS_EXCERPT_CHARS = 1500
"""How much of each patent's full text goes into the rationale prompt, per
patent. Every provider (search/google.py, search/epo.py, search/lens.py)
builds ``full_text`` as ``description + claims``, claims last - so the *tail*
of full_text is where a patent's actual structure (what connects to what,
what triggers what) is specified, not the abstract. Using the abstract here
was the bug: compare.py's similarity score already reads full text, but this
prompt - the one writing "why this overlaps" - was reading a 350-character
marketing-style summary instead, which structurally cannot describe
architecture it was never shown. ~1500 chars covers a claim or two without
blowing up prompt size across up to MAX_CANDIDATES patents in one batched call."""


def _rationale_source_text(patent: PatentRecord) -> str:
    """The text this prompt reasons about for one patent: the tail of full
    text (claims) when available, else the abstract as a last resort - never
    both truncated together, since that would dilute the claims with however
    much of the description fits first."""
    if not patent.full_text:
        return patent.abstract
    if len(patent.full_text) <= CLAIMS_EXCERPT_CHARS:
        return patent.full_text
    return patent.full_text[-CLAIMS_EXCERPT_CHARS:]


def analyze_all_matches(invention: str, patents: list[PatentRecord]) -> MatchAnalysis:
    """Rationalize every patent and propose recommendations in one LLM call.

    A single batched request is far friendlier to the model provider's rate
    limits than issuing one call per patent. Raises on any failure - a missing
    rationale or an empty recommendation list is a failure of this call, not a
    reason to substitute canned text.
    """
    count = len(patents)
    if count == 0:
        return MatchAnalysis(rationales=[], key_overlaps=[], recommendations=[])

    patent_block = "\n".join(
        f"[{index}] Number: {patent.id}\n    Title: {patent.title}\n    Excerpt: {_rationale_source_text(patent)}"
        for index, patent in enumerate(patents, start=1)
    )
    prompt = f'''You are an expert patent examiner comparing a user's invention against {count} prior-art patents.

User Invention:
{invention}

Prior-art patents:
{patent_block}

Tasks:
1. For EACH of the {count} patents, write a concise overlap summary (1 to 2 sentences, not more) explaining the actual technical overlap - the shared mechanism, purpose, structure, or technical approach that makes it relevant prior art. Reference the concrete technical features of that individual patent that are similar to the user's invention. Do NOT describe how the invention differs from the patent, and do NOT explain why an algorithm or search selected this patent - focus entirely on describing the real technical overlap itself. Every summary must be distinct - never reuse wording between patents.
2. For EACH of the {count} patents, also extract 3 to 4 short key-overlap tags (2-4 words each, e.g. "distributed sensing", "strain measurement") naming the specific overlapping technical concepts between that patent and the invention. These must name real shared concepts, not generic category words like "patent" or "invention".
3. Provide AS MANY actionable, specific uniqueness recommendations as you can (at least 10) describing concrete engineering, structural, or claim-drafting changes the user can make to distinguish their invention from this body of prior art.

Return ONLY valid JSON in exactly this schema:
{{"rationales": [{{"index": 1, "text": "1-2 sentence overlap summary for patent 1", "key_overlap": ["tag 1", "tag 2", "tag 3"]}}], "recommendations": ["tip 1", "tip 2"]}}
Include one rationale object for every patent from index 1 through {count}, in order. Do not include markdown code fences or any text outside the JSON object.'''

    trace("analyze_all_matches.start", patent_count=count)
    try:
        parsed = _llm_json(
            prompt,
            # Scales with the workload instead of a flat 3200. Shorter
            # rationales (1-2 sentences, ~40 tokens) plus 3-4 short tags
            # (~20 tokens) per patent use less than the old 3-5 sentence
            # version did, but the headroom is kept generous rather than
            # re-tuned tight - a prior truncation incident on this exact call
            # is why (see LLMTruncatedError's docstring).
            max_tokens=min(8000, 1200 + 250 * count),
            temperature=0.3,
            timeout=90,
        )
    except Exception:
        logger.exception("Batched multi-match analysis failed")
        raise

    rationales = [""] * count
    key_overlaps: list[list[str]] = [[] for _ in range(count)]
    raw_rationales = parsed.get("rationales", [])
    for item in raw_rationales:
        if isinstance(item, dict):
            index = int(item.get("index", 0)) - 1
            if 0 <= index < count:
                rationales[index] = str(item.get("text", "")).strip()
                key_overlaps[index] = [
                    str(tag).strip() for tag in item.get("key_overlap", []) if str(tag).strip()
                ]
    if not any(rationales):
        # Tolerate a plain array of strings instead of indexed objects. No
        # key_overlap data exists in this shape, so those stay empty - caught
        # by the same-shaped check below as a missing field, same as a
        # missing rationale.
        for index, item in enumerate(raw_rationales[:count]):
            if isinstance(item, str):
                rationales[index] = item.strip()

    missing_rationale = [index + 1 for index, text in enumerate(rationales) if not text]
    if missing_rationale:
        raise ValueError(f"LLM omitted rationale(s) for patent index/indices: {missing_rationale}")

    missing_overlap = [index + 1 for index, tags in enumerate(key_overlaps) if not tags]
    if missing_overlap:
        raise ValueError(f"LLM omitted key-overlap tags for patent index/indices: {missing_overlap}")

    recommendations = [str(item).strip() for item in parsed.get("recommendations", []) if str(item).strip()]
    if not recommendations:
        raise ValueError("LLM returned no uniqueness recommendations")

    trace(
        "analyze_all_matches.done",
        rationale_count=len(rationales),
        key_overlap_counts=[len(tags) for tags in key_overlaps],
        recommendation_count=len(recommendations),
    )
    return MatchAnalysis(rationales=rationales, key_overlaps=key_overlaps, recommendations=recommendations)


def validate_relevance(invention: str, patents: list[PatentRecord]) -> list[bool]:
    """Judge which candidate patents are plausibly relevant prior art, in one batched call.

    Aligned 1:1 with ``patents``. Raises rather than assuming irrelevance: a missing
    or malformed verdict for any candidate is a failure of this call, not evidence
    that the candidate should be dropped.
    """
    count = len(patents)
    if count == 0:
        return []

    patent_block = "\n".join(
        f"[{index}] Number: {patent.id}\n    Title: {patent.title}\n    Abstract: {patent.abstract[:300]}"
        for index, patent in enumerate(patents, start=1)
    )
    prompt = f'''You are an expert patent examiner screening {count} candidate prior-art patents for relevance before a detailed comparison is written.

User Invention:
{invention}

Candidate patents:
{patent_block}

For EACH of the {count} candidates, judge whether it is plausibly relevant prior art for the user's invention - meaning it shares enough of the same technical field, function, or mechanism that a detailed comparison is worth writing. Exclude only candidates that are clearly unrelated.

Return ONLY valid JSON in exactly this schema:
{{"verdicts": [{{"index": 1, "relevant": true}}]}}
Include one verdict object for every candidate from index 1 through {count}, in order. Do not include markdown code fences or any text outside the JSON object.'''

    trace("validate_relevance.start", candidate_count=count)
    try:
        parsed = _llm_json(
            prompt,
            # Each verdict object costs ~15 tokens, and graph/validation.py sends
            # up to MAX_CANDIDATES = 30 of them - about 450 tokens plus overhead,
            # against a flat 800. Not the failure that was observed, but the same
            # too-tight-ceiling shape, so it scales too.
            max_tokens=min(2000, 300 + 30 * count),
            temperature=0.0,
            timeout=40,
        )
    except Exception:
        logger.exception("Relevance validation failed")
        raise

    verdicts: list[bool | None] = [None] * count
    for item in parsed.get("verdicts", []):
        if isinstance(item, dict):
            index = int(item.get("index", 0)) - 1
            if 0 <= index < count:
                verdicts[index] = bool(item.get("relevant"))

    missing = [index + 1 for index, verdict in enumerate(verdicts) if verdict is None]
    if missing:
        raise ValueError(f"LLM omitted a relevance verdict for patent index/indices: {missing}")

    trace("validate_relevance.done", kept=sum(1 for verdict in verdicts if verdict))
    return [bool(verdict) for verdict in verdicts]


DECOMPOSITION_PROMPT_MARKER = "feature decomposition assistant"
"""The substring `run_mock.py`'s double and `tests/test_pipeline_contract.py`
key off. Imported rather than duplicated as a literal, so a prompt edit that
breaks the marker raises at the double instead of passing silently."""

# `RELATIONSHIP_PROMPT_MARKER` was REMOVED here, with the prompt it recognised.
# Relationships are gone from the product: the invention understanding is a flat
# list of technical elements, `decomposition_relationships` gets no rows, and the
# `DANGLING_RELATIONSHIP_REFERENCE` check that guarded them went with them. A
# marker exists so `run_mock.py` can recognise a prompt without making a metered
# call; a marker for a prompt nothing builds is a branch nothing can reach.


DECOMPOSITION_COMPONENT = "graph.verification_nodes.decompose_features"
"""**Spec §16 decision 8 is unresolved and this names what is unresolved.**

The specification says the verifier consumes approved elements and relationships
but does not say which component generates the candidates a human edits at
Workflow Gate A. This is that component, named explicitly rather than left
implicit: an LLM call in `llm.decompose_features`, prompted below, whose output
is written to `candidate_decomposition` and to nothing else.

Requirement quality caps evidence quality -- a vague element yields an
unfalsifiable verdict -- so the open question is not *whether* something produces
candidates but whether an 8B-class instruct model is the right thing to. That is
an owner decision, not a code decision, and it is recorded as outstanding in
`docs/decisions/` rather than settled here by default.
"""


def propose_understanding(
    invention: str,
    *,
    problem_solved: str = "",
    novelty_statement: str = "",
    feedback: str = "",
    max_features: int = 5,
    max_elements_per_feature: int = 8,
) -> dict:
    """Propose a **candidate** understanding of an invention, for a human to edit.

    Returns ``{"summary": str, "features": [...]}`` where ``features`` is the
    spec section 2 wire shape: `feature_id`, `feature_version`, `feature_text`,
    `elements[]` and `relationships[]`.

    **This output is never approved and never sufficient to verify against.** It
    is written to `candidate_decomposition` and to `understanding_proposals`,
    neither of which any verification path reads. A human edits and approves it
    at the understanding stage, and only `workflow/approvals.py` turns the result
    into an approved artifact.

    `problem_solved` and `novelty_statement` are the inventor's optional Stage 1
    context. They steer the proposal and are **never** treated as evidence or
    copied into a requirement: what the inventor believes is new is a claim to be
    tested, not a finding about prior art.

    `feedback` is what the inventor said the system misunderstood on a previous
    version. It is additional instruction, never a licence to keep the previous
    proposal -- a regeneration produces a whole new proposal at a new version.

    No `except`: a failed or malformed response fails the request, like every
    other LLM call in this file.
    """
    trace(
        "propose_understanding.start",
        invention_chars=len(invention),
        has_problem=bool(problem_solved.strip()),
        has_novelty=bool(novelty_statement.strip()),
        regenerating=bool(feedback.strip()),
    )

    context = ""
    if problem_solved.strip():
        context += "\n\nPROBLEM THE INVENTOR SAYS THIS SOLVES:\n" + problem_solved.strip()
    if novelty_statement.strip():
        context += (
            "\n\nWHAT THE INVENTOR BELIEVES IS NEW (context only - a claim to be "
            "tested, not a fact):\n" + novelty_statement.strip()
        )
    correction = ""
    if feedback.strip():
        correction = (
            "\n\nA PREVIOUS ANALYSIS WAS REJECTED BY THE INVENTOR, WHO SAID:\n"
            + feedback.strip()
            + "\nProduce a fresh analysis that addresses this. Do not repeat the "
            "previous one."
        )

    prompt = f"""You are a feature decomposition assistant for patent novelty analysis.

Write a short technical summary of the invention, then decompose it into at most {max_features} distinct technical features.

The SUMMARY is 2-4 sentences restating the invention in precise technical language. It is shown to the inventor as the system's understanding of their invention, and must not add capabilities the description does not state.
A FEATURE is one self-contained technical capability of the invention.
An ELEMENT is a single structural or functional component the feature requires.
A RELATIONSHIP is how two or more elements interact, are arranged, or are connected.

Rules:
- Each element must be independently checkable against a patent passage. "A heat exchanger disposed within the exhaust duct" is checkable; "an efficient design" is not.
- Mark an element core=true when the feature is not the claimed feature without it.
- At most {max_elements_per_feature} elements per feature.
- Every relationship must refer only to elements of its own feature.
- Every element_id and relationship_id must be unique ACROSS the whole feature - never reuse an id between an element and a relationship.
- Use element ids E1, E2, ... and relationship ids R1, R2, ... within each feature.
- feature_version is "1" for a first proposal.
- Do not invent components the invention does not describe.

Return ONLY valid JSON in this exact schema:
{{"summary": "string", "features": [{{"feature_id": "F1", "feature_version": "1", "feature_text": "string", "elements": [{{"element_id": "E1", "text": "string", "core": true, "order": 1}}], "relationships": [{{"relationship_id": "R1", "text": "string", "order": 1}}]}}]}}
Do not change the schema. Do not add explanations. Return JSON only.

Invention:
{invention}{context}{correction}"""
    data = _llm_json(
        prompt,
        # Scales with the workload, like analyze_all_matches and
        # validate_relevance: a flat ceiling truncates on the largest input, and
        # truncation is never retried at the same ceiling. The extra 400 over the
        # old decomposition-only ceiling is the summary this call added.
        max_tokens=1200 + 600 * max_features,
        temperature=0.0,
        timeout=60,
    )

    summary = str(data.get("summary") or "").strip()
    if not summary:
        raise LLMResponseError(
            "LLM returned no technical summary; the understanding stage shows the "
            "summary as the system's interpretation, and an empty one is not an "
            "interpretation a human can correct"
        )

    features = data.get("features")
    if not isinstance(features, list) or not features:
        raise LLMResponseError(
            "LLM returned no features; a decomposition with nothing in it is not a "
            "proposal a human can edit, and an empty candidate set is not invented here"
        )
    trace(
        "propose_understanding.done",
        features=len(features),
        summary_chars=len(summary),
    )
    return {"summary": summary, "features": [dict(feature) for feature in features]}


# `regenerate_relationships` was REMOVED here. It asked the model for a fresh set
# of relationships whenever a capability's elements were edited, which was the
# real defence against a relationship naming a deleted element. There are no
# relationships to regenerate and no caller left; spec section 4.2.3 removes the
# check and the caller together.


STRATEGY_PROMPT_MARKER = "patent search strategist"
"""The substring `run_mock.py` keys off for the search-strategy proposal.

Deliberately shares no wording with `DECOMPOSITION_PROMPT_MARKER`. When there
were two decomposition prompts they overlapped on purpose (same component, same
job) and needed an ordering rule at the double to tell them apart; this one is a
different component asking a different question, so it is a marker no other
prompt contains and the double can match it in any order.

Imported rather than duplicated as a literal, so a prompt edit that breaks the
marker raises at the double instead of quietly reaching a metered endpoint.
"""

STRATEGY_COMPONENT = "graph.strategy.propose_search_strategy"
"""The component that proposes search queries, named for the same reason
`DECOMPOSITION_COMPONENT` is: the specification does not assign an owner to it,
and a component nobody has named cannot be assigned one later."""


def propose_search_strategy(
    summary: str,
    features: Sequence[dict],
    *,
    existing_queries: Sequence[str] = (),
    feedback: str = "",
    max_queries: int = 10,
) -> list[dict]:
    """Propose **candidate** search queries for an approved understanding.

    Returns a list of ``{"query_id", "query_text", "rationale",
    "covered_element_ids"}``. Each query names the approved element ids it is
    meant to retrieve prior art for, which is what the coverage check at the
    stage's gate is computed from -- a core element no enabled query covers
    blocks approval.

    Ids are **feature-qualified** (``F1.E2``). A requirement id is unique within
    one feature and not across a run, so a bare ``E1`` would name an element in
    every feature that happens to have one -- and a coverage claim that resolves
    to more than one element is not a coverage claim.

    **This output is never approved and never dispatched as it stands.** It is
    written to ``candidate_search_strategy`` and to ``search_strategy_proposals``,
    neither of which the search reads; only the queries a human approved and left
    enabled are executed.

    `existing_queries` are the queries the understanding call already produced.
    They are offered as material rather than as a floor: the model may keep,
    reword or replace them, and nothing here silently appends them to the result.

    `feedback` is what the reviewer said was wrong with a previous strategy. It is
    additional instruction, never a licence to keep the previous proposal -- a
    regeneration produces a whole new strategy at a new version.

    A coverage claim naming an element id that is not in `features` is left
    exactly as the model returned it. It is refused at the gate
    (`UNKNOWN_ELEMENT_REFERENCE`) rather than dropped here, because silently
    discarding a claim would turn a model that misunderstood the requirements into
    a strategy that merely looks like it covers fewer of them.

    No `except`: a failed or malformed response fails the request, like every
    other LLM call in this file.
    """
    trace(
        "propose_search_strategy.start",
        features=len(features),
        existing=len(existing_queries),
        regenerating=bool(feedback.strip()),
    )

    listing = []
    for feature in features:
        feature_id = str(feature.get("feature_id"))
        listing.append(
            "FEATURE " + feature_id + ": " + str(feature.get("feature_text") or "")
        )
        for element in feature.get("elements") or ():
            core = " [core]" if element.get("core") else ""
            # Feature-qualified, because an element id is unique within a feature
            # and not across a run: two features may each have an E1, and a bare
            # id in a coverage claim would name both or neither.
            listing.append(
                "  " + feature_id + "." + str(element.get("element_id")) + core + ": "
                + str(element.get("text") or "")
            )
    elements_block = "\n".join(listing)

    prior = ""
    if existing_queries:
        prior = (
            "\n\nQUERIES ALREADY DRAFTED FROM THE DESCRIPTION (material, not a "
            "floor - keep, reword or replace them):\n"
            + "\n".join("- " + str(query) for query in existing_queries)
        )
    correction = ""
    if feedback.strip():
        correction = (
            "\n\nA PREVIOUS SEARCH STRATEGY WAS REJECTED BY THE REVIEWER, WHO SAID:\n"
            + feedback.strip()
            + "\nProduce a fresh strategy that addresses this. Do not repeat the "
            "previous one."
        )

    prompt = f"""You are a patent search strategist.

Below is an APPROVED technical summary of an invention and the approved technical elements a human confirmed. Propose at most {max_queries} patent search queries that together retrieve prior art for those elements.

APPROVED SUMMARY:
{summary}

APPROVED TECHNICAL ELEMENTS:
{elements_block}

Rules:
- Every element marked [core] must be covered by at least one query. A core element is one the invention is not itself without.
- covered_element_ids lists the element ids a query is aimed at, written exactly as they appear above, including the feature prefix (for example F1.E2). Never invent an id that is not listed.
- Do not claim coverage a query does not have. A query listed against an element it would not retrieve prior art for is worse than one fewer query.
- Keep each query between 3 and 8 words, in the vocabulary patent titles, abstracts and claims actually use.
- Vary the angle across the set: what the invention does, what it is made of, the domain it is used in, and how an examiner would classify it.
- Avoid near-duplicate queries; each one should be able to return something the others would miss.
- rationale is one short sentence saying what this query is looking for.
- Use query ids Q1, Q2, ... in the order you propose them.

Return ONLY valid JSON in this exact schema:
{{"queries": [{{"query_id": "Q1", "query_text": "string", "rationale": "string", "covered_element_ids": ["F1.E1"]}}]}}
Do not change the schema. Do not add explanations. Return JSON only.{prior}{correction}"""

    data = _llm_json(
        prompt,
        # Scales with the workload, like every other batched call here: a flat
        # ceiling truncates on the largest input, and truncation is never retried
        # at the same ceiling.
        max_tokens=400 + 160 * max_queries,
        temperature=0.0,
        timeout=60,
    )

    queries = data.get("queries")
    if not isinstance(queries, list) or not queries:
        raise LLMResponseError(
            "LLM returned no search queries; a strategy with nothing in it is not a "
            "proposal a reviewer can edit, and an empty query set is not invented here"
        )
    trace("propose_search_strategy.done", queries=len(queries))
    return [dict(query) for query in queries]
