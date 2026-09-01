"""Ownership scoping and the one place a workflow error becomes an HTTP status.

## Two rules, and both are about not leaking

**Authentication is not authorization** (spec section 3.5). Every run belongs to
exactly one account, and every read, stream, mutation and resume is scoped by
`run_id + authenticated user`. The scoping happens **in the SQL that fetches the
run** -- never fetch-then-compare in Python, where a missed comparison is a data
leak while a missed `WHERE` clause is merely a no-result.

**A run the caller does not own answers `404 RUN_NOT_FOUND`, never `403`**, and it
is deliberately indistinguishable from an unknown run. A `403` would confirm the
id exists, which is all an enumerator needs.

## Why the error mapping lives here and nowhere else

The store raises custom SQLSTATEs (`FN001`-`FN006`) and typed workflow errors.
Spec section 10.2 fixes what each becomes over HTTP. Doing that translation once,
here, is what keeps a handler from inventing its own status for a case it did not
think about -- and what keeps an internal SQLSTATE from ever reaching a client
verbatim.

Every message that crosses this boundary goes through `workflow/redaction.py`
first. A provider quota message (429, 402) is exactly what the user needs and
survives; a stack trace, a URL bearing a key, or a raw provider body does not.
"""

from typing import Annotated, Any, Optional

from fastapi import Depends, Header, HTTPException, Query

from auth import require_user
from workflow import redaction
from workflow.idempotency import IdempotencyConflict, MissingIdempotencyKey
from workflow.review import GateBlocked
from workflow.types import (
    CheckpointUnavailable,
    FencedError,
    IllegalStateError,
    LeaseLifecycleError,
    ReplayConflictError,
    RunNotFoundError,
    StaleResultVersionError,
    TerminalExistsError,
    UnapprovedArtifactError,
    WorkflowError,
)


class ApiError(HTTPException):
    """An HTTP error carrying spec section 10.2's stable `code`.

    The body is always `{"code", "detail"}` with `detail` sanitized. A client
    branches on `code`; `detail` is for a human to read, and is never a place to
    smuggle structure a caller might start parsing.
    """

    def __init__(self, status_code: int, code: str, detail: str, **extra: Any) -> None:
        payload: dict[str, Any] = {"code": code, "detail": detail}
        payload.update(extra)
        super().__init__(status_code=status_code, detail=payload)
        self.code = code


def run_not_found(run_id: str) -> ApiError:
    """The same answer for "no such run" and "not yours". Deliberately."""
    return ApiError(404, "RUN_NOT_FOUND", f"No run {run_id} was found for this account.")


def _detail(error: Exception) -> str:
    """The sanitized sentence a client sees.

    **A `WorkflowError` is trusted; anything else is rebuilt.** That distinction
    is CLAUDE.md's and it is load-bearing in both directions. The workflow errors
    are constructed by this codebase and by SQL functions that interpolate run
    ids, statuses and fence tokens and nothing else, so redacting and passing
    their message through tells the caller what actually went wrong -- "run X is
    RESUMING, not WAITING_FOR_REVIEW" rather than "an unexpected
    IllegalStateError occurred".

    Every other exception is rebuilt from allow-listed facts, because pattern
    matching an unknown exception's prose is never complete and this is the
    boundary where a provider body or a URL bearing a key would reach a browser.
    """
    return redaction.safe_detail(error, trusted=isinstance(error, WorkflowError))


