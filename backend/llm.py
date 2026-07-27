"""LLM integration for invention understanding and top-match explanation."""

import json
import logging
import re
import time

import requests

from config import settings
from models import InventionAnalysis, PatentRecord
from tracing import trace


logger = logging.getLogger(__name__)
_STOPWORDS = {"a", "an", "the", "and", "or", "if", "with", "by", "for", "is", "that", "this", "using", "system", "device", "method", "process", "of", "to", "in", "on", "as", "are", "be", "from", "at", "it", "its", "into"}


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
3. Generate meaningful synonyms for those keywords.
4. Generate exactly 5 high-quality patent search queries that maximize retrieval of patents describing the SAME invention.

Search query guidelines:
- Focus on the invention's primary functionality, purpose, and claimed mechanism.
- Prioritize the core invention over enabling technologies.
- Do NOT build queries around generic implementation details such as AI, cloud, encryption, Bluetooth, IoT, APIs, databases, or sensors unless they are the novel aspect of the invention.
- Use terminology that is commonly found in patent titles, abstracts, and claims.
- Generate queries from different perspectives (functional, structural, application, industry terminology, and common patent wording).
- Keep each query between 3 and 8 words.
- Avoid repeating nearly identical queries.

Return ONLY valid JSON in this exact schema:
{{"concepts": ["string"], "keywords": ["string"], "synonyms": {{"keyword": ["synonym1", "synonym2"]}}, "queries": ["string"]}}
Do not change the schema. Do not add explanations. Return JSON only.

Invention:
{invention}'''
    raw = _llm_chat(prompt, max_tokens=1200, temperature=0.0, timeout=40)
    data = json.loads(raw.replace("```json", "").replace("```", "").strip())
    analysis = InventionAnalysis(
        queries=[str(value) for value in data.get("queries", []) if str(value).strip()],
        keywords=[str(value) for value in data.get("keywords", []) if str(value).strip()],
        concepts=[str(value) for value in data.get("concepts", []) if str(value).strip()],
        synonyms={str(key): [str(value) for value in values] for key, values in data.get("synonyms", {}).items() if isinstance(values, list)},
    )
    trace("understand_invention.done", queries=analysis.queries, keywords=analysis.keywords, concepts=analysis.concepts)
    return analysis


def _keywords(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.lower()))


def _fallback_rationale(invention: str, patent: PatentRecord) -> str:
    shared = list((_keywords(invention) & _keywords(f"{patent.title} {patent.abstract}")) - _STOPWORDS)[:4]
    if shared:
        return (f"This reference ({patent.title}) overlaps with your invention through shared technical terminology around {', '.join(shared)}. "
                "The disclosed implementation and claim scope may nevertheless differ, so emphasize the structural configuration and control logic that distinguish your approach.")
    return (f"This reference ({patent.title}) was surfaced in a related technical domain but has limited direct textual overlap. "
            "Confirm that the independent claims avoid the broad concepts it covers.")





def rationale_for_match(invention: str, patent: PatentRecord) -> str:
    """Explain a single top-ranked normalized patent, with a local fallback."""
    prompt = f'''You are an expert patent examiner. In 3 to 5 full sentences, write a DETAILED technical rationale explaining specifically how the prior-art patent below overlaps with or differs from the user's invention. Reference the patent's concrete technical features. Return ONLY the paragraph text - no preamble, no bullet points, no JSON.

User Invention:
{invention}

Prior-art patent:
Number: {patent.id}
Title: {patent.title}
Abstract: {patent.abstract[:400]}'''
    try:
        text = _llm_chat(prompt, max_tokens=280, temperature=0.3, timeout=30).strip().strip('"')
        if text and not text.lstrip().startswith("{"):
            trace("rationale_for_match.llm_ok", patent_id=patent.id)
            return text
        raise ValueError("unusable rationale text")
    except Exception:
        logger.exception("Rationale generation failed for %s", patent.id)
        trace("rationale_for_match.fallback", patent_id=patent.id)
        return _fallback_rationale(invention, patent)


def recommendations_for(invention: str, patents: list[PatentRecord]) -> list[str]:
    """Generate claim and engineering recommendations for the top patents only."""
    titles = "; ".join(patent.title for patent in patents[:8])
    prompt = f'''You are an expert patent examiner. Given the user's invention and the closest prior art, list AS MANY specific, actionable uniqueness recommendations as you can (at least 10) describing concrete engineering, structural, or claim-drafting changes to distinguish the invention. Return ONLY a JSON array of strings like ["tip one","tip two"]. No other text.

User Invention:
{invention}

Closest prior-art titles: {titles}'''
    try:
        raw = _llm_chat(prompt, max_tokens=900, temperature=0.4, timeout=40)
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        recommendations = json.loads(match.group(0)) if match else []
        if isinstance(recommendations, list) and recommendations:
            trace("recommendations_for.llm_ok", count=len(recommendations))
            return [str(item).strip() for item in recommendations if str(item).strip()]
        raise ValueError("no recommendation JSON array found")
    except Exception:
        logger.exception("Recommendation generation failed")
        trace("recommendations_for.failed")
        return []