"""The executor: poll, acquire, reconcile, execute, transition, release --
driven against a **trivial two-node graph with one `interrupt()`** defined here.

Keeping the graph in the test module is deliberate. At this gate there is no
verification graph to run, and a test that waited for one would be testing
nothing; a two-node graph with a gate exercises every transition, the lease, the
reconcile path, the attempt telemetry and the event log, and keeps this work
independent of the gates that build the real one.

Everything here runs against the real Compose PostgreSQL and the real
`PostgresSaver`. The `real_db` fixture undoes conftest's in-memory `db.connect`
patch, because the executor opens its own connections and would otherwise be
handed the dict double.

**No outbound HTTP.** The graph's nodes are pure functions over their state. That
is what makes it safe to run this file in a loop.
"""

import threading
from typing import TypedDict

import psycopg
import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from checkpointer import checkpointer
from workflow import attempts, events, lease, store
from workflow.executor import Executor, GraphAdapter, load_adapter
from workflow.types import (
    ExecutorUnavailable,
    RunStatus,
    WorkflowGate,
)

pytestmark = pytest.mark.postgres

ACTOR = "testnovelty@gmail.com"


# --- the two-node graph under test ---------------------------------------


class _State(TypedDict, total=False):
    """A `TypedDict`, not a bare `dict`.

    With an untyped `dict` schema LangGraph **replaces** the state with each
    node's return value instead of merging per channel, so a key one node wrote
    vanishes the moment the next returns without it. The real `graph/state.py` is
    a `TypedDict` for the same reason.
    """

    steps: list[str]
    approved: str


_CRASH = threading.Event()
"""Set by a test to make `work` raise, standing in for a node that dies
mid-execution. A module-level flag rather than graph state, because it has to
survive into the node without becoming part of what the checkpoint stores."""


def _work(state: _State) -> dict:
    from langgraph.config import get_stream_writer

    if _CRASH.is_set():
        raise RuntimeError("node failed while the executor was driving it")
    get_stream_writer()(
        {"type": "progress", "phase": "understand", "message": "Reading the invention."}
    )
    return {"steps": [*(state.get("steps") or []), "work"]}


def _gate(state: _State) -> dict:
    decision = interrupt({"gate": "A", "question": "Approve?"})
    return {"approved": decision, "steps": [*(state.get("steps") or []), "gate"]}


def _build(saver) -> object:
    builder = StateGraph(_State)
    builder.add_node("work", _work)
    builder.add_node("gate", _gate)
    builder.add_edge(START, "work")
    builder.add_edge("work", "gate")
    builder.add_edge("gate", END)
    return builder.compile(checkpointer=saver)


ADAPTER = GraphAdapter(
    build=_build,
    initial_input=lambda run: {"steps": []},
    result_payload=lambda run, state: {
        "steps": state.get("steps") or [],
        "approved": state.get("approved"),
    },
    # Explicit no-op: this is an executor-only adapter over a two-node toy
    # graph that produces no report, so there is nothing for the standard
    # History model to store. `persist_result` has no default so that saying so
    # is a deliberate line rather than an omission.
    persist_result=lambda conn, run, state, data: None,
    gate_of=lambda value: WorkflowGate((value or {}).get("gate", "A")),
    review_payload=lambda run, gate, value: {"summary": {"question": value.get("question")}},
    resume_value=lambda run, resumed: (resumed or {}).get("payload", {}).get("decision"),
)


@pytest.fixture(autouse=True)
def no_crash():
    """Reset the crash flag around every test, so one test's injected failure
    cannot leak into the next."""
    _CRASH.clear()
    yield
    _CRASH.clear()


@pytest.fixture
def executor(real_db):
    """An executor with a short TTL, so an expiry can be simulated without a
    minute of waiting. The heartbeat stays comfortably below it."""
    return Executor(
        adapter=ADAPTER,
        checkpointer_factory=checkpointer,
        owner_id="test-worker-1",
        ttl_seconds=30,
        heartbeat_seconds=5,
        poll_interval_seconds=0.05,
    )