def to_http(error: Exception, *, run_id: str = "") -> ApiError:
    """Spec section 10.2's mapping, in one place.

    `FN006` (a lease-lifecycle violation) is the one that becomes a **500**: a
    deleted lease row, a backwards fence token or a run with no lease row means
    an invariant was broken, not that the caller did something wrong. Reporting
    it as a 4xx would blame the client for a bug in this system.
    """
    if isinstance(error, RunNotFoundError):
        return run_not_found(run_id)
    if isinstance(error, MissingIdempotencyKey):
        return ApiError(422, "MISSING_IDEMPOTENCY_KEY", _detail(error))
    if isinstance(error, IdempotencyConflict):
        return ApiError(409, "IDEMPOTENCY_CONFLICT", _detail(error))
    if isinstance(error, StaleResultVersionError):
        # Ahead of IllegalStateError: it is a subclass-free sibling today, but the
        # ordering states the intent -- a stale decision is its own answer, not a
        # generic wrong-state one, because the caller must re-read and decide again.
        return ApiError(409, "STALE_RESULT_VERSION", _detail(error))
    if isinstance(error, ReplayConflictError):
        return ApiError(409, "IDEMPOTENCY_CONFLICT", _detail(error))
    if isinstance(error, TerminalExistsError):
        return ApiError(409, "RUN_NOT_RESUMABLE", _detail(error))
    if isinstance(error, GateBlocked):
        # The body lists every blocking condition, not just the first: a reviewer
        # who fixes one and is refused again for the next has been told half the
        # truth twice.
        return ApiError(
            409,
            "GATE_BLOCKED",
            redaction.safe_detail(error),
            blocking=getattr(error, "blocking", []),
        )
    if isinstance(error, IllegalStateError):
        return ApiError(409, "WRONG_GATE", _detail(error))
    if isinstance(error, FencedError):
        # The executor retries via its poll; the caller should too.
        return ApiError(503, "CHECKPOINT_UNAVAILABLE", _detail(error))
    if isinstance(error, CheckpointUnavailable):
        return ApiError(503, "CHECKPOINT_UNAVAILABLE", _detail(error))
    if isinstance(error, UnapprovedArtifactError):
        return ApiError(409, "WRONG_GATE", _detail(error))
    if isinstance(error, LeaseLifecycleError):
        return ApiError(500, "LEASE_INVARIANT_VIOLATED", _detail(error))
    if isinstance(error, WorkflowError):
        return ApiError(409, "RUN_NOT_RESUMABLE", _detail(error))
    # Anything else is REBUILT from allow-listed facts rather than passed through.
    # Pattern-matching an unknown exception's text is never complete, and this is
    # the boundary where an unredacted provider body would reach a browser.
    return ApiError(500, "INTERNAL_ERROR", _detail(error))


def last_event_id(
    last_event_id_header: Annotated[Optional[str], Header(alias="Last-Event-ID")] = None,
    last_event_id_query: Annotated[Optional[int], Query(alias="last_event_id")] = None,
) -> Optional[int]:
    """The reconnection cursor, from the header **or** the query parameter.

    Both, because the frontend uses raw `fetch` rather than `EventSource` (spec
    section 2.2): the browser will not send `Last-Event-ID` for it, and a
    contract that only accepted the header would be unreachable from the one
    client this project has. **The header wins** when both are present.

    A malformed header is treated as absent rather than rejected: the honest
    reading of an unparseable cursor is "this client does not know where it was",
    and the correct response to that is a `snapshot`, not a 422.
    """
    if last_event_id_header is not None:
        try:
            return max(int(str(last_event_id_header).strip()), 0)
        except (TypeError, ValueError):
            return None
    if last_event_id_query is not None:
        return max(int(last_event_id_query), 0)
    return None


CurrentUser = Annotated[str, Depends(require_user)]
"""The authenticated account. 401 -- never 403 -- when the token is missing or
expired, because the frontend treats exactly one status as "session gone"."""

LastEventId = Annotated[Optional[int], Depends(last_event_id)]


def require_idempotency_key(body: dict[str, Any]) -> str:
    """The `idempotency_key` every mutating endpoint requires (spec section 8.2).

    A missing key is `422`, not a generated one. Generating a key server-side
    would make every retry a *new* mutation, which is the exact failure the
    mechanism exists to prevent.
    """
    key = str((body or {}).get("idempotency_key") or "").strip()
    if not key:
        raise MissingIdempotencyKey(
            "this endpoint mutates a run and requires an idempotency_key. It is "
            "not generated for you: a server-generated key would make every "
            "retry a new mutation."
        )
    return key


__all__ = [
    "ApiError",
    "CurrentUser",
    "LastEventId",
    "last_event_id",
    "require_idempotency_key",
    "run_not_found",
    "to_http",
]
