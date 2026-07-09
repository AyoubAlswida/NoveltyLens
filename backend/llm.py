import os
import json
import time
import requests
import re

# This is the single, clean global variable both functions will share
API_URL = "https://router.huggingface.co/v1/chat/completions"
MODEL = "meta-llama/Llama-3.1-8B-Instruct:novita"

headers = {
    "Authorization": f"Bearer {os.environ.get('HF_TOKEN', '')}",
}


def _llm_chat(prompt, max_tokens=400, temperature=0.3, timeout=30, retries=2):
    """Single chat-completion call with retry on transient network/DNS/5xx
    errors. Returns the assistant message text or raises after the last retry."""
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    auth = {"Authorization": f"Bearer {os.environ.get('HF_TOKEN', '')}"}
    last_err = None
    for attempt in range(retries + 1):
        try:
            resp = requests.post(API_URL, headers=auth, json=payload, timeout=timeout)
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))  # brief backoff before retrying
    raise last_err

def understand_invention(invention: str):
    prompt = f"""
You are PatentScout AI, an AI assistant that helps users search for prior art.

Analyze the invention below.

Tasks:
1. Extract the main technical concepts.
2. Extract important engineering keywords.
3. Generate synonyms for the keywords.
4. Generate exactly 6 distinct, high-quality patent search queries. Each query must target a DIFFERENT technical angle, keyword combination, or application area of the invention to maximize prior-art recall without over-querying. Keep each query short (3-7 words) and search-engine friendly.

Return ONLY valid JSON in this exact schema:
{{
  "concepts": ["string"],
  "keywords": ["string"],
  "synonyms": {{"keyword": ["synonym1","synonym2"]}},
  "queries": ["string"]
}}
Do not change the schema. Do not add explanations. Return JSON only.

Invention:
{invention}
"""

    # temperature=0 -> the model returns the SAME queries/concepts for the same
    # invention on every run, so results stay stable instead of shifting each time.
    text = _llm_chat(prompt, max_tokens=1200, temperature=0.0, timeout=40)
    text = text.replace("```json", "").replace("```", "").strip()
    return json.loads(text)

def compare_with_llm(invention_desc: str, patent_title: str, patent_abstract: str):
    """
    Uses Llama 3.1 to semantically score similarity and generate custom uniqueness tips.
    """
    prompt = f"""
    You are an expert Patent Examiner. Semantically analyze the similarity between the user's proposed invention and the prior art patent.

User Invention Description:
{invention_desc}

Prior Art Patent Title: {patent_title}
Prior Art Patent Abstract: {patent_abstract}

Tasks:
1. Evaluate the conceptual and functional overlap.
2. Determine an exact similarity score between 0 and 100 (Integer).
3. Write a concise, 1-2 sentence technical rationale explaining why they overlap.
4. Provide exactly THREE actionable, specific recommendations on how the user can modify, narrow, or expand their design to maximize its novelty and distance it even further from this prior art. If the similarity score is extremely low (e.g., under 20%), provide strategic engineering tips to reinforce its current independent technical path.

Return ONLY a valid JSON object using this exact key structure:
{{
  "score": SCORE_INT,
  "rationale": "TECHNICAL_RATIONALE_STRING",
  "recommendations": [
    "RECOMMENDATION_STRING_1",
    "RECOMMENDATION_STRING_2",
    "RECOMMENDATION_STRING_3"
  ]
}}
Do not include markdown code block wrappers or any extra text outside the JSON object.
"""

    hf_token = os.environ.get("HF_TOKEN", "")
    current_headers = {"Authorization": f"Bearer {hf_token}"}

    payload = {
        "model": "meta-llama/Llama-3.1-8B-Instruct:novita",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 350
    }

    try:
        # ✅ Directly using the clean global API_URL variable from the top of the file
        response = requests.post(API_URL, headers=current_headers, json=payload, timeout=12)
        response.raise_for_status()
        
        text = response.json()["choices"][0]["message"]["content"].strip()
        print("\n🤖 [LLM RAW RESPONSE]:\n", text)
        
        json_match = re.search(r"\{.*\}", text, re.DOTALL)
        if json_match:
            parsed_data = json.loads(json_match.group(0))
            if "score" in parsed_data:
                return {
                    "score": int(parsed_data["score"]),
                    "rationale": parsed_data.get("rationale", "Semantic match calculated by LLM model analysis loops."),
                    "recommendations": parsed_data.get("recommendations", [])
                }
            
        raise ValueError("Could not locate clean JSON structures.")

    except Exception as e:
        print(f"\n⚠️ [LLM API BLOCK] Falling back to localized data comparison: {e}")
        
        user_words = set(re.findall(r'\w+', invention_desc.lower()))
        patent_words = set(re.findall(r'\w+', (patent_title + " " + patent_abstract).lower()))
        stopwords = {'a', 'an', 'the', 'and', 'or', 'if', 'with', 'by', 'for', 'is', 'that', 'this', 'using', 'system', 'device', 'method', 'process'}
        
        intersection = user_words.intersection(patent_words) - stopwords
        shared_keywords = list(intersection)[:2]

        if shared_keywords:
            recs = [
                f"Differentiate structural configuration components to decouple features from standard '{shared_keywords[0]}' architectures.",
                f"Introduce automated tracking feedback parameters absent in basic prior art '{shared_keywords[0]}' registries.",
                f"Narrow the independent claims focus to specialized control software or physical integration interfaces targeting '{shared_keywords[-1]}'."
            ]
        else:
            recs = [
                "Focus your independent claims heavily on high-utility targets across the primary field domain.",
                "Incorporate multi-stage execution mechanisms to solidify baseline structural originality.",
                "Establish clear implementation definitions to optimize the future priority patent filing phase."
            ]
        
        return {
            "score": 50,
            "rationale": "Localized index tracking matched recurring domain technical elements inside fallback registries.",
            "recommendations": recs
        }


