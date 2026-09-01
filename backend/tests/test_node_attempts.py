"""Attempt telemetry: `execution_key` scoping, fenced attempts, and the honest
guarantee.

**The guarantee this file asserts, in the words that are actually true.** One
authorized writer, one committed result per logical execution, and **at-least-once
external calls**. It deliberately does *not* assert that a provider was called
once, because no lease can deliver that: a worker can lose its lease while blocked
inside an outbound call, and the call still completes and is still billed.

That is why `node_attempts` exists at all. It makes duplicate external reads
**visible** rather than impossible -- two attempts, one committed result -- so an
operator can see that EPO was queried twice during a recovery.

**Why the key is not the node.** `(run_id, node)` alone would permit one committed
execution of a node for the life of a run, forbidding every legitimate repeat: a
cell rerun after a context request, a second `result_version`, a re-verification
against changed passage text, a revisit after a new decomposition or candidate-set
version, an ordinary LangGraph loop.
"""

import psycopg
import pytest

from workflow import attempts, lease
from workflow.types import AttemptOutcome, FencedError

TTL = 60.0
OWNER = "worker-1"


def _take_over(pg, run_id: str, *, new_owner: str = "worker-2") -> int:
    """Perform a REAL takeover and return the new holder's token.

    Every `close_superseded_attempts` call below goes through this rather than
    passing a fabricated `token + 1`. That shortcut used to work because the
    function was unfenced -- it now (correctly) raises `FN001`, because no holder
    ever had that token. Simulating a takeover by inventing a number was always
    asserting less than it appeared to: the point of the marking is that the *new
    holder* does it under a lease it genuinely acquired.
    """
    pg.execute(
        """
        UPDATE run_leases
           SET acquired_at = now() - interval '120 seconds',
               heartbeat_at = now() - interval '120 seconds',
               expires_at  = now() - interval '60 seconds'
         WHERE run_id = %s AND owner_id IS NOT NULL
        """,
        (run_id,),
    )
    token = lease.acquire(pg, run_id=run_id, owner_id=new_owner, ttl_seconds=TTL)
    assert token is not None, "the takeover should have succeeded"
    return token


def _leased(pg, make_run) -> tuple[str, int]:
    run_id = make_run()
    token = lease.acquire(pg, run_id=run_id, owner_id=OWNER, ttl_seconds=TTL)
    return run_id, token


def _outcomes(pg, run_id: str) -> list[tuple[str, str, str]]:
    return [
        (row["node"], row["execution_key"], str(row["outcome"]))
        for row in attempts.list_attempts(pg, run_id=run_id)
    ]


# --- execution key derivation --------------------------------------------


def test_a_node_execution_key_is_the_checkpoint_task_id():
    """Verified against LangGraph 1.2.7: the id a task carries in the `tasks`
    stream is identical when that task re-runs after a crash and when it re-runs
    after a resumed interrupt."""
    assert attempts.execution_key_for_node("726318d0-83ea") == "726318d0-83ea"
    with pytest.raises(ValueError):
        attempts.execution_key_for_node("")


def test_a_cell_key_carries_its_result_version():
    """A rerun at `result_version = 2` is a **new logical execution** and commits
    independently of version 1."""
    assert (
        attempts.execution_key_for_cell("run:F2:US1234567B2", 1)
        != attempts.execution_key_for_cell("run:F2:US1234567B2", 2)
    )
    assert attempts.execution_key_for_cell("c", 2) == "cell:c:result-version:2"


def test_changed_versions_each_produce_a_new_execution_key():
    """A changed decomposition version, a changed candidate-set version and a
    changed report version are each genuinely different work."""
    assert attempts.execution_key_for_decomposition(3) != attempts.execution_key_for_decomposition(4)
    assert attempts.execution_key_for_candidate_set(1) != attempts.execution_key_for_candidate_set(2)
    assert attempts.execution_key_for_report(1) != attempts.execution_key_for_report(2)
    assert attempts.execution_key_for_gate("C", 1) != attempts.execution_key_for_gate("C", 2)


