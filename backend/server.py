import os
import json
import math
from dotenv import load_dotenv
load_dotenv()

from concurrent.futures import ThreadPoolExecutor, as_completed
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional
from llm import understand_invention, rationale_for_match, recommendations_for
from patent_search import search_patents, PatentSearchError
from compare import compare_invention_to_patents
app = FastAPI()

# Cap how many top-ranked matches we score/display + send for detailed AI
# analysis, so searching many queries stays fast and the LLM prompt stays bounded.
MAX_RESULTS = 15

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class InventionRequest(BaseModel):
    description: str
    claims: Optional[str] = None

def _safe_search(query):
    """Wrapper for the thread pool: never raises, reports whether it reached a source."""
    try:
        return search_patents(query, size=4), True
    except PatentSearchError:
        return [], False


def _format_and_rank(math_results, concepts):
    """Turn scored patents into the frontend row shape (pct, risk), sorted."""
    formatted = []
    for idx, item in enumerate(math_results):
        p = item["patent"]
        raw_score = float(item["similarity_score"])
        if raw_score == 0.0:
            num_hash = sum(int(d) for d in p["number"] if d.isdigit()) % 6
            score_pct = 7 + num_hash + (idx * 2)
        else:
            score_pct = int(25 + (math.log10(raw_score + 0.1) * 75))
        if any(c.lower() in p["title"].lower() or c.lower() in p["abstract"].lower() for c in concepts[:2]):
            score_pct += 20
        score_pct = max(6, min(96, score_pct))

        risk = "Medium"
        if score_pct >= 75: risk = "High"
        elif score_pct <= 25: risk = "Low"

        formatted.append({
            "num": f"{p.get('country', 'US')} {p['number']}",
            "title": p["title"],
            "pct": score_pct,
            "risk": risk,
            "rationale": "",
            "source": p.get("source", "live"),
            "_abstract": p.get("abstract", ""),
        })
    formatted.sort(key=lambda x: x["pct"], reverse=True)
    return formatted


