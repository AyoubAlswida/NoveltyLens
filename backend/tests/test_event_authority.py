"""Every persisted event declares whose authority wrote it.

There is no generic append function, and this file is what keeps it that way. A
generic `append_workflow_event(run_id, type, ...)` would take no owner and no
fence token, so a worker whose lease had expired could still append progress, a
pause -- or a terminal `result` -- and it would let a caller write a terminal event
without atomically transitioning the run. Both are execution-integrity holes.

Four writers, four authorities:

* `create_run` -- admission, no lease exists yet, the seq-1 `queued` event only.
* `append_worker_event` -- worker, **fenced**, non-terminal, `RUNNING` only.
* `append_api_event` -- API, **ownership-scoped**, `resumed` only.
* `finalize_run_success` / `finalize_run_failure` -- worker, fenced **and
  atomic**, the only writers of `result` and `error`.
"""

from pathlib import Path

import psycopg
import pytest

from workflow import events, lease
from workflow.types import (
    FencedError,
    IllegalStateError,
    ReplayConflictError,
    RunNotFoundError,
    RunStatus,
    TerminalExistsError,
    WorkflowEventType,
    WorkflowGate,
)

BACKEND = Path(__file__).resolve().parent.parent
TTL = 60.0
OWNER = "worker-1"


def _running(pg, make_run) -> tuple[str, int]:
    """A run in the one state a worker event is legal in, with a live lease."""
    run_id = make_run()
    token = lease.acquire(pg, run_id=run_id, owner_id=OWNER, ttl_seconds=TTL)
    pg.execute("UPDATE verification_runs SET status = 'RUNNING' WHERE id = %s", (run_id,))
    return run_id, token


def _progress(run_id: str, message: str = "Reading the invention.") -> dict:
    return {"type": "progress", "run_id": run_id, "phase": "understand", "message": message}


def _event_types(pg, run_id: str) -> list[str]:
    return [
        row[0]
        for row in pg.execute(
            "SELECT event_type FROM workflow_events WHERE run_id = %s ORDER BY sequence",
            (run_id,),
        )
    ]


# --- the worker path ------------------------------------------------------


@pytest.mark.postgres
def test_an_active_worker_appends_a_progress_event(pg, make_run):
    run_id, token = _running(pg, make_run)
    sequence = events.append_worker_event(
        pg,
        run_id=run_id,
        owner_id=OWNER,
        fence_token=token,
        event_type=WorkflowEventType.PROGRESS,
        phase="understand",
        payload=_progress(run_id),
        dedupe_key="progress:understand",
    )
    assert sequence == 2  # 1 is the admission `queued` event
    assert _event_types(pg, run_id) == ["progress", "progress"]


@pytest.mark.postgres
@pytest.mark.parametrize("wrong", ["owner", "token", "expired"])
def test_a_worker_without_a_live_matching_lease_appends_nothing(pg, make_run, wrong):
    run_id, token = _running(pg, make_run)
    owner, used = OWNER, token
    if wrong == "owner":
        owner = "someone-else"
    elif wrong == "token":
        used = token + 5
    else:
        pg.execute(
            """
            UPDATE run_leases
               SET acquired_at = now() - interval '20 seconds',
                   expires_at  = now() - interval '10 seconds'
             WHERE run_id = %s
            """,
            (run_id,),
        )

    with pytest.raises(FencedError):
        events.append_worker_event(
            pg,
            run_id=run_id,
            owner_id=owner,
            fence_token=used,
            event_type=WorkflowEventType.PROGRESS,
            phase="understand",
            payload=_progress(run_id),
            dedupe_key="progress:understand",
        )
    assert _event_types(pg, run_id) == ["progress"], "nothing was appended"


