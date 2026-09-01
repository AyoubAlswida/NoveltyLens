"""FastAPI transport layer for the patent novelty-search pipeline.

Pure transport: it owns the HTTP surface, the SSE framing, and the auth
boundary, and nothing else. All pipeline behavior lives in ``backend/graph/``,
all persistence in ``backend/reports.py``, all credential handling in
``backend/auth.py``.

Every endpoint except ``POST /api/auth/login`` requires a session token. The
pipeline endpoints are gated because a single run spends real, small, metered
SerpAPI and HuggingFace quota; ``GET /api/history`` because the stored reports
are other people's invention descriptions.

**This process can also HOST the durable executor, but it never becomes one.**
With ``WORKFLOW_EXECUTOR=inprocess`` the lifespan below starts
``backend/workflow/executor.py``'s loop on a daemon thread for local development
-- the same loop, the same lease, the same recovery, hosted differently. With the
default ``worker`` it starts nothing, and production runs ``python -m
workflow.worker`` as a separate workload. No endpoint here executes a graph on
behalf of the executor, and ``BackgroundTasks`` is not used for it: that dies with
the request, holds no lease, and recovers from nothing.

Nothing about ``POST /api/analyze`` or ``POST /api/analyze/stream`` changes either
way. They still run inline, driven by the HTTP request, with no run row, no lease
and no event log.
"""

import json
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Iterator, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

load_dotenv(override=True)  # see config.py's load_dotenv(override=True) for why

import db
from auth import authenticate, issue_token, require_user
from config import ConfigError, settings
from graph.pipeline import run_graph_pipeline
from reports import clear_history, list_history
from workflow.executor import InprocessExecutor, run_inprocess
from workflow.redaction import safe_detail, safe_log
from workflow.types import ExecutorUnavailable

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

VALID_EXECUTOR_TOPOLOGIES = ("worker", "inprocess")
"""Where the executor loop runs -- not which implementation runs. There is one
executor; this chooses its host process."""

_inprocess_executor: Optional[InprocessExecutor] = None
"""The running in-process host, or None. Module-level rather than app state so
the shutdown handler can find it whatever created it, and so a second startup can
see that one is already running."""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start and stop the in-process executor around the API's lifetime.

    **Startup fails loudly on a bad configuration.** An unknown
    ``WORKFLOW_EXECUTOR`` value, an invalid lease pair, or ``inprocess`` with no
    ``WORKFLOW_GRAPH_ADAPTER`` all stop the API from serving rather than being
    absorbed. That last one has no mock fallback on purpose: quietly substituting
    the deterministic adapter would let a deployment believe it was running real
    verification while it fabricated everything.

    With ``worker`` -- the default, and production -- this starts nothing at all.
    """
    global _inprocess_executor

    if settings.workflow_executor not in VALID_EXECUTOR_TOPOLOGIES:
        raise ConfigError(
            f"WORKFLOW_EXECUTOR must be one of {', '.join(VALID_EXECUTOR_TOPOLOGIES)}, "
            f"got {settings.workflow_executor!r}. It selects the executor's host "
            "process, not its implementation."
        )

    if settings.workflow_executor == "inprocess":
        if _inprocess_executor is not None and _inprocess_executor.running:
            raise ExecutorUnavailable(
                "an in-process executor is already running in this process"
            )
        _inprocess_executor = run_inprocess()
        logger.info("WORKFLOW_EXECUTOR=inprocess: executor started on a daemon thread")
    else:
        logger.info(
            "WORKFLOW_EXECUTOR=%s: no in-process executor. Run `python -m "
            "workflow.worker` as a separate workload to advance durable runs.",
            settings.workflow_executor,
        )

    try:
        yield
    finally:
        if _inprocess_executor is not None:
            _inprocess_executor.stop()
            _inprocess_executor = None
            logger.info("in-process executor stopped")


app = FastAPI(lifespan=lifespan)

# Verified runs (Implementation Gate E). Mounted as its own router so the
# existing endpoints below are untouched: `POST /api/analyze`,
# `POST /api/analyze/stream`, `GET`/`DELETE /api/history` and the auth endpoints
# keep their exact behaviour, are not migrated onto the executor, and their SSE
# frames still carry no `id:` line (spec section 3.2).
from api.deps import ApiError  # noqa: E402  - after `app` exists
from api.runs import router as runs_router  # noqa: E402

app.include_router(runs_router)


@app.exception_handler(ApiError)
async def _api_error(_request, error: ApiError):
    """Render spec section 10.2's `{code, detail}` body at the TOP level.

    FastAPI nests whatever an `HTTPException` carries under a `detail` key, which
    would make the contract `{"detail": {"code": ..., "detail": ...}}` -- a shape
    no client was written against and one that hides `code` exactly where a
    client branches on it. `ApiError` already builds the correct object; this
    handler stops it being wrapped a second time.

    Extra structured fields an error carries (`missing` for
    POLICY_NOT_APPROVED, `blocking` for GATE_BLOCKED) travel at the top level
    beside them, and every one of them is safe: configuration names and blocking
    conditions, never a value, a provider body or a traceback.
    """
    return JSONResponse(status_code=error.status_code, content=error.detail)
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
        # Redacted: a connection failure embeds the DSN it tried.
        logger.warning("readiness probe failed: %s", safe_log(error))
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
            # Sanitized, not `str(error)`. This detail is written straight into
            # the SSE stream and rendered verbatim by the frontend's ErrorScreen,
            # so it is a browser-visible surface -- the same one CLAUDE.md records
            # a live SerpAPI key reaching once through an HTTPError message. The
            # actionable half survives: `safe_detail` keeps the provider and the
            # HTTP status, which is what tells a user which quota ran out.
            logger.warning("Streaming pipeline error: %s", safe_log(error))
            yield f"data: {json.dumps({'type': 'error', 'detail': safe_detail(error)})}\n\n"
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
        # Same surface, same reasoning as the streaming path above: this detail
        # is serialized into the HTTP response body and shown to the caller.
        logger.warning("Analysis pipeline error: %s", safe_log(error))
        raise HTTPException(status_code=502, detail=safe_detail(error)) from error
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

    from config import ConfigError, settings

    # Host/port come from settings so a container can bind 0.0.0.0 without a
    # code change; the defaults are still 127.0.0.1:8001, so running this file
    # on a laptop behaves exactly as it always has. See config.server_host.
    uvicorn.run(app, host=settings.server_host, port=settings.server_port)
