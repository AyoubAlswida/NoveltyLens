"""Lease fencing under **real concurrency**, on real PostgreSQL, with two
connections.

Every test here is marked `postgres` and every one of them is meaningless
without a database. The in-memory double executes one call at a time in one
process, so it cannot exhibit a lock-ordering bug, a lost update, a blocked
takeover or a deadlock -- asserting these against it would be asserting nothing.
A skipped run of this file is a skipped run, and the Gate A report says so.

**The bug this file exists to catch.** An earlier draft validated the lease with
a plain `SELECT` and then wrote. That leaves a window: a takeover can bump
`fence_token` between the check and the write, so the write commits under
authority the writer no longer holds. Holding the lease row `FOR UPDATE` for the
life of the transaction closes it -- and the only way to prove the row is *held*
rather than merely read is to have a second connection block on it.
"""

import threading
import time

import psycopg
import pytest

from workflow import attempts, events, lease, store
from workflow.types import (
    FencedError,
    IllegalStateError,
    ReplayConflictError,
    RunStatus,
    WorkflowEventType,
)

pytestmark = pytest.mark.postgres

TTL = 60.0

LEASE_SENSITIVE_FUNCTIONS = (
    "_lock_run_and_require_lease",
    "acquire_run_lease",
    "heartbeat_run_lease",
    "release_run_lease",
    "pause_run_for_review",
    "finalize_run_success",
    "finalize_run_failure",
    # Every attempt mutation, all four of them. `commit_node_attempt` was fenced
    # from the start; the other three arrived with migration 0016, which closed
    # the last unfenced writes in the system.
    "commit_node_attempt",
    "begin_node_attempt",
    "fail_node_attempt",
    "close_superseded_node_attempts",
)


class _Background:
    """Run one callable on a thread and report whether it has finished yet.

    Used to prove a statement is *blocked*: a call that has not returned after a
    generous wait, and then returns promptly once the blocking transaction
    commits, was waiting on a lock rather than merely being slow.
    """

    def __init__(self, target) -> None:
        self.result = None
        self.error: BaseException | None = None
        self.done = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(target,), daemon=True)

    def _run(self, target) -> None:
        try:
            self.result = target()
        except BaseException as error:  # noqa: BLE001 - reported to the test
            self.error = error
        finally:
            self.done.set()

    def start(self) -> "_Background":
        self._thread.start()
        return self

    def finished_within(self, seconds: float) -> bool:
        return self.done.wait(seconds)


def _running_leased(pg, make_run, owner: str = "worker-1") -> tuple[str, int]:
    run_id = make_run()
    token = lease.acquire(pg, run_id=run_id, owner_id=owner, ttl_seconds=TTL)
    pg.execute("UPDATE verification_runs SET status = 'RUNNING' WHERE id = %s", (run_id,))
    return run_id, token


def _expire(pg, run_id: str) -> None:
    """Age the whole holding into the past, keeping `expires_at > acquired_at`."""
    pg.execute(
        """
        UPDATE run_leases
           SET acquired_at = now() - interval '120 seconds',
               heartbeat_at = now() - interval '120 seconds',
               expires_at  = now() - interval '60 seconds'
         WHERE run_id = %s
        """,
        (run_id,),
    )


# --- the lock is held, not merely read -----------------------------------


def test_a_takeover_blocks_until_the_authorized_transaction_commits(
    pg, postgres_url, make_run
):
    """A second connection's acquisition **waits** while a validated lease holder
    has an open transaction.

    That waiting is the whole property. If `_lock_run_and_require_lease` merely
    read the rows, this call would return immediately and a takeover could slip
    between another worker's check and its write.
    """
    run_id, token = _running_leased(pg, make_run)

    holder = psycopg.connect(postgres_url)
    try:
        with holder.transaction():
            holder.execute(
                "SELECT _lock_run_and_require_lease(%s, %s, %s)",
                (run_id, "worker-1", token),
            )

            def takeover() -> object:
                with psycopg.connect(postgres_url, autocommit=True) as other:
                    return other.execute(
                        "SELECT acquire_run_lease(%s, %s, interval '60 seconds')",
                        (run_id, "worker-2"),
                    ).fetchone()[0]

            attempt = _Background(takeover).start()
            assert not attempt.finished_within(1.5), (
                "the takeover returned while the holder still had both rows locked, "
                "which means the lease was read rather than held"
            )
        # The holder's transaction has now committed and released both locks.
        assert attempt.finished_within(10), "the takeover never unblocked"
        assert attempt.error is None
        # The lease was still live, so the correct answer is "not claimable".
        assert attempt.result is None
    finally:
        holder.close()


