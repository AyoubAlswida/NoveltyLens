"""FastAPI transport layer for the patent novelty-search pipeline.

Pure transport: it owns the HTTP surface and the SSE framing, and nothing else.
All pipeline behavior lives in ``backend/graph/``. There is one pipeline - the
legacy generator and its ``PIPELINE`` flag were retired along with Postgres.
"""

import json
import logging
from typing import Iterator, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

load_dotenv(override=True)  # see config.py's load_dotenv(override=True) for why

from graph.pipeline import run_graph_pipeline

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


class InventionRequest(BaseModel):
    description: str
    claims: Optional[str] = None


@app.post("/api/analyze/stream")
async def analyze_stream(request: InventionRequest) -> StreamingResponse:
    def sse() -> Iterator[str]:
        try:
            for event in run_graph_pipeline(request.description, request.claims):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as error:
            logger.exception("Streaming pipeline error")
            yield f"data: {json.dumps({'type': 'error', 'detail': str(error)})}\n\n"
    return StreamingResponse(sse(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/analyze")
async def analyze_invention(request: InventionRequest) -> dict[str, object]:
    try:
        for event in run_graph_pipeline(request.description, request.claims):
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8001)