def _approve(pg, run_id: str, decision: str = "approved") -> None:
    """The API half of a gate approval, exactly as an endpoint would do it."""
    with pg.transaction():
        store.begin_resume(
            pg,
            run_id=run_id,
            actor_email=ACTOR,
            gate=WorkflowGate.A,
            resume_payload={
                "type": "resumed",
                "run_id": run_id,
                "gate": "A",
                "decision": decision,
            },
            dedupe_key="resumed:A:1",
        )


def _types(pg, run_id: str) -> list[str]:
    return [row["event_type"] for row in events.read_events(pg, run_id=run_id)]


# --- the happy path -------------------------------------------------------


def test_a_run_pauses_at_its_gate_and_resumes_to_completion(pg, make_run, executor):
    run_id = make_run()

    assert executor.claim_and_run_one() == run_id
    run = store.get_run(pg, run_id=run_id, actor_email=ACTOR)
    assert run["status"] is RunStatus.WAITING_FOR_REVIEW
    assert run["current_gate"] is WorkflowGate.A
    assert _types(pg, run_id) == ["progress", "progress", "review_required", "paused"]
    assert lease.lease_row(pg, run_id=run_id)["owner_id"] is None, "the pause released it"

    _approve(pg, run_id, "approved-by-a-human")

    assert executor.claim_and_run_one() == run_id
    run = store.get_run(pg, run_id=run_id, actor_email=ACTOR)
    assert run["status"] is RunStatus.COMPLETED
    assert _types(pg, run_id)[-1] == "result"

    terminal = events.terminal_event(pg, run_id=run_id)
    assert terminal["payload"]["data"]["approved"] == "approved-by-a-human", (
        "the human decision reached the graph through Command(resume=...)"
    )


def test_the_executor_never_calls_the_graph_from_the_approval_path(pg, make_run, executor):
    """`begin_resume` commits and returns. Only the executor resumes -- which is
    what makes a duplicate approval harmless and a crash mid-approval
    recoverable."""
    run_id = make_run()
    executor.claim_and_run_one()
    _approve(pg, run_id)

    assert store.get_run(pg, run_id=run_id, actor_email=ACTOR)["status"] is RunStatus.RESUMING
    assert lease.lease_row(pg, run_id=run_id)["owner_id"] is None
    assert _types(pg, run_id)[-1] == "resumed"


def test_a_completed_run_is_not_claimed_again(pg, make_run, executor):
    run_id = make_run()
    executor.claim_and_run_one()
    _approve(pg, run_id)
    executor.claim_and_run_one()
    before = len(_types(pg, run_id))

    assert executor.claim_and_run_one() != run_id
    assert len(_types(pg, run_id)) == before


# --- duplicate executors --------------------------------------------------