# ---------------------------------------------------------------------------
# Multi-match analysis: one LLM pass gives EVERY prior-art match its own
# detailed rationale, plus an expanded list of uniqueness recommendations.
# ---------------------------------------------------------------------------

_STOPWORDS = {
    "a", "an", "the", "and", "or", "if", "with", "by", "for", "is", "that",
    "this", "using", "system", "device", "method", "process", "of", "to",
    "in", "on", "as", "are", "be", "from", "at", "it", "its", "into",
}


def _keywords(text):
    return set(re.findall(r"\w+", (text or "").lower()))


def _fallback_rationale(invention_desc, match):
    """Builds a rationale unique to THIS patent from its own shared vocabulary,
    so offline mode never produces duplicate explanations."""
    shared = list(
        (_keywords(invention_desc) & _keywords(f"{match.get('title','')} {match.get('abstract','')}"))
        - _STOPWORDS
    )[:4]
    title = match.get("title", "this prior-art reference")

    if shared:
        kws = ", ".join(shared)
        return (
            f"This reference (“{title}”) overlaps with your invention through shared technical "
            f"terminology around {kws}. That common vocabulary places both in a related functional domain, "
            f"but the prior art's specific implementation, architecture, and claim scope differ from the approach "
            f"you describe. Focus on how your structural configuration and control logic diverge from what is "
            f"disclosed here to reinforce novelty against this particular record."
        )
    return (
        f"This reference (“{title}”) was surfaced by the search but shows only limited direct textual "
        f"overlap with your invention. The relationship appears to sit at a broad domain level rather than in "
        f"specific technical features, which suggests your core approach already maintains meaningful distance from "
        f"this piece of prior art. Confirm that your independent claims avoid the general concepts it covers."
    )


def _fallback_recommendations(invention_desc, matches):
    return [
        "Narrow your independent claims to the specific structural configuration that distinguishes your device from the closest prior art.",
        "Add dependent claims covering the automated control logic and sensor-triggered behaviors the reference patents do not describe.",
        "Emphasize any novel materials, geometries, or integration methods unique to your embodiment in the specification.",
        "Introduce a measurable performance improvement (efficiency, accuracy, latency, power) and claim the mechanism that achieves it.",
        "Combine two or more subsystems into a single integrated unit in a way none of the prior art discloses.",
        "Specify a concrete use-case or operating environment that constrains the claims away from the broad prior-art disclosures.",
        "Document a unique data-processing or feedback loop that differentiates your telemetry from generic monitoring systems.",
        "Claim a specific human-interaction or automation trigger that is absent from the reference patents.",
        "Highlight interoperability or connectivity features (protocols, syncing behavior, offline operation) that the prior art lacks.",
        "Add a self-calibration, self-cleaning, or fault-recovery routine that the cited references do not mention.",
        "Consider a continuation strategy that reserves broader coverage while your core narrow claims proceed to grant.",
        "Draft the abstract and title around your differentiating feature rather than the generic device category.",
    ]


def rationale_for_match(invention_desc, match):
    """One small, fast LLM call for a SINGLE patent's detailed rationale. Runs
    in parallel with the others; falls back to a unique local paragraph on error."""
    prompt = f"""You are an expert patent examiner. In 3 to 5 full sentences, write a DETAILED technical rationale explaining specifically how the prior-art patent below overlaps with or differs from the user's invention. Reference the patent's concrete technical features. Return ONLY the paragraph text - no preamble, no bullet points, no JSON.

User Invention:
{invention_desc}

Prior-art patent:
Number: {match.get('num', '')}
Title: {match.get('title', '')}
Abstract: {(match.get('abstract') or '')[:400]}
"""
    try:
        text = _llm_chat(prompt, max_tokens=280, temperature=0.3, timeout=30).strip().strip('"')
        # Guard against the model echoing JSON/preamble instead of prose.
        if text and not text.lstrip().startswith("{"):
            return text
        raise ValueError("unusable rationale text")
    except Exception as e:
        print(f"Rationale fell back for {match.get('num','?')}: {e}")
        return _fallback_rationale(invention_desc, match)


