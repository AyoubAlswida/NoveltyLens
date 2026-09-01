"""The persisted event log: the **only** module in the backend that writes
`workflow_events`.

`tests/test_event_authority.py` greps every module under `backend/` and fails if
any other one names the table. That is not a style rule. A generic
`append_workflow_event(run_id, type, ...)` would take no owner and no fence
token, so a worker whose lease had expired could still append progress, cell
completions, a pause -- or a terminal `result` -- and it would let a caller write
a terminal event without atomically transitioning the run. Both are
execution-integrity holes.

**Four writers, four authorities**, each enforced by the SQL function it calls
(migration 0008), not by an `if` here:

===========================  ======================  ===============================
Writer                       Authority               May append
===========================  ======================  ===============================
`create_run`                 admission, no lease     the seq-1 `queued` progress event
`append_worker_event`        worker, **fenced**      `progress`, `verification_progress`
`pause_run_for_review`       worker, **fenced**      `review_required` + `paused`, with
                                                     the transition and the release
`append_api_event`           API, ownership-scoped   `resumed`, and nothing else
`finalize_run_success`       worker, fenced+atomic   `result` -- the only writer
`finalize_run_failure`       worker, fenced+atomic   `error` -- the only writer
===========================  ======================  ===============================

Every function here is a thin call into its SQL counterpart. The logic lives in
the database on purpose: the lock order, the fencing revalidation, the sequence
allocation, the dedupe check and the terminal check all have to happen inside one
transaction under one row lock, and a Python function that read the lease and
then wrote would reintroduce exactly the race the lock exists to close.

**Transactions are the caller's.** These take a `psycopg.Connection` and issue
one statement; the caller wraps the group it needs to be atomic in
`conn.transaction()`. The pause and the two finalizers are each internally atomic
as a single SQL function call, so they need no wrapper of their own.
"""

import json
from typing import Any, Optional

import psycopg
from psycopg.types.json import Jsonb

from workflow.types import (
    RunStatus,
    WorkflowEventType,
    WorkflowGate,
    raise_for_sqlstate,
)


def create_run(
    conn: psycopg.Connection,
    *,
    description: str,
    claims: Optional[str],
    cutoff_date: Optional[str],
    date_policy: str,
    text_version_policy: str,
    created_by: str,
) -> str:
    """Admit a run: insert the row, its permanent lease row, and its seq-1
    `queued` event, all in one transaction.

    Policy validation happens **before** this is called and refuses the request
    outright when a decision is missing -- so there is no half-created run to
    clean up, because nothing was written. This function is reached only once
    admission has already passed.

    The lease row is created **unheld at token 0**. It is the run's permanent
    fencing counter and is never deleted while the run exists; the first
    acquisition returns 1.

    Returns:
        The new run's id, which is also its LangGraph `thread_id`.
    """
    with conn.transaction():
        row = conn.execute(
            "SELECT create_run(%s, %s, %s, %s, %s, %s)",
            (description, claims, cutoff_date, date_policy, text_version_policy, created_by),
        ).fetchone()
    return str(row[0])


def append_worker_event(
    conn: psycopg.Connection,
    *,
    run_id: str,
    owner_id: str,
    fence_token: int,
    event_type: WorkflowEventType,
    phase: Optional[str],
    payload: dict[str, Any],
    dedupe_key: str,
) -> int:
    """Append one non-terminal worker event under a live, locked lease.

    Rejects `result` and `error` outright (`FN005`) -- a terminal event exists
    only as half of a finalization that also moves the run to COMPLETED or
    FAILED. Rejects `resumed` too: that describes a human approval the API
    committed, and a worker asserting it would be claiming an approval happened.

    Rejects any run status other than `RUNNING` (`FN005`). That narrowing is
    deliberate: the executor transitions QUEUED/RESUMING to RUNNING *before* it
    executes any node, so progress from either of those states is a bug in the
    executor rather than a state to accommodate.

    Raises:
        FencedError: `FN001` -- wrong owner, wrong token, expired or released
            lease. A fenced worker appends nothing at all.
        IllegalStateError: `FN005` -- illegal event type or run status.
        TerminalExistsError: `FN002` -- the run already ended.
        ReplayConflictError: `FN003` -- the dedupe key exists with a different
            payload.
    """
    try:
        row = conn.execute(
            "SELECT append_worker_event(%s, %s, %s, %s, %s, %s, %s)",
            (
                run_id,
                owner_id,
                fence_token,
                str(event_type),
                phase,
                Jsonb(payload),
                dedupe_key,
            ),
        ).fetchone()
    except psycopg.Error as error:
        raise_for_sqlstate(error)
        raise
    return int(row[0])


