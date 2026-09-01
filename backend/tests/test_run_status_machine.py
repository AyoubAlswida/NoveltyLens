"""The run state machine as a **database property**, not a convention.

Every assertion here runs against the real Compose PostgreSQL, because every one
of them is a property of migration 0006's trigger and its `run_status_transitions`
table. The in-memory double reproduces the vocabulary; it cannot reproduce a
trigger, so asserting these against it would be asserting nothing.

Two things worth understanding before editing:

* **Creation is not a transition.** A run is born `QUEUED` by *insert invariant*.
  The transition table describes updates only -- a `(NULL, 'QUEUED')` creation row
  cannot exist, because a primary-key column is implicitly NOT NULL, and
  inventing a synthetic `NEW`/`CREATING` state to satisfy the constraint would
  put a value in the enum that nothing ever observes at runtime.
* **`COMPLETED` and `FAILED` appear only as targets.** They have no outgoing rows
  at all, which is what makes "terminal" enforced rather than documented.
"""

import psycopg
import pytest

from workflow.types import ALLOWED_TRANSITIONS, RunStatus

pytestmark = pytest.mark.postgres


def _status(pg, run_id: str) -> str:
    return pg.execute(
        "SELECT status FROM verification_runs WHERE id = %s", (run_id,)
    ).fetchone()[0]


def _force_status(pg, run_id: str, status: str) -> None:
    """Put a run into a starting state for a transition test.

    Walks a legal path rather than writing the status directly, because a direct
    write would have to defeat the very trigger under test. Terminal states are
    deliberately unreachable here -- they come only from the finalizers, and
    `test_terminal_replay.py` covers those.
    """
    paths = {
        "QUEUED": [],
        "RUNNING": ["RUNNING"],
        "WAITING_FOR_REVIEW": ["RUNNING", "WAITING_FOR_REVIEW"],
        "RESUMING": ["RUNNING", "WAITING_FOR_REVIEW", "RESUMING"],
    }
    for step in paths[status]:
        gate = "A" if step == "WAITING_FOR_REVIEW" else None
        pg.execute(
            """
            UPDATE verification_runs
               SET status = %s, current_gate = COALESCE(%s, current_gate)
             WHERE id = %s
            """,
            (step, gate, run_id),
        )


# --- the insert invariant -------------------------------------------------


def test_a_run_is_born_queued(pg, make_run):
    """`create_run` produces a `QUEUED` run, its lease row and its seq-1 event."""
    run_id = make_run()
    assert _status(pg, run_id) == "QUEUED"
    assert pg.execute(
        "SELECT fence_token, owner_id FROM run_leases WHERE run_id = %s", (run_id,)
    ).fetchone() == (0, None)
    assert pg.execute(
        "SELECT sequence, event_type, phase FROM workflow_events WHERE run_id = %s",
        (run_id,),
    ).fetchall() == [(1, "progress", "queued")]


@pytest.mark.parametrize(
    "status", ["RUNNING", "WAITING_FOR_REVIEW", "RESUMING", "COMPLETED", "FAILED"]
)
def test_a_run_cannot_be_inserted_in_any_other_status(pg, created_runs, status):
    """A run inserted directly as RUNNING or COMPLETED would bypass the lease and
    the event log entirely, so the insert invariant refuses every status but
    QUEUED."""
    with pytest.raises(psycopg.errors.CheckViolation) as caught:
        pg.execute(
            """
            INSERT INTO verification_runs
                (description, date_policy, text_version_policy, thread_id, created_by, status)
            VALUES ('x', 'p', 't', %s, 'testnovelty@gmail.com', %s)
            """,
            (f"thread-{status}", status),
        )
    assert "must be inserted as QUEUED" in str(caught.value)


def test_the_transition_table_has_no_creation_row(pg):
    """There is no `(NULL, 'QUEUED')` row and there cannot be: `from_status` is
    half of a primary key and therefore implicitly NOT NULL."""
    assert pg.execute(
        "SELECT count(*) FROM run_status_transitions WHERE from_status IS NULL"
    ).fetchone()[0] == 0
    assert pg.execute(
        """
        SELECT a.attnotnull FROM pg_attribute a
          JOIN pg_class c ON c.oid = a.attrelid
         WHERE c.relname = 'run_status_transitions' AND a.attname = 'from_status'
        """
    ).fetchone()[0] is True


# --- the update table -----------------------------------------------------


def test_python_and_sql_transition_tables_agree(pg):
    """`workflow.types.ALLOWED_TRANSITIONS` is a mirror, and this is what keeps it
    honest. The database is the authority; a Python copy that drifted would let
    code reason about a transition the trigger rejects."""
    in_sql = {
        (row[0], row[1])
        for row in pg.execute("SELECT from_status, to_status FROM run_status_transitions")
    }
    in_python = {(str(a), str(b)) for a, b in ALLOWED_TRANSITIONS}
    assert in_sql == in_python


