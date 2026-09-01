"""Terminal replay: identical retries after the run is already terminal, and
every shape that is a conflict rather than a replay.

**The ordering this file pins.** Inside both finalizers the replay check runs
*before* the lease and status checks, and the obvious order is wrong. Checking
the lease first makes an identical retry impossible: by the time a retry arrives
the run is already `COMPLETED` and the lease has usually been released, so the
retry would fail on status or on `FN001` before it ever reached the dedupe check
-- an "idempotent" path that only works if it is never used.

So the tests below deliberately finalize, release the lease, and *then* retry.
"""

import pytest

from workflow import events, lease
from workflow.types import ReplayConflictError

pytestmark = pytest.mark.postgres

TTL = 60.0
OWNER = "worker-1"

RESULT_PAYLOAD = {"type": "result", "data": {"top_score": 62, "verdict": "Moderate"}}
ERROR_PAYLOAD = {"type": "error", "code": "PROVIDER_UNAVAILABLE", "detail": "EPO 503"}


def _completed(pg, make_run) -> tuple[str, int, int]:
    """A run finalized successfully **and its lease released** -- the state a real
    retry actually arrives in."""
    run_id = make_run()
    token = lease.acquire(pg, run_id=run_id, owner_id=OWNER, ttl_seconds=TTL)
    pg.execute("UPDATE verification_runs SET status = 'RUNNING' WHERE id = %s", (run_id,))
    sequence = events.finalize_run_success(
        pg, run_id=run_id, owner_id=OWNER, fence_token=token, payload=RESULT_PAYLOAD
    )
    lease.release(pg, run_id=run_id, owner_id=OWNER, fence_token=token)
    return run_id, token, sequence


def _failed(pg, make_run) -> tuple[str, int, int]:
    run_id = make_run()
    token = lease.acquire(pg, run_id=run_id, owner_id=OWNER, ttl_seconds=TTL)
    pg.execute("UPDATE verification_runs SET status = 'RUNNING' WHERE id = %s", (run_id,))
    sequence = events.finalize_run_failure(
        pg,
        run_id=run_id,
        owner_id=OWNER,
        fence_token=token,
        payload=ERROR_PAYLOAD,
        failure_code="PROVIDER_UNAVAILABLE",
    )
    lease.release(pg, run_id=run_id, owner_id=OWNER, fence_token=token)
    return run_id, token, sequence


def _event_count(pg, run_id: str) -> int:
    return pg.execute(
        "SELECT count(*) FROM workflow_events WHERE run_id = %s", (run_id,)
    ).fetchone()[0]


# --- a true replay --------------------------------------------------------


def test_an_identical_retry_after_completed_returns_the_same_sequence(pg, make_run):
    run_id, token, first = _completed(pg, make_run)
    before = _event_count(pg, run_id)

    again = events.finalize_run_success(
        pg, run_id=run_id, owner_id=OWNER, fence_token=token, payload=RESULT_PAYLOAD
    )

    assert again == first
    assert _event_count(pg, run_id) == before, "a replay creates nothing"
    assert pg.execute(
        "SELECT status FROM verification_runs WHERE id = %s", (run_id,)
    ).fetchone()[0] == "COMPLETED"


def test_an_identical_retry_after_failed_returns_the_same_sequence(pg, make_run):
    run_id, token, first = _failed(pg, make_run)
    again = events.finalize_run_failure(
        pg,
        run_id=run_id,
        owner_id=OWNER,
        fence_token=token,
        payload=ERROR_PAYLOAD,
        failure_code="PROVIDER_UNAVAILABLE",
    )
    assert again == first
    assert _event_count(pg, run_id) == 2


def test_a_replay_needs_no_lease_at_all(pg, make_run):
    """The whole reason the terminal check precedes the lease check.

    The lease is released *and* superseded by a different holder, and the replay
    still returns the original sequence -- because it is answering "what did this
    run end with?", not asking to write anything.
    """
    run_id, old_token, first = _completed(pg, make_run)
    assert lease.lease_row(pg, run_id=run_id)["owner_id"] is None

    again = events.finalize_run_success(
        pg,
        run_id=run_id,
        owner_id="a-completely-different-worker",
        fence_token=old_token + 99,
        payload=RESULT_PAYLOAD,
    )
    assert again == first


def test_a_valid_replay_mutates_nothing(pg, make_run):
    run_id, token, _ = _completed(pg, make_run)
    before = pg.execute(
        "SELECT status, failure_code, next_sequence, updated_at FROM verification_runs "
        "WHERE id = %s",
        (run_id,),
    ).fetchone()

    events.finalize_run_success(
        pg, run_id=run_id, owner_id=OWNER, fence_token=token, payload=RESULT_PAYLOAD
    )

    assert (
        pg.execute(
            "SELECT status, failure_code, next_sequence, updated_at FROM verification_runs "
            "WHERE id = %s",
            (run_id,),
        ).fetchone()
        == before
    )


# --- every conflicting shape ---------------------------------------------


def test_the_same_dedupe_key_with_a_different_terminal_type_raises_fn003(pg, make_run):
    run_id, token, _ = _completed(pg, make_run)
    with pytest.raises(ReplayConflictError):
        events.finalize_run_failure(
            pg,
            run_id=run_id,
            owner_id=OWNER,
            fence_token=token,
            payload=RESULT_PAYLOAD,
            failure_code=None,
            dedupe_key="terminal:result",
        )


def test_the_same_dedupe_key_with_a_different_payload_raises_fn003(pg, make_run):
    """Returning the stored one would answer a different question than the caller
    asked."""
    run_id, token, _ = _completed(pg, make_run)
    with pytest.raises(ReplayConflictError):
        events.finalize_run_success(
            pg,
            run_id=run_id,
            owner_id=OWNER,
            fence_token=token,
            payload={"type": "result", "data": {"top_score": 99}},
        )


def test_the_same_failure_payload_with_a_different_failure_code_raises_fn003(pg, make_run):
    run_id, token, _ = _failed(pg, make_run)
    with pytest.raises(ReplayConflictError):
        events.finalize_run_failure(
            pg,
            run_id=run_id,
            owner_id=OWNER,
            fence_token=token,
            payload=ERROR_PAYLOAD,
            failure_code="CHECKPOINT_UNAVAILABLE",
        )


def test_a_different_dedupe_key_when_a_terminal_exists_raises_fn003(pg, make_run):
    run_id, token, _ = _completed(pg, make_run)
    with pytest.raises(ReplayConflictError):
        events.finalize_run_success(
            pg,
            run_id=run_id,
            owner_id=OWNER,
            fence_token=token,
            payload=RESULT_PAYLOAD,
            dedupe_key="terminal:result:retry-2",
        )


def test_the_losing_conflicting_retry_leaves_nothing_behind(pg, make_run):
    run_id, token, _ = _completed(pg, make_run)
    before = _event_count(pg, run_id)
    with pytest.raises(ReplayConflictError):
        events.finalize_run_failure(
            pg,
            run_id=run_id,
            owner_id=OWNER,
            fence_token=token,
            payload=ERROR_PAYLOAD,
            failure_code="X",
        )
    assert _event_count(pg, run_id) == before
    assert pg.execute(
        "SELECT status, failure_code FROM verification_runs WHERE id = %s", (run_id,)
    ).fetchone() == ("COMPLETED", None)