def pause_run_for_review(
    conn: psycopg.Connection,
    *,
    run_id: str,
    owner_id: str,
    fence_token: int,
    gate: WorkflowGate,
    review_payload: dict[str, Any],
    pause_payload: dict[str, Any],
    checkpoint_id: str,
) -> int:
    """Park the run at a workflow gate: `review_required`, `paused`, the
    `RUNNING -> WAITING_FOR_REVIEW` transition and the lease release, in **one**
    transaction.

    One call precisely so those five cannot partially apply. Splitting them
    across round trips would create intermediate states a crash could expose, and
    the pause is the one place `paused` is legal *because* it accompanies the
    transition out of RUNNING.

    `checkpoint_id` comes from the committed LangGraph checkpoint, not from the
    attempt, which is what makes this safe to replay: a crash between LangGraph's
    commit (its own transaction, which this does not join) and this one is
    repaired by reconcile calling it again with the same id, and both dedupe keys
    then replay as no-ops.

    Returns:
        The `paused` event's sequence.
    """
    try:
        row = conn.execute(
            "SELECT pause_run_for_review(%s, %s, %s, %s, %s, %s, %s)",
            (
                run_id,
                owner_id,
                fence_token,
                str(gate),
                Jsonb(review_payload),
                Jsonb(pause_payload),
                checkpoint_id,
            ),
        ).fetchone()
    except psycopg.Error as error:
        raise_for_sqlstate(error)
        raise
    return int(row[0])


def append_api_event(
    conn: psycopg.Connection,
    *,
    run_id: str,
    actor_email: str,
    expected_status: RunStatus,
    payload: dict[str, Any],
    dedupe_key: str,
) -> int:
    """Append the API's one and only event type: `resumed`.

    No lease is taken, because a parked run has no holder. The guard is ownership
    (`run_id + actor_email`, resolved in the same query that fetches the run) plus
    `expected_status`, so this cannot be used to bypass fencing: it can only ever
    write `resumed`, and only to a run the caller owns and that is parked exactly
    where the caller thinks it is.

    Called **inside** the approval transaction, alongside the human decision, the
    audit row, the idempotency record and the `WAITING_FOR_REVIEW -> RESUMING`
    transition. All five commit together or none does.

    Raises:
        RunNotFoundError: `FN004` -- unknown run, or another user's. The two are
            deliberately indistinguishable.
        IllegalStateError: `FN005` -- the run is not in `expected_status`.
    """
    try:
        row = conn.execute(
            "SELECT append_api_event(%s, %s, %s, %s, %s, %s)",
            (
                run_id,
                actor_email,
                str(expected_status),
                str(WorkflowEventType.RESUMED),
                Jsonb(payload),
                dedupe_key,
            ),
        ).fetchone()
    except psycopg.Error as error:
        raise_for_sqlstate(error)
        raise
    return int(row[0])


def finalize_run_success(
    conn: psycopg.Connection,
    *,
    run_id: str,
    owner_id: str,
    fence_token: int,
    payload: dict[str, Any],
    dedupe_key: str = "terminal:result",
) -> int:
    """Write the single terminal `result` event **and** move the run to
    `COMPLETED`, atomically.

    The replay check runs **before** the lease and status checks, and that order
    is the whole reason the path works. Checking the lease first would make an
    identical retry impossible: by then the run is already COMPLETED and the
    lease has usually been released, so the retry would fail on status or on
    FN001 before it ever reached the dedupe check -- an "idempotent" path that
    only works if it is never used.

    Returns:
        The terminal event's sequence -- the same one on every true replay.

    Raises:
        ReplayConflictError: `FN003` -- a terminal already exists whose type,
            dedupe key, payload hash or failure code differs.
    """
    try:
        row = conn.execute(
            "SELECT finalize_run_success(%s, %s, %s, %s, %s)",
            (run_id, owner_id, fence_token, Jsonb(payload), dedupe_key),
        ).fetchone()
    except psycopg.Error as error:
        raise_for_sqlstate(error)
        raise
    return int(row[0])


