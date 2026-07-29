"""FastAPI transport layer for the patent novelty-search pipeline."""

import json
import logging
from typing import Iterator, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

load_dotenv()

from cache import store_enriched_patents
from compare import compare_invention_to_patents
from config import settings
from db import connect
from llm import analyze_all_matches, understand_invention
from models import ComparisonResult
from reports import find_recent_report, list_history, save_report
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
    for result in results:
        patent = result.patent
        score = max(0, min(100, int(result.similarity_score * 100)))
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
    """Run the typed pipeline and emit the existing SSE event contract.

    Opens one Postgres connection for the whole run (ticket P2.2): search
    caching and the completed-run report both need it. A connection failure
    is not caught here, so it propagates out and fails the request with a
    clean error event -- a down database is not a degraded-but-successful
    search. This only applies to this legacy generator; the LangGraph path
    (`graph/pipeline.py`) has no persistence yet -- see CLAUDE.md.
    """
    with connect() as conn:
        cached_report = find_recent_report(conn, description, claims)
        if cached_report is not None:
            # An exact resubmission within REPORT_TTL: skip every LLM/search
            # call and return the same genuine result as last time, not a
            # fresh (and possibly differently-worded, or differently-ranked)
            # one. See reports.py's find_recent_report docstring.
            yield {"type": "progress", "phase": "understand", "message": "Found a recent identical search — reusing its result…"}
            yield {"type": "result", "data": {"title": description[:45] + "...", "date": "Today", **cached_report}}
            return

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
            patents = search_patents(conn, queries, results_per_query=4)
        except PatentSearchError:
            logger.exception("All patent providers failed")
            yield {"type": "error", "detail": "Patent search failed: the patent databases could not be reached. Please check the backend network and try again."}
            return
        yield {"type": "progress", "phase": "search", "message": f"Searching patent databases… {len(queries)}/{len(queries)} · {len(patents)} found", "current": len(queries), "total": len(queries), "found": len(patents)}

        if not patents:
            recommendations = ["No reference targets found. Focus claims on wide utility targets."]
            save_report(
                conn,
                description=description,
                claims=claims,
                concepts=analysis.concepts,
                query_count=len(queries),
                top_score=0,
                verdict="Strong",
                recommendations=recommendations,
                matches=[],
            )
            yield {"type": "result", "data": {"title": description[:45] + "...", "date": "Today", "concepts": analysis.concepts, "patents": [], "top_score": 0, "verdict": "Strong", "query_count": len(queries), "recommendations": recommendations}}
            return

        yield {"type": "progress", "phase": "enrich", "message": f"Fetching full text for {len(patents)} patents…"}
        patents = enrich_with_full_text(patents)
        # Persist the now-enriched full_text: search_patents already cached
        # these patents once, before enrichment ran (full_text was still
        # empty then). Without this second write, a future query-cache hit
        # could never skip re-enrichment, because nothing enriched would be
        # on record to skip it with (see cache.py's store_enriched_patents).
        store_enriched_patents(conn, patents)
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
        yield {"type": "progress", "phase": "analyze", "message": f"Writing detailed analysis for {len(top_patents)} matches…", "current": 0, "total": len(top_patents)}
        analysis_result = analyze_all_matches(description, top_patents)
        for item, rationale in zip(formatted, analysis_result.rationales):
            item["rationale"] = rationale
        recommendations = analysis_result.recommendations
        yield {"type": "progress", "phase": "analyze", "message": f"Writing detailed analysis… {len(top_patents)}/{len(top_patents)}", "current": len(top_patents), "total": len(top_patents)}

        for item in formatted:
            item.pop("_patent", None)

        # Persist before yielding "result": if this raises, the caller's
        # except-Exception wrapper turns it into an "error" event instead --
        # never tell the user the search succeeded and then drop the report.
        save_report(
            conn,
            description=description,
            claims=claims,
            concepts=analysis.concepts,
            query_count=len(queries),
            top_score=top_score,
            verdict=verdict,
            recommendations=recommendations,
            matches=formatted,
        )
        yield {"type": "result", "data": {"title": description[:45] + "...", "date": "Today", "concepts": analysis.concepts, "patents": formatted, "top_score": top_score, "verdict": verdict, "query_count": len(queries), "recommendations": recommendations}}


# Resolved once, at import, on the PIPELINE flag that config.py has already
# validated. LangGraph is the default; PIPELINE=legacy selects the original
# generator and skips importing backend/graph/ entirely, so the fallback stays
# available even if something is wrong with the langgraph install. Resolving
# here rather than per-request means a broken selection fails at startup.
if settings.pipeline == "langgraph":
    from graph.pipeline import run_graph_pipeline as _active_pipeline
else:
    _active_pipeline = _run_pipeline

logger.info("Serving requests with the %r pipeline", settings.pipeline)


@app.post("/api/analyze/stream")
async def analyze_stream(request: InventionRequest) -> StreamingResponse:
    def sse() -> Iterator[str]:
        try:
            for event in _active_pipeline(request.description, request.claims):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as error:
            logger.exception("Streaming pipeline error")
            yield f"data: {json.dumps({'type': 'error', 'detail': str(error)})}\n\n"
    return StreamingResponse(sse(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/analyze")
async def analyze_invention(request: InventionRequest) -> dict[str, object]:
    try:
        for event in _active_pipeline(request.description, request.claims):
            if event["type"] == "result":
                return event["data"]  # type: ignore[return-value]
            if event["type"] == "error":
                raise HTTPException(status_code=502, detail=event["detail"])
    except HTTPException:
        raise
    except Exception as error:
        logger.exception("Analysis pipeline error")
        raise HTTPException(status_code=502, detail=str(error)) from error
    raise HTTPException(status_code=500, detail="Analysis produced no result.")


@app.get("/api/history")
async def get_history() -> list[dict[str, object]]:
    try:
        with connect() as conn:
            return list_history(conn)
    except Exception as error:
        logger.exception("History fetch failed")
        raise HTTPException(status_code=502, detail=str(error)) from error


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8001)