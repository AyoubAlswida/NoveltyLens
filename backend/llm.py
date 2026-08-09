"""LLM integration for invention understanding, match explanation, and relevance validation."""

import json
import logging
import time

import requests

from config import settings
from models import InventionAnalysis, MatchAnalysis, PatentRecord
from tracing import trace


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


def _llm_chat(
    prompt: str,
    max_tokens: int = 400,
    temperature: float = 0.3,
    timeout: int = 30,
    retries: int = 2,
    json_object: bool = True,
) -> str:
    """Send one chat completion request with bounded transient-error retries.

    ``json_object`` asks the provider for guaranteed-parseable output via
    ``response_format`` (verified supported by the HF Router + Novita endpoint
    this project targets). It constrains decoding rather than merely requesting
    JSON in the prompt, which removes the unescaped-quote and stray-prose
    failure modes at the source.

    Raises:
        LLMTruncatedError: if the response hit ``max_tokens``. Not retried - see
            that class.
        RuntimeError: if every transport attempt failed.
    """
    if not settings.hf_token:
        raise RuntimeError("HF_TOKEN is not configured")
    payload: dict[str, object] = {
        "model": settings.llm_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_object:
        payload["response_format"] = {"type": "json_object"}
    trace("llm.request", model=settings.llm_model, prompt_chars=len(prompt), max_tokens=max_tokens, temperature=temperature)
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = requests.post(settings.llm_api_url, headers={"Authorization": f"Bearer {settings.hf_token}"}, json=payload, timeout=timeout)
            response.raise_for_status()
            choice = response.json()["choices"][0]
        except Exception as error:
            # Transport and HTTP failures only. A truncated *successful*
            # response is handled below, outside this retry loop.
            last_error = error
            trace("llm.attempt_failed", attempt=attempt, error=str(error))
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


def analyze_all_matches(invention: str, patents: list[PatentRecord]) -> MatchAnalysis:
    """Rationalize every patent and propose recommendations in one LLM call.

    A single batched request is far friendlier to the model provider's rate
    limits than issuing one call per patent. Raises on any failure - a missing
    rationale or an empty recommendation list is a failure of this call, not a
    reason to substitute canned text.
    """
    count = len(patents)
    if count == 0:
        return MatchAnalysis(rationales=[], recommendations=[])

    patent_block = "\n".join(
        f"[{index}] Number: {patent.id}\n    Title: {patent.title}\n    Abstract: {patent.abstract[:350]}"
        for index, patent in enumerate(patents, start=1)
    )
    prompt = f'''You are an expert patent examiner comparing a user's invention against {count} prior-art patents.

User Invention:
{invention}

Prior-art patents:
{patent_block}

Tasks:
1. For EACH of the {count} patents, write a DETAILED one-paragraph technical rationale (3 to 5 full sentences) explaining specifically how that patent's technology OVERLAPS with the user's invention - the shared mechanism, purpose, structure, or technical approach that makes it relevant prior art. Reference the concrete technical features of that individual patent that are similar to the user's invention. Do NOT describe how the invention differs from the patent - that is covered separately; focus this rationale entirely on the similarity. Every rationale must be distinct - never reuse wording between patents.
2. Provide AS MANY actionable, specific uniqueness recommendations as you can (at least 10) describing concrete engineering, structural, or claim-drafting changes the user can make to distinguish their invention from this body of prior art.

Return ONLY valid JSON in exactly this schema:
{{"rationales": [{{"index": 1, "text": "detailed paragraph for patent 1"}}], "recommendations": ["tip 1", "tip 2"]}}
Include one rationale object for every patent from index 1 through {count}, in order. Do not include markdown code fences or any text outside the JSON object.'''

    trace("analyze_all_matches.start", patent_count=count)
    try:
        parsed = _llm_json(
            prompt,
            # Scales with the workload instead of a flat 3200. This prompt asks
            # for 3-5 full sentences per patent (~135 tokens each) plus at least
            # ten recommendations, so at the MAX_RESULTS cap of 15 patents the
            # honest requirement is ~2700 tokens - close enough to a flat 3200
            # that ordinary variation truncated it. Observed doing exactly that
            # on a real 14-match run.
            max_tokens=min(8000, 1200 + 250 * count),
            temperature=0.3,
            timeout=90,
        )
    except Exception:
        logger.exception("Batched multi-match analysis failed")
        raise

    rationales = [""] * count
    raw_rationales = parsed.get("rationales", [])
    for item in raw_rationales:
        if isinstance(item, dict):
            index = int(item.get("index", 0)) - 1
            if 0 <= index < count:
                rationales[index] = str(item.get("text", "")).strip()
    if not any(rationales):
        # Tolerate a plain array of strings instead of indexed objects.
        for index, item in enumerate(raw_rationales[:count]):
            if isinstance(item, str):
                rationales[index] = item.strip()

    missing = [index + 1 for index, text in enumerate(rationales) if not text]
    if missing:
        raise ValueError(f"LLM omitted rationale(s) for patent index/indices: {missing}")

    recommendations = [str(item).strip() for item in parsed.get("recommendations", []) if str(item).strip()]
    if not recommendations:
        raise ValueError("LLM returned no uniqueness recommendations")

    trace("analyze_all_matches.done", rationale_count=len(rationales), recommendation_count=len(recommendations))
    return MatchAnalysis(rationales=rationales, recommendations=recommendations)


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