@pytest.mark.postgres
@pytest.mark.parametrize(
    "event_type",
    [
        WorkflowEventType.PROGRESS,
        WorkflowEventType.VERIFICATION_PROGRESS,
        WorkflowEventType.REVIEW_REQUIRED,
        WorkflowEventType.PAUSED,
    ],
)
def test_an_old_worker_after_a_takeover_cannot_append_anything_at_all(
    pg, make_run, event_type
):
    """Not progress, not verification progress, not review-required, not paused.

    A fenced worker persists nothing. Its in-flight provider call may still
    return -- nothing can cancel that -- but its output can never be committed.
    """
    run_id, old_token = _running(pg, make_run)
    pg.execute(
        """
        UPDATE run_leases
           SET acquired_at = now() - interval '20 seconds',
               expires_at  = now() - interval '10 seconds'
         WHERE run_id = %s
        """,
        (run_id,),
    )
    new_token = lease.acquire(pg, run_id=run_id, owner_id="worker-2", ttl_seconds=TTL)
    assert new_token == old_token + 1

    with pytest.raises(FencedError):
        events.append_worker_event(
            pg,
            run_id=run_id,
            owner_id=OWNER,
            fence_token=old_token,
            event_type=event_type,
            phase="understand" if event_type is WorkflowEventType.PROGRESS else None,
            payload={"type": str(event_type), "run_id": run_id},
            dedupe_key=f"{event_type}:after-takeover",
        )
    assert _event_types(pg, run_id) == ["progress"]


@pytest.mark.postgres
@pytest.mark.parametrize("event_type", ["result", "error"])
def test_append_worker_event_rejects_terminals(pg, make_run, event_type):
    """`FN005`. A terminal event exists only as half of a finalization that also
    moves the run to COMPLETED or FAILED, atomically."""
    run_id, token = _running(pg, make_run)
    with pytest.raises(IllegalStateError) as caught:
        events.append_worker_event(
            pg,
            run_id=run_id,
            owner_id=OWNER,
            fence_token=token,
            event_type=WorkflowEventType(event_type),
            phase=None,
            payload={"type": event_type},
            dedupe_key=f"terminal:{event_type}",
        )
    assert "finalize_run_success/failure" in str(caught.value)


@pytest.mark.postgres
def test_append_worker_event_rejects_resumed(pg, make_run):
    """`resumed` describes a human approval the API committed. A worker asserting
    it would be claiming an approval happened."""
    run_id, token = _running(pg, make_run)
    with pytest.raises(IllegalStateError) as caught:
        events.append_worker_event(
            pg,
            run_id=run_id,
            owner_id=OWNER,
            fence_token=token,
            event_type=WorkflowEventType.RESUMED,
            phase=None,
            payload={"type": "resumed"},
            dedupe_key="resumed:A",
        )
    assert "API event" in str(caught.value)


@pytest.mark.postgres
@pytest.mark.parametrize("status", ["QUEUED", "RESUMING", "WAITING_FOR_REVIEW"])
def test_worker_events_are_running_only(pg, make_run, status):
    """A deliberate narrowing, not an oversight.

    The executor transitions QUEUED/RESUMING to RUNNING *before* it executes any
    node, so progress from either is a bug in the executor rather than a state to
    accommodate.
    """
    run_id = make_run()
    token = lease.acquire(pg, run_id=run_id, owner_id=OWNER, ttl_seconds=TTL)
    if status == "RESUMING":
        pg.execute(
            "UPDATE verification_runs SET status = 'RUNNING' WHERE id = %s", (run_id,)
        )
        pg.execute(
            "UPDATE verification_runs SET status = 'WAITING_FOR_REVIEW', current_gate = 'A' "
            "WHERE id = %s",
            (run_id,),
        )
        pg.execute(
            "UPDATE verification_runs SET status = 'RESUMING' WHERE id = %s", (run_id,)
        )
    elif status == "WAITING_FOR_REVIEW":
        pg.execute(
            "UPDATE verification_runs SET status = 'RUNNING' WHERE id = %s", (run_id,)
        )
        pg.execute(
            "UPDATE verification_runs SET status = 'WAITING_FOR_REVIEW', current_gate = 'A' "
            "WHERE id = %s",
            (run_id,),
        )

    with pytest.raises(IllegalStateError) as caught:
        events.append_worker_event(
            pg,
            run_id=run_id,
            owner_id=OWNER,
            fence_token=token,
            event_type=WorkflowEventType.PROGRESS,
            phase="understand",
            payload=_progress(run_id),
            dedupe_key="progress:understand",
        )
    assert "not RUNNING" in str(caught.value)