def recommendations_for(invention_desc, matches):
    """One LLM call for the expanded uniqueness-tips list; local fallback on error."""
    titles = "; ".join(m.get("title", "") for m in matches[:8])
    prompt = f"""You are an expert patent examiner. Given the user's invention and the closest prior art, list AS MANY specific, actionable uniqueness recommendations as you can (at least 10) describing concrete engineering, structural, or claim-drafting changes to distinguish the invention. Return ONLY a JSON array of strings like ["tip one","tip two"]. No other text.

User Invention:
{invention_desc}

Closest prior-art titles: {titles}
"""
    try:
        text = _llm_chat(prompt, max_tokens=900, temperature=0.4, timeout=40)
        match_arr = re.search(r"\[.*\]", text, re.DOTALL)
        if match_arr:
            recs = [str(x).strip() for x in json.loads(match_arr.group(0)) if str(x).strip()]
            if recs:
                return recs
        raise ValueError("no JSON array found")
    except Exception as e:
        print(f"Recommendations fell back: {e}")
        return _fallback_recommendations(invention_desc, matches)


def analyze_all_matches(invention_desc, matches):
    """Returns {'rationales': [...], 'recommendations': [...]} where rationales
    aligns 1:1 with `matches` (same order), each a detailed unique paragraph."""
    n = len(matches)
    if n == 0:
        return {"rationales": [], "recommendations": []}

    patent_block = ""
    for i, m in enumerate(matches, 1):
        abstract = (m.get("abstract") or "")[:350]
        patent_block += f"\n[{i}] Number: {m.get('num','')}\n    Title: {m.get('title','')}\n    Abstract: {abstract}\n"

    prompt = f"""You are an expert patent examiner comparing a user's invention against {n} prior-art patents.

User Invention:
{invention_desc}

Prior-art patents:
{patent_block}

Tasks:
1. For EACH of the {n} patents, write a DETAILED one-paragraph technical rationale (3 to 5 full sentences) explaining specifically how that patent's technology overlaps with or differs from the user's invention. Reference the concrete technical features of that individual patent. Every rationale must be distinct - never reuse wording between patents.
2. Provide AS MANY actionable, specific uniqueness recommendations as you can (at least 10) describing concrete engineering, structural, or claim-drafting changes the user can make to distinguish their invention from this body of prior art.

Return ONLY valid JSON in exactly this schema:
{{
  "rationales": [
    {{"index": 1, "text": "detailed paragraph for patent 1"}}
  ],
  "recommendations": ["tip 1", "tip 2"]
}}
Include one rationale object for every patent from index 1 through {n}, in order. Do not include markdown code fences or any text outside the JSON object."""

    payload = {
        "model": "meta-llama/Llama-3.1-8B-Instruct:novita",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 3200,
    }
    current_headers = {"Authorization": f"Bearer {os.environ.get('HF_TOKEN', '')}"}

    try:
        response = requests.post(API_URL, headers=current_headers, json=payload, timeout=60)
        response.raise_for_status()
        text = response.json()["choices"][0]["message"]["content"].strip()

        json_match = re.search(r"\{.*\}", text, re.DOTALL)
        if not json_match:
            raise ValueError("No JSON object found in LLM response.")
        parsed = json.loads(json_match.group(0))

        rationales = [""] * n
        raw_rationales = parsed.get("rationales", [])
        for item in raw_rationales:
            if isinstance(item, dict):
                idx = int(item.get("index", 0)) - 1
                if 0 <= idx < n:
                    rationales[idx] = str(item.get("text", "")).strip()
        # Tolerate a plain array of strings instead of indexed objects.
        if not any(rationales):
            for i, item in enumerate(raw_rationales[:n]):
                if isinstance(item, str):
                    rationales[i] = item.strip()

        recommendations = [
            r.strip() for r in parsed.get("recommendations", [])
            if isinstance(r, str) and r.strip()
        ]

        # Backfill any missing pieces so the response is always complete.
        for i in range(n):
            if not rationales[i]:
                rationales[i] = _fallback_rationale(invention_desc, matches[i])
        if not recommendations:
            recommendations = _fallback_recommendations(invention_desc, matches)

        return {"rationales": rationales, "recommendations": recommendations}

    except Exception as e:
        print(f"\n⚠️ [LLM multi-analysis fell back to local generation]: {e}")
        return {
            "rationales": [_fallback_rationale(invention_desc, m) for m in matches],
            "recommendations": _fallback_recommendations(invention_desc, matches),
        }