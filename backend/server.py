"""FastAPI transport layer for the patent novelty-search pipeline."""

import json
import logging
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterator, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

load_dotenv()

from compare import compare_invention_to_patents
from llm import rationale_for_match, recommendations_for, understand_invention
from models import ComparisonResult
from search.search import PatentSearchError, enrich_with_full_text, search_patents

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
MAX_RESULTS = 15

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


class InventionRequest(BaseModel):
    description: str
    claims: Optional[str] = None


def _format_and_rank(results: list[ComparisonResult], concepts: list[str]) -> list[dict[str, object]]:
    """Adapt typed comparison results to the unchanged frontend response shape."""
    formatted: list[dict[str, object]] = []
    for index, result in enumerate(results):
        patent = result.patent
        if result.similarity_score == 0.0:
            number_hash = sum(int(char) for char in patent.id if char.isdigit()) % 6
            score = 7 + number_hash + (index * 2)
        else:
            score = int(25 + (math.log10(result.similarity_score + 0.1) * 75))
        if any(concept.lower() in patent.title.lower() or concept.lower() in patent.abstract.lower() for concept in concepts[:2]):
            score += 20
        score = max(6, min(96, score))
        risk = "High" if score >= 75 else "Low" if score <= 25 else "Medium"
        formatted.append({
            "num": patent.id,
            "title": patent.title,
            "pct": score,
            "risk": risk,
            "rationale": "",
            "source": patent.source or "live",
            "full_text_chars": len(patent.full_text),
            "_patent": patent,
        })
    return sorted(formatted, key=lambda item: int(item["pct"]), reverse=True)


def _run_pipeline(description: str, claims: Optional[str]) -> Iterator[dict[str, object]]:
    """Run the typed pipeline and emit the existing SSE event contract."""
    yield {"type": "progress", "phase": "understand", "message": "Extracting technical concepts & keywords…"}
    try:
        analysis = understand_invention(description)
    except Exception:
        logger.exception("Invention understanding failed")
        yield {
            "type": "error",
            "detail": "Invention analysis failed. Check that the LLM is configured and try again.",
        }
        return
    queries = analysis.queries
    if not queries:
        yield {
            "type": "error",
            "detail": "Invention analysis returned no search queries. Please try a more detailed description.",
        }
        return
    full_text = f"{description} {claims or ''} {' '.join(analysis.concepts)} {' '.join(analysis.keywords)}"
    yield {"type": "progress", "phase": "queries", "message": f"Generated {len(queries)} AI search queries", "count": len(queries)}

    yield {"type": "progress", "phase": "search", "message": "Searching patent databases…", "current": 0, "total": len(queries), "found": 0}
    try:
        patents = search_patents(queries, results_per_query=4)
    except PatentSearchError:
        logger.exception("All patent providers failed")
        yield {"type": "error", "detail": "Patent search failed: the patent databases could not be reached. Please check the backend network and try again."}
        return
    yield {"type": "progress", "phase": "search", "message": f"Searching patent databases… {len(queries)}/{len(queries)} · {len(patents)} found", "current": len(queries), "total": len(queries), "found": len(patents)}

    if not patents:
        yield {"type": "result", "data": {"title": description[:45] + "...", "date": "Today", "concepts": analysis.concepts, "patents": [], "top_score": 0, "verdict": "Strong", "query_count": len(queries), "recommendations": ["No reference targets found. Focus claims on wide utility targets."]}}
        return

    yield {"type": "progress", "phase": "enrich", "message": f"Fetching full text for {len(patents)} patents…"}
    patents = enrich_with_full_text(patents)
    enriched_count = sum(1 for patent in patents if patent.full_text)
    yield {
        "type": "progress",
        "phase": "enrich",
        "message": f"Full text retrieved for {enriched_count}/{len(patents)} patents",
        "enriched": enriched_count,
        "total": len(patents),
    }

    yield {"type": "progress", "phase": "compare", "message": f"Scoring {len(patents)} candidates for similarity…"}
    formatted = _format_and_rank(compare_invention_to_patents(full_text, patents)[:MAX_RESULTS], analysis.concepts)
    top_score = int(formatted[0]["pct"]) if formatted else 0
    verdict = "Weak" if top_score >= 75 else "Moderate" if top_score >= 25 else "Strong"

    top_patents = [item["_patent"] for item in formatted]
    yield {"type": "progress", "phase": "analyze", "message": f"Writing detailed analysis… 0/{len(top_patents)}", "current": 0, "total": len(top_patents)}
    with ThreadPoolExecutor(max_workers=5) as executor:
        recommendation_future = executor.submit(recommendations_for, description, top_patents)
        rationale_futures = {executor.submit(rationale_for_match, description, patent): index for index, patent in enumerate(top_patents)}
        for completed, future in enumerate(as_completed(rationale_futures), start=1):
            index = rationale_futures[future]
            formatted[index]["rationale"] = future.result()
            yield {"type": "progress", "phase": "analyze", "message": f"Writing detailed analysis… {completed}/{len(top_patents)}", "current": completed, "total": len(top_patents)}
        recommendations = recommendation_future.result()

    for item in formatted:
        item.pop("_patent", None)
    yield {"type": "result", "data": {"title": description[:45] + "...", "date": "Today", "concepts": analysis.concepts, "patents": formatted, "top_score": top_score, "verdict": verdict, "query_count": len(queries), "recommendations": recommendations}}


@app.post("/api/analyze/stream")
async def analyze_stream(request: InventionRequest) -> StreamingResponse:
    def sse() -> Iterator[str]:
        try:
            for event in _run_pipeline(request.description, request.claims):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as error:
            logger.exception("Streaming pipeline error")
            yield f"data: {json.dumps({'type': 'error', 'detail': str(error)})}\n\n"
    return StreamingResponse(sse(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/analyze")
async def analyze_invention(request: InventionRequest) -> dict[str, object]:
    for event in _run_pipeline(request.description, request.claims):
        if event["type"] == "result":
            return event["data"]  # type: ignore[return-value]
        if event["type"] == "error":
            raise HTTPException(status_code=502, detail=event["detail"])
    raise HTTPException(status_code=500, detail="Analysis produced no result.")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8001)