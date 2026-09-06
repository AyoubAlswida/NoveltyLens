"""The only read/write surface for the durable-workflow tables, and the place
run ownership is enforced.

Mirrors how `reports.py` is the only surface for the search tables: no other
module issues SQL against `verification_runs`, `run_leases`, `node_attempts`,
`workflow_events` or the rest. `tests/test_workflow_store.py` asserts it by
source contract.

**Authentication is not authorization.** Every run-scoped function here takes an
``actor_email`` and filters on it **in the same query that fetches the run** --
never fetch-then-compare in Python, where a missed comparison is a data leak
while a missed `WHERE` clause is merely a no-result. Another user's run answers
`RUN_NOT_FOUND`, not a 403: 403 confirms the run exists and lets an attacker
enumerate ids, and the frontend treats 401 alone as "session gone", so 403 would
be a dead status anyway.

**Executor-only readers are named `_system_*`.** They skip ownership because the
executor is a trusted internal identity with no human behind it, and they are
never reachable from an HTTP handler -- `tests/test_workflow_store.py` greps the
HTTP surface for the prefix. That trusted identity can never perform a
user-triggered mutation either: approvals, reviews and waivers are committed by
the API under the authenticated human's email before the executor sees them.

**Event writes are not here.** They live in `workflow/events.py`, which is the
only module that names `workflow_events`; this module re-exports its four
authorized writers so callers have one import site without giving the table a
second writer.
"""

import logging
from typing import Any, Optional

import psycopg
from psycopg.types.json import Jsonb

from workflow.events import (  # re-exported: the four authorized event writers
    append_api_event,
    append_worker_event,
    create_run,
    finalize_run_failure,
    finalize_run_success,
    pause_run_for_review,
    read_events,
    terminal_event,
)
from workflow.types import (
    RunStatus,
    WorkflowGate,
    IllegalStateError,
    RunNotFoundError,
    StaleResultVersionError,
    raise_for_sqlstate,
)

logger = logging.getLogger(__name__)

__all__ = [
    "append_api_event",
    "append_worker_event",
    "create_run",
    "finalize_run_failure",
    "finalize_run_success",
    "pause_run_for_review",
    "get_run",
    "list_runs",
    "list_events",
    "cancel_run",
    "transition_status",
    "begin_resume",
    "_system_get_run",
    "_system_claimable_run_ids",
    "save_cell_result",
    "list_cells",
]

_RUN_COLUMNS = """
    id, description, claims, cutoff_date, date_policy, text_version_policy,
    status, current_gate, thread_id, created_by, next_sequence,
    events_compacted_below, failure_code, failure_detail, created_at, updated_at,
    problem_solved, inventor_novelty_statement
"""


def _run_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "run_id": str(row[0]),
        "description": row[1],
        "claims": row[2],
        "cutoff_date": row[3],
        "date_policy": row[4],
        "text_version_policy": row[5],
        "status": RunStatus(row[6]),
        "current_gate": None if row[7] is None else WorkflowGate(row[7]),
        "thread_id": row[8],
        "created_by": row[9],
        "next_sequence": int(row[10]),
        "events_compacted_below": int(row[11]),
        "failure_code": row[12],
        "failure_detail": row[13],
        "created_at": row[14],
        "updated_at": row[15],
        # NULL on a run admitted before migration 0021. Carried through as `None`
        # rather than coerced to "", because "this run predates the field" and
        # "the inventor left it blank" are different facts and a report that
        # cannot tell them apart would state the second when it means the first.
        "problem_solved": row[16],
        "inventor_novelty_statement": row[17],
    }


# --- ownership-scoped reads ------------------------------------------------


def get_run(
    conn: psycopg.Connection, *, run_id: str, actor_email: str
) -> dict[str, Any]:
    """One run the caller owns.

    Raises:
        RunNotFoundError: for an unknown run **and** for another user's run. The
            two are deliberately indistinguishable.
    """
    row = conn.execute(
        f"SELECT {_RUN_COLUMNS} FROM verification_runs WHERE id = %s AND created_by = %s",
        (run_id, actor_email),
    ).fetchone()
    if row is None:
        raise RunNotFoundError(f"run {run_id} not found")
    return _run_dict(row)


def list_runs(
    conn: psycopg.Connection, *, actor_email: str, limit: int = 50
) -> list[dict[str, Any]]:
    """The caller's runs, newest first. Never anybody else's."""
    rows = conn.execute(
        f"""
        SELECT {_RUN_COLUMNS} FROM verification_runs
         WHERE created_by = %s ORDER BY created_at DESC LIMIT %s
        """,
        (actor_email, limit),
    ).fetchall()
    return [_run_dict(row) for row in rows]