@pytest.mark.postgres
def test_the_seq_one_queued_event_can_only_come_from_create_run(pg, make_run):
    """The worker path cannot produce it: at seq 1 the run is still QUEUED, and
    `append_worker_event` is RUNNING-only."""
    run_id = make_run()
    token = lease.acquire(pg, run_id=run_id, owner_id=OWNER, ttl_seconds=TTL)
    with pytest.raises(IllegalStateError):
        events.append_worker_event(
            pg,
            run_id=run_id,
            owner_id=OWNER,
            fence_token=token,
            event_type=WorkflowEventType.PROGRESS,
            phase="queued",
            payload={"type": "progress", "phase": "queued"},
            dedupe_key="queued",
        )
    assert pg.execute(
        "SELECT count(*) FROM workflow_events WHERE run_id = %s AND dedupe_key = 'queued'",
        (run_id,),
    ).fetchone()[0] == 1


# --- the pause transaction ------------------------------------------------


@pytest.mark.postgres
def test_pause_appends_both_events_transitions_and_releases_in_one_call(pg, make_run):
    run_id, token = _running(pg, make_run)
    sequence = events.pause_run_for_review(
        pg,
        run_id=run_id,
        owner_id=OWNER,
        fence_token=token,
        gate=WorkflowGate.C,
        review_payload={"type": "review_required", "gate": "C"},
        pause_payload={"type": "paused", "gate": "C"},
        checkpoint_id="cp-42",
    )
    assert _event_types(pg, run_id) == ["progress", "review_required", "paused"]
    assert sequence == 3
    assert pg.execute(
        "SELECT status, current_gate FROM verification_runs WHERE id = %s", (run_id,)
    ).fetchone() == ("WAITING_FOR_REVIEW", "C")
    row = lease.lease_row(pg, run_id=run_id)
    assert row["owner_id"] is None and row["fence_token"] == token


@pytest.mark.postgres
def test_the_pause_transaction_rolls_back_as_a_unit(pg, make_run):
    """No partial pause state survives a forced failure.

    The five effects are one SQL function precisely so they cannot partially
    apply; this drives the whole call inside a transaction that is then rolled
    back, which is the only way to observe the atomicity from outside.
    """
    run_id, token = _running(pg, make_run)
    try:
        with pg.transaction():
            events.pause_run_for_review(
                pg,
                run_id=run_id,
                owner_id=OWNER,
                fence_token=token,
                gate=WorkflowGate.A,
                review_payload={"type": "review_required"},
                pause_payload={"type": "paused"},
                checkpoint_id="cp-1",
            )
            raise RuntimeError("forced failure after the pause, before commit")
    except RuntimeError:
        pass

    assert _event_types(pg, run_id) == ["progress"], "no event survived"
    assert pg.execute(
        "SELECT status FROM verification_runs WHERE id = %s", (run_id,)
    ).fetchone()[0] == "RUNNING", "no transition survived"
    row = lease.lease_row(pg, run_id=run_id)
    assert row["owner_id"] == OWNER, "the release did not survive either"


@pytest.mark.postgres
def test_a_fenced_worker_cannot_pause(pg, make_run):
    run_id, token = _running(pg, make_run)
    with pytest.raises(FencedError):
        events.pause_run_for_review(
            pg,
            run_id=run_id,
            owner_id="someone-else",
            fence_token=token,
            gate=WorkflowGate.A,
            review_payload={"type": "review_required"},
            pause_payload={"type": "paused"},
            checkpoint_id="cp-1",
        )


