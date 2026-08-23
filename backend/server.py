"""FastAPI transport layer for the patent novelty-search pipeline.

Pure transport: it owns the HTTP surface, the SSE framing, and the auth
boundary, and nothing else. All pipeline behavior lives in ``backend/graph/``,
all persistence in ``backend/reports.py``, all credential handling in
``backend/auth.py``.

Every endpoint except ``POST /api/auth/login`` requires a session token. The
pipeline endpoints are gated because a single run spends real, small, metered
SerpAPI and HuggingFace quota; ``GET /api/history`` because the stored reports
are other people's invention descriptions.
"""

import json
import logging
from typing import Iterator, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

load_dotenv(override=True)  # see config.py's load_dotenv(override=True) for why

import db
from auth import authenticate, issue_token, require_user
from graph.pipeline import run_graph_pipeline
from reports import clear_history, list_history

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

HISTORY_LIMIT = 50
"""How many past runs GET /api/history returns. The response inlines every
match of every run, so this is the one thing keeping the payload bounded."""


class InventionRequest(BaseModel):
    description: str
    claims: Optional[str] = None


class LoginRequest(BaseModel):
    email: str
    password: str


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe: is this process up. Deliberately unauthenticated and
    deliberately touching nothing else - a probe that needed a token could not
    be called by the platform, and one that hit the database would restart a
    healthy pod every time Postgres blinked.

    This is the one endpoint outside the auth gate besides login, and it is safe
    there because it reveals nothing: no version, no config, no counts."""
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> dict[str, str]:
    """Readiness probe: can this pod serve a real request yet.

    Unlike ``/healthz`` this *does* check Postgres, because a backend that
    cannot reach its database cannot serve a single endpoint - every route
    either persists a run or reads one. Failing readiness pulls the pod out of
    the Service's endpoints until the database is back, which is the correct
    response; failing *liveness* would kill and restart it, which would not fix
    a database outage and would lose in-flight SSE streams for no reason.

    Raises:
        HTTPException: 503 when the database is unreachable, which is what marks
            the pod not-ready.
    """
    try:
        with db.connect() as conn:
            conn.execute("SELECT 1")
    except Exception as error:
        logger.warning("readiness probe failed: %s", error)
        raise HTTPException(status_code=503, detail="database unavailable")
    return {"status": "ready"}


@app.post("/api/auth/login")
async def login(request: LoginRequest) -> dict[str, str]:
    """Exchange credentials for a session token.

    There is no signup counterpart, by design: accounts are rows inserted by
    migration or by hand (see ``migrations/0004.create-users.sql``).

    A wrong password and an unknown address produce the identical 401, so the
    response cannot be used to enumerate which addresses have accounts.
    """
    with db.connect() as conn:
        email = authenticate(conn, request.email, request.password)
    if email is None:
        raise HTTPException(status_code=401, detail="Incorrect email or password.")
    return {"token": issue_token(email), "email": email}


@app.get("/api/auth/me")
async def whoami(email: str = Depends(require_user)) -> dict[str, str]:
    """Confirm a stored token is still valid.

    The frontend calls this on load to decide whether to restore the session or
    show the login screen, rather than trusting a token in localStorage that may
    have expired while the tab was closed.
    """
    return {"email": email}


@app.post("/api/analyze/stream")
async def analyze_stream(
    request: InventionRequest, email: str = Depends(require_user)
) -> StreamingResponse:
    def sse() -> Iterator[str]:
        try:
            for event in run_graph_pipeline(request.description, request.claims):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as error:
            logger.exception("Streaming pipeline error")
            yield f"data: {json.dumps({'type': 'error', 'detail': str(error)})}\n\n"
    return StreamingResponse(sse(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/analyze")
async def analyze_invention(
    request: InventionRequest, email: str = Depends(require_user)
) -> dict[str, object]:
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


@app.get("/api/history")
async def history(email: str = Depends(require_user)) -> list[dict[str, object]]:
    """The most recent stored runs, newest first.

    Not caught and softened into an empty list: a database that cannot be read
    is not the same answer as "you have not run a search yet," and the History
    screen must not show the second when the first is true.
    """
    with db.connect() as conn:
        return list_history(conn, limit=HISTORY_LIMIT)


@app.delete("/api/history")
async def delete_history(email: str = Depends(require_user)) -> dict[str, int]:
    """Delete every stored run. Irreversible, and there is no undo anywhere.

    Deliberately not scoped to the caller: there is one seeded account and no
    per-user ownership column, so this clears the whole table. **If accounts
    ever become per-user, this endpoint has to grow a WHERE clause before that
    ships** — otherwise one user's "Clear all" wipes everybody's history.
    """
    with db.connect() as conn:
        deleted = clear_history(conn)
    logger.info("History cleared by %s: %d run(s) deleted", email, deleted)
    return {"deleted": deleted}


if __name__ == "__main__":
    import uvicorn

    from config import settings

    # Host/port come from settings so a container can bind 0.0.0.0 without a
    # code change; the defaults are still 127.0.0.1:8001, so running this file
    # on a laptop behaves exactly as it always has. See config.server_host.
    uvicorn.run(app, host=settings.server_host, port=settings.server_port)