def test_two_executors_racing_one_run_yield_one_committed_set_of_events(
    pg, make_run, real_db
):
    """Exactly one acquires; the other gets zero rows and does not execute.

    Asserted: one set of committed events and one authorized writer. **Not**
    asserted: that no external HTTP request is ever duplicated -- no lease can
    deliver that, and claiming it would be a lie about what this design does.
    """
    run_id = make_run()
    first = Executor(
        adapter=ADAPTER, checkpointer_factory=checkpointer, owner_id="w1",
        ttl_seconds=30, heartbeat_seconds=5, poll_interval_seconds=0.05,
    )
    second = Executor(
        adapter=ADAPTER, checkpointer_factory=checkpointer, owner_id="w2",
        ttl_seconds=30, heartbeat_seconds=5, poll_interval_seconds=0.05,
    )
    barrier = threading.Barrier(2)
    claimed: list = []
    lock = threading.Lock()

    def drive(which: Executor) -> None:
        barrier.wait(timeout=10)
        with lock:
            claimed.append(which.claim_and_run_one())

    threads = [threading.Thread(target=drive, args=(e,)) for e in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive()

    assert claimed.count(run_id) == 1, f"exactly one executor drove the run, got {claimed}"
    assert _types(pg, run_id) == ["progress", "progress", "review_required", "paused"]
    assert lease.lease_row(pg, run_id=run_id)["fence_token"] == 1


# --- crash and recovery ---------------------------------------------------


def test_a_node_that_raises_fails_the_run_with_one_sanitized_terminal(
    pg, make_run, executor
):
    """The terminal `error` carries `code` and one safe sentence. Never a
    traceback, never a provider body, never a URL bearing a key."""
    run_id = make_run()
    _CRASH.set()
    executor.claim_and_run_one()

    run = store.get_run(pg, run_id=run_id, actor_email=ACTOR)
    assert run["status"] is RunStatus.FAILED
    assert run["failure_code"] == "RuntimeError"
    terminal = events.terminal_event(pg, run_id=run_id)
    assert terminal["event_type"] == "error"
    detail = terminal["payload"]["detail"]
    # The node's own message no longer survives, and that is the fix rather than a
    # regression: an untrusted exception's text could be carrying a credential, so
    # it is rebuilt from allow-listed facts instead of passed through. See
    # tests/test_workflow_secret_redaction.py for the whole corpus.
    assert detail == "an unexpected RuntimeError occurred."
    assert "Traceback" not in detail and "\n" not in detail
    assert run["failure_detail"] == detail, "the run row carries the same safe string"
    assert lease.lease_row(pg, run_id=run_id)["owner_id"] is None, "the lease was released"


def test_a_failed_node_leaves_a_failed_attempt_and_no_committed_one(
    pg, make_run, executor
):
    run_id = make_run()
    _CRASH.set()
    executor.claim_and_run_one()

    rows = attempts.list_attempts(pg, run_id=run_id)
    assert [str(row["outcome"]) for row in rows] == ["FAILED"]
    assert attempts.committed_execution_keys(pg, run_id=run_id) == set()


def test_a_second_executor_reclaims_an_expired_lease_and_continues(
    pg, make_run, real_db
):
    """Backend termination during execution: the first worker vanishes without
    releasing, its lease expires, and a replacement continues **from the
    committed checkpoint**.

    The killed worker is simulated by claiming and then abandoning without a
    release, which is exactly what a `kill -9` leaves behind.
    """
    run_id = make_run()
    dead = Executor(
        adapter=ADAPTER, checkpointer_factory=checkpointer, owner_id="dead-worker",
        ttl_seconds=30, heartbeat_seconds=5, poll_interval_seconds=0.05,
    )
    # Claim, transition to RUNNING, and walk away -- no release, no pause.
    token = lease.acquire(pg, run_id=run_id, owner_id="dead-worker", ttl_seconds=30)
    store.transition_status(
        pg,
        run_id=run_id,
        owner_id="dead-worker",
        fence_token=token,
        expected_from=(RunStatus.QUEUED,),
        to_status=RunStatus.RUNNING,
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

    replacement = Executor(
        adapter=ADAPTER, checkpointer_factory=checkpointer, owner_id="replacement",
        ttl_seconds=30, heartbeat_seconds=5, poll_interval_seconds=0.05,
    )
    assert replacement.claim_and_run_one() == run_id

    row = lease.lease_row(pg, run_id=run_id)
    assert row["fence_token"] == token + 1, "the takeover incremented the token"
    run = store.get_run(pg, run_id=run_id, actor_email=ACTOR)
    assert run["status"] is RunStatus.WAITING_FOR_REVIEW, "it continued to the gate"
    assert dead.owner_id != replacement.owner_id


def test_the_old_holders_writes_are_rejected_after_the_takeover(pg, make_run, real_db):
    """The fenced worker returns to find every write refused."""
    from workflow.types import FencedError

    run_id = make_run()
    old_token = lease.acquire(pg, run_id=run_id, owner_id="dead-worker", ttl_seconds=30)
    store.transition_status(
        pg,
        run_id=run_id,
        owner_id="dead-worker",
        fence_token=old_token,
        expected_from=(RunStatus.QUEUED,),
        to_status=RunStatus.RUNNING,
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
    Executor(
        adapter=ADAPTER, checkpointer_factory=checkpointer, owner_id="replacement",
        ttl_seconds=30, heartbeat_seconds=5, poll_interval_seconds=0.05,
    ).claim_and_run_one()

    with pytest.raises(FencedError):
        events.finalize_run_success(
            pg,
            run_id=run_id,
            owner_id="dead-worker",
            fence_token=old_token,
            payload={"type": "result", "data": {}},
        )


def test_a_run_recovered_mid_execution_does_not_repeat_a_committed_node(
    pg, make_run, real_db
):
    """Rule 2 of the recovery rules: never repeat a node whose successful
    checkpoint is already committed. The checkpoint is the record of what
    finished.

    `work` runs once and commits its checkpoint; the run is then abandoned
    `RUNNING`; a replacement continues and reaches the gate having run `work`
    exactly once.
    """
    run_id = make_run()
    executor = Executor(
        adapter=ADAPTER, checkpointer_factory=checkpointer, owner_id="w1",
        ttl_seconds=30, heartbeat_seconds=5, poll_interval_seconds=0.05,
    )
    executor.claim_and_run_one()  # pauses at the gate

    work_attempts = [
        row for row in attempts.list_attempts(pg, run_id=run_id) if row["node"] == "work"
    ]
    assert len(work_attempts) == 1
    assert str(work_attempts[0]["outcome"]) == "COMMITTED"

    _approve(pg, run_id)
    Executor(
        adapter=ADAPTER, checkpointer_factory=checkpointer, owner_id="w2",
        ttl_seconds=30, heartbeat_seconds=5, poll_interval_seconds=0.05,
    ).claim_and_run_one()

    work_attempts = [
        row for row in attempts.list_attempts(pg, run_id=run_id) if row["node"] == "work"
    ]
    assert len(work_attempts) == 1, "a committed node is never re-executed"
    assert store.get_run(pg, run_id=run_id, actor_email=ACTOR)["status"] is RunStatus.COMPLETED


def test_the_paused_gate_attempt_is_closed_as_fenced_by_the_next_holder(
    pg, make_run, real_db
):
    """A task that ends at an `interrupt()` neither failed nor committed -- it
    paused, and the pause released the lease.

    It is left `RUNNING` and closed as `FENCED` by the **next** holder under its
    own valid lease, which is the one place a superseded attempt may be marked at
    all. A fenced worker must never write telemetry using the authority it just
    lost.
    """
    run_id = make_run()
    Executor(
        adapter=ADAPTER, checkpointer_factory=checkpointer, owner_id="w1",
        ttl_seconds=30, heartbeat_seconds=5, poll_interval_seconds=0.05,
    ).claim_and_run_one()

    gate_attempts = [
        row for row in attempts.list_attempts(pg, run_id=run_id) if row["node"] == "gate"
    ]
    assert [str(row["outcome"]) for row in gate_attempts] == ["RUNNING"]

    _approve(pg, run_id)
    Executor(
        adapter=ADAPTER, checkpointer_factory=checkpointer, owner_id="w2",
        ttl_seconds=30, heartbeat_seconds=5, poll_interval_seconds=0.05,
    ).claim_and_run_one()

    gate_attempts = [
        row for row in attempts.list_attempts(pg, run_id=run_id) if row["node"] == "gate"
    ]
    assert [str(row["outcome"]) for row in gate_attempts] == ["FENCED", "COMMITTED"]
    assert len({row["execution_key"] for row in gate_attempts}) == 1, (
        "one logical execution, two physical attempts"
    )


# --- reconciliation of the pause crash boundaries ------------------------


def test_reconcile_repairs_a_checkpoint_committed_without_its_pause_transaction(
    pg, make_run, real_db
):
    """Crash boundary 1 of 3: the interrupt is in the checkpoint, the run still
    reads `RUNNING`, and no pause events exist.

    LangGraph commits its checkpoint in **its own** transaction, which this work
    does not join, so this gap is real rather than hypothetical. Reconcile reads
    the checkpoint, sees the interrupt, and calls `pause_run_for_review` with the
    same `checkpoint_id` -- repairing forward only.
    """
    run_id = make_run()
    token = lease.acquire(pg, run_id=run_id, owner_id="w1", ttl_seconds=30)
    store.transition_status(
        pg,
        run_id=run_id,
        owner_id="w1",
        fence_token=token,
        expected_from=(RunStatus.QUEUED,),
        to_status=RunStatus.RUNNING,
    )
    # Drive the graph to its interrupt directly, so LangGraph's checkpoint is
    # committed and none of our pause transaction ever ran.
    with checkpointer() as saver:
        graph = _build(saver)
        for _ in graph.stream({"steps": []}, {"configurable": {"thread_id": run_id}}):
            pass
    pg.execute(
        """
        UPDATE run_leases
           SET acquired_at = now() - interval '120 seconds',
               expires_at  = now() - interval '60 seconds'
         WHERE run_id = %s
        """,
        (run_id,),
    )
    assert store.get_run(pg, run_id=run_id, actor_email=ACTOR)["status"] is RunStatus.RUNNING
    assert _types(pg, run_id) == ["progress"], "no pause events exist yet"

    Executor(
        adapter=ADAPTER, checkpointer_factory=checkpointer, owner_id="w2",
        ttl_seconds=30, heartbeat_seconds=5, poll_interval_seconds=0.05,
    ).claim_and_run_one()

    run = store.get_run(pg, run_id=run_id, actor_email=ACTOR)
    assert run["status"] is RunStatus.WAITING_FOR_REVIEW
    assert run["current_gate"] is WorkflowGate.A
    assert _types(pg, run_id) == ["progress", "review_required", "paused"]


def _drive_to_the_interrupt(pg, run_id: str) -> str:
    """Put a run in the exact shape a crash between LangGraph's commit and ours
    leaves behind: RUNNING, leased, interrupt in the checkpoint, no pause events.

    Returns the committed `checkpoint_id`, which is what every pause dedupe key is
    a function of -- that is what makes the repair a replay rather than a second
    write.
    """
    token = lease.acquire(pg, run_id=run_id, owner_id="w1", ttl_seconds=30)
    store.transition_status(
        pg,
        run_id=run_id,
        owner_id="w1",
        fence_token=token,
        expected_from=(RunStatus.QUEUED,),
        to_status=RunStatus.RUNNING,
    )
    with checkpointer() as saver:
        graph = _build(saver)
        config = {"configurable": {"thread_id": run_id}}
        for _ in graph.stream({"steps": []}, config):
            pass
        checkpoint_id = graph.get_state(config).config["configurable"]["checkpoint_id"]
    pg.execute(
        """
        UPDATE run_leases
           SET acquired_at = now() - interval '120 seconds',
               expires_at  = now() - interval '60 seconds'
         WHERE run_id = %s
        """,
        (run_id,),
    )
    return str(checkpoint_id)


def _pause_payloads(run_id: str, checkpoint_id: str) -> tuple[dict, dict]:
    """The exact payloads `Executor._pause` will write for this run.

    Reproduced here rather than imported, because the point of the boundary-2 and
    boundary-3 tests is that a *pre-existing* row and the one reconcile would
    write are byte-identical -- if they were not, the dedupe key would raise
    `FN003` instead of replaying, which is also correct behaviour but a different
    property.
    """
    review = {
        "summary": {"question": "Approve?"},
        "type": "review_required",
        "run_id": run_id,
        "gate": "A",
        "checkpoint_id": checkpoint_id,
    }
    pause = {
        "type": "paused",
        "run_id": run_id,
        "gate": "A",
        "status": "WAITING_FOR_REVIEW",
    }
    return review, pause


def _reconcile(run_id: str) -> None:
    Executor(
        adapter=ADAPTER, checkpointer_factory=checkpointer, owner_id="w2",
        ttl_seconds=30, heartbeat_seconds=5, poll_interval_seconds=0.05,
    ).claim_and_run_one()


def test_reconcile_repairs_review_required_without_paused(pg, make_run, real_db):
    """Crash boundary 2 of 3.

    Unreachable while the pause is one SQL function, and covered anyway: a future
    refactor that split it would otherwise reintroduce it silently, and a database
    restored from a partial backup can present this shape.
    """
    run_id = make_run()
    checkpoint_id = _drive_to_the_interrupt(pg, run_id)
    review, _ = _pause_payloads(run_id, checkpoint_id)
    pg.execute(
        "SELECT _append_event_locked(%s, 'review_required', NULL, %s, %s)",
        (
            run_id,
            psycopg.types.json.Jsonb(review),
            f"review_required:A:{checkpoint_id}",
        ),
    )
    assert _types(pg, run_id) == ["progress", "review_required"]

    _reconcile(run_id)

    assert _types(pg, run_id) == ["progress", "review_required", "paused"], (
        "the existing key replayed as a no-op; the missing one was appended once"
    )
    assert store.get_run(pg, run_id=run_id, actor_email=ACTOR)["status"] is (
        RunStatus.WAITING_FOR_REVIEW
    )


def test_reconcile_repairs_both_events_without_the_transition(pg, make_run, real_db):
    """Crash boundary 3 of 3: both events committed, status transition absent.

    Both keys replay as no-ops and the guarded `RUNNING -> WAITING_FOR_REVIEW`
    applies exactly once. Forward only -- no rewritten payload, no renumbered
    sequence.
    """
    run_id = make_run()
    checkpoint_id = _drive_to_the_interrupt(pg, run_id)
    review, pause = _pause_payloads(run_id, checkpoint_id)
    for event_type, payload, key in (
        ("review_required", review, f"review_required:A:{checkpoint_id}"),
        ("paused", pause, f"paused:A:{checkpoint_id}"),
    ):
        pg.execute(
            "SELECT _append_event_locked(%s, %s, NULL, %s, %s)",
            (run_id, event_type, psycopg.types.json.Jsonb(payload), key),
        )
    before = events.read_events(pg, run_id=run_id)
    assert store.get_run(pg, run_id=run_id, actor_email=ACTOR)["status"] is RunStatus.RUNNING

    _reconcile(run_id)

    after = events.read_events(pg, run_id=run_id)
    assert [row["sequence"] for row in after] == [row["sequence"] for row in before], (
        "no duplicate event and no renumbered sequence"
    )
    assert [row["payload"] for row in after] == [row["payload"] for row in before], (
        "and never a rewritten payload"
    )
    assert store.get_run(pg, run_id=run_id, actor_email=ACTOR)["status"] is (
        RunStatus.WAITING_FOR_REVIEW
    )


def test_a_run_marked_running_with_no_checkpoint_starts_rather_than_failing(
    pg, make_run, real_db
):
    """A previous holder committed `QUEUED -> RUNNING` and died before the graph
    produced its first checkpoint.

    Resuming that thread with `None` would raise `EmptyInputError`. The honest
    recovery is to start it: nothing has run yet, so nothing is being repeated
    beyond what recovery rule 3 already permits.
    """
    run_id = make_run()
    token = lease.acquire(pg, run_id=run_id, owner_id="dead", ttl_seconds=30)
    store.transition_status(
        pg,
        run_id=run_id,
        owner_id="dead",
        fence_token=token,
        expected_from=(RunStatus.QUEUED,),
        to_status=RunStatus.RUNNING,
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

    _reconcile(run_id)

    run = store.get_run(pg, run_id=run_id, actor_email=ACTOR)
    assert run["status"] is RunStatus.WAITING_FOR_REVIEW
    assert run["failure_code"] is None
    assert _types(pg, run_id) == ["progress", "progress", "review_required", "paused"]


# --- configuration refusals ----------------------------------------------


@pytest.mark.parametrize(
    "ttl,heartbeat", [(0, 5), (-1, 5), (60, 0), (60, -1), (30, 30), (30, 45)]
)
def test_an_invalid_lease_configuration_refuses_to_build_an_executor(ttl, heartbeat):
    """A heartbeat at or above the TTL guarantees the lease lapses between beats
    and the worker fences itself mid-run. The value is never silently replaced
    with a default."""
    from config import ConfigError

    with pytest.raises(ConfigError):
        Executor(
            adapter=ADAPTER,
            checkpointer_factory=checkpointer,
            ttl_seconds=ttl,
            heartbeat_seconds=heartbeat,
        )


def test_no_graph_adapter_configured_refuses_to_start():
    """An executor with no graph refuses to start rather than admitting runs it
    cannot advance. There is deliberately no default adapter."""
    with pytest.raises(ExecutorUnavailable) as caught:
        load_adapter("")
    assert "no default" in str(caught.value)

    with pytest.raises(ExecutorUnavailable):
        load_adapter("not-a-module-spec")
    with pytest.raises(ExecutorUnavailable):
        load_adapter("workflow.executor:Executor")  # a class, not a GraphAdapter


def test_the_mock_adapter_is_a_real_graph_adapter():
    """`MockExecutor` is `Executor` bound to `MOCK_ADAPTER` -- one executor, one
    state machine, no second implementation to drift."""
    from workflow.executor import MOCK_ADAPTER, MockExecutor

    assert isinstance(MOCK_ADAPTER, GraphAdapter)
    assert isinstance(
        MockExecutor(checkpointer_factory=checkpointer, owner_id="x"), Executor
    )