# --- the API path ---------------------------------------------------------


def _parked(pg, make_run) -> str:
    run_id, token = _running(pg, make_run)
    events.pause_run_for_review(
        pg,
        run_id=run_id,
        owner_id=OWNER,
        fence_token=token,
        gate=WorkflowGate.A,
        review_payload={"type": "review_required"},
        pause_payload={"type": "paused"},
        checkpoint_id="cp-1",
    )
    return run_id


@pytest.mark.postgres
def test_the_api_writer_accepts_resumed_from_the_owner(pg, make_run):
    run_id = _parked(pg, make_run)
    sequence = events.append_api_event(
        pg,
        run_id=run_id,
        actor_email="testnovelty@gmail.com",
        expected_status=RunStatus.WAITING_FOR_REVIEW,
        payload={"type": "resumed", "gate": "A"},
        dedupe_key="resumed:A:1",
    )
    assert sequence == 4
    assert _event_types(pg, run_id)[-1] == "resumed"


@pytest.mark.postgres
@pytest.mark.parametrize(
    "event_type", ["progress", "verification_progress", "review_required", "paused", "result", "error"]
)
def test_the_api_writer_rejects_every_other_event_type(pg, make_run, event_type):
    """A closed set of one. Every other persisted event is admission, worker, or
    terminal, so a generic API writer would be an unnecessary hole in the fencing
    model."""
    run_id = _parked(pg, make_run)
    with pytest.raises(psycopg.Error) as caught:
        pg.execute(
            "SELECT append_api_event(%s, %s, %s, %s, %s, %s)",
            (
                run_id,
                "testnovelty@gmail.com",
                "WAITING_FOR_REVIEW",
                event_type,
                '{"type": "x"}',
                "k",
            ),
        )
    assert caught.value.sqlstate == "FN005"


@pytest.mark.postgres
def test_the_api_writer_rejects_the_wrong_owner(pg, make_run, second_user):
    """`FN004`, and it is deliberately indistinguishable from an unknown run: 403
    would confirm the run exists and let an attacker enumerate ids."""
    run_id = _parked(pg, make_run)
    with pytest.raises(RunNotFoundError):
        events.append_api_event(
            pg,
            run_id=run_id,
            actor_email=second_user,
            expected_status=RunStatus.WAITING_FOR_REVIEW,
            payload={"type": "resumed"},
            dedupe_key="resumed:A:1",
        )
    assert "resumed" not in _event_types(pg, run_id)


@pytest.mark.postgres
def test_the_api_writer_rejects_a_stale_expected_status(pg, make_run):
    """The run is parked exactly where the caller thinks it is, or the write does
    not happen."""
    run_id = _parked(pg, make_run)
    with pytest.raises(IllegalStateError):
        events.append_api_event(
            pg,
            run_id=run_id,
            actor_email="testnovelty@gmail.com",
            expected_status=RunStatus.RUNNING,
            payload={"type": "resumed"},
            dedupe_key="resumed:A:1",
        )


@pytest.mark.postgres
def test_the_api_writer_takes_no_lease(pg, make_run):
    """A parked run has no holder, which is why this path is guarded by ownership
    and `expected_status` instead of by fencing -- and why it can only ever write
    `resumed`."""
    run_id = _parked(pg, make_run)
    assert lease.lease_row(pg, run_id=run_id)["owner_id"] is None
    events.append_api_event(
        pg,
        run_id=run_id,
        actor_email="testnovelty@gmail.com",
        expected_status=RunStatus.WAITING_FOR_REVIEW,
        payload={"type": "resumed"},
        dedupe_key="resumed:A:1",
    )
    assert lease.lease_row(pg, run_id=run_id)["owner_id"] is None