def finalize_run_failure(
    conn: psycopg.Connection,
    *,
    run_id: str,
    owner_id: str,
    fence_token: int,
    payload: dict[str, Any],
    failure_code: str,
    dedupe_key: str = "terminal:error",
) -> int:
    """Write the single terminal `error` event **and** move the run to `FAILED`,
    atomically.

    `payload` must already be sanitized: no traceback, no provider body, no URL
    carrying a key. Nothing downstream redacts it -- this text reaches a browser
    verbatim, which is the point (a 429 or a depleted-credits 402 is exactly what
    the user needs to see) and the danger (SerpAPI authenticates by query
    parameter, and that has leaked a live key into a console once already).

    A run that is already terminal replays instead, on the same terms as
    `finalize_run_success`.
    """
    try:
        row = conn.execute(
            "SELECT finalize_run_failure(%s, %s, %s, %s, %s, %s)",
            (run_id, owner_id, fence_token, Jsonb(payload), failure_code, dedupe_key),
        ).fetchone()
    except psycopg.Error as error:
        raise_for_sqlstate(error)
        raise
    return int(row[0])


# --- the reader side ------------------------------------------------------
#
# Reads carry no authority of their own: ownership is checked by the caller
# (`store.list_events` resolves the run with `actor_email` first). These stay
# here only because this module owns the table.


def read_events(
    conn: psycopg.Connection,
    *,
    run_id: str,
    after_sequence: int = 0,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Read a run's events in sequence order, after `after_sequence`.

    Sequences are monotonic but callers must not rely on contiguity: a
    rolled-back transaction restores the allocator along with everything else,
    and compaction deletes `progress` rows outright.
    """
    rows = conn.execute(
        """
        SELECT sequence, event_type, phase, payload, dedupe_key, is_terminal, created_at
          FROM workflow_events
         WHERE run_id = %s AND sequence > %s
         ORDER BY sequence
         LIMIT %s
        """,
        (run_id, after_sequence, limit),
    ).fetchall()
    return [
        {
            "sequence": row[0],
            "event_type": row[1],
            "phase": row[2],
            "payload": row[3],
            "dedupe_key": row[4],
            "is_terminal": row[5],
            "created_at": row[6],
        }
        for row in rows
    ]


def terminal_event(conn: psycopg.Connection, *, run_id: str) -> Optional[dict[str, Any]]:
    """The run's single terminal event, or None while it is still live."""
    row = conn.execute(
        """
        SELECT sequence, event_type, payload, dedupe_key
          FROM workflow_events
         WHERE run_id = %s AND is_terminal
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "sequence": row[0],
        "event_type": row[1],
        "payload": row[2],
        "dedupe_key": row[3],
    }


def max_sequence(conn: psycopg.Connection, *, run_id: str) -> int:
    """The highest committed sequence for a run, or 0 if it has none.

    This is what a `snapshot` frame's `id:` carries, so a client attaching with
    no `last_event_id` reconnects correctly afterwards.
    """
    row = conn.execute(
        "SELECT COALESCE(MAX(sequence), 0) FROM workflow_events WHERE run_id = %s",
        (run_id,),
    ).fetchone()
    return int(row[0])


def payload_digest(payload: dict[str, Any]) -> str:
    """The SHA-256 the database will compute over this payload.

    Exposed so a caller can derive a content-addressed dedupe key without a
    round trip. It must match `_append_event_locked`'s
    ``encode(digest(payload::text, 'sha256'), 'hex')`` -- **it does not**, because
    PostgreSQL normalizes and reorders JSONB keys while `json.dumps` does not.
    Use it only where the *Python* value is the identity (deriving a dedupe key
    from what the emitter meant to say), never to predict or compare the stored
    `payload_sha256`.
    """
    import hashlib

    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
