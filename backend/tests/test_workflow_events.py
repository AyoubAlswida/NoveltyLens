"""The event log as a **replay** store: sequences, ordering, the reader side, and
compaction with its watermark.

Authority -- who may write what -- lives in `test_workflow_events`'s sibling
`test_event_authority.py`. This file is about the other half: that a client can
reconstruct what it missed.

**The event log is not the checkpoint.** A checkpoint answers "where is
execution?"; this answers "what has the client been told?". The checkpoint
structurally cannot answer the second question, because it holds state rather
than a history of emissions.
"""

import pytest

from workflow import cleanup, events, lease, store
from workflow.types import RunStatus, WorkflowEventType, WorkflowGate

pytestmark = pytest.mark.postgres

TTL = 60.0
OWNER = "worker-1"
ACTOR = "testnovelty@gmail.com"


def _running(pg, make_run) -> tuple[str, int]:
    run_id = make_run()
    token = lease.acquire(pg, run_id=run_id, owner_id=OWNER, ttl_seconds=TTL)
    pg.execute("UPDATE verification_runs SET status = 'RUNNING' WHERE id = %s", (run_id,))
    return run_id, token


def _emit(pg, run_id: str, token: int, n: int) -> int:
    return events.append_worker_event(
        pg,
        run_id=run_id,
        owner_id=OWNER,
        fence_token=token,
        event_type=WorkflowEventType.PROGRESS,
        phase="understand",
        payload={"type": "progress", "phase": "understand", "n": n},
        dedupe_key=f"progress:{n}",
    )


# --- sequences and the reader --------------------------------------------


def test_the_admission_event_is_sequence_one(pg, make_run):
    run_id = make_run()
    rows = events.read_events(pg, run_id=run_id)
    assert [(row["sequence"], row["event_type"], row["phase"]) for row in rows] == [
        (1, "progress", "queued")
    ]


def test_reading_after_a_sequence_returns_exactly_the_rest(pg, make_run):
    """The replay primitive: a reconnecting client sends the last id it saw and
    receives everything after it."""
    run_id, token = _running(pg, make_run)
    for n in range(5):
        _emit(pg, run_id, token, n)

    missed = events.read_events(pg, run_id=run_id, after_sequence=3)
    assert [row["sequence"] for row in missed] == [4, 5, 6]


def test_reading_from_the_max_sequence_returns_nothing(pg, make_run):
    """A client already up to date is sent nothing and the connection closes --
    it is never re-delivered an event it has seen."""
    run_id, token = _running(pg, make_run)
    _emit(pg, run_id, token, 0)
    top = events.max_sequence(pg, run_id=run_id)
    assert events.read_events(pg, run_id=run_id, after_sequence=top) == []


def test_max_sequence_is_what_a_snapshot_frame_carries(pg, make_run):
    """A `snapshot` is connection-local and never persisted, but its `id:` has to
    be the current maximum so the client's *next* reconnect is correct."""
    run_id, token = _running(pg, make_run)
    assert events.max_sequence(pg, run_id=run_id) == 1
    last = _emit(pg, run_id, token, 0)
    assert events.max_sequence(pg, run_id=run_id) == last


def test_snapshot_is_not_a_persisted_event_type(pg):
    """Deliberately absent from the enum. A typo'd event type is a database error
    rather than a row nothing will ever render."""
    labels = {
        row[0]
        for row in pg.execute(
            "SELECT unnest(enum_range(NULL::workflow_event_type))::text"
        )
    }
    assert labels == {
        "progress",
        "verification_progress",
        "review_required",
        "paused",
        "resumed",
        "result",
        "error",
    }
    assert "snapshot" not in labels
    assert {str(member) for member in WorkflowEventType} == labels


def test_the_reader_is_ownership_scoped_through_the_store(pg, make_run, second_user):
    """The check is a separate statement rather than a join: a join would return
    an empty list, which reads to a client as "this run has no events" and quietly
    confirms nothing about whether it exists."""
    from workflow.types import RunNotFoundError

    run_id, token = _running(pg, make_run)
    _emit(pg, run_id, token, 0)

    assert len(store.list_events(pg, run_id=run_id, actor_email=ACTOR)) == 2
    with pytest.raises(RunNotFoundError):
        store.list_events(pg, run_id=run_id, actor_email=second_user)


def test_terminal_event_reads_back_the_single_terminal(pg, make_run):
    run_id, token = _running(pg, make_run)
    assert events.terminal_event(pg, run_id=run_id) is None
    events.finalize_run_success(
        pg,
        run_id=run_id,
        owner_id=OWNER,
        fence_token=token,
        payload={"type": "result", "data": {"query_count": 3}},
    )
    terminal = events.terminal_event(pg, run_id=run_id)
    assert terminal["event_type"] == "result"
    assert terminal["payload"]["data"]["query_count"] == 3


def test_a_rolled_back_transaction_restores_the_sequence_allocator(pg, make_run):
    """Sequences are monotonic and gapless within *committed* transactions -- a
    rollback restores the counter along with everything else. Clients must still
    rely only on monotonicity, never on contiguity."""
    run_id, token = _running(pg, make_run)
    before = store.get_run(pg, run_id=run_id, actor_email=ACTOR)["next_sequence"]
    try:
        with pg.transaction():
            _emit(pg, run_id, token, 0)
            raise RuntimeError("rolled back")
    except RuntimeError:
        pass
    assert store.get_run(pg, run_id=run_id, actor_email=ACTOR)["next_sequence"] == before
    assert _emit(pg, run_id, token, 0) == before + 1


def test_the_full_lifecycle_sequence_of_a_gated_run(pg, make_run):
    """queued, progress, review_required, paused, resumed, progress, result --
    each one written by the authority that is allowed to write it."""
    run_id, token = _running(pg, make_run)
    _emit(pg, run_id, token, 0)
    events.pause_run_for_review(
        pg,
        run_id=run_id,
        owner_id=OWNER,
        fence_token=token,
        gate=WorkflowGate.A,
        review_payload={"type": "review_required", "gate": "A"},
        pause_payload={"type": "paused", "gate": "A"},
        checkpoint_id="cp-1",
    )
    store.begin_resume(
        pg,
        run_id=run_id,
        actor_email=ACTOR,
        gate=WorkflowGate.A,
        resume_payload={"type": "resumed", "gate": "A"},
        dedupe_key="resumed:A:cp-1",
    )
    second_token = lease.acquire(pg, run_id=run_id, owner_id="worker-2", ttl_seconds=TTL)
    store.transition_status(
        pg,
        run_id=run_id,
        owner_id="worker-2",
        fence_token=second_token,
        expected_from=(RunStatus.RESUMING,),
        to_status=RunStatus.RUNNING,
    )
    events.append_worker_event(
        pg,
        run_id=run_id,
        owner_id="worker-2",
        fence_token=second_token,
        event_type=WorkflowEventType.VERIFICATION_PROGRESS,
        phase=None,
        payload={"type": "verification_progress", "completed": 1, "total": 1},
        dedupe_key="cell:c1:v1",
    )
    events.finalize_run_success(
        pg,
        run_id=run_id,
        owner_id="worker-2",
        fence_token=second_token,
        payload={"type": "result", "data": {}},
    )

    assert [row["event_type"] for row in events.read_events(pg, run_id=run_id)] == [
        "progress",
        "progress",
        "review_required",
        "paused",
        "resumed",
        "verification_progress",
        "result",
    ]


# --- compaction and the watermark ----------------------------------------


def _finish(pg, run_id: str, token: int, owner: str = OWNER) -> None:
    events.finalize_run_success(
        pg,
        run_id=run_id,
        owner_id=owner,
        fence_token=token,
        payload={"type": "result", "data": {}},
    )


def test_compaction_removes_only_progress_rows_and_keeps_the_lifecycle(pg, make_run):
    """Lifecycle events -- review required, paused, resumed, and the terminal --
    are kept for as long as the run row exists. They are what somebody may have to
    audit; the progress rows are high-volume telemetry a finished run no longer
    needs frame by frame."""
    run_id, token = _running(pg, make_run)
    for n in range(3):
        _emit(pg, run_id, token, n)
    events.append_worker_event(
        pg,
        run_id=run_id,
        owner_id=OWNER,
        fence_token=token,
        event_type=WorkflowEventType.VERIFICATION_PROGRESS,
        phase=None,
        payload={"type": "verification_progress"},
        dedupe_key="cell:c1",
    )
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
    store.begin_resume(
        pg,
        run_id=run_id,
        actor_email=ACTOR,
        gate=WorkflowGate.A,
        resume_payload={"type": "resumed"},
        dedupe_key="resumed:A:cp-1",
    )
    second = lease.acquire(pg, run_id=run_id, owner_id="worker-2", ttl_seconds=TTL)
    store.transition_status(
        pg,
        run_id=run_id,
        owner_id="worker-2",
        fence_token=second,
        expected_from=(RunStatus.RESUMING,),
        to_status=RunStatus.RUNNING,
    )
    _finish(pg, run_id, second, owner="worker-2")

    removed = cleanup.compact_run(pg, run_id=run_id)
    kept = [row["event_type"] for row in events.read_events(pg, run_id=run_id)]

    assert removed == 5, "four progress rows and one verification_progress"
    assert kept == ["review_required", "paused", "resumed", "result"]


def test_compaction_raises_the_replay_watermark(pg, make_run):
    """The watermark is a column, not an implicit consequence of deletion.

    Deriving it from "the lowest surviving sequence" would be wrong the moment a
    run's oldest surviving event is a lifecycle event that was never deleted --
    which is exactly what compaction leaves behind.
    """
    run_id, token = _running(pg, make_run)
    for n in range(3):
        _emit(pg, run_id, token, n)
    _finish(pg, run_id, token)

    cleanup.compact_run(pg, run_id=run_id)
    run = store.get_run(pg, run_id=run_id, actor_email=ACTOR)
    assert run["events_compacted_below"] == 4, "the highest deleted sequence"

    surviving = events.read_events(pg, run_id=run_id)
    assert [row["sequence"] for row in surviving] == [5]
    assert min(row["sequence"] for row in surviving) > run["events_compacted_below"]


def test_compaction_never_removes_a_terminal_event(pg, make_run):
    """A completed run must still be able to answer "what was the result?" years
    later."""
    run_id, token = _running(pg, make_run)
    _emit(pg, run_id, token, 0)
    _finish(pg, run_id, token)
    cleanup.compact_run(pg, run_id=run_id)
    assert events.terminal_event(pg, run_id=run_id) is not None


def test_compaction_never_touches_a_live_run(pg, make_run):
    run_id, token = _running(pg, make_run)
    _emit(pg, run_id, token, 0)
    assert run_id not in cleanup.compactable_run_ids(pg, retention_days=0)
    assert len(events.read_events(pg, run_id=run_id)) == 2


def test_a_finished_run_is_compactable_once_it_is_old_enough(pg, make_run):
    run_id, token = _running(pg, make_run)
    _finish(pg, run_id, token)
    assert run_id not in cleanup.compactable_run_ids(pg, retention_days=30)
    # updated_at is stamped by the finalizer, so age it rather than waiting.
    pg.execute(
        "UPDATE verification_runs SET updated_at = now() - interval '31 days' WHERE id = %s",
        (run_id,),
    )
    assert run_id in cleanup.compactable_run_ids(pg, retention_days=30)


def test_compacting_a_run_with_nothing_to_remove_is_a_no_op(pg, make_run):
    run_id, token = _running(pg, make_run)
    _finish(pg, run_id, token)
    cleanup.compact_run(pg, run_id=run_id)
    watermark = store.get_run(pg, run_id=run_id, actor_email=ACTOR)["events_compacted_below"]
    assert cleanup.compact_run(pg, run_id=run_id) == 0
    assert (
        store.get_run(pg, run_id=run_id, actor_email=ACTOR)["events_compacted_below"]
        == watermark
    ), "the watermark never moves backwards or drifts on a no-op"