# --- terminal finalization ------------------------------------------------


@pytest.mark.postgres
def test_finalize_success_writes_one_result_and_completes(pg, make_run):
    run_id, token = _running(pg, make_run)
    sequence = events.finalize_run_success(
        pg,
        run_id=run_id,
        owner_id=OWNER,
        fence_token=token,
        payload={"type": "result", "data": {"query_count": 0}},
    )
    assert _event_types(pg, run_id) == ["progress", "result"]
    assert pg.execute(
        "SELECT status FROM verification_runs WHERE id = %s", (run_id,)
    ).fetchone()[0] == "COMPLETED"
    assert sequence == 2


@pytest.mark.postgres
def test_finalize_failure_writes_one_error_and_fails(pg, make_run):
    run_id, token = _running(pg, make_run)
    events.finalize_run_failure(
        pg,
        run_id=run_id,
        owner_id=OWNER,
        fence_token=token,
        payload={"type": "error", "code": "PROVIDER_UNAVAILABLE", "detail": "EPO 503"},
        failure_code="PROVIDER_UNAVAILABLE",
    )
    assert _event_types(pg, run_id) == ["progress", "error"]
    assert pg.execute(
        "SELECT status, failure_code FROM verification_runs WHERE id = %s", (run_id,)
    ).fetchone() == ("FAILED", "PROVIDER_UNAVAILABLE")


@pytest.mark.postgres
def test_a_transaction_failure_mid_finalization_leaves_neither_half(pg, make_run):
    """A client can never observe a COMPLETED run with no result to fetch, nor a
    result with no COMPLETED."""
    run_id, token = _running(pg, make_run)
    try:
        with pg.transaction():
            events.finalize_run_success(
                pg,
                run_id=run_id,
                owner_id=OWNER,
                fence_token=token,
                payload={"type": "result", "data": {}},
            )
            raise RuntimeError("forced failure before commit")
    except RuntimeError:
        pass

    assert _event_types(pg, run_id) == ["progress"]
    assert pg.execute(
        "SELECT status FROM verification_runs WHERE id = %s", (run_id,)
    ).fetchone()[0] == "RUNNING"


@pytest.mark.postgres
@pytest.mark.parametrize("status,event_type", [("COMPLETED", "error"), ("FAILED", "result")])
def test_a_terminal_status_cannot_disagree_with_its_terminal_event(
    pg, make_run, status, event_type
):
    """The trigger makes this true of *any* writer, including a hand-run UPDATE in
    psql -- not only of the finalizers."""
    run_id, _ = _running(pg, make_run)
    pg.execute(
        """
        INSERT INTO workflow_events
            (run_id, sequence, event_type, payload, payload_sha256, dedupe_key, is_terminal)
        VALUES (%s, 50, %s, '{}'::jsonb, 'h', 'terminal', true)
        """,
        (run_id, event_type),
    )
    with pytest.raises(psycopg.errors.CheckViolation) as caught:
        pg.execute(
            "UPDATE verification_runs SET status = %s, failure_code = 'X' WHERE id = %s",
            (status, run_id),
        )
    assert "terminal event is" in str(caught.value)


@pytest.mark.postgres
@pytest.mark.parametrize("status", ["COMPLETED", "FAILED"])
def test_a_run_cannot_become_terminal_before_its_terminal_event_exists(
    pg, make_run, status
):
    run_id, _ = _running(pg, make_run)
    with pytest.raises(psycopg.errors.CheckViolation) as caught:
        pg.execute(
            "UPDATE verification_runs SET status = %s, failure_code = 'X' WHERE id = %s",
            (status, run_id),
        )
    assert "before its terminal event is appended" in str(caught.value)


