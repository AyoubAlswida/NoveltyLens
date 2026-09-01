"""Execution-attempt telemetry: `attempt_id`, `execution_key`, and the record of
every physical attempt including the ones that were fenced.

**Two identifiers, and confusing them is the bug this module exists to prevent.**

``attempt_id``
    One **physical** execution attempt. Minted when the executor begins a node
    under a specific fence token. A crash recovery mints a new one.

``execution_key``
    The identity of the **logical** invocation. Deterministic and stable across
    retries of the same unit of work, and *different* when the work genuinely
    differs.

Scoping "at most one committed result" by node alone would forbid every
legitimate repeat: a cell rerun after a context request, a second
`result_version`, a re-verification against changed passage text, a revisit after
a new decomposition or candidate-set version, an ordinary LangGraph loop reaching
the same node again for different work. Those are not retries. So the partial
unique index is `(run_id, node, execution_key) WHERE outcome = 'COMMITTED'`, and
the key is what lets it mean "one committed result per unit of work" rather than
"one per node, forever".

**The executor derives the key from trusted graph state.** It is never accepted
from an HTTP client -- :func:`reject_client_execution_key` is the explicit refusal
-- because a caller supplying one could collide two distinct executions or force a
duplicate commit. And it carries **no secrets and no raw user text**: identifiers
and versions only. It is logged, it reaches operators, and an invention
description does not belong in a telemetry column.

**Every mutation here is fenced.** Opening, failing, committing and closing an
attempt all go through a SQL function that takes the mandatory lock order
(`verification_runs` then `run_leases`, both `FOR UPDATE`, held to commit) and
proves the caller holds the current lease. That is not belt-and-braces on top of
an application check -- it is the only place the check can live, because a lease
can be superseded between a Python `if` and the write that follows it.

The rule being enforced is that **a fenced worker never writes telemetry using
the authority it just lost**. `FAILED` and `FENCED` are different facts, and only
the *new* holder, under its own valid lease, may record that a previous attempt
was superseded (:func:`close_superseded_attempts`).

**Never infer commitment from a provider call returning.** A response arriving
proves an external call happened; it proves nothing about whether this worker
still held the lease or whether anything was written. That is what
:func:`committed_execution_keys` is for -- reconciliation asks the database which
logical executions already committed rather than guessing.
"""

import logging
from typing import Any, Optional

import psycopg

from workflow.types import AttemptOutcome, raise_for_sqlstate

logger = logging.getLogger(__name__)


# --- execution key derivation ---------------------------------------------


def execution_key_for_node(checkpoint_task_id: str) -> str:
    """The general case: a LangGraph node's **stable checkpoint task id**.

    Verified against LangGraph 1.2.7: the id a task carries in the `tasks` stream
    is identical when that task re-runs after a crash and when it re-runs after an
    `interrupt()` is resumed, and differs for a later visit that is genuinely new
    work. That is exactly the stability the index needs.
    """
    if not checkpoint_task_id:
        raise ValueError("a node execution key needs the checkpoint task id")
    return checkpoint_task_id


def execution_key_for_cell(cell_id: str, result_version: int) -> str:
    """One verification cell at one result version.

    A rerun at `result_version = 2` is a **new logical execution** and commits
    independently of version 1 -- which is why the version is in the key.
    """
    return f"cell:{cell_id}:result-version:{result_version}"


def execution_key_for_decomposition(decomposition_version: int) -> str:
    return f"decomposition:{decomposition_version}"


def execution_key_for_candidate_set(candidate_set_version: int) -> str:
    return f"candidate-set:{candidate_set_version}"


def execution_key_for_report(report_version: int) -> str:
    return f"report:{report_version}"


def execution_key_for_gate(gate: str, approved_artifact_version: int) -> str:
    return f"gate:{gate}:version:{approved_artifact_version}"


def reject_client_execution_key(supplied: Any) -> None:
    """Refuse an execution key that arrived from an HTTP client.

    Called at every boundary where a request body could carry one. The key is
    derived from trusted graph state and nowhere else; accepting a client's would
    let a caller collide two distinct executions or force a duplicate commit.
    """
    if supplied not in (None, "", {}, []):
        raise ValueError(
            "execution_key is derived from graph state and is never accepted from a "
            "client request"
        )


# --- the attempt record ---------------------------------------------------


def begin_attempt(
    conn: psycopg.Connection,
    *,
    run_id: str,
    node: str,
    execution_key: str,
    fence_token: int,
    owner_id: str,
) -> str:
    """Record that a physical attempt at one logical execution has started -- a
    **fenced** write.

    Opened *before* the work, so an attempt that crashes leaves a row rather than
    vanishing. It starts `RUNNING`; only `commit_attempt` may move it to
    `COMMITTED`.

    Fenced because a fenced worker must never write telemetry using the authority
    it just lost -- not even a row that merely says it started. The row is
    attributed to the lease the database verified, not to whatever the caller
    claims.

    Raises:
        FencedError: `FN001` -- stale, expired, released, or wrong owner.
    """
    try:
        row = conn.execute(
            "SELECT begin_node_attempt(%s, %s, %s, %s, %s)",
            (run_id, owner_id, fence_token, node, execution_key),
        ).fetchone()
    except psycopg.Error as error:
        raise_for_sqlstate(error)
        raise
    return str(row[0])