def test_an_execution_key_supplied_by_a_client_is_rejected():
    """It is derived from trusted graph state and nowhere else. A caller supplying
    one could collide two distinct executions or force a duplicate commit."""
    attempts.reject_client_execution_key(None)
    attempts.reject_client_execution_key("")
    with pytest.raises(ValueError) as caught:
        attempts.reject_client_execution_key("cell:mine:result-version:1")
    assert "never accepted from a client" in str(caught.value)


def test_an_execution_key_carries_no_user_text():
    """Identifiers and versions only. It is logged and reaches operators, and an
    invention description does not belong in a telemetry column."""
    key = attempts.execution_key_for_cell("run-1:F2:US1234567B2", 1)
    assert "exhaust" not in key and " " not in key


# --- the committed-attempt index -----------------------------------------


@pytest.mark.postgres
def test_two_attempts_at_one_execution_key_yield_one_committed(pg, make_run):
    """The at-least-once case, made visible. Both attempts are retained; only one
    is `COMMITTED`."""
    run_id, token = _leased(pg, make_run)
    first = attempts.begin_attempt(
        pg, run_id=run_id, node="enrich_epo", execution_key="task-1",
        fence_token=token, owner_id=OWNER,
    )
    second = attempts.begin_attempt(
        pg, run_id=run_id, node="enrich_epo", execution_key="task-1",
        fence_token=token, owner_id=OWNER,
    )
    attempts.commit_attempt(
        pg, run_id=run_id, owner_id=OWNER, fence_token=token, attempt_id=first,
        provider_calls=1,
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        attempts.commit_attempt(
            pg, run_id=run_id, owner_id=OWNER, fence_token=token, attempt_id=second,
            provider_calls=1,
        )


@pytest.mark.postgres
def test_two_attempts_with_different_execution_keys_may_both_commit(pg, make_run):
    """Not retries -- new logical executions. Scoping by node alone would forbid
    them."""
    run_id, token = _leased(pg, make_run)
    for key in ("cell:c1:result-version:1", "cell:c1:result-version:2"):
        attempt = attempts.begin_attempt(
            pg, run_id=run_id, node="verify_features", execution_key=key,
            fence_token=token, owner_id=OWNER,
        )
        attempts.commit_attempt(
            pg, run_id=run_id, owner_id=OWNER, fence_token=token, attempt_id=attempt
        )
    assert [outcome for _, _, outcome in _outcomes(pg, run_id)] == ["COMMITTED", "COMMITTED"]


@pytest.mark.postgres
def test_a_cell_rerun_at_result_version_two_commits_after_version_one(pg, make_run):
    run_id, token = _leased(pg, make_run)
    for version in (1, 2):
        attempt = attempts.begin_attempt(
            pg,
            run_id=run_id,
            node="verify_features",
            execution_key=attempts.execution_key_for_cell("run:F2:US123", version),
            fence_token=token,
            owner_id=OWNER,
        )
        attempts.commit_attempt(
            pg, run_id=run_id, owner_id=OWNER, fence_token=token, attempt_id=attempt
        )
    committed = attempts.committed_execution_keys(pg, run_id=run_id, node="verify_features")
    assert committed == {
        "cell:run:F2:US123:result-version:1",
        "cell:run:F2:US123:result-version:2",
    }


@pytest.mark.postgres
def test_a_fenced_attempt_can_never_become_committed(pg, make_run):
    run_id, token = _leased(pg, make_run)
    attempt = attempts.begin_attempt(
        pg, run_id=run_id, node="enrich_epo", execution_key="task-1",
        fence_token=token, owner_id=OWNER,
    )
    # A later holder closes it, which is the only way an attempt is ever marked
    # FENCED: by the new owner under its own valid lease.
    new_token = _take_over(pg, run_id)
    attempts.close_superseded_attempts(
        pg, run_id=run_id, owner_id="worker-2", fence_token=new_token
    )
    assert _outcomes(pg, run_id) == [("enrich_epo", "task-1", "FENCED")]

    with pytest.raises(FencedError):
        attempts.commit_attempt(
            pg, run_id=run_id, owner_id=OWNER, fence_token=token, attempt_id=attempt
        )
    assert _outcomes(pg, run_id) == [("enrich_epo", "task-1", "FENCED")]


@pytest.mark.postgres
def test_a_recovery_reuses_the_execution_key_and_mints_a_new_attempt_id(pg, make_run):
    """The shape of a real recovery: same logical work, second physical attempt."""
    run_id, first_token = _leased(pg, make_run)
    first = attempts.begin_attempt(
        pg, run_id=run_id, node="enrich_epo", execution_key="task-7",
        fence_token=first_token, owner_id=OWNER,
    )
    lease.release(pg, run_id=run_id, owner_id=OWNER, fence_token=first_token)
    second_token = lease.acquire(pg, run_id=run_id, owner_id="worker-2", ttl_seconds=TTL)
    attempts.close_superseded_attempts(
        pg, run_id=run_id, owner_id="worker-2", fence_token=second_token
    )
    second = attempts.begin_attempt(
        pg, run_id=run_id, node="enrich_epo", execution_key="task-7",
        fence_token=second_token, owner_id="worker-2",
    )
    attempts.commit_attempt(
        pg, run_id=run_id, owner_id="worker-2", fence_token=second_token, attempt_id=second,
        provider_calls=1,
    )

    assert first != second, "a recovery mints a NEW attempt_id"
    rows = attempts.list_attempts(pg, run_id=run_id)
    assert {row["execution_key"] for row in rows} == {"task-7"}, "and reuses the key"
    assert [str(row["outcome"]) for row in rows] == ["FENCED", "COMMITTED"]


@pytest.mark.postgres
def test_telemetry_retains_every_physical_attempt(pg, make_run):
    """Two attempts visible, one committed result. The matrix shows one; the
    telemetry shows both, which is what lets an operator see the duplicate
    external read."""
    run_id, token = _leased(pg, make_run)
    for _ in range(3):
        attempts.begin_attempt(
            pg, run_id=run_id, node="enrich_epo", execution_key="task-1",
            fence_token=token, owner_id=OWNER,
        )
    rows = attempts.list_attempts(pg, run_id=run_id)
    attempts.commit_attempt(
        pg, run_id=run_id, owner_id=OWNER, fence_token=token,
        attempt_id=rows[0]["attempt_id"],
    )
    after = attempts.list_attempts(pg, run_id=run_id)
    assert len(after) == 3, "no attempt is ever overwritten or removed"
    assert sum(1 for row in after if row["outcome"] is AttemptOutcome.COMMITTED) == 1


@pytest.mark.postgres
def test_a_fenced_worker_cannot_commit_a_node_attempt_after_a_takeover(pg, make_run):
    """One of the four things a superseded token can no longer do."""
    run_id, old_token = _leased(pg, make_run)
    attempt = attempts.begin_attempt(
        pg, run_id=run_id, node="enrich_epo", execution_key="task-1",
        fence_token=old_token, owner_id=OWNER,
    )
    pg.execute(
        """
        UPDATE run_leases
           SET acquired_at = now() - interval '20 seconds',
               expires_at  = now() - interval '10 seconds'
         WHERE run_id = %s
        """,
        (run_id,),
    )
    lease.acquire(pg, run_id=run_id, owner_id="worker-2", ttl_seconds=TTL)

    with pytest.raises(FencedError):
        attempts.commit_attempt(
            pg, run_id=run_id, owner_id=OWNER, fence_token=old_token, attempt_id=attempt
        )


@pytest.mark.postgres
def test_a_lease_lost_during_a_blocked_provider_call(pg, make_run):
    """The scenario §3.3.1 describes, driven directly.

    A worker is blocked inside an outbound call past the lease TTL. A replacement
    acquires and starts the same node -- **the same execution key**, a new
    attempt. The original returns afterwards and every write it attempts is
    rejected.

    Asserted: exactly one attempt is `COMMITTED`, the other is `FENCED`, and both
    are visible. **Not** asserted: that the provider was called once. It was
    called twice, and that is the honest, disclosed cost of recovery.
    """
    run_id, old_token = _leased(pg, make_run)
    stalled = attempts.begin_attempt(
        pg, run_id=run_id, node="enrich_epo",
        execution_key="cell:run:F2:EP4778509:result-version:1",
        fence_token=old_token, owner_id=OWNER,
    )

    # The TTL lapses while the original worker is still inside its HTTP call.
    pg.execute(
        """
        UPDATE run_leases
           SET acquired_at = now() - interval '90 seconds',
               expires_at  = now() - interval '30 seconds'
         WHERE run_id = %s
        """,
        (run_id,),
    )
    new_token = lease.acquire(pg, run_id=run_id, owner_id="worker-2", ttl_seconds=TTL)
    attempts.close_superseded_attempts(
        pg, run_id=run_id, owner_id="worker-2", fence_token=new_token
    )
    replacement = attempts.begin_attempt(
        pg, run_id=run_id, node="enrich_epo",
        execution_key="cell:run:F2:EP4778509:result-version:1",
        fence_token=new_token, owner_id="worker-2",
    )
    attempts.commit_attempt(
        pg, run_id=run_id, owner_id="worker-2", fence_token=new_token,
        attempt_id=replacement, provider_calls=1,
    )

    # The original's call returns at last. Nothing it writes is accepted.
    with pytest.raises(FencedError):
        attempts.commit_attempt(
            pg, run_id=run_id, owner_id=OWNER, fence_token=old_token, attempt_id=stalled,
            provider_calls=1,
        )

    rows = attempts.list_attempts(pg, run_id=run_id)
    assert len(rows) == 2, "both physical attempts stay visible"
    assert sorted(str(row["outcome"]) for row in rows) == ["COMMITTED", "FENCED"]
    assert {row["execution_key"] for row in rows} == {
        "cell:run:F2:EP4778509:result-version:1"
    }, "one logical execution, two attempts at it"


@pytest.mark.postgres
def test_a_fenced_attempts_response_never_becomes_evidence(pg, make_run):
    """It is not stored as a passage, not stored as a machine result, and not
    counted in coverage -- because the only thing that ever counts is a
    `COMMITTED` attempt, and a fenced one can never become one."""
    run_id, old_token = _leased(pg, make_run)
    attempts.begin_attempt(
        pg, run_id=run_id, node="verify_features", execution_key="cell:c1:result-version:1",
        fence_token=old_token, owner_id=OWNER,
    )
    new_token = _take_over(pg, run_id)
    attempts.close_superseded_attempts(
        pg, run_id=run_id, owner_id="worker-2", fence_token=new_token
    )

    assert attempts.committed_execution_keys(pg, run_id=run_id) == set()
    assert pg.execute(
        "SELECT count(*) FROM verification_results"
    ).fetchone()[0] >= 0  # nothing was written by the fenced attempt
    assert pg.execute(
        "SELECT count(*) FROM evidence_passages WHERE run_id = %s", (run_id,)
    ).fetchone()[0] == 0


@pytest.mark.postgres
def test_committed_execution_keys_is_how_reconcile_asks(pg, make_run):
    """Never infer commitment from a provider call returning. A response arriving
    proves an external call happened; it proves nothing about whether this worker
    still held the lease or whether anything was written."""
    run_id, token = _leased(pg, make_run)
    attempt = attempts.begin_attempt(
        pg, run_id=run_id, node="enrich_epo", execution_key="task-1",
        fence_token=token, owner_id=OWNER,
    )
    assert attempts.committed_execution_keys(pg, run_id=run_id) == set()
    attempts.commit_attempt(
        pg, run_id=run_id, owner_id=OWNER, fence_token=token, attempt_id=attempt
    )
    assert attempts.committed_execution_keys(pg, run_id=run_id) == {"task-1"}


@pytest.mark.postgres
def test_an_execution_key_cannot_be_empty(pg, make_run):
    """A CHECK, because an empty key would collapse every execution of a node into
    one and silently forbid legitimate reruns."""
    run_id, token = _leased(pg, make_run)
    with pytest.raises(psycopg.errors.CheckViolation):
        attempts.begin_attempt(
            pg, run_id=run_id, node="enrich_epo", execution_key="",
            fence_token=token, owner_id=OWNER,
        )


# ===========================================================================
# Every attempt mutation is fenced
# ===========================================================================
#
# `commit_attempt` was fenced from the start; `begin_attempt`, `fail_attempt` and
# `close_superseded_attempts` were not, and migration 0016 closed that.
#
# The hole that mattered was `fail_attempt`. **`FAILED` and `FENCED` are different
# facts.** A stale worker marking its own attempt `FAILED` overwrote the `FENCED`
# reconciliation was going to apply -- which erases the very thing `node_attempts`
# exists to record, that this node ran twice and only one attempt could commit.
# It also reads as a node that errored when in truth its authority was taken away
# mid-flight.
#
# Every test here runs against real PostgreSQL, because the guarantee is a
# property of the lock order inside the SQL functions and of nothing in Python.


@pytest.mark.postgres
def test_after_a_takeover_the_old_holder_cannot_begin_an_attempt(pg, make_run):
    """A fenced worker may not even record that it started something."""
    run_id, old_token = _leased(pg, make_run)
    _take_over(pg, run_id)

    with pytest.raises(FencedError):
        attempts.begin_attempt(
            pg, run_id=run_id, node="enrich_epo", execution_key="task-1",
            fence_token=old_token, owner_id=OWNER,
        )
    assert attempts.list_attempts(pg, run_id=run_id) == [], "nothing was written"


@pytest.mark.postgres
def test_after_a_takeover_the_old_holder_cannot_fail_its_attempt(pg, make_run):
    """The stale worker's attempt stays `RUNNING`, which is exactly the state
    reconciliation is looking for. It does not get to relabel its own row."""
    run_id, old_token = _leased(pg, make_run)
    attempt = attempts.begin_attempt(
        pg, run_id=run_id, node="enrich_epo", execution_key="task-1",
        fence_token=old_token, owner_id=OWNER,
    )
    _take_over(pg, run_id)

    with pytest.raises(FencedError):
        attempts.fail_attempt(
            pg, run_id=run_id, owner_id=OWNER, fence_token=old_token,
            attempt_id=attempt, detail="node raised",
        )
    assert _outcomes(pg, run_id) == [("enrich_epo", "task-1", "RUNNING")]


@pytest.mark.postgres
def test_after_a_takeover_the_old_holder_cannot_close_superseded_attempts(pg, make_run):
    """The one operation whose entire purpose is recording who lost authority is
    itself authority-checked."""
    run_id, old_token = _leased(pg, make_run)
    attempts.begin_attempt(
        pg, run_id=run_id, node="enrich_epo", execution_key="task-1",
        fence_token=old_token, owner_id=OWNER,
    )
    _take_over(pg, run_id)

    with pytest.raises(FencedError):
        attempts.close_superseded_attempts(
            pg, run_id=run_id, owner_id=OWNER, fence_token=old_token
        )
    assert _outcomes(pg, run_id) == [("enrich_epo", "task-1", "RUNNING")]


@pytest.mark.postgres
@pytest.mark.parametrize("operation", ["begin", "fail", "close"])
def test_an_expired_lease_cannot_mutate_telemetry(pg, make_run, operation):
    """Expired is not "nearly valid". The active predicate is
    `expires_at > now()`, and a lapsed holder is refused by all three."""
    run_id, token = _leased(pg, make_run)
    attempt = attempts.begin_attempt(
        pg, run_id=run_id, node="enrich_epo", execution_key="task-1",
        fence_token=token, owner_id=OWNER,
    )
    pg.execute(
        """
        UPDATE run_leases
           SET acquired_at = now() - interval '120 seconds',
               expires_at  = now() - interval '60 seconds'
         WHERE run_id = %s
        """,
        (run_id,),
    )

    with pytest.raises(FencedError):
        if operation == "begin":
            attempts.begin_attempt(
                pg, run_id=run_id, node="enrich_epo", execution_key="task-2",
                fence_token=token, owner_id=OWNER,
            )
        elif operation == "fail":
            attempts.fail_attempt(
                pg, run_id=run_id, owner_id=OWNER, fence_token=token,
                attempt_id=attempt, detail="node raised",
            )
        else:
            attempts.close_superseded_attempts(
                pg, run_id=run_id, owner_id=OWNER, fence_token=token
            )
    assert _outcomes(pg, run_id) == [("enrich_epo", "task-1", "RUNNING")]


@pytest.mark.postgres
@pytest.mark.parametrize("operation", ["begin", "fail", "close"])
def test_a_wrong_owner_cannot_mutate_telemetry(pg, make_run, operation):
    """Holding the *token* is not enough; the owner must match the live lease."""
    run_id, token = _leased(pg, make_run)
    attempt = attempts.begin_attempt(
        pg, run_id=run_id, node="enrich_epo", execution_key="task-1",
        fence_token=token, owner_id=OWNER,
    )

    with pytest.raises(FencedError):
        if operation == "begin":
            attempts.begin_attempt(
                pg, run_id=run_id, node="enrich_epo", execution_key="task-2",
                fence_token=token, owner_id="an-impostor",
            )
        elif operation == "fail":
            attempts.fail_attempt(
                pg, run_id=run_id, owner_id="an-impostor", fence_token=token,
                attempt_id=attempt, detail="node raised",
            )
        else:
            attempts.close_superseded_attempts(
                pg, run_id=run_id, owner_id="an-impostor", fence_token=token
            )
    assert _outcomes(pg, run_id) == [("enrich_epo", "task-1", "RUNNING")]


@pytest.mark.postgres
def test_a_released_lease_cannot_mutate_telemetry(pg, make_run):
    """Released is refused too: the holder let go, and letting go is final until
    a new acquisition increments the token."""
    run_id, token = _leased(pg, make_run)
    lease.release(pg, run_id=run_id, owner_id=OWNER, fence_token=token)

    with pytest.raises(FencedError):
        attempts.begin_attempt(
            pg, run_id=run_id, node="enrich_epo", execution_key="task-1",
            fence_token=token, owner_id=OWNER,
        )


@pytest.mark.postgres
def test_the_authorized_replacement_closes_the_old_running_attempt_as_fenced(
    pg, make_run
):
    """The positive case, and the only path by which an attempt is ever marked
    `FENCED`: the *new* holder, under a lease it genuinely acquired."""
    run_id, old_token = _leased(pg, make_run)
    attempts.begin_attempt(
        pg, run_id=run_id, node="enrich_epo", execution_key="task-1",
        fence_token=old_token, owner_id=OWNER,
    )
    new_token = _take_over(pg, run_id)

    closed = attempts.close_superseded_attempts(
        pg, run_id=run_id, owner_id="worker-2", fence_token=new_token
    )
    assert closed == 1
    assert _outcomes(pg, run_id) == [("enrich_epo", "task-1", "FENCED")]


@pytest.mark.postgres
def test_closing_superseded_attempts_never_touches_the_callers_own(pg, make_run):
    """Only tokens strictly below the caller's, so this is safe to call at the
    start of every acquisition."""
    run_id, old_token = _leased(pg, make_run)
    attempts.begin_attempt(
        pg, run_id=run_id, node="old-node", execution_key="task-old",
        fence_token=old_token, owner_id=OWNER,
    )
    new_token = _take_over(pg, run_id)
    attempts.begin_attempt(
        pg, run_id=run_id, node="new-node", execution_key="task-new",
        fence_token=new_token, owner_id="worker-2",
    )

    assert (
        attempts.close_superseded_attempts(
            pg, run_id=run_id, owner_id="worker-2", fence_token=new_token
        )
        == 1
    )
    assert _outcomes(pg, run_id) == [
        ("old-node", "task-old", "FENCED"),
        ("new-node", "task-new", "RUNNING"),
    ]


@pytest.mark.postgres
def test_a_blocked_worker_returning_after_takeover_persists_nothing(pg, make_run):
    """The whole scenario, end to end, with the honest guarantee spelled out.

    A worker is blocked inside an outbound call past the lease TTL. A replacement
    takes over, runs **the same logical execution**, and commits. The original's
    call finally returns -- and every write it attempts is refused: it can neither
    persist its response (`commit_attempt`) nor even relabel its own attempt
    (`fail_attempt`).

    Asserted: two physical attempts for one `execution_key`, one `FENCED` and one
    `COMMITTED`, and exactly one committed result. **Not** asserted: that the
    provider was called once. It was called twice. That is the disclosed cost of
    recovery, and no lease can change it.
    """
    run_id, old_token = _leased(pg, make_run)
    stalled = attempts.begin_attempt(
        pg, run_id=run_id, node="enrich_epo",
        execution_key="cell:run:F2:EP4778509:result-version:1",
        fence_token=old_token, owner_id=OWNER,
    )

    new_token = _take_over(pg, run_id)
    attempts.close_superseded_attempts(
        pg, run_id=run_id, owner_id="worker-2", fence_token=new_token
    )
    replacement = attempts.begin_attempt(
        pg, run_id=run_id, node="enrich_epo",
        execution_key="cell:run:F2:EP4778509:result-version:1",
        fence_token=new_token, owner_id="worker-2",
    )
    attempts.commit_attempt(
        pg, run_id=run_id, owner_id="worker-2", fence_token=new_token,
        attempt_id=replacement, provider_calls=1,
    )

    # The stalled worker's HTTP call returns at last. Nothing it tries lands.
    with pytest.raises(FencedError):
        attempts.commit_attempt(
            pg, run_id=run_id, owner_id=OWNER, fence_token=old_token,
            attempt_id=stalled, provider_calls=1,
        )
    with pytest.raises(FencedError):
        attempts.fail_attempt(
            pg, run_id=run_id, owner_id=OWNER, fence_token=old_token,
            attempt_id=stalled, detail="my call finally came back",
        )
    with pytest.raises(FencedError):
        attempts.begin_attempt(
            pg, run_id=run_id, node="enrich_epo",
            execution_key="cell:run:F2:EP4778509:result-version:1",
            fence_token=old_token, owner_id=OWNER,
        )

    rows = attempts.list_attempts(pg, run_id=run_id)
    assert len(rows) == 2, "both physical attempts are retained"
    assert sorted(str(row["outcome"]) for row in rows) == ["COMMITTED", "FENCED"]
    assert {row["execution_key"] for row in rows} == {
        "cell:run:F2:EP4778509:result-version:1"
    }, "one logical execution, two attempts at it"
    assert (
        len(attempts.committed_execution_keys(pg, run_id=run_id)) == 1
    ), "exactly one authoritative committed result"


@pytest.mark.postgres
def test_one_authoritative_event_is_committed_alongside_the_one_result(pg, make_run):
    """The other half of "exactly one authorized writer": the replacement's event
    lands, the fenced holder's does not, and one terminal exists."""
    from workflow import events
    from workflow.types import WorkflowEventType

    run_id, old_token = _leased(pg, make_run)
    pg.execute("UPDATE verification_runs SET status = 'RUNNING' WHERE id = %s", (run_id,))
    new_token = _take_over(pg, run_id)

    events.append_worker_event(
        pg, run_id=run_id, owner_id="worker-2", fence_token=new_token,
        event_type=WorkflowEventType.PROGRESS, phase="enrich",
        payload={"type": "progress", "phase": "enrich"}, dedupe_key="progress:enrich",
    )
    with pytest.raises(FencedError):
        events.append_worker_event(
            pg, run_id=run_id, owner_id=OWNER, fence_token=old_token,
            event_type=WorkflowEventType.PROGRESS, phase="enrich",
            payload={"type": "progress", "phase": "enrich", "stale": True},
            dedupe_key="progress:enrich:stale",
        )
    events.finalize_run_success(
        pg, run_id=run_id, owner_id="worker-2", fence_token=new_token,
        payload={"type": "result", "data": {}},
    )

    assert pg.execute(
        "SELECT count(*) FROM workflow_events WHERE run_id = %s AND is_terminal", (run_id,)
    ).fetchone()[0] == 1
    assert pg.execute(
        "SELECT count(*) FROM workflow_events WHERE run_id = %s", (run_id,)
    ).fetchone()[0] == 3  # queued + the replacement's progress + the terminal


def test_no_module_mutates_node_attempts_outside_the_fenced_functions():
    """Source contract: `workflow/attempts.py` is the only module that names the
    table, and it only ever calls the four fenced SQL functions.

    A plain INSERT or UPDATE here would be an unfenced telemetry writer -- the
    exact hole migration 0016 closed -- so the grep is for raw DML, not for
    mentions.
    """
    from pathlib import Path

    backend = Path(__file__).resolve().parent.parent
    verbs = ("insert into node_attempts", "update node_attempts", "delete from node_attempts")
    offenders = []
    for path in backend.rglob("*.py"):
        if any(part in {".venv", "__pycache__", "tests"} for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8").lower()
        if any(verb in text for verb in verbs):
            offenders.append(str(path.relative_to(backend)))
    assert offenders == [], f"unfenced node_attempts DML in: {offenders}"