@pytest.mark.postgres
def test_nothing_of_any_type_can_be_appended_after_a_terminal(pg, make_run):
    run_id, token = _running(pg, make_run)
    events.finalize_run_success(
        pg,
        run_id=run_id,
        owner_id=OWNER,
        fence_token=token,
        payload={"type": "result", "data": {}},
    )
    # The run is COMPLETED so the worker path also fails on status; drive
    # _append_event_locked directly to prove the terminal check is what refuses.
    for event_type in ("progress", "verification_progress", "paused", "resumed"):
        with pytest.raises(psycopg.Error) as caught:
            pg.execute(
                "SELECT _append_event_locked(%s, %s, NULL, '{}'::jsonb, %s)",
                (run_id, event_type, f"after-terminal-{event_type}"),
            )
        assert caught.value.sqlstate == "FN002"


@pytest.mark.postgres
def test_a_second_terminal_of_any_type_is_refused(pg, make_run):
    run_id, token = _running(pg, make_run)
    events.finalize_run_success(
        pg,
        run_id=run_id,
        owner_id=OWNER,
        fence_token=token,
        payload={"type": "result", "data": {}},
    )
    with pytest.raises(ReplayConflictError):
        events.finalize_run_failure(
            pg,
            run_id=run_id,
            owner_id=OWNER,
            fence_token=token,
            payload={"type": "error", "code": "X"},
            failure_code="X",
        )
    assert _event_types(pg, run_id) == ["progress", "result"]