def list_events(
    conn: psycopg.Connection,
    *,
    run_id: str,
    actor_email: str,
    after_sequence: int = 0,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """A run's events, after an ownership check on the run itself.

    The ownership check is a separate statement rather than a join because it has
    to raise `RUN_NOT_FOUND` for a run the caller does not own -- a join would
    return an empty list, which reads to a client as "this run has no events" and
    quietly confirms nothing about whether it exists.
    """
    get_run(conn, run_id=run_id, actor_email=actor_email)
    return read_events(conn, run_id=run_id, after_sequence=after_sequence, limit=limit)


def cancel_run(
    conn: psycopg.Connection, *, run_id: str, actor_email: str
) -> bool:
    """Delete one owned, non-terminal run and fence any worker holding it.

    The run row is locked before its status is inspected. Deleting it cascades
    to its permanent lease row, so the next heartbeat fences a worker that was
    between external calls. An external request already in flight may return,
    but without the run and lease its result cannot be committed.

    Completed and failed runs are retained: refreshing a finished report must
    not erase history. Returns ``True`` only when a live run was removed.
    """
    with conn.transaction():
        row = conn.execute(
            """
            SELECT status
              FROM verification_runs
             WHERE id = %s AND created_by = %s
             FOR UPDATE
            """,
            (run_id, actor_email),
        ).fetchone()
        if row is None:
            raise RunNotFoundError(f"run {run_id} not found")
        if RunStatus(row[0]) in (RunStatus.COMPLETED, RunStatus.FAILED):
            return False
        conn.execute(
            "DELETE FROM verification_runs WHERE id = %s AND created_by = %s",
            (run_id, actor_email),
        )
    return True


def get_terminal_event(
    conn: psycopg.Connection, *, run_id: str, actor_email: str
) -> Optional[dict[str, Any]]:
    """The run's single terminal event, if it has ended. Ownership-scoped."""
    get_run(conn, run_id=run_id, actor_email=actor_email)
    return terminal_event(conn, run_id=run_id)


# --- executor-only reads ---------------------------------------------------
#
# `_system_` marks a function that skips ownership because no human is behind the
# call. None of these may be reachable from an HTTP handler.


def _system_get_run(conn: psycopg.Connection, *, run_id: str) -> dict[str, Any]:
    """The executor's unscoped run read.

    Trusted-internal: the worker has no user identity and must be able to reach
    any run it has legitimately leased. It is never called from a request handler.
    """
    row = conn.execute(
        f"SELECT {_RUN_COLUMNS} FROM verification_runs WHERE id = %s", (run_id,)
    ).fetchone()
    if row is None:
        raise RunNotFoundError(f"run {run_id} not found")
    return _run_dict(row)


def _system_claimable_run_ids(
    conn: psycopg.Connection, *, limit: int = 10
) -> list[str]:
    """Runs the executor may try to claim. Delegates to the lease module's sweep,
    which owns the claimable predicate."""
    from workflow import lease

    return lease.claimable_runs(conn, limit=limit)


# --- guarded transitions ---------------------------------------------------


def transition_status(
    conn: psycopg.Connection,
    *,
    run_id: str,
    owner_id: str,
    fence_token: int,
    expected_from: tuple[RunStatus, ...],
    to_status: RunStatus,
    current_gate: Optional[WorkflowGate] = None,
) -> RunStatus:
    """Move a run's status under a live, **locked** lease.

    Two guards, both necessary. `_lock_run_and_require_lease` takes
    `verification_runs` then `run_leases`, both `FOR UPDATE`, and holds them to
    commit -- so a takeover cannot bump the fence token between the check and the
    write. The `status = ANY(...)` predicate then makes the update itself
    conditional on the caller's belief about where the run is.

    Zero affected rows **raises**. It is never a retry-until-it-works loop: a
    transition that did not apply means the run was not where the caller thought,
    and looping would eventually apply it from a state nobody checked.

    The terminal transitions are deliberately not reachable here -- `COMPLETED`
    and `FAILED` come only from the finalizers, atomically with their event.

    Raises:
        FencedError: `FN001` -- the caller does not hold the lease.
        IllegalStateError: the run was not in `expected_from`, or the pair is not
            in the transition table (the trigger raises `check_violation`, which
            surfaces as the psycopg error rather than an `FN` code).
    """
    if to_status in (RunStatus.COMPLETED, RunStatus.FAILED):
        raise IllegalStateError(
            f"{to_status} is written only by finalize_run_success/failure, atomically "
            "with its terminal event"
        )
    try:
        conn.execute(
            "SELECT _lock_run_and_require_lease(%s, %s, %s)",
            (run_id, owner_id, fence_token),
        )
        row = conn.execute(
            """
            UPDATE verification_runs
               SET status = %s,
                   current_gate = COALESCE(%s, current_gate),
                   updated_at = now()
             WHERE id = %s AND status = ANY(%s)
            RETURNING status
            """,
            (
                str(to_status),
                None if current_gate is None else str(current_gate),
                run_id,
                [str(status) for status in expected_from],
            ),
        ).fetchone()
    except psycopg.Error as error:
        raise_for_sqlstate(error)
        raise
    if row is None:
        raise IllegalStateError(
            f"run {run_id} is not in {', '.join(expected_from)}; it cannot move to "
            f"{to_status}"
        )
    return RunStatus(row[0])


def begin_resume(
    conn: psycopg.Connection,
    *,
    run_id: str,
    actor_email: str,
    gate: WorkflowGate,
    resume_payload: dict[str, Any],
    dedupe_key: str,
) -> int:
    """The API half of a gate approval: append `resumed` and move
    `WAITING_FOR_REVIEW -> RESUMING`, in the caller's transaction.

    **It never touches the graph.** The executor picks the run up on its next
    poll, acquires the lease, and issues `Command(resume=...)`. That separation is
    what makes a duplicate approval harmless and a crash mid-approval
    recoverable: the second approval matches zero rows and the idempotency record
    returns the first one's response.

    No fence token, because no executor holds a parked run. The guards are
    ownership (`run_id + actor_email`, inside `append_api_event`) and
    `status = 'WAITING_FOR_REVIEW'` on the update.

    Callers wrap this together with the human decision, the audit row and the
    idempotency record in one `conn.transaction()`; all of them commit together
    or none does.

    Returns:
        The `resumed` event's sequence.
    """
    sequence = append_api_event(
        conn,
        run_id=run_id,
        actor_email=actor_email,
        expected_status=RunStatus.WAITING_FOR_REVIEW,
        payload=resume_payload,
        dedupe_key=dedupe_key,
    )
    row = conn.execute(
        """
        UPDATE verification_runs
           SET status = 'RESUMING', current_gate = %s, updated_at = now()
         WHERE id = %s AND created_by = %s AND status = 'WAITING_FOR_REVIEW'
        RETURNING status
        """,
        (str(gate), run_id, actor_email),
    ).fetchone()
    if row is None:
        raise IllegalStateError(
            f"run {run_id} is not WAITING_FOR_REVIEW; it cannot begin resuming"
        )
    return sequence


# --- attempt telemetry, ownership-scoped ----------------------------------


def list_attempts(
    conn: psycopg.Connection, *, run_id: str, actor_email: str
) -> list[dict[str, Any]]:
    """Every physical attempt for a run the caller owns.

    Every one, including the fenced: that is what lets a reader see a node was
    executed twice during a recovery while the matrix still shows one result.
    """
    from workflow import attempts

    get_run(conn, run_id=run_id, actor_email=actor_email)
    return attempts.list_attempts(conn, run_id=run_id)


# --------------------------------------------------------------------------
# Verification cells and their machine results (Implementation Gate C)
# --------------------------------------------------------------------------
#
# Two tables, one write, one transaction. A cell with no result would render in
# the matrix as a blank -- indistinguishable from a cell that has not been
# verified yet -- and a result with no cell cannot be reviewed, because a review
# overlay binds to `(cell_id, result_version)`.
#
# **The machine result is stored verbatim.** It goes in as the JSON the verifier
# validated, not a re-serialization assembled here from its fields. A round trip
# through this function must produce the identical document, because that is what
# a reviewer is shown and what a stored verdict is checked against.


def save_cell_result(
    conn: psycopg.Connection,
    *,
    run_id: str,
    owner_id: str,
    fence_token: int,
    cell: dict[str, Any],
) -> None:
    """Persist one matrix cell and its result, fenced and atomic.

    `cell` is the shape `graph/verification_nodes.py` builds. Both rows are
    written inside one transaction: a half-written cell is a blank in the matrix,
    and a blank is exactly what spec §16 decision 9 forbids -- `NOT_EVALUATED`
    and `VALIDATION_FAILURE` are cell states that must always render as
    themselves.

    The three CHECK constraints on `verification_results` are the schema's half
    of the same rule, and they are why the `machine_result`, `failed_check` and
    `not_evaluated_reason` columns below are set from the state rather than
    unconditionally: a `VALIDATION_FAILURE` carrying a machine result, or a
    `VERIFIED` carrying none, cannot be stored at all.

    Raises:
        FencedError: the caller does not hold the lease.
    """
    _require_verification_lease(
        conn, run_id=run_id, owner_id=owner_id, fence_token=fence_token
    )

    state = cell["state"]
    machine_result = cell.get("machine_result")

    with conn.transaction():
        conn.execute(
            """
            INSERT INTO verification_cells
                (cell_id, run_id, feature_id, feature_version, publication_number,
                 decomposition_version, candidate_set_version)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (cell_id) DO NOTHING
            """,
            (
                cell["cell_id"],
                run_id,
                cell["feature_id"],
                cell["feature_version"],
                cell["publication_number"],
                cell["decomposition_version"],
                cell["candidate_set_version"],
            ),
        )
        conn.execute(
            """
            INSERT INTO verification_results
                (cell_id, result_version, state, machine_result, outcome, failed_check,
                 failure_reason, raw_response, not_evaluated_reason, model,
                 prompt_version, passage_chunk_ids, passage_text_hashes, attempts,
                 source_result_version)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (cell_id, result_version) DO NOTHING
            """,
            (
                cell["cell_id"],
                int(cell.get("result_version") or 1),
                state,
                Jsonb(machine_result) if machine_result is not None else None,
                cell.get("outcome") or None,
                cell.get("failed_check") or None if state == "VALIDATION_FAILURE" else None,
                cell.get("reason") or None,
                cell.get("raw_response") or "",
                cell.get("reason") or None if state == "NOT_EVALUATED" else None,
                cell.get("model") or "",
                cell.get("prompt_version") or "",
                list(cell.get("passage_chunk_ids") or ()),
                # **The hash of each cited passage, positionally beside its chunk
                # id.** The column's own comment says it "is what makes a changed
                # source text detectable", and nothing wrote it -- so
                # `stale_passage_citations` compared every citation against a
                # NULL and the staleness rule could never fire correctly.
                list(cell.get("passage_text_hashes") or ()),
                int(cell.get("attempts") or 0),
                # The version this rerun came from, so v2 points back at the v1 a
                # reviewer may already have decided on (§13.4 check 16 binds a
                # review to one cell AND one result version). `None` on a first
                # result, which is the honest value: it came from nothing.
                cell.get("source_result_version"),
            ),
        )


def list_cells(
    conn: psycopg.Connection, *, run_id: str, actor_email: str
) -> list[dict[str, Any]]:
    """A run's matrix, ownership-scoped, latest result per cell.

    Every cell that exists is returned, in every state. Filtering out
    `VALIDATION_FAILURE` or `NOT_EVALUATED` here would turn "we could not answer
    this" into "this was not asked", which is the substitution the whole
    verification vocabulary exists to prevent.
    """
    get_run(conn, run_id=run_id, actor_email=actor_email)
    rows = conn.execute(
        """
        SELECT c.cell_id, c.feature_id, c.feature_version, c.publication_number,
               c.decomposition_version, c.candidate_set_version, c.stale,
               r.result_version, r.state, r.machine_result, r.outcome, r.failed_check,
               r.failure_reason, r.not_evaluated_reason, r.model, r.prompt_version,
               r.passage_chunk_ids, r.attempts, r.source_result_version
          FROM verification_cells c
          JOIN verification_results r ON r.cell_id = c.cell_id
         WHERE c.run_id = %s
           AND r.result_version = (
               SELECT max(result_version) FROM verification_results
                WHERE cell_id = c.cell_id
           )
         ORDER BY c.publication_number, c.feature_id
        """,
        (run_id,),
    ).fetchall()
    return [
        {
            "cell_id": row[0],
            "feature_id": row[1],
            "feature_version": row[2],
            "publication_number": row[3],
            "decomposition_version": row[4],
            "candidate_set_version": row[5],
            "stale": row[6],
            "result_version": row[7],
            "state": row[8],
            "machine_result": row[9],
            "outcome": row[10],
            "failed_check": row[11],
            "failure_reason": row[12],
            "not_evaluated_reason": row[13],
            "model": row[14],
            "prompt_version": row[15],
            "passage_chunk_ids": list(row[16] or ()),
            "attempts": row[17],
            # The version this one superseded, or None on a first result. A
            # reviewer needs to see that v2 replaced a v1 rather than appearing
            # from nowhere.
            "source_result_version": row[18],
        }
        for row in rows
    ]


def _require_verification_lease(
    conn: psycopg.Connection, *, run_id: str, owner_id: str, fence_token: int
) -> None:
    """The mandatory lock order, taken before any verification write.

    Identical to `workflow/evidence_store.py`'s fence, and deliberately not
    shared with it through a third module: both are three lines around one SQL
    function, and a shared helper would be one more place a caller could reach
    the write path without going through the lock.
    """
    try:
        conn.execute(
            "SELECT _lock_run_and_require_lease(%s, %s, %s)",
            (run_id, owner_id, fence_token),
        )
    except psycopg.Error as error:
        raise_for_sqlstate(error)
        raise


# --- human work requests (spec §10: POST /candidates, POST /cells/{id}/rerun) ---
#
# Both record what a person asked for and neither does the work. The API accepts
# the request (202) and the WORKER carries it out under a lease, which is the
# same boundary every gate approval respects: no endpoint fetches a publication,
# no endpoint calls the graph, and no endpoint can make anything eligible.


def request_manual_candidate(
    conn: psycopg.Connection, *, run_id: str, actor_email: str, publication_number: str
) -> dict[str, Any]:
    """Record a manually added candidate, or return the existing request.

    Ownership is resolved **in the same query that fetches the run**, before
    anything is written -- a caller who owns nothing must not be able to add a
    publication to somebody else's run.

    Idempotent on `(run_id, requested_number)` by returning the existing row
    rather than raising: asking twice for the same publication is not a
    conflict, it is the same request. `already_requested` says which happened so
    the endpoint can be honest about it.

    Raises:
        RunNotFoundError: unknown run, or a run this caller does not own.
    """
    get_run(conn, run_id=run_id, actor_email=actor_email)

    number = (publication_number or "").strip()
    if not number:
        raise IllegalStateError(
            "a manual candidate needs a publication number; an empty one names "
            "no document"
        )

    with conn.transaction():
        row = conn.execute(
            """
            INSERT INTO manual_candidates (run_id, requested_number, requested_by)
            VALUES (%s, %s, %s)
            ON CONFLICT (run_id, requested_number) DO NOTHING
            RETURNING id, requested_at
            """,
            (run_id, number, actor_email),
        ).fetchone()
        if row is None:
            existing = conn.execute(
                """
                SELECT id, requested_at, processed_at FROM manual_candidates
                 WHERE run_id = %s AND requested_number = %s
                """,
                (run_id, number),
            ).fetchone()
            return {
                "id": str(existing[0]),
                "publication_number": number,
                "requested_at": existing[1],
                "processed": existing[2] is not None,
                "already_requested": True,
            }
    return {
        "id": str(row[0]),
        "publication_number": number,
        "requested_at": row[1],
        "processed": False,
        "already_requested": False,
    }


def list_manual_candidates(
    conn: psycopg.Connection, *, run_id: str, actor_email: str
) -> list[dict[str, Any]]:
    """Every manual candidate requested for a run the caller owns."""
    get_run(conn, run_id=run_id, actor_email=actor_email)
    rows = conn.execute(
        """
        SELECT requested_number, requested_by, requested_at, processed_at
          FROM manual_candidates WHERE run_id = %s ORDER BY requested_at
        """,
        (run_id,),
    ).fetchall()
    return [
        {
            "publication_number": row[0],
            "requested_by": row[1],
            "requested_at": row[2],
            "processed": row[3] is not None,
        }
        for row in rows
    ]


def request_cell_rerun(
    conn: psycopg.Connection,
    *,
    run_id: str,
    cell_id: str,
    actor_email: str,
    context_requests: list[Any],
    source_result_version: int,
) -> dict[str, Any]:
    """Record a rerun of ONE cell with named additional context.

    **Nothing is edited, replaced or deleted.** A rerun produces a *new*
    `result_version` linked to the one it came from; the prior version stays
    immutable and retrievable (spec §13.1, case U). The returned
    `result_version` is therefore the next one, not the current one.

    `source_result_version` is what the reviewer was looking at. Recorded rather
    than derived when the worker runs it, so a rerun cannot silently attach to a
    result that superseded the reviewed one while the request sat in the queue --
    which is the same reason §13.4 check 16 rejects a stale review.

    Raises:
        RunNotFoundError: unknown run, or one this caller does not own.
        IllegalStateError: the cell does not belong to this run.
    """
    get_run(conn, run_id=run_id, actor_email=actor_email)

    row = conn.execute(
        """
        SELECT max(r.result_version) FROM verification_results r
          JOIN verification_cells c ON c.cell_id = r.cell_id
         WHERE c.run_id = %s AND r.cell_id = %s
        """,
        (run_id, cell_id),
    ).fetchone()
    current = row[0] if row and row[0] is not None else None
    if current is None:
        raise IllegalStateError(
            f"cell {cell_id} has no result to re-run in run {run_id}. A rerun "
            "supersedes a result; it does not create the first one."
        )
    if int(source_result_version) != int(current):
        # The same rule §13.4 check 16 applies to a review: the reviewer approved
        # specific evidence, not a conclusion in perpetuity.
        raise StaleResultVersionError(
            f"cell {cell_id} is at result_version {current}; this rerun was "
            f"composed against {source_result_version}. Re-read the cell and "
            "decide again against what it says now."
        )

    with conn.transaction():
        inserted = conn.execute(
            """
            INSERT INTO cell_rerun_requests
                (run_id, cell_id, source_result_version, context_requests, requested_by)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (run_id, cell_id, source_result_version) DO NOTHING
            RETURNING id
            """,
            (run_id, cell_id, int(source_result_version),
             Jsonb(list(context_requests or [])), actor_email),
        ).fetchone()

    return {
        "cell_id": cell_id,
        "source_result_version": int(source_result_version),
        # The version the rerun WILL produce. The prior one stays retrievable.
        "result_version": int(current) + 1,
        "already_requested": inserted is None,
    }


def list_cell_rerun_requests(
    conn: psycopg.Connection, *, run_id: str, actor_email: str
) -> list[dict[str, Any]]:
    get_run(conn, run_id=run_id, actor_email=actor_email)
    rows = conn.execute(
        """
        SELECT cell_id, source_result_version, context_requests, requested_by,
               requested_at, processed_at
          FROM cell_rerun_requests WHERE run_id = %s ORDER BY requested_at
        """,
        (run_id,),
    ).fetchall()
    return [
        {
            "cell_id": row[0],
            "source_result_version": row[1],
            "context_requests": row[2],
            "requested_by": row[3],
            "requested_at": row[4],
            "processed": row[5] is not None,
        }
        for row in rows
    ]


def claim_manual_candidates(
    conn: psycopg.Connection,
    *,
    run_id: str,
    actor_email: str,
    publication_numbers: list[str],
) -> int:
    """Mark manual-candidate requests as carried out.

    **Claimed, not deleted.** The row is the record that a person asked for this
    publication and that the worker acted on it; deleting it would erase the
    request along with the evidence that it was honoured, and the report's audit
    appendix has to be able to say a candidate was added by hand.

    Idempotent by the `processed_at IS NULL` predicate: a recovery that re-runs
    the node claims nothing a second time, so the publication is not added twice.
    """
    if not publication_numbers:
        return 0
    get_run(conn, run_id=run_id, actor_email=actor_email)
    with conn.transaction():
        rows = conn.execute(
            """
            UPDATE manual_candidates SET processed_at = now()
             WHERE run_id = %s AND requested_number = ANY(%s) AND processed_at IS NULL
            RETURNING id
            """,
            (run_id, list(publication_numbers)),
        ).fetchall()
    return len(rows)


def claim_cell_reruns(
    conn: psycopg.Connection,
    *,
    run_id: str,
    actor_email: str,
    keys: list[tuple[str, int]],
) -> int:
    """Mark cell-rerun requests as carried out. Same claim-don't-delete rule."""
    if not keys:
        return 0
    get_run(conn, run_id=run_id, actor_email=actor_email)
    claimed = 0
    with conn.transaction():
        for cell_id, source_version in keys:
            rows = conn.execute(
                """
                UPDATE cell_rerun_requests SET processed_at = now()
                 WHERE run_id = %s AND cell_id = %s AND source_result_version = %s
                   AND processed_at IS NULL
                RETURNING id
                """,
                (run_id, cell_id, int(source_version)),
            ).fetchall()
            claimed += len(rows)
    return claimed


def current_result_version(
    conn: psycopg.Connection, *, run_id: str, cell_id: str
) -> Optional[int]:
    """The highest `result_version` a cell holds, or `None` if it has no result."""
    row = conn.execute(
        """
        SELECT max(r.result_version) FROM verification_results r
          JOIN verification_cells c ON c.cell_id = r.cell_id
         WHERE c.run_id = %s AND r.cell_id = %s
        """,
        (run_id, cell_id),
    ).fetchone()
    return None if row is None or row[0] is None else int(row[0])


# --- Understanding regeneration requests ------------------------------------


def request_understanding_regeneration(
    conn: psycopg.Connection,
    *,
    run_id: str,
    actor_email: str,
    from_version: int,
    feedback: str = "",
) -> dict[str, Any]:
    """Record a request to re-analyse the invention, or return the existing one.

    Records a **request** and does none of the work, for the same reason
    `request_manual_candidate` does: the API has no lease, no fencing and no
    recovery, and an LLM call on a request thread has none of the guarantees the
    executor provides. The worker carries this out inside the graph.

    Ownership is resolved **in the same query that fetches the run**, before
    anything is written.

    Idempotent on `(run_id, from_version)` by returning the existing row rather
    than raising: asking twice to regenerate the same version is the same
    request, and a client whose connection dropped will ask again.

    Raises:
        RunNotFoundError: unknown run, or a run this caller does not own.
        IllegalStateError: a version that cannot name a proposal.
    """
    get_run(conn, run_id=run_id, actor_email=actor_email)

    if from_version < 1:
        raise IllegalStateError(
            "a regeneration request must name the version it was asked about; "
            "without one there is nothing to say the inventor was looking at the "
            "proposal they are rejecting"
        )

    with conn.transaction():
        row = conn.execute(
            """
            INSERT INTO understanding_regeneration_requests
                (run_id, from_version, feedback, requested_by)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (run_id, from_version) DO NOTHING
            RETURNING id, requested_at
            """,
            (run_id, from_version, feedback or "", actor_email),
        ).fetchone()
        if row is None:
            existing = conn.execute(
                """
                SELECT id, requested_at, processed_at, feedback
                  FROM understanding_regeneration_requests
                 WHERE run_id = %s AND from_version = %s
                """,
                (run_id, from_version),
            ).fetchone()
            return {
                "id": str(existing[0]),
                "from_version": from_version,
                "feedback": existing[3] or "",
                "requested_at": existing[1],
                "processed": existing[2] is not None,
                "already_requested": True,
            }
    return {
        "id": str(row[0]),
        "from_version": from_version,
        "feedback": feedback or "",
        "requested_at": row[1],
        "processed": False,
        "already_requested": False,
    }


def list_understanding_regenerations(
    conn: psycopg.Connection, *, run_id: str, actor_email: str
) -> list[dict[str, Any]]:
    """Every regeneration requested for a run the caller owns, oldest first."""
    get_run(conn, run_id=run_id, actor_email=actor_email)
    rows = conn.execute(
        """
        SELECT from_version, feedback, requested_by, requested_at, processed_at
          FROM understanding_regeneration_requests
         WHERE run_id = %s ORDER BY requested_at
        """,
        (run_id,),
    ).fetchall()
    return [
        {
            "from_version": int(row[0]),
            "feedback": row[1] or "",
            "requested_by": str(row[2]),
            "requested_at": row[3],
            "processed": row[4] is not None,
        }
        for row in rows
    ]


def _system_pending_understanding_regeneration(
    conn: psycopg.Connection, *, run_id: str
) -> Optional[dict[str, Any]]:
    """The oldest unprocessed regeneration request, without an ownership check.

    `_system_*` because no human is behind the call: the executor reads it while
    deciding whether to route the graph back for a re-analysis, and there is no
    actor to scope to. `tests/test_workflow_store.py` asserts at the AST level
    that nothing in `api/` reaches a `_system_*` function.
    """
    row = conn.execute(
        """
        SELECT from_version, feedback
          FROM understanding_regeneration_requests
         WHERE run_id = %s AND processed_at IS NULL
         ORDER BY requested_at
         LIMIT 1
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    return {"from_version": int(row[0]), "feedback": row[1] or ""}


def claim_understanding_regeneration(
    conn: psycopg.Connection, *, run_id: str, from_version: int
) -> bool:
    """Stamp one regeneration request as carried out. True if this call claimed it.

    Idempotent by the `processed_at IS NULL` predicate: a recovery that re-runs
    the claiming node finds nothing left to claim and regenerates nothing, which
    is what keeps at-least-once execution from producing two re-analyses for one
    request.

    The row is **kept**. It is the evidence that a person asked and the worker
    acted, and the report's audit appendix needs it.
    """
    row = conn.execute(
        """
        UPDATE understanding_regeneration_requests SET processed_at = now()
         WHERE run_id = %s AND from_version = %s AND processed_at IS NULL
        RETURNING id
        """,
        (run_id, from_version),
    ).fetchone()
    return row is not None


def set_submission_context(
    conn: psycopg.Connection,
    *,
    run_id: str,
    problem_solved: str,
    inventor_novelty_statement: str,
) -> None:
    """Record the optional Stage 1 inputs on a run just admitted.

    Written as a second statement rather than as arguments to `create_run`
    because `create_run` is a **SQL function**, and widening its signature means
    dropping and recreating it -- which puts a rollback burden on a purely
    additive change. The caller runs both inside one transaction, so a run never
    exists without the context that was submitted with it.

    `description` is not touched here and is never touched anywhere: it is the
    source input, and the approved summary is stored beside it rather than over
    it.

    Empty strings are stored as empty strings, not NULL. NULL means "this run
    predates the column" and an empty string means "the inventor left it blank",
    and a reader must be able to tell those apart.
    """
    conn.execute(
        """
        UPDATE verification_runs
           SET problem_solved = %s, inventor_novelty_statement = %s
         WHERE id = %s
        """,
        (problem_solved or "", inventor_novelty_statement or "", run_id),
    )


# --- the search strategy: regeneration as a durable work request -----------


def request_strategy_regeneration(
    conn: psycopg.Connection,
    *,
    run_id: str,
    actor_email: str,
    from_version: int,
    feedback: str = "",
) -> dict[str, Any]:
    """Record a request for fresh search queries, or return the existing one.

    Records a **request** and does none of the work, for the same reason
    `request_understanding_regeneration` does: the API has no lease, no fencing
    and no recovery, and an LLM call on a request thread has none of the
    guarantees the executor provides. The worker carries this out inside the
    graph, under the run lease.

    Ownership is resolved **in the same query that fetches the run**, before
    anything is written.

    Idempotent on `(run_id, from_version)` by returning the existing row rather
    than raising: asking twice to regenerate the same version is the same request,
    and a client whose connection dropped will ask again.

    Raises:
        RunNotFoundError: unknown run, or a run this caller does not own.
        IllegalStateError: a version that cannot name a proposal.
    """
    get_run(conn, run_id=run_id, actor_email=actor_email)

    if from_version < 1:
        raise IllegalStateError(
            "a regeneration request must name the strategy version it was asked "
            "about; without one there is nothing to say the reviewer was looking "
            "at the queries they are rejecting"
        )

    with conn.transaction():
        row = conn.execute(
            """
            INSERT INTO strategy_regeneration_requests
                (run_id, from_version, feedback, requested_by)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (run_id, from_version) DO NOTHING
            RETURNING id, requested_at
            """,
            (run_id, from_version, feedback or "", actor_email),
        ).fetchone()
        if row is None:
            existing = conn.execute(
                """
                SELECT id, requested_at, processed_at, feedback
                  FROM strategy_regeneration_requests
                 WHERE run_id = %s AND from_version = %s
                """,
                (run_id, from_version),
            ).fetchone()
            return {
                "id": str(existing[0]),
                "from_version": from_version,
                "feedback": existing[3] or "",
                "requested_at": existing[1],
                "processed": existing[2] is not None,
                "already_requested": True,
            }
    return {
        "id": str(row[0]),
        "from_version": from_version,
        "feedback": feedback or "",
        "requested_at": row[1],
        "processed": False,
        "already_requested": False,
    }


def list_strategy_regenerations(
    conn: psycopg.Connection, *, run_id: str, actor_email: str
) -> list[dict[str, Any]]:
    """Every strategy regeneration requested for a run the caller owns, oldest
    first."""
    get_run(conn, run_id=run_id, actor_email=actor_email)
    rows = conn.execute(
        """
        SELECT from_version, feedback, requested_by, requested_at, processed_at
          FROM strategy_regeneration_requests
         WHERE run_id = %s ORDER BY requested_at
        """,
        (run_id,),
    ).fetchall()
    return [
        {
            "from_version": int(row[0]),
            "feedback": row[1] or "",
            "requested_by": str(row[2]),
            "requested_at": row[3],
            "processed": row[4] is not None,
        }
        for row in rows
    ]


def _system_pending_strategy_regeneration(
    conn: psycopg.Connection, *, run_id: str
) -> Optional[dict[str, Any]]:
    """The oldest unprocessed strategy regeneration request, without an ownership
    check.

    `_system_*` because no human is behind the call: the executor reads it while
    deciding whether to route the graph back for fresh queries, under the run
    lease, and there is no actor to scope to. `tests/test_workflow_store.py`
    asserts at the AST level that nothing in `api/` reaches a `_system_*`
    function.
    """
    row = conn.execute(
        """
        SELECT from_version, feedback
          FROM strategy_regeneration_requests
         WHERE run_id = %s AND processed_at IS NULL
         ORDER BY requested_at
         LIMIT 1
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    return {"from_version": int(row[0]), "feedback": row[1] or ""}


def claim_strategy_regeneration(
    conn: psycopg.Connection, *, run_id: str, from_version: int
) -> bool:
    """Stamp one strategy regeneration as carried out. True if this call claimed it.

    Idempotent by the `processed_at IS NULL` predicate: a recovery that re-runs
    the claiming node finds nothing left to claim and regenerates nothing, which
    is what keeps at-least-once execution from producing two strategies for one
    request.

    The row is **kept**. It is the evidence that a person asked and the worker
    acted, and the report's audit appendix needs it.
    """
    row = conn.execute(
        """
        UPDATE strategy_regeneration_requests SET processed_at = now()
         WHERE run_id = %s AND from_version = %s AND processed_at IS NULL
        RETURNING id
        """,
        (run_id, from_version),
    ).fetchone()
    return row is not None
