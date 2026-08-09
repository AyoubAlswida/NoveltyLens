"""LLM integration for invention understanding, match explanation, and relevance validation."""

import json
import logging
import re
import time

import requests

from config import settings
from models import InventionAnalysis, MatchAnalysis, PatentRecord
from tracing import trace


logger = logging.getLogger(__name__)


def _llm_chat(prompt: str, max_tokens: int = 400, temperature: float = 0.3, timeout: int = 30, retries: int = 2) -> str:
    """Send one chat completion request with bounded transient-error retries."""
    if not settings.hf_token:
        raise RuntimeError("HF_TOKEN is not configured")
    payload = {"model": settings.llm_model, "messages": [{"role": "user", "content": prompt}], "temperature": temperature, "max_tokens": max_tokens}
    trace("llm.request", model=settings.llm_model, prompt_chars=len(prompt), max_tokens=max_tokens, temperature=temperature)
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = requests.post(settings.llm_api_url, headers={"Authorization": f"Bearer {settings.hf_token}"}, json=payload, timeout=timeout)
            response.raise_for_status()
            content = str(response.json()["choices"][0]["message"]["content"])
            trace("llm.response", attempt=attempt, response_chars=len(content))
            return content
        except Exception as error:
            last_error = error
            trace("llm.attempt_failed", attempt=attempt, error=str(error))
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("LLM request failed") from last_error


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
    raw = _llm_chat(prompt, max_tokens=1200, temperature=0.0, timeout=40)
    data = json.loads(raw.replace("```json", "").replace("```", "").strip())
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
        raw = _llm_chat(prompt, max_tokens=6000, temperature=0.3, timeout=60)
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise ValueError("no JSON object found in LLM response")
        parsed = json.loads(match.group(0))
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
        raw = _llm_chat(prompt, max_tokens=800, temperature=0.0, timeout=40)
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise ValueError("no JSON object found in validation response")
        parsed = json.loads(match.group(0))
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