@pytest.mark.parametrize(
    "from_status,to_status",
    sorted((str(a), str(b)) for a, b in ALLOWED_TRANSITIONS),
)
def test_every_listed_transition_is_permitted(pg, make_run, from_status, to_status):
    """All nine listed pairs succeed.

    The two terminal targets are written through the finalizers in production; the
    trigger under test here is the *transition* rule, and the terminal-agreement
    trigger is exercised separately in `test_workflow_events.py`. So a terminal
    target gets its terminal event appended first, which is exactly the ordering
    the finalizers use.
    """
    run_id = make_run()
    _force_status(pg, run_id, from_status)
    if to_status in ("COMPLETED", "FAILED"):
        event_type = "result" if to_status == "COMPLETED" else "error"
        pg.execute(
            """
            INSERT INTO workflow_events
                (run_id, sequence, event_type, payload, payload_sha256, dedupe_key, is_terminal)
            VALUES (%s, 99, %s, '{}'::jsonb, 'hash', 'terminal', true)
            """,
            (run_id, event_type),
        )
    pg.execute(
        """
        UPDATE verification_runs
           SET status = %s, current_gate = COALESCE(current_gate, 'A'), failure_code = %s
         WHERE id = %s
        """,
        (to_status, "TEST" if to_status == "FAILED" else None, run_id),
    )
    assert _status(pg, run_id) == to_status


@pytest.mark.parametrize(
    "from_status,to_status",
    sorted(
        (a, b)
        for a in [str(s) for s in RunStatus]
        for b in [str(s) for s in RunStatus]
        if a != b
        and (a, b) not in {(str(x), str(y)) for x, y in ALLOWED_TRANSITIONS}
        and a in ("QUEUED", "RUNNING", "WAITING_FOR_REVIEW", "RESUMING")
    ),
)
def test_every_unlisted_transition_is_rejected(pg, make_run, from_status, to_status):
    """Every pair outside the table raises. This is the half that matters: a state
    machine is defined by what it refuses."""
    run_id = make_run()
    _force_status(pg, run_id, from_status)
    with pytest.raises(psycopg.errors.CheckViolation) as caught:
        pg.execute(
            "UPDATE verification_runs SET status = %s WHERE id = %s", (to_status, run_id)
        )
    assert "illegal run status transition" in str(caught.value)


@pytest.mark.parametrize("terminal", ["COMPLETED", "FAILED"])
@pytest.mark.parametrize(
    "target", ["QUEUED", "RUNNING", "WAITING_FOR_REVIEW", "RESUMING", "COMPLETED", "FAILED"]
)
def test_terminal_statuses_have_no_outgoing_transitions(pg, make_run, terminal, target):
    """Any update away from `COMPLETED` or `FAILED` raises -- including to the
    other terminal. The run is over."""
    if target == terminal:
        pytest.skip("not a transition: the status is unchanged")
    run_id = make_run()
    _force_status(pg, run_id, "RUNNING")
    event_type = "result" if terminal == "COMPLETED" else "error"
    pg.execute(
        """
        INSERT INTO workflow_events
            (run_id, sequence, event_type, payload, payload_sha256, dedupe_key, is_terminal)
        VALUES (%s, 99, %s, '{}'::jsonb, 'hash', 'terminal', true)
        """,
        (run_id, event_type),
    )
    pg.execute(
        "UPDATE verification_runs SET status = %s, failure_code = 'TEST' WHERE id = %s",
        (terminal, run_id),
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        pg.execute(
            "UPDATE verification_runs SET status = %s WHERE id = %s", (target, run_id)
        )
    assert _status(pg, run_id) == terminal


def test_no_transition_row_targets_a_terminal_source(pg):
    """`COMPLETED` and `FAILED` appear only as `to_status`, never as `from_status`."""
    assert pg.execute(
        "SELECT count(*) FROM run_status_transitions "
        "WHERE from_status IN ('COMPLETED', 'FAILED')"
    ).fetchone()[0] == 0


def test_lease_takeover_is_not_in_the_transition_table(pg):
    """There is no row for it because a takeover **does not change status**.

    Status tracks workflow position; the lease tracks who is allowed to advance
    it. Conflating them produces a state machine with holes -- a `RUNNING` run
    whose lease expired is reclaimable, and the new owner keeps the status and
    continues from the checkpoint.
    """
    self_edges = pg.execute(
        "SELECT count(*) FROM run_status_transitions WHERE from_status = to_status"
    ).fetchone()[0]
    assert self_edges == 0


def test_waiting_for_review_requires_a_gate(pg, make_run):
    """A parked run that cannot say which gate it is parked at is not reviewable,
    so the CHECK refuses it."""
    run_id = make_run()
    pg.execute("UPDATE verification_runs SET status = 'RUNNING' WHERE id = %s", (run_id,))
    with pytest.raises(psycopg.errors.CheckViolation):
        pg.execute(
            "UPDATE verification_runs SET status = 'WAITING_FOR_REVIEW', current_gate = NULL "
            "WHERE id = %s",
            (run_id,),
        )


def test_failed_requires_a_failure_code(pg, make_run):
    """A `FAILED` run with no code cannot tell anyone what went wrong."""
    run_id = make_run()
    pg.execute(
        """
        INSERT INTO workflow_events
            (run_id, sequence, event_type, payload, payload_sha256, dedupe_key, is_terminal)
        VALUES (%s, 99, 'error', '{}'::jsonb, 'hash', 'terminal', true)
        """,
        (run_id,),
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        pg.execute(
            "UPDATE verification_runs SET status = 'FAILED' WHERE id = %s", (run_id,)
        )
