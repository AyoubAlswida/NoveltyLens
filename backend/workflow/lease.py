"""The lease: acquisition, heartbeat, release, the reclaim sweep, and the fencing
token every state and event write carries.

**What a lease guarantees.** At most one current holder per run at any instant,
fence tokens that only ever increase, exactly one authorized state writer, and a
fenced worker that persists nothing -- not a status, not an event, not a
committed attempt, not a terminal result.

**What it cannot guarantee, and must never be described as guaranteeing.** A
lease controls *who may commit*. It has no authority over a socket already open
to a provider or an LLM gateway. A worker can lose its lease while blocked inside
an outbound call: the heartbeat fails to renew, the sweep hands the run to a
replacement, and the original call is still in flight, will still complete, and
will still have been billed. External calls are **at-least-once** across
recovery. Guard the commit, record the attempt (`workflow/attempts.py`), disclose
the duplicate read.

**One lock order, held to commit, in every function here:**
``verification_runs FOR UPDATE`` then ``run_leases FOR UPDATE``. That order lives
in the SQL functions (migration 0007), not in this module, because validating a
lease and then writing in two steps leaves a window a takeover can slip through.

**Nothing here deletes a lease row.** The row is the run's permanent fencing
counter, created with the run and removed only by the run's cascade. Release
clears the holder and leaves `fence_token` exactly where it was, so a superseded
token is burned forever -- which is what stops a restarted pod whose `owner_id`
happens to regenerate identically from matching a later lease that cycled back
through the same number. `tests/test_lease_lifecycle.py` asserts by source
contract that no module under `backend/` issues a DELETE against the table.
"""

import logging
import os
import socket
import threading
import uuid
from datetime import timedelta
from typing import Optional

import psycopg

import db
from workflow import redaction
from workflow.types import raise_for_sqlstate

logger = logging.getLogger(__name__)


def new_owner_id() -> str:
    """A worker identity: ``host:pid:uuid``.

    The uuid is what makes it unique rather than merely descriptive -- a
    restarted pod can land on the same host and pid, and a deterministic owner id
    would then be indistinguishable from the previous holder's. Fence-token
    monotonicity means even a *deliberately* reused owner id cannot write under a
    superseded token, but there is no reason to lean on that when a uuid is free.
    """
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4()}"


def acquire(
    conn: psycopg.Connection, *, run_id: str, owner_id: str, ttl_seconds: float
) -> Optional[int]:
    """Claim a run's lease, or report that someone else holds it.

    Returns:
        The new fence token -- **always** the previous one plus 1, never 0 and
        never a repeat -- or ``None`` when a live lease is held elsewhere or the
        run is not in a claimable status.

    ``None`` means *do not execute*. Not "retry in a loop", not "assume it is
    stale": return to the poll and let the current holder finish or expire.

    Raises:
        LeaseLifecycleError: `FN006` -- the run has no lease row, meaning it was
            not created by `create_run`. Failing loudly beats inserting one,
            which would restart the fencing counter at a value already used.
    """
    try:
        row = conn.execute(
            "SELECT acquire_run_lease(%s, %s, %s)",
            (run_id, owner_id, timedelta(seconds=ttl_seconds)),
        ).fetchone()
    except psycopg.Error as error:
        raise_for_sqlstate(error)
        raise
    return None if row[0] is None else int(row[0])


def heartbeat(
    conn: psycopg.Connection,
    *,
    run_id: str,
    owner_id: str,
    fence_token: int,
    ttl_seconds: float,
) -> bool:
    """Extend an **active** lease. It can never revive an expired or released one.

    Returns:
        ``True`` if the lease was extended. ``False`` means **we have been
        fenced** -- and nothing was touched: not `heartbeat_at`, not `expires_at`,
        not `owner_id`, not `released_at`, not `fence_token`.

    The boolean is the documented asymmetry: every *other* lease-sensitive path
    raises `FN001`, but this one runs on a thread whose entire job is to answer
    "do we still hold this?", and an exception there would only have to be caught
    and converted into exactly this boolean.
    """
    try:
        row = conn.execute(
            "SELECT heartbeat_run_lease(%s, %s, %s, %s)",
            (run_id, owner_id, fence_token, timedelta(seconds=ttl_seconds)),
        ).fetchone()
    except psycopg.Error as error:
        raise_for_sqlstate(error)
        raise
    return bool(row[0])


def release(
    conn: psycopg.Connection, *, run_id: str, owner_id: str, fence_token: int
) -> None:
    """Release an actively-held lease by **clearing the holder**.

    The row stays, `fence_token` is untouched, and `released_at` is stamped.

    Raises:
        FencedError: `FN001` when the caller is not the active holder -- wrong
            owner, wrong token, expired, or already released. That is a fencing
            failure, not a harmless no-op: a worker releasing a lease it no longer
            holds has been operating on a false belief about its own authority and
            needs to learn that here rather than carry on as though the handover
            were clean. A repeated release is evidence of exactly that confusion.
    """
    try:
        conn.execute(
            "SELECT release_run_lease(%s, %s, %s)", (run_id, owner_id, fence_token)
        )
    except psycopg.Error as error:
        raise_for_sqlstate(error)
        raise