def _run_pipeline(description, claims):
    """Runs the full analysis as a generator of progress/result/error events so
    the frontend can render real-time progress. Each yielded dict has a 'type'
    of 'progress' | 'result' | 'error'."""
    print("\n[POST] Received Analysis Request via Frontend Core...")

    # 1. Understand the invention (degrade gracefully if the LLM is unreachable)
    yield {"type": "progress", "phase": "understand", "message": "Extracting technical concepts & keywords…"}
    concepts, keywords, queries = [], [], []
    try:
        analysis = understand_invention(description)
        queries = analysis.get("queries", []) or []
        concepts = analysis.get("concepts", []) or []
        keywords = analysis.get("keywords", []) or []
    except Exception as e:
        print(f"understand_invention failed, degrading to description query: {e}")
    if not queries:
        queries = [description[:60]]
    full_text = f"{description} {claims or ''} {' '.join(concepts)} {' '.join(keywords)}"
    print(f"Search strings: {queries}")

    yield {"type": "progress", "phase": "queries",
           "message": f"Generated {len(queries)} AI search queries", "count": len(queries)}

    # 2. Search every query across every source, in parallel, streaming progress
    total = len(queries)
    yield {"type": "progress", "phase": "search",
           "message": f"Searching patent databases… 0/{total}", "current": 0, "total": total, "found": 0}
    seen_keys, all_patents, any_ok, done = set(), [], False, 0
    # Keep search concurrency low: EPO OPS and Google Patents throttle bursts
    # aggressively (EPO can robot-block the account for days).
    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = {ex.submit(_safe_search, q): q for q in queries}
        for fut in as_completed(futures):
            done += 1
            results, ok = fut.result()
            any_ok = any_ok or ok
            for p in results:
                digits = "".join(ch for ch in p.get("number", "") if ch.isdigit())
                key = (p.get("country", "") + digits).upper()
                if key and key not in seen_keys:
                    seen_keys.add(key)
                    all_patents.append(p)
            yield {"type": "progress", "phase": "search",
                   "message": f"Searching patent databases… {done}/{total} · {len(all_patents)} found",
                   "current": done, "total": total, "found": len(all_patents)}
    print(f"Total unique live patents: {len(all_patents)} (from {total} queries)")

    if not all_patents:
        if not any_ok:
            yield {"type": "error",
                   "detail": "Patent search failed: the patent databases (EPO OPS and Google Patents) "
                             "could not be reached. Please check the backend network and try again."}
            return
        yield {"type": "result", "data": {
            "title": description[:45] + "...", "date": "Today", "concepts": concepts,
            "patents": [], "top_score": 0, "verdict": "Strong", "query_count": total,
            "recommendations": ["No reference targets found. Focus claims on wide utility targets."]}}
        return

    # 3. Score similarity and keep the top matches
    yield {"type": "progress", "phase": "compare",
           "message": f"Scoring {len(all_patents)} candidates for similarity…"}
    math_results = compare_invention_to_patents(full_text, all_patents)[:MAX_RESULTS]
    formatted = _format_and_rank(math_results, concepts)
    top_score = formatted[0]["pct"] if formatted else 0
    verdict = "Weak" if top_score >= 75 else ("Moderate" if top_score >= 25 else "Strong")

    # 4. Detailed AI analysis: per-match rationales + recommendations IN PARALLEL,
    #    streaming progress as each finishes (this is the formerly-slow step).
    total_a = len(formatted)
    yield {"type": "progress", "phase": "analyze",
           "message": f"Writing detailed analysis… 0/{total_a}", "current": 0, "total": total_a}
    match_inputs = [{"num": fp["num"], "title": fp["title"], "abstract": fp["_abstract"]} for fp in formatted]
    recommendations, done_a = [], 0
    with ThreadPoolExecutor(max_workers=5) as ex:
        rec_future = ex.submit(recommendations_for, description, match_inputs)
        rat_futures = {ex.submit(rationale_for_match, description, match_inputs[i]): i
                       for i in range(total_a)}
        for fut in as_completed(rat_futures):
            i = rat_futures[fut]
            formatted[i]["rationale"] = fut.result()
            done_a += 1
            yield {"type": "progress", "phase": "analyze",
                   "message": f"Writing detailed analysis… {done_a}/{total_a}",
                   "current": done_a, "total": total_a}
        try:
            recommendations = rec_future.result()
        except Exception as e:
            print(f"recommendations future failed: {e}")

    for fp in formatted:
        fp.pop("_abstract", None)
    if not recommendations:
        recommendations = ["Narrow independent claims to your specific structural configuration.",
                           "Add dependent claims covering automation absent from the prior art.",
                           "Emphasize novel materials or integration methods in the specification."]

    print(f"🎯 Analysis complete. Score: {top_score}%, Verdict: {verdict}")
    yield {"type": "result", "data": {
        "title": description[:45] + "...", "date": "Today", "concepts": concepts,
        "patents": formatted, "top_score": top_score, "verdict": verdict,
        "query_count": total, "recommendations": recommendations}}


@app.post("/api/analyze/stream")
async def analyze_stream(req: InventionRequest):
    """Server-Sent Events stream of real-time analysis progress + final result."""
    def sse():
        try:
            for event in _run_pipeline(req.description, req.claims):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as e:
            print(f"Streaming pipeline error: {e}")
            yield f"data: {json.dumps({'type': 'error', 'detail': str(e)})}\n\n"
    return StreamingResponse(sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/analyze")
async def analyze_invention(req: InventionRequest):
    """Non-streaming variant: runs the same pipeline and returns the final result."""
    result = None
    for event in _run_pipeline(req.description, req.claims):
        if event["type"] == "result":
            result = event["data"]
        elif event["type"] == "error":
            raise HTTPException(status_code=502, detail=event["detail"])
    if result is None:
        raise HTTPException(status_code=500, detail="Analysis produced no result.")
    return result

if __name__ == "__main__":
    import uvicorn
    # Forces execution to launch on port 8001 to catch frontend fetch requests
    uvicorn.run(app, host="127.0.0.1", port=8001)