def test_a_release_racing_a_finalization_serializes_and_the_terminal_commits(
    pg, postgres_url, make_run
):
    """They serialize on the run row rather than interleaving.

    The finalizer holds the locks in an uncommitted transaction; the release
    blocks behind it; the terminal still commits, and the release then succeeds
    because the finalizer does not release on its own.
    """
    run_id, token = _running_leased(pg, make_run)

    finalizer = psycopg.connect(postgres_url)
    try:
        with finalizer.transaction():
            events.finalize_run_success(
                finalizer,
                run_id=run_id,
                owner_id="worker-1",
                fence_token=token,
                payload={"type": "result", "data": {"top_score": 0}},
            )

            def release() -> None:
                with psycopg.connect(postgres_url, autocommit=True) as other:
                    lease.release(
                        other, run_id=run_id, owner_id="worker-1", fence_token=token
                    )

            racer = _Background(release).start()
            assert not racer.finished_within(1.5), "the release did not serialize"
        assert racer.finished_within(10)
        assert racer.error is None
    finally:
        finalizer.close()

    assert pg.execute(
        "SELECT count(*) FROM workflow_events WHERE run_id = %s AND is_terminal", (run_id,)
    ).fetchone()[0] == 1
    assert pg.execute(
        "SELECT status FROM verification_runs WHERE id = %s", (run_id,)
    ).fetchone()[0] == "COMPLETED"
    assert lease.lease_row(pg, run_id=run_id)["owner_id"] is None


# --- after a takeover commits, the old token can write nothing -----------


def test_after_a_takeover_the_old_token_can_write_nothing_at_all(pg, make_run):
    """Four writes, four refusals: an event, a status transition, a node-attempt
    commitment, and both finalizers.

    This is "exactly one authorized writer" as a database property rather than a
    deployment assumption.
    """
    run_id, old_token = _running_leased(pg, make_run)
    stalled_attempt = attempts.begin_attempt(
        pg,
        run_id=run_id,
        node="enrich_epo",
        execution_key="task-1",
        fence_token=old_token,
        owner_id="worker-1",
    )
    _expire(pg, run_id)
    new_token = lease.acquire(pg, run_id=run_id, owner_id="worker-2", ttl_seconds=TTL)
    assert new_token == old_token + 1

    with pytest.raises(FencedError):
        events.append_worker_event(
            pg,
            run_id=run_id,
            owner_id="worker-1",
            fence_token=old_token,
            event_type=WorkflowEventType.PROGRESS,
            phase="understand",
            payload={"type": "progress", "phase": "understand"},
            dedupe_key="progress:after-takeover",
        )

    with pytest.raises(FencedError):
        store.transition_status(
            pg,
            run_id=run_id,
            owner_id="worker-1",
            fence_token=old_token,
            expected_from=(RunStatus.RUNNING,),
            to_status=RunStatus.WAITING_FOR_REVIEW,
            current_gate="A",
        )

    with pytest.raises(FencedError):
        attempts.commit_attempt(
            pg,
            run_id=run_id,
            owner_id="worker-1",
            fence_token=old_token,
            attempt_id=stalled_attempt,
        )

    with pytest.raises(FencedError):
        events.finalize_run_success(
            pg,
            run_id=run_id,
            owner_id="worker-1",
            fence_token=old_token,
            payload={"type": "result", "data": {}},
        )
    with pytest.raises(FencedError):
        events.finalize_run_failure(
            pg,
            run_id=run_id,
            owner_id="worker-1",
            fence_token=old_token,
            payload={"type": "error", "code": "X"},
            failure_code="X",
        )

    assert pg.execute(
        "SELECT count(*) FROM workflow_events WHERE run_id = %s", (run_id,)
    ).fetchone()[0] == 1, "only the admission event exists"
    assert pg.execute(
        "SELECT status FROM verification_runs WHERE id = %s", (run_id,)
    ).fetchone()[0] == "RUNNING"