def commit_attempt(
    conn: psycopg.Connection,
    *,
    run_id: str,
    owner_id: str,
    fence_token: int,
    attempt_id: str,
    provider_calls: int = 0,
    llm_calls: int = 0,
    detail: str = "",
) -> None:
    """Mark one attempt `COMMITTED` -- a **fenced** write.

    Takes the mandatory lock order through `_lock_run_and_require_lease`, so a
    takeover cannot slip in between the check and the update. Call it in the same
    transaction as whatever result or event it describes, which is possible for
    everything in this schema because it is all one database. The LangGraph
    checkpoint commits separately (another library's transaction), which is why
    reconciliation asks :func:`committed_execution_keys` rather than assuming.

    Raises:
        FencedError: `FN001` -- fenced, or the attempt is no longer `RUNNING`. A
            `FENCED` or `FAILED` attempt can never become `COMMITTED`.
    """
    try:
        conn.execute(
            "SELECT commit_node_attempt(%s, %s, %s, %s, %s, %s, %s)",
            (run_id, owner_id, fence_token, attempt_id, provider_calls, llm_calls, detail),
        )
    except psycopg.Error as error:
        raise_for_sqlstate(error)
        raise


def fail_attempt(
    conn: psycopg.Connection,
    *,
    run_id: str,
    owner_id: str,
    fence_token: int,
    attempt_id: str,
    detail: str = "",
) -> None:
    """Close **the caller's own** attempt that raised -- a fenced write.

    An earlier revision left this unfenced, reasoning that a worker recording its
    own failure claims no authority over the run. That reasoning was wrong, and
    the consequence was the loss of evidence this table exists to preserve:
    `FAILED` and `FENCED` are **different facts**, and a stale worker marking its
    own row `FAILED` overwrites the `FENCED` that reconciliation would otherwise
    apply. The duplicate external read then disappears from the record, and the
    row reads as a node that errored when in truth it was superseded.

    So a stale worker gets `FN001` here and its attempt stays `RUNNING`, which is
    exactly the state :func:`close_superseded_attempts` is looking for.

    `detail` must already be sanitized; it is never a traceback.

    Raises:
        FencedError: `FN001` -- stale, expired, released, wrong owner, or an
            attempt that is not this holder's own and still `RUNNING`.
    """
    try:
        conn.execute(
            "SELECT fail_node_attempt(%s, %s, %s, %s, %s)",
            (run_id, owner_id, fence_token, attempt_id, detail),
        )
    except psycopg.Error as error:
        raise_for_sqlstate(error)
        raise


def close_superseded_attempts(
    conn: psycopg.Connection, *, run_id: str, owner_id: str, fence_token: int
) -> int:
    """Reconciliation: close attempts left `RUNNING` under a **superseded** token.

    This is the only place an attempt is ever marked `FENCED`, and it is a fenced
    write itself: the caller must hold the current live lease. Without that the
    function was a mass-close anyone could call against any run, which is a strange
    hole to leave in the one operation whose entire purpose is recording who lost
    authority.

    An attempt is superseded when its `fence_token` is strictly below the caller's,
    so the caller's own in-flight attempts are never disturbed and this is safe to
    call at the start of every acquisition.

    Returns:
        How many attempts were closed. A non-zero count is the operator-visible
        record that a provider or LLM call was made twice for one logical
        execution -- two attempts, one committed result. That is the
        at-least-once reality, made visible rather than hidden.

    Raises:
        FencedError: `FN001` -- the caller does not hold the current lease.
    """
    try:
        row = conn.execute(
            "SELECT close_superseded_node_attempts(%s, %s, %s)",
            (run_id, owner_id, fence_token),
        ).fetchone()
    except psycopg.Error as error:
        raise_for_sqlstate(error)
        raise
    closed = int(row[0])
    if closed:
        logger.info("run %s: closed %d superseded attempt(s) as FENCED", run_id, closed)
    return closed


def committed_execution_keys(
    conn: psycopg.Connection, *, run_id: str, node: Optional[str] = None
) -> set[str]:
    """Which logical executions already committed for this run.

    Reconciliation consults this **before repeating anything**: a response having
    arrived proves an external call happened and nothing about whether it was
    committed, so the database is the only honest source for that question.
    """
    if node is None:
        rows = conn.execute(
            "SELECT execution_key FROM node_attempts WHERE run_id = %s AND outcome = 'COMMITTED'",
            (run_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT execution_key FROM node_attempts
             WHERE run_id = %s AND node = %s AND outcome = 'COMMITTED'
            """,
            (run_id, node),
        ).fetchall()
    return {str(row[0]) for row in rows}


def list_attempts(conn: psycopg.Connection, *, run_id: str) -> list[dict[str, Any]]:
    """Every physical attempt for a run, oldest first.

    **Every** attempt: telemetry retains the fenced and failed ones alongside the
    committed one, because that is what lets an operator see a node was executed
    twice during a recovery. The matrix still shows one result.
    """
    rows = conn.execute(
        """
        SELECT attempt_id, node, execution_key, fence_token, owner_id, outcome,
               provider_calls, llm_calls, started_at, finished_at, detail
          FROM node_attempts
         WHERE run_id = %s
         ORDER BY started_at, attempt_id
        """,
        (run_id,),
    ).fetchall()
    return [
        {
            "attempt_id": str(row[0]),
            "node": row[1],
            "execution_key": row[2],
            "fence_token": int(row[3]),
            "owner_id": row[4],
            "outcome": AttemptOutcome(row[5]),
            "provider_calls": int(row[6]),
            "llm_calls": int(row[7]),
            "started_at": row[8],
            "finished_at": row[9],
            "detail": row[10],
        }
        for row in rows
    ]
