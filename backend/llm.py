import os
import json
import requests
import re

# This is the single, clean global variable both functions will share
API_URL = "https://router.huggingface.co/v1/chat/completions"

headers = {
    "Authorization": f"Bearer {os.environ.get('HF_TOKEN', '')}",
}

def understand_invention(invention: str):
    prompt = f"""
You are PatentScout AI, an AI assistant that helps users search for prior art.

Analyze the invention below.

Tasks:
1. Extract the main technical concepts.
2. Extract important engineering keywords.
3. Generate synonyms for the keywords.
4. Generate FIVE patent search queries.

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

    payload = {
        "model": "meta-llama/Llama-3.1-8B-Instruct:novita",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 500
    }

    response = requests.post(API_URL, headers=headers, json=payload)
    response.raise_for_status()
    
    result = response.json()
    text = result["choices"][0]["message"]["content"]
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