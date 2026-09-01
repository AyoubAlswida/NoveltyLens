"""Replay, snapshot, terminal semantics and reconnection (spec section 9).

**The property every test here serves: a client that disconnects for an hour
reconnects and misses nothing.** That is what makes a Human-in-the-Loop workflow
usable at all -- a gate pause is measured in minutes to days, and no browser tab
stays open across one.

Driven against real PostgreSQL, because every guarantee is a property of the
event log: gapless-within-commit sequences, exactly one terminal row, the
compaction watermark, and ownership on the read. An in-memory double has none of
those and asserting against one would assert nothing.

No test here sleeps on a real clock: `stream_frames` takes an injected sleeper
and a frame bound, so a live tail is exercised without waiting for one.
"""

import json

import pytest

from workflow import events, lease, store, stream
from workflow.types import RunStatus, WorkflowEventType, WorkflowGate

pytestmark = pytest.mark.postgres

OWNER = "testnovelty@gmail.com"
INTRUDER = "someone-else@example.com"
TTL = 60
WORKER = "worker-1"


class Clock:
    """Records what a poll would have waited for, and never waits."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


@pytest.fixture
def run(pg, make_run):
    run_id = make_run(created_by=OWNER)
    token = lease.acquire(pg, run_id=run_id, owner_id=WORKER, ttl_seconds=TTL)
    store.transition_status(
        pg,
        run_id=run_id,
        owner_id=WORKER,
        fence_token=token,
        expected_from=(store.RunStatus.QUEUED,),
        to_status=store.RunStatus.RUNNING,
    )
    return run_id, token


def progress(pg, run_id: str, token: int, *, phase: str, key: str) -> int:
    return events.append_worker_event(
        pg,
        run_id=run_id,
        owner_id=WORKER,
        fence_token=token,
        event_type=WorkflowEventType.PROGRESS,
        phase=phase,
        payload={"phase": phase, "message": f"{phase}…"},
        dedupe_key=key,
    )


def frames(run_id: str, *, last_event_id, max_frames=None, sleeper=None) -> list[dict]:
    """Collect frames as parsed payloads, with their `id:` line."""
    out = []
    for raw in stream.stream_frames(
        run_id=run_id,
        actor_email=OWNER,
        last_event_id=last_event_id,
        max_frames=max_frames,
        sleeper=sleeper or Clock(),
        poll_interval=0.0,
    ):
        head, body = raw.split("\n", 1)
        out.append((int(head.removeprefix("id: ")), json.loads(body.removeprefix("data: ").strip())))
    return out


# --- framing ----------------------------------------------------------------


def test_every_frame_carries_its_persisted_sequence_as_the_id():
    """The `id:` is what makes reconnection correct: the client hands the highest
    one back and replay is `sequence > it`."""
    raw = stream.frame(42, {"type": "progress", "run_id": "r"})
    assert raw.startswith("id: 42\n")
    assert raw.endswith("\n\n"), "an SSE frame ends with a blank line"


def test_every_event_payload_carries_its_run_id_and_type(pg, run, real_db):
    run_id, token = run
    progress(pg, run_id, token, phase="understand", key="p:understand")
    collected = frames(run_id, last_event_id=0, max_frames=1)
    _, payload = collected[0]
    assert payload["run_id"] == run_id
    assert payload["type"] == "progress"


def test_no_frame_carries_an_executor_internal(pg, run, real_db):
    """Spec §9.8: no lease token, no owner id, no checkpoint blob."""
    run_id, token = run
    progress(pg, run_id, token, phase="understand", key="p:understand")
    rendered = json.dumps(frames(run_id, last_event_id=0, max_frames=1))
    for leaked in ("fence_token", "owner_id", "checkpoint", "lease"):
        assert leaked not in rendered


# --- replay -----------------------------------------------------------------


def test_missed_event_replay_delivers_exactly_the_missed_events_in_order(pg, run, real_db):
    """**The core promise.** Disconnect, let the run emit N events, reconnect with
    `Last-Event-ID`, receive exactly those N in order and nothing else."""
    run_id, token = run
    first = progress(pg, run_id, token, phase="understand", key="p:1")
    missed = [
        progress(pg, run_id, token, phase=phase, key=f"p:{phase}")
        for phase in ("queries", "search", "filter")
    ]

    collected = frames(run_id, last_event_id=first, max_frames=len(missed))
    assert [seq for seq, _ in collected] == missed
    assert [p["phase"] for _, p in collected] == ["queries", "search", "filter"]


def test_reconnecting_twice_with_the_same_id_yields_the_same_frames(pg, run, real_db):
    """Replay is a function of the id, not of how many times it has been asked
    for. Anything else makes a retry lossy."""
    run_id, token = run
    first = progress(pg, run_id, token, phase="understand", key="p:1")
    progress(pg, run_id, token, phase="queries", key="p:2")

    once = frames(run_id, last_event_id=first, max_frames=1)
    twice = frames(run_id, last_event_id=first, max_frames=1)
    assert once == twice


def test_no_event_is_delivered_twice_within_one_connection(pg, run, real_db):
    run_id, token = run
    for n, phase in enumerate(("understand", "queries", "search"), start=1):
        progress(pg, run_id, token, phase=phase, key=f"p:{n}")
    collected = frames(run_id, last_event_id=0, max_frames=3)
    sequences = [seq for seq, _ in collected]
    assert sequences == sorted(sequences)
    assert len(sequences) == len(set(sequences))


# --- snapshot ---------------------------------------------------------------


def test_attaching_with_no_id_gets_a_snapshot_first(pg, run, real_db):
    """It is connection-local, never persisted, and its `id:` is the current
    maximum so the client's NEXT reconnect is correct."""
    run_id, token = run
    last = progress(pg, run_id, token, phase="understand", key="p:1")

    collected = frames(run_id, last_event_id=None, max_frames=1)
    sequence, payload = collected[0]
    assert payload["type"] == "snapshot"
    assert sequence == last == payload["last_event_id"]
    assert payload["status"] == "RUNNING"


def test_the_snapshot_is_never_persisted(pg, run, real_db):
    run_id, token = run
    progress(pg, run_id, token, phase="understand", key="p:1")
    frames(run_id, last_event_id=None, max_frames=1)

    stored = {row["event_type"] for row in events.read_events(pg, run_id=run_id, after_sequence=0)}
    assert "snapshot" not in stored


def test_after_a_snapshot_only_subsequent_events_arrive(pg, run, real_db):
    """**Subsequent** is the load-bearing word, and it is why the second event is
    written *after* the snapshot is pulled.

    A snapshot's `id:` is the current maximum sequence, so everything already
    stored is behind it by construction. A client attaching fresh is told "here
    is where things stand"; what it receives next is what happens next.
    """
    run_id, token = run
    progress(pg, run_id, token, phase="understand", key="p:1")

    generator = stream.stream_frames(
        run_id=run_id,
        actor_email=OWNER,
        last_event_id=None,
        sleeper=Clock(),
        poll_interval=0.0,
    )
    first = next(generator)
    assert json.loads(first.split(chr(10), 1)[1].removeprefix("data: ").strip())["type"] == "snapshot"

    later = progress(pg, run_id, token, phase="queries", key="p:2")
    head, body = next(generator).split(chr(10), 1)
    generator.close()

    assert int(head.removeprefix("id: ")) == later
    assert json.loads(body.removeprefix("data: ").strip())["phase"] == "queries"


# --- compaction -------------------------------------------------------------


def test_an_id_below_the_compaction_watermark_gets_a_snapshot(pg, run, real_db):
    """A replay that no longer exists in full must not be served as a partial
    one. That is why the watermark is a column rather than an implicit
    consequence of deletion."""
    run_id, token = run
    progress(pg, run_id, token, phase="understand", key="p:1")
    pg.execute(
        "UPDATE verification_runs SET events_compacted_below = %s WHERE id = %s",
        (99, run_id),
    )

    collected = frames(run_id, last_event_id=1, max_frames=1)
    assert collected[0][1]["type"] == "snapshot"
    assert collected[0][1]["events_compacted_below"] == 99


# --- terminal semantics -----------------------------------------------------


def test_a_terminal_event_ends_the_connection(pg, run, real_db):
    run_id, token = run
    progress(pg, run_id, token, phase="understand", key="p:1")
    events.finalize_run_success(
        pg, run_id=run_id, owner_id=WORKER, fence_token=token, payload={"data": {}}
    )

    collected = frames(run_id, last_event_id=0)
    assert collected[-1][1]["type"] == "result"
    assert sum(1 for _, p in collected if p["type"] in stream.TERMINAL_TYPES) == 1


def test_reconnecting_below_the_terminal_delivers_it_exactly_once(pg, run, real_db):
    """The client had not seen it, and withholding it would strand the UI."""
    run_id, token = run
    first = progress(pg, run_id, token, phase="understand", key="p:1")
    terminal = events.finalize_run_success(
        pg, run_id=run_id, owner_id=WORKER, fence_token=token, payload={"data": {}}
    )
    assert terminal > first

    collected = frames(run_id, last_event_id=first)
    assert [p["type"] for _, p in collected] == ["result"]


def test_reconnecting_at_or_above_the_terminal_delivers_nothing_and_closes(pg, run, real_db):
    """The other direction, and it must not re-emit: at most one terminal frame
    per connection, always."""
    run_id, token = run
    progress(pg, run_id, token, phase="understand", key="p:1")
    terminal = events.finalize_run_success(
        pg, run_id=run_id, owner_id=WORKER, fence_token=token, payload={"data": {}}
    )

    assert frames(run_id, last_event_id=terminal) == []
    assert frames(run_id, last_event_id=terminal + 5) == []


def test_nothing_is_stored_after_the_terminal_sequence(pg, run, real_db):
    run_id, token = run
    terminal = events.finalize_run_success(
        pg, run_id=run_id, owner_id=WORKER, fence_token=token, payload={"data": {}}
    )
    rows = events.read_events(pg, run_id=run_id, after_sequence=0)
    assert max(int(row["sequence"]) for row in rows) == terminal


def test_a_failed_run_persists_one_error_and_no_result(pg, run, real_db):
    run_id, token = run
    events.finalize_run_failure(
        pg,
        run_id=run_id,
        owner_id=WORKER,
        fence_token=token,
        failure_code="PROVIDER_UNAVAILABLE",
        payload={
            "code": "PROVIDER_UNAVAILABLE",
            "phase": "enrich_epo",
            "detail": "EPO OPS returned HTTP 503 for EP4778509 after 3 attempts.",
        },
    )
    collected = frames(run_id, last_event_id=0)
    types = [p["type"] for _, p in collected]
    assert types.count("error") == 1
    assert "result" not in types


# --- pause ends the connection, not the run --------------------------------


def test_paused_ends_the_connection_and_leaves_the_run_waiting(pg, run, real_db):
    """Holding the connection open across a human review would occupy an API
    worker and a database poll for minutes to days to deliver nothing."""
    run_id, token = run
    events.pause_run_for_review(
        pg,
        run_id=run_id,
        owner_id=WORKER,
        fence_token=token,
        gate=WorkflowGate.C,
        review_payload={"gate": "C", "summary": {}, "payload_url": f"/api/runs/{run_id}/gates/C"},
        pause_payload={"gate": "C", "status": "WAITING_FOR_REVIEW"},
        checkpoint_id="ckpt-1",
    )
    collected = frames(run_id, last_event_id=0)
    assert [p["type"] for _, p in collected][-1] == "paused"

    row = pg.execute("SELECT status FROM verification_runs WHERE id = %s", (run_id,)).fetchone()
    assert row[0] == "WAITING_FOR_REVIEW"


# --- delivery across processes ---------------------------------------------


def test_an_event_written_by_another_connection_is_delivered_by_this_stream(pg, run, real_db):
    """The API process serving a stream is usually **not** the worker executing
    the run. Correctness is polling `workflow_events`, so a write on a different
    connection is delivered here with no pub/sub, no shared memory and no sticky
    session."""
    import psycopg
    from config import settings

    run_id, token = run
    with psycopg.connect(settings.database_url, autocommit=True) as other:
        written = progress(other, run_id, token, phase="understand", key="p:other")

    # Replaying from 0 also delivers the seq-1 `queued` event `create_run` wrote,
    # so the assertion is that the OTHER connection's write arrives at all -- not
    # that it is first.
    collected = frames(run_id, last_event_id=0, max_frames=written)
    assert written in [sequence for sequence, _ in collected]


# --- ownership on the stream ------------------------------------------------


def test_another_user_cannot_stream_the_run(pg, run, real_db):
    """`RUN_NOT_FOUND`, the same answer an unknown run gets."""
    from workflow.types import RunNotFoundError

    run_id, _ = run
    with pytest.raises(RunNotFoundError):
        list(
            stream.stream_frames(
                run_id=run_id, actor_email=INTRUDER, last_event_id=0, sleeper=Clock()
            )
        )


# --- a client never matters to the run --------------------------------------


def test_abandoning_the_stream_changes_nothing_about_the_run(pg, run, real_db):
    """No cancellation, no status change, no event. The worker has never heard of
    the connection."""
    run_id, token = run
    progress(pg, run_id, token, phase="understand", key="p:1")

    generator = stream.stream_frames(
        run_id=run_id, actor_email=OWNER, last_event_id=0, sleeper=Clock(), poll_interval=0.0
    )
    next(generator)
    generator.close()

    row = pg.execute("SELECT status FROM verification_runs WHERE id = %s", (run_id,)).fetchone()
    assert row[0] == "RUNNING"
    assert len(events.read_events(pg, run_id=run_id, after_sequence=0)) == 2


def test_the_stream_module_never_consults_client_disconnection():
    """Spec constraint 10: if you find yourself writing `request.is_disconnected()`
    anywhere near the graph, stop."""
    from pathlib import Path

    for name in ("workflow/stream.py", "api/runs.py"):
        text = (Path(__file__).resolve().parent.parent / name).read_text(encoding="utf-8")
        assert "is_disconnected" not in text.replace("`request.is_disconnected()`", "")


# --- backpressure -----------------------------------------------------------


def test_a_stalled_reader_is_disconnected_and_the_run_is_unaffected(pg, run, real_db):
    """Spec §9.5. A client that cannot keep up has its connection closed; the run
    is untouched, and nothing is lost because every frame is still stored.

    The clock is injected, so this measures the rule rather than waiting for it.
    """
    run_id, token = run
    for n, phase in enumerate(("understand", "queries"), start=1):
        progress(pg, run_id, token, phase=phase, key=f"p:{n}")

    ticks = iter([0.0, 999.0, 999.0, 999.0, 999.0])

    generator = stream.stream_frames(
        run_id=run_id,
        actor_email=OWNER,
        last_event_id=0,
        sleeper=Clock(),
        poll_interval=0.0,
        monotonic=lambda: next(ticks),
    )
    next(generator)  # the first frame is handed over
    with pytest.raises(stream.StreamBackpressure):
        next(generator)  # the consumer took 999s to come back for it

    row = pg.execute("SELECT status FROM verification_runs WHERE id = %s", (run_id,)).fetchone()
    assert row[0] == "RUNNING", "backpressure must not touch the run"
    assert len(events.read_events(pg, run_id=run_id, after_sequence=0)) == 3


def test_a_client_that_keeps_up_is_never_disconnected(pg, run, real_db):
    """The positive control: without it the test above could pass against a
    stream that disconnected everybody."""
    run_id, token = run
    progress(pg, run_id, token, phase="understand", key="p:1")
    events.finalize_run_success(
        pg, run_id=run_id, owner_id=WORKER, fence_token=token, payload={"data": {}}
    )
    steady = iter([0.0] * 60)
    collected = list(
        stream.stream_frames(
            run_id=run_id,
            actor_email=OWNER,
            last_event_id=0,
            sleeper=Clock(),
            poll_interval=0.0,
            monotonic=lambda: next(steady),
        )
    )
    assert any('"type":"result"' in raw for raw in collected)


def test_a_historical_pause_does_not_end_a_replay_of_a_finished_run(pg, run, real_db):
    """`paused` ends the connection only while the run is STILL parked.

    The rule exists so a connection is not held open across a review that may
    take days. Once the run has moved past that pause it is history, and stopping
    on it would cost a catching-up client one reconnect per historical pause --
    four for a four-gate run, never reaching the terminal on the first attempt.
    """
    run_id, token = run
    events.pause_run_for_review(
        pg,
        run_id=run_id,
        owner_id=WORKER,
        fence_token=token,
        gate=WorkflowGate.A,
        review_payload={"gate": "A", "summary": {}, "payload_url": "/x"},
        pause_payload={"gate": "A", "status": "WAITING_FOR_REVIEW"},
        checkpoint_id="ckpt-1",
    )
    # The run moves on, exactly as an approval and a worker resume would take it.
    store.begin_resume(
        pg,
        run_id=run_id,
        actor_email=OWNER,
        gate=WorkflowGate.A,
        resume_payload={"gate": "A", "status": "RESUMING"},
        dedupe_key="resumed:A",
    )
    token2 = lease.acquire(pg, run_id=run_id, owner_id=WORKER, ttl_seconds=TTL)
    store.transition_status(
        pg,
        run_id=run_id,
        owner_id=WORKER,
        fence_token=token2,
        expected_from=(RunStatus.RESUMING,),
        to_status=RunStatus.RUNNING,
    )
    events.finalize_run_success(
        pg, run_id=run_id, owner_id=WORKER, fence_token=token2, payload={"data": {}}
    )

    collected = frames(run_id, last_event_id=0)
    types = [payload["type"] for _, payload in collected]
    assert "paused" in types, "the historical pause is still delivered"
    assert types[-1] == "result", f"the replay stopped early: {types}"


def test_a_pause_still_ends_the_connection_while_the_run_is_parked(pg, run, real_db):
    """The positive control for the rule the test above narrows."""
    run_id, token = run
    events.pause_run_for_review(
        pg,
        run_id=run_id,
        owner_id=WORKER,
        fence_token=token,
        gate=WorkflowGate.A,
        review_payload={"gate": "A", "summary": {}, "payload_url": "/x"},
        pause_payload={"gate": "A", "status": "WAITING_FOR_REVIEW"},
        checkpoint_id="ckpt-1",
    )
    collected = frames(run_id, last_event_id=0)
    assert [payload["type"] for _, payload in collected][-1] == "paused"