def claimable_runs(conn: psycopg.Connection, *, limit: int = 10) -> list[str]:
    """The reclaim sweep: runs that need an executor and have no live holder.

    A run is claimable when its status is QUEUED, RUNNING or RESUMING **and** its
    lease is unheld or expired -- ``owner_id IS NULL OR released_at IS NOT NULL OR
    expires_at <= now()``, the exact negation of the active predicate. There is no
    instant that is neither active nor claimable: with ``expires_at < now()`` the
    run would be stranded for one tick with nobody entitled to advance it.

    The lease row is never absent -- only ever unheld -- so this is a status
    question, not an existence question. An INNER JOIN that found no row would be
    hiding the `FN006` invariant break rather than surfacing it, so the join is
    LEFT and a missing lease row still selects the run: `acquire` then raises.

    **The sweep does not change status.** The new owner keeps the run's status and
    continues from the checkpoint. Status tracks workflow position; the lease
    tracks who may advance it, and conflating them produces a state machine with
    holes.
    """
    rows = conn.execute(
        """
        SELECT r.id
          FROM verification_runs r
          LEFT JOIN run_leases l ON l.run_id = r.id
         WHERE r.status IN ('QUEUED', 'RUNNING', 'RESUMING')
           AND (l.run_id IS NULL
                OR l.owner_id IS NULL
                OR l.released_at IS NOT NULL
                OR l.expires_at <= now())
         ORDER BY r.updated_at
         LIMIT %s
        """,
        (limit,),
    ).fetchall()
    return [str(row[0]) for row in rows]


def lease_row(conn: psycopg.Connection, *, run_id: str) -> Optional[dict[str, object]]:
    """The raw lease row, for operators and for tests that snapshot it before and
    after a failed call to prove nothing moved."""
    row = conn.execute(
        """
        SELECT run_id, owner_id, fence_token, acquired_at, heartbeat_at, expires_at,
               released_at
          FROM run_leases WHERE run_id = %s
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "run_id": str(row[0]),
        "owner_id": row[1],
        "fence_token": int(row[2]),
        "acquired_at": row[3],
        "heartbeat_at": row[4],
        "expires_at": row[5],
        "released_at": row[6],
    }


class Heartbeat:
    """Renews one run's lease on a thread **independent of node execution**.

    That independence is the point: a single verifier call can take 60 seconds,
    so a heartbeat living on the execution path would let the lease lapse inside
    one ordinary node and fence a perfectly healthy worker.

    It holds **its own database connection**. Sharing the executor's would
    interleave two statement streams on one psycopg connection, which is not safe
    and would serialize the beat behind whatever the executor is doing -- exactly
    what independence is supposed to avoid.

    When a beat returns ``False`` the thread sets :attr:`fenced` and stops. The
    executor checks it before every write and abandons the run. It does **not**
    then try to release, mark its attempt, or record an error: those are writes,
    and a fenced worker's writes would all raise `FN001` anyway. The replacement
    holder's reconcile closes the abandoned attempt as FENCED under its own valid
    lease -- a fenced worker must never write telemetry using the authority it
    just lost.
    """

    def __init__(
        self, *, run_id: str, owner_id: str, fence_token: int, ttl_seconds: float,
        interval_seconds: float,
    ) -> None:
        self.run_id = run_id
        self.owner_id = owner_id
        self.fence_token = fence_token
        self.ttl_seconds = ttl_seconds
        self.interval_seconds = interval_seconds
        self._fenced = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def fenced(self) -> bool:
        """True once a beat has failed. The worker stops scheduling work
        immediately; an already-running external call cannot be cancelled and its
        output can never be persisted."""
        return self._fenced.is_set()

    def start(self) -> "Heartbeat":
        self._thread = threading.Thread(
            target=self._loop, name=f"lease-heartbeat-{self.run_id[:8]}", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval_seconds + 5)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                with db.connect() as conn:
                    alive = heartbeat(
                        conn,
                        run_id=self.run_id,
                        owner_id=self.owner_id,
                        fence_token=self.fence_token,
                        ttl_seconds=self.ttl_seconds,
                    )
            except Exception as error:
                # An unreachable database is indistinguishable from a lost lease
                # from here, and the safe reading is the pessimistic one: assume
                # we no longer hold the run and stop. Continuing would risk a
                # second executor's writes racing ours.
                logger.warning(
                    "heartbeat for run %s could not reach the database: %s",
                    self.run_id,
                    redaction.safe_log(error),
                )
                self._fenced.set()
                return
            if not alive:
                logger.warning(
                    "run %s was fenced at token %s; this worker stops now",
                    self.run_id,
                    self.fence_token,
                )
                self._fenced.set()
                return

    def __enter__(self) -> "Heartbeat":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
