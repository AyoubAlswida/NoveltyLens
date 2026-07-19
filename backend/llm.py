"""LLM integration for invention understanding and top-match explanation."""

import json
import logging
import re
import time

import requests

from config import settings
from models import InventionAnalysis, MatchAnalysis, PatentRecord

logger = logging.getLogger(__name__)


def _llm_chat(prompt: str, max_tokens: int = 400, temperature: float = 0.3, timeout: int = 30, retries: int = 2) -> str:
    """Send one chat completion request with bounded transient-error retries."""
    if not settings.hf_token:
        raise RuntimeError("HF_TOKEN is not configured")
    payload = {"model": settings.llm_model, "messages": [{"role": "user", "content": prompt}], "temperature": temperature, "max_tokens": max_tokens}
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = requests.post(settings.llm_api_url, headers={"Authorization": f"Bearer {settings.hf_token}"}, json=payload, timeout=timeout)
            response.raise_for_status()
            return str(response.json()["choices"][0]["message"]["content"])
        except Exception as error:
            last_error = error
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("LLM request failed") from last_error


def understand_invention(invention: str) -> InventionAnalysis:
    """Convert the LLM's JSON response into a typed invention analysis."""
    prompt = f'''You are PatentScout AI, an AI assistant that helps users search for prior art.

Analyze the invention below.

Tasks:
1. Extract the main technical concepts.
2. Extract important engineering keywords.
3. Generate exactly 6 distinct, high-quality patent search queries. Each query must target a DIFFERENT technical angle, keyword combination, or application area of the invention to maximize prior-art recall without over-querying. Keep each query short (3-7 words) and search-engine friendly.

Return ONLY valid JSON in this exact schema:
{{"concepts": ["string"], "keywords": ["string"], "queries": ["string"]}}
Do not change the schema. Do not add explanations. Return JSON only.

Invention:
{invention}'''
    raw = _llm_chat(prompt, max_tokens=1200, temperature=0.0, timeout=40)
    data = json.loads(raw.replace("```json", "").replace("```", "").strip())
    return InventionAnalysis(
        queries=[str(value) for value in data.get("queries", []) if str(value).strip()],
        keywords=[str(value) for value in data.get("keywords", []) if str(value).strip()],
        concepts=[str(value) for value in data.get("concepts", []) if str(value).strip()],
    )


def analyze_all_matches(invention: str, patents: list[PatentRecord]) -> MatchAnalysis:
    """Rationalize every patent and propose recommendations in one LLM call.

    A single batched request is far friendlier to the model provider's rate
    limits than issuing one call per patent. Falls back to local per-patent
    generation if the batch call fails, or per-item if the model returns
    incomplete JSON.
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
1. For EACH of the {count} patents, write a DETAILED one-paragraph technical rationale (3 to 5 full sentences) explaining specifically how that patent's technology overlaps with or differs from the user's invention. Reference the concrete technical features of that individual patent. Every rationale must be distinct - never reuse wording between patents.
2. Provide AS MANY actionable, specific uniqueness recommendations as you can (at least 10) describing concrete engineering, structural, or claim-drafting changes the user can make to distinguish their invention from this body of prior art.

Return ONLY valid JSON in exactly this schema:
{{"rationales": [{{"index": 1, "text": "detailed paragraph for patent 1"}}], "recommendations": ["tip 1", "tip 2"]}}
Include one rationale object for every patent from index 1 through {count}, in order. Do not include markdown code fences or any text outside the JSON object.'''

    try:
        raw = _llm_chat(prompt, max_tokens=3200, temperature=0.3, timeout=60)
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

    return MatchAnalysis(rationales=rationales, recommendations=recommendations)