@pytest.mark.postgres
def test_the_one_terminal_index_refuses_a_hand_written_second_terminal(pg, make_run):
    """Belt and braces: the partial unique index holds even against a direct
    INSERT that bypasses every function."""
    run_id, _ = _running(pg, make_run)
    pg.execute(
        """
        INSERT INTO workflow_events
            (run_id, sequence, event_type, payload, payload_sha256, dedupe_key, is_terminal)
        VALUES (%s, 50, 'result', '{}'::jsonb, 'h', 'terminal:result', true)
        """,
        (run_id,),
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        pg.execute(
            """
            INSERT INTO workflow_events
                (run_id, sequence, event_type, payload, payload_sha256, dedupe_key, is_terminal)
            VALUES (%s, 51, 'error', '{}'::jsonb, 'h', 'terminal:error', true)
            """,
            (run_id,),
        )


@pytest.mark.postgres
@pytest.mark.parametrize(
    "event_type,is_terminal", [("progress", True), ("result", False), ("error", False)]
)
def test_is_terminal_must_match_the_event_type(pg, make_run, event_type, is_terminal):
    """`is_terminal` is not an independent fact a caller may assert. It is a
    function of the type, and the CHECK says so."""
    run_id = make_run()
    with pytest.raises(psycopg.errors.CheckViolation):
        pg.execute(
            """
            INSERT INTO workflow_events
                (run_id, sequence, event_type, payload, payload_sha256, dedupe_key, is_terminal)
            VALUES (%s, 50, %s, '{}'::jsonb, 'h', 'k', %s)
            """,
            (run_id, event_type, is_terminal),
        )


@pytest.mark.postgres
def test_a_fenced_worker_can_finalize_neither_success_nor_failure(pg, make_run):
    run_id, old_token = _running(pg, make_run)
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
        events.finalize_run_success(
            pg,
            run_id=run_id,
            owner_id=OWNER,
            fence_token=old_token,
            payload={"type": "result", "data": {}},
        )
    with pytest.raises(FencedError):
        events.finalize_run_failure(
            pg,
            run_id=run_id,
            owner_id=OWNER,
            fence_token=old_token,
            payload={"type": "error", "code": "X"},
            failure_code="X",
        )
    assert _event_types(pg, run_id) == ["progress"]


# --- sequences and dedupe -------------------------------------------------


@pytest.mark.postgres
def test_sequences_are_monotonic(pg, make_run):
    run_id, token = _running(pg, make_run)
    sequences = [
        events.append_worker_event(
            pg,
            run_id=run_id,
            owner_id=OWNER,
            fence_token=token,
            event_type=WorkflowEventType.PROGRESS,
            phase="understand",
            payload=_progress(run_id, f"step {n}"),
            dedupe_key=f"progress:{n}",
        )
        for n in range(5)
    ]
    assert sequences == sorted(sequences)
    assert sequences == [2, 3, 4, 5, 6]


@pytest.mark.postgres
def test_a_dedupe_collision_with_an_identical_payload_is_a_no_op(pg, make_run):
    """This is what makes reconcile safe to run as often as a lease is acquired,
    including after a recovery in which a node was executed twice: two attempts,
    one event."""
    run_id, token = _running(pg, make_run)
    payload = _progress(run_id)
    first = events.append_worker_event(
        pg,
        run_id=run_id,
        owner_id=OWNER,
        fence_token=token,
        event_type=WorkflowEventType.PROGRESS,
        phase="understand",
        payload=payload,
        dedupe_key="progress:understand",
    )
    second = events.append_worker_event(
        pg,
        run_id=run_id,
        owner_id=OWNER,
        fence_token=token,
        event_type=WorkflowEventType.PROGRESS,
        phase="understand",
        payload=payload,
        dedupe_key="progress:understand",
    )
    assert first == second
    assert len(_event_types(pg, run_id)) == 2


@pytest.mark.postgres
def test_a_dedupe_collision_with_a_different_payload_raises_fn003(pg, make_run):
    """A conflict, not a replay. Silently returning the old row would hide it."""
    run_id, token = _running(pg, make_run)
    events.append_worker_event(
        pg,
        run_id=run_id,
        owner_id=OWNER,
        fence_token=token,
        event_type=WorkflowEventType.PROGRESS,
        phase="understand",
        payload=_progress(run_id, "first"),
        dedupe_key="progress:understand",
    )
    with pytest.raises(ReplayConflictError):
        events.append_worker_event(
            pg,
            run_id=run_id,
            owner_id=OWNER,
            fence_token=token,
            event_type=WorkflowEventType.PROGRESS,
            phase="understand",
            payload=_progress(run_id, "different"),
            dedupe_key="progress:understand",
        )


# --- the source contract (always runs) -----------------------------------


def test_only_workflow_events_writes_the_event_table():
    """No module outside `workflow/events.py` names `workflow_events` in SQL.

    The grep is on INSERT/UPDATE/DELETE specifically: `workflow/cleanup.py`
    legitimately deletes compacted rows, and that is the one exception, declared
    here rather than left as an unexplained grep failure waiting to happen.
    """
    allowed = {"workflow/events.py", "workflow/cleanup.py"}
    offenders = []
    for path in BACKEND.rglob("*.py"):
        if any(part in {".venv", "__pycache__", "tests"} for part in path.parts):
            continue
        relative = path.relative_to(BACKEND).as_posix()
        if relative in allowed:
            continue
        text = path.read_text(encoding="utf-8").lower()
        if "workflow_events" in text and any(
            verb in text for verb in ("insert into workflow_events", "update workflow_events", "delete from workflow_events")
        ):
            offenders.append(relative)
    assert offenders == [], f"these modules write workflow_events directly: {offenders}"


def test_there_is_no_generic_event_append_function():
    """The deleted `append_workflow_event(run_id, type, ...)` must not come back.

    It took no owner and no fence token, so a fenced worker could keep writing,
    and it let a terminal event exist without the status transition that belongs
    with it.
    """
    for path in BACKEND.rglob("*.py"):
        # `tests` is excluded because this file itself names the forbidden
        # function in order to search for it.
        if any(part in {".venv", "__pycache__", "tests"} for part in path.parts):
            continue
        assert "def append_workflow_event" not in path.read_text(encoding="utf-8"), (
            f"{path} defines a generic event writer"
        )
    migrations = BACKEND / "migrations"
    for path in migrations.glob("*.sql"):
        assert "FUNCTION append_workflow_event" not in path.read_text(encoding="utf-8")