def test_two_executors_racing_one_run_yield_exactly_one_holder(postgres_url, make_run):
    """Both start together; one acquires, the other gets `None` and does not
    execute."""
    run_id = make_run()
    barrier = threading.Barrier(2)
    tokens: list = []
    lock = threading.Lock()

    def race(owner: str) -> None:
        with psycopg.connect(postgres_url, autocommit=True) as conn:
            barrier.wait(timeout=10)
            token = lease.acquire(conn, run_id=run_id, owner_id=owner, ttl_seconds=TTL)
            with lock:
                tokens.append(token)

    threads = [threading.Thread(target=race, args=(f"worker-{n}",)) for n in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()

    assert sorted(tokens, key=lambda t: (t is None, t)) == [1, None], (
        f"exactly one acquisition should succeed, got {tokens}"
    )


def test_a_heartbeat_racing_a_takeover_resolves_to_exactly_one_holder(
    pg, postgres_url, make_run
):
    """A late beat cannot resurrect the old lease once the sweep is entitled to
    claim it.

    Both run against an expired lease. Whatever the interleaving, the row ends
    with one owner, and it is never the old holder at the old token.
    """
    run_id, old_token = _running_leased(pg, make_run)
    _expire(pg, run_id)

    barrier = threading.Barrier(2)
    outcomes: dict[str, object] = {}
    lock = threading.Lock()

    def beat() -> None:
        with psycopg.connect(postgres_url, autocommit=True) as conn:
            barrier.wait(timeout=10)
            alive = lease.heartbeat(
                conn,
                run_id=run_id,
                owner_id="worker-1",
                fence_token=old_token,
                ttl_seconds=TTL,
            )
            with lock:
                outcomes["heartbeat"] = alive

    def takeover() -> None:
        with psycopg.connect(postgres_url, autocommit=True) as conn:
            barrier.wait(timeout=10)
            with lock:
                outcomes["acquire"] = lease.acquire(
                    conn, run_id=run_id, owner_id="worker-2", ttl_seconds=TTL
                )

    threads = [threading.Thread(target=beat), threading.Thread(target=takeover)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()

    row = lease.lease_row(pg, run_id=run_id)
    if outcomes["acquire"] is not None:
        assert outcomes["acquire"] == old_token + 1
        assert row["owner_id"] == "worker-2"
        assert row["fence_token"] == old_token + 1
    else:
        # The beat landed first and extended a lease that was still active in its
        # own transaction snapshot. Even then the old holder is the only holder --
        # never two.
        assert row["owner_id"] == "worker-1"
    assert not (outcomes["heartbeat"] and outcomes["acquire"] is not None), (
        "a heartbeat and a takeover both succeeding would mean two live holders"
    )


def test_concurrent_success_and_failure_finalizers_yield_one_terminal(
    postgres_url, pg, make_run
):
    """Exactly one terminal outcome. The loser raises and leaves nothing behind."""
    run_id, token = _running_leased(pg, make_run)
    barrier = threading.Barrier(2)
    errors: list = []
    successes: list = []
    lock = threading.Lock()

    def finalize(kind: str) -> None:
        with psycopg.connect(postgres_url, autocommit=True) as conn:
            barrier.wait(timeout=10)
            try:
                if kind == "success":
                    sequence = events.finalize_run_success(
                        conn,
                        run_id=run_id,
                        owner_id="worker-1",
                        fence_token=token,
                        payload={"type": "result", "data": {}},
                    )
                else:
                    sequence = events.finalize_run_failure(
                        conn,
                        run_id=run_id,
                        owner_id="worker-1",
                        fence_token=token,
                        payload={"type": "error", "code": "X"},
                        failure_code="X",
                    )
                with lock:
                    successes.append((kind, sequence))
            except (ReplayConflictError, IllegalStateError, FencedError) as error:
                with lock:
                    errors.append(error)

    threads = [
        threading.Thread(target=finalize, args=("success",)),
        threading.Thread(target=finalize, args=("failure",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()

    assert len(successes) == 1, f"exactly one terminal outcome, got {successes}"
    assert len(errors) == 1, "the loser must raise, not silently succeed"
    assert pg.execute(
        "SELECT count(*) FROM workflow_events WHERE run_id = %s AND is_terminal", (run_id,)
    ).fetchone()[0] == 1

    status = pg.execute(
        "SELECT status FROM verification_runs WHERE id = %s", (run_id,)
    ).fetchone()[0]
    assert status == ("COMPLETED" if successes[0][0] == "success" else "FAILED")


def test_concurrent_identical_finalizers_return_the_same_sequence(
    postgres_url, pg, make_run
):
    """One event, one status, and both callers get the same sequence -- because
    the second is a genuine replay, not a second write."""
    run_id, token = _running_leased(pg, make_run)
    payload = {"type": "result", "data": {"top_score": 7}}
    barrier = threading.Barrier(2)
    sequences: list = []
    lock = threading.Lock()

    def finalize() -> None:
        with psycopg.connect(postgres_url, autocommit=True) as conn:
            barrier.wait(timeout=10)
            sequence = events.finalize_run_success(
                conn,
                run_id=run_id,
                owner_id="worker-1",
                fence_token=token,
                payload=payload,
            )
            with lock:
                sequences.append(sequence)

    threads = [threading.Thread(target=finalize) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()

    assert len(sequences) == 2 and sequences[0] == sequences[1]
    assert pg.execute(
        "SELECT count(*) FROM workflow_events WHERE run_id = %s AND is_terminal", (run_id,)
    ).fetchone()[0] == 1


# --- the mandatory lock order ---------------------------------------------


def test_every_lease_sensitive_function_locks_the_run_row_first(pg):
    """A static check over the function bodies, which is the form that scales:
    `pg_locks` can prove both rows are locked but not in which order they were
    taken.

    One order, no exceptions. If any operation took `run_leases` before
    `verification_runs`, two concurrent workers would each hold what the other
    needs.
    """
    for name in LEASE_SENSITIVE_FUNCTIONS:
        body = pg.execute(
            "SELECT prosrc FROM pg_proc WHERE proname = %s", (name,)
        ).fetchone()[0]
        run_lock = body.find("FROM verification_runs WHERE id = p_run_id FOR UPDATE")
        lease_lock = body.find("FROM run_leases")
        if lease_lock == -1:
            # Delegates its locking to _lock_run_and_require_lease, which is in
            # this same list and is checked on its own.
            assert "_lock_run_and_require_lease" in body, (
                f"{name} touches neither lease locking nor the helper that does it"
            )
            continue
        assert run_lock != -1, f"{name} locks run_leases without locking the run row"
        assert run_lock < lease_lock, (
            f"{name} takes run_leases before verification_runs, which is the deadlock"
        )


def test_the_lease_row_is_actually_locked_not_merely_read(pg, postgres_url, make_run):
    """`pg_locks` shows row-level exclusive locks on **both** relations while a
    validated holder's transaction is open."""
    run_id, token = _running_leased(pg, make_run)
    holder = psycopg.connect(postgres_url)
    try:
        with holder.transaction():
            holder.execute(
                "SELECT _lock_run_and_require_lease(%s, %s, %s)",
                (run_id, "worker-1", token),
            )
            backend_pid = holder.info.backend_pid
            locked = {
                row[0]
                for row in pg.execute(
                    """
                    SELECT c.relname FROM pg_locks l
                      JOIN pg_class c ON c.oid = l.relation
                     WHERE l.pid = %s AND c.relname IN ('verification_runs', 'run_leases')
                    """,
                    (backend_pid,),
                )
            }
            assert locked == {"verification_runs", "run_leases"}
    finally:
        holder.close()


def test_no_deadlock_under_a_repeated_mixed_workload(postgres_url, pg, make_run):
    """Concurrent acquire / heartbeat / append / finalize in a loop, asserted by
    the absence of SQLSTATE 40P01.

    One lock order is what makes this true; the test exists so a future function
    that takes them in the other order fails here rather than in production at
    3am.
    """
    run_ids = [make_run() for _ in range(4)]
    for run_id in run_ids:
        token = lease.acquire(pg, run_id=run_id, owner_id="seed", ttl_seconds=TTL)
        pg.execute(
            "UPDATE verification_runs SET status = 'RUNNING' WHERE id = %s", (run_id,)
        )
        lease.release(pg, run_id=run_id, owner_id="seed", fence_token=token)

    deadlocks: list = []
    lock = threading.Lock()
    stop = threading.Event()

    def churn(worker: str) -> None:
        try:
            with psycopg.connect(postgres_url, autocommit=True) as conn:
                for cycle in range(12):
                    if stop.is_set():
                        return
                    for run_id in run_ids:
                        try:
                            token = lease.acquire(
                                conn, run_id=run_id, owner_id=worker, ttl_seconds=TTL
                            )
                            if token is None:
                                continue
                            lease.heartbeat(
                                conn,
                                run_id=run_id,
                                owner_id=worker,
                                fence_token=token,
                                ttl_seconds=TTL,
                            )
                            events.append_worker_event(
                                conn,
                                run_id=run_id,
                                owner_id=worker,
                                fence_token=token,
                                event_type=WorkflowEventType.PROGRESS,
                                phase="understand",
                                payload={
                                    "type": "progress",
                                    "phase": "understand",
                                    "n": f"{worker}-{cycle}",
                                },
                                dedupe_key=f"progress:{worker}:{cycle}",
                            )
                            lease.release(
                                conn, run_id=run_id, owner_id=worker, fence_token=token
                            )
                        except psycopg.errors.DeadlockDetected as error:
                            with lock:
                                deadlocks.append(error)
                            stop.set()
                            return
                        except (FencedError, IllegalStateError, ReplayConflictError):
                            # Losing a race is the expected outcome, not a defect.
                            continue
        except psycopg.errors.DeadlockDetected as error:  # pragma: no cover
            with lock:
                deadlocks.append(error)

    threads = [threading.Thread(target=churn, args=(f"w{n}",)) for n in range(4)]
    started = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
        assert not thread.is_alive(), "a worker hung, which a deadlock timeout would show"

    assert deadlocks == [], f"SQLSTATE 40P01 under concurrent load: {deadlocks}"
    assert time.monotonic() - started < 120
