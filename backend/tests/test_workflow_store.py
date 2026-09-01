"""The workflow store: ownership scoping, guarded transitions, the API resume
half, and the source contracts that keep the surface narrow.

**Authentication is not authorization.** Every run-scoped function filters on
`actor_email` in the same query that fetches the run -- never fetch-then-compare
in Python, where a missed comparison is a data leak while a missed `WHERE` clause
is merely a no-result. Another user's run answers `RUN_NOT_FOUND`, never 403: 403
confirms the run exists and lets an attacker enumerate ids, and the frontend
treats 401 alone as "session gone", so 403 would be a dead status anyway.
"""

from pathlib import Path

import pytest

from workflow import lease, store
from workflow.types import (
    FencedError,
    IllegalStateError,
    RunNotFoundError,
    RunStatus,
    WorkflowGate,
)

BACKEND = Path(__file__).resolve().parent.parent
TTL = 60.0
OWNER = "worker-1"


# --- admission ------------------------------------------------------------


@pytest.mark.postgres
def test_create_run_writes_the_run_the_lease_and_the_queued_event(pg, make_run):
    run_id = make_run(description="A closed-loop exhaust heat recovery system")
    run = store.get_run(pg, run_id=run_id, actor_email="testnovelty@gmail.com")

    assert run["status"] is RunStatus.QUEUED
    assert run["current_gate"] is None
    assert run["thread_id"] == run_id, "the LangGraph thread id is the run id"
    assert run["next_sequence"] == 1
    assert run["events_compacted_below"] == 0
    assert lease.lease_row(pg, run_id=run_id)["fence_token"] == 0
    assert len(store.list_events(pg, run_id=run_id, actor_email=run["created_by"])) == 1


@pytest.mark.postgres
def test_the_policy_columns_are_frozen_on_the_run(pg, make_run):
    """Frozen at admission, so changing a setting cannot retroactively
    reinterpret a finished run's date screening."""
    run_id = make_run(date_policy="publication_lte_cutoff", text_version_policy="exclude")
    run = store.get_run(pg, run_id=run_id, actor_email="testnovelty@gmail.com")
    assert run["date_policy"] == "publication_lte_cutoff"
    assert run["text_version_policy"] == "exclude"


# --- ownership ------------------------------------------------------------


@pytest.mark.postgres
def test_a_second_users_actor_email_sees_nothing(pg, make_run, second_user):
    run_id = make_run()

    with pytest.raises(RunNotFoundError):
        store.get_run(pg, run_id=run_id, actor_email=second_user)
    assert store.list_runs(pg, actor_email=second_user) == []
    with pytest.raises(RunNotFoundError):
        store.list_events(pg, run_id=run_id, actor_email=second_user)
    with pytest.raises(RunNotFoundError):
        store.list_attempts(pg, run_id=run_id, actor_email=second_user)
    with pytest.raises(RunNotFoundError):
        store.get_terminal_event(pg, run_id=run_id, actor_email=second_user)


@pytest.mark.postgres
def test_an_unknown_run_and_another_users_run_are_indistinguishable(
    pg, make_run, second_user
):
    """Both raise the same error with the same shape. 403 would confirm the run
    exists."""
    run_id = make_run()
    unknown = "00000000-0000-4000-8000-000000000000"

    with pytest.raises(RunNotFoundError) as other_users:
        store.get_run(pg, run_id=run_id, actor_email=second_user)
    with pytest.raises(RunNotFoundError) as does_not_exist:
        store.get_run(pg, run_id=unknown, actor_email=second_user)

    assert type(other_users.value) is type(does_not_exist.value)
    assert other_users.value.code == does_not_exist.value.code == "RUN_NOT_FOUND"


@pytest.mark.postgres
def test_list_runs_returns_only_the_callers_runs(pg, make_run, second_user, created_runs):
    mine = make_run(description="mine")
    theirs = store.create_run(
        pg,
        description="theirs",
        claims=None,
        cutoff_date=None,
        date_policy="p",
        text_version_policy="t",
        created_by=second_user,
    )
    created_runs.append(theirs)

    # Scoped to the two runs THIS test made, in both directions.
    #
    # The assertion used to be positional -- "the owner's newest run is `mine`"
    # and "the second user has exactly one run" -- which quietly depended on the
    # whole table rather than on anything this test did. Any other run for either
    # account broke it, and a second process running this same suite against the
    # same database is exactly that: it creates a run for the same seeded owner
    # and the same second user, and the positional assertion fails in a test that
    # has nothing to do with concurrency.
    #
    # The property being checked is unchanged and is now stated directly: each
    # run appears for its own owner and for nobody else. That is what
    # `list_runs` promises; "how many rows exist in total" never was.
    ours = {run["run_id"] for run in store.list_runs(pg, actor_email="testnovelty@gmail.com")}
    yours = {run["run_id"] for run in store.list_runs(pg, actor_email=second_user)}

    assert mine in ours, "the owner's own run is missing from their list"
    assert theirs not in ours, "another user's run leaked into the owner's list"
    assert theirs in yours, "the second user's own run is missing from their list"
    assert mine not in yours, "the owner's run leaked into another user's list"


# --- guarded transitions --------------------------------------------------


@pytest.mark.postgres
def test_a_transition_requires_a_live_lease(pg, make_run):
    run_id = make_run()
    with pytest.raises(FencedError):
        store.transition_status(
            pg,
            run_id=run_id,
            owner_id=OWNER,
            fence_token=1,
            expected_from=(RunStatus.QUEUED,),
            to_status=RunStatus.RUNNING,
        )


@pytest.mark.postgres
def test_a_transition_from_an_unexpected_status_raises_and_does_not_loop(pg, make_run):
    """Zero affected rows raises. It is never a retry-until-it-works loop: a
    transition that did not apply means the run was not where the caller thought,
    and looping would eventually apply it from a state nobody checked."""
    run_id = make_run()
    token = lease.acquire(pg, run_id=run_id, owner_id=OWNER, ttl_seconds=TTL)
    with pytest.raises(IllegalStateError):
        store.transition_status(
            pg,
            run_id=run_id,
            owner_id=OWNER,
            fence_token=token,
            expected_from=(RunStatus.RESUMING,),
            to_status=RunStatus.RUNNING,
        )
    assert store.get_run(pg, run_id=run_id, actor_email="testnovelty@gmail.com")[
        "status"
    ] is RunStatus.QUEUED


@pytest.mark.postgres
@pytest.mark.parametrize("terminal", [RunStatus.COMPLETED, RunStatus.FAILED])
def test_transition_status_refuses_to_write_a_terminal(pg, make_run, terminal):
    """Terminals come only from the finalizers, atomically with their event. A
    transition helper that could write one would let a `COMPLETED` run exist with
    no result to fetch."""
    run_id = make_run()
    token = lease.acquire(pg, run_id=run_id, owner_id=OWNER, ttl_seconds=TTL)
    with pytest.raises(IllegalStateError) as caught:
        store.transition_status(
            pg,
            run_id=run_id,
            owner_id=OWNER,
            fence_token=token,
            expected_from=(RunStatus.QUEUED,),
            to_status=terminal,
        )
    assert "finalize_run_success/failure" in str(caught.value)


@pytest.mark.postgres
def test_the_happy_path_transitions(pg, make_run):
    run_id = make_run()
    token = lease.acquire(pg, run_id=run_id, owner_id=OWNER, ttl_seconds=TTL)
    assert (
        store.transition_status(
            pg,
            run_id=run_id,
            owner_id=OWNER,
            fence_token=token,
            expected_from=(RunStatus.QUEUED,),
            to_status=RunStatus.RUNNING,
        )
        is RunStatus.RUNNING
    )


# --- the API resume half --------------------------------------------------


def _parked(pg, make_run) -> str:
    from workflow import events

    run_id = make_run()
    token = lease.acquire(pg, run_id=run_id, owner_id=OWNER, ttl_seconds=TTL)
    store.transition_status(
        pg,
        run_id=run_id,
        owner_id=OWNER,
        fence_token=token,
        expected_from=(RunStatus.QUEUED,),
        to_status=RunStatus.RUNNING,
    )
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
    return run_id


@pytest.mark.postgres
def test_begin_resume_appends_resumed_and_transitions(pg, make_run):
    run_id = _parked(pg, make_run)
    sequence = store.begin_resume(
        pg,
        run_id=run_id,
        actor_email="testnovelty@gmail.com",
        gate=WorkflowGate.A,
        resume_payload={"type": "resumed", "gate": "A", "decision": "approved"},
        dedupe_key="resumed:A:1",
    )
    run = store.get_run(pg, run_id=run_id, actor_email="testnovelty@gmail.com")
    assert run["status"] is RunStatus.RESUMING
    assert sequence == 4


@pytest.mark.postgres
def test_begin_resume_never_touches_the_graph_or_the_lease(pg, make_run):
    """The approval endpoint commits and returns. The **executor** picks the run
    up on its next poll and issues `Command(resume=...)` -- which is what makes a
    duplicate approval harmless and a crash mid-approval recoverable."""
    run_id = _parked(pg, make_run)
    before = lease.lease_row(pg, run_id=run_id)
    store.begin_resume(
        pg,
        run_id=run_id,
        actor_email="testnovelty@gmail.com",
        gate=WorkflowGate.A,
        resume_payload={"type": "resumed"},
        dedupe_key="resumed:A:1",
    )
    assert lease.lease_row(pg, run_id=run_id) == before, "no lease was taken"


@pytest.mark.postgres
def test_begin_resume_is_ownership_scoped(pg, make_run, second_user):
    run_id = _parked(pg, make_run)
    with pytest.raises(RunNotFoundError):
        store.begin_resume(
            pg,
            run_id=run_id,
            actor_email=second_user,
            gate=WorkflowGate.A,
            resume_payload={"type": "resumed"},
            dedupe_key="resumed:A:1",
        )
    assert store.get_run(pg, run_id=run_id, actor_email="testnovelty@gmail.com")[
        "status"
    ] is RunStatus.WAITING_FOR_REVIEW


@pytest.mark.postgres
def test_a_duplicate_resume_matches_zero_rows_the_second_time(pg, make_run):
    """The `resumed` event dedupes to the same sequence, and the second
    transition finds no `WAITING_FOR_REVIEW` row -- which is why a duplicate
    approval is harmless rather than a second resume."""
    run_id = _parked(pg, make_run)
    payload = {"type": "resumed", "gate": "A"}
    first = store.begin_resume(
        pg,
        run_id=run_id,
        actor_email="testnovelty@gmail.com",
        gate=WorkflowGate.A,
        resume_payload=payload,
        dedupe_key="resumed:A:1",
    )
    with pytest.raises(IllegalStateError):
        store.begin_resume(
            pg,
            run_id=run_id,
            actor_email="testnovelty@gmail.com",
            gate=WorkflowGate.A,
            resume_payload=payload,
            dedupe_key="resumed:A:1",
        )
    assert pg.execute(
        "SELECT count(*) FROM workflow_events WHERE run_id = %s AND event_type = 'resumed'",
        (run_id,),
    ).fetchone()[0] == 1
    assert first == 4


# --- the executor's trusted-internal reads --------------------------------


@pytest.mark.postgres
def test_system_readers_skip_ownership_deliberately(pg, make_run):
    """The worker has no user identity and must reach any run it has legitimately
    leased. That is why these are named `_system_` and why the test below asserts
    they never reach an HTTP handler."""
    run_id = make_run()
    assert store._system_get_run(pg, run_id=run_id)["run_id"] == run_id
    assert run_id in store._system_claimable_run_ids(pg, limit=50)


@pytest.mark.postgres
def test_a_parked_run_is_not_claimable(pg, make_run):
    """A `WAITING_FOR_REVIEW` run is waiting on a human, not on a worker. Leasing
    it would let a worker resume work nobody approved."""
    run_id = _parked(pg, make_run)
    assert run_id not in store._system_claimable_run_ids(pg, limit=200)
    assert lease.acquire(pg, run_id=run_id, owner_id="worker-2", ttl_seconds=TTL) is None


@pytest.mark.postgres
def test_a_terminal_run_is_not_claimable(pg, make_run):
    from workflow import events

    run_id = make_run()
    token = lease.acquire(pg, run_id=run_id, owner_id=OWNER, ttl_seconds=TTL)
    store.transition_status(
        pg,
        run_id=run_id,
        owner_id=OWNER,
        fence_token=token,
        expected_from=(RunStatus.QUEUED,),
        to_status=RunStatus.RUNNING,
    )
    events.finalize_run_success(
        pg,
        run_id=run_id,
        owner_id=OWNER,
        fence_token=token,
        payload={"type": "result", "data": {}},
    )
    assert run_id not in store._system_claimable_run_ids(pg, limit=200)


# --- source contracts (always run) ---------------------------------------


def test_system_readers_are_unreachable_from_the_http_surface():
    """A `_system_` function skips ownership, so it must never be callable from a
    request handler.

    Checks `backend/api/` when it exists (Gate E builds it) and `server.py`,
    which is the whole HTTP surface today -- so this is a real check now rather
    than one that only starts working later.
    """
    import ast

    surfaces = [BACKEND / "server.py"]
    api = BACKEND / "api"
    if api.is_dir():
        surfaces.extend(api.rglob("*.py"))

    # An **AST** walk over what the module actually references, not a substring
    # search over its text. `api/__init__.py`'s docstring names the `_system_`
    # convention in order to state that nothing in the package may reach one, and
    # a grep that fires on its own prose is a test somebody eventually deletes.
    #
    # This is also strictly stronger than the grep it replaces: it catches
    # `store._system_get_run(...)`, a bare `_system_get_run(...)`, an import of
    # one, and an attribute lookup used as a value -- none of which a comment can
    # fake and none of which prose can trigger.
    offenders: list[str] = []
    for path in surfaces:
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            referenced = ""
            if isinstance(node, ast.Attribute):
                referenced = node.attr
            elif isinstance(node, ast.Name):
                referenced = node.id
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name.startswith("_system_"):
                        referenced = alias.name
            if referenced.startswith("_system_"):
                offenders.append(f"{path.relative_to(BACKEND)} -> {referenced}")

    assert offenders == [], (
        f"these HTTP modules reach an ownership-skipping reader: {offenders}"
    )


def test_the_workflow_tables_are_written_only_by_the_workflow_package():
    """`workflow/` is to the new tables what `reports.py` is to the old ones.

    A write from anywhere else would bypass the ownership filter, the fencing
    check, or both.

    `tests/` and `acceptance/` are excluded because **the rule is about code a
    request can reach**, and neither is. Both are harness code that sets up and
    tears down its own fixtures — `tests/conftest.py`'s `pg` fixture deletes the
    runs it created, and `acceptance/` deletes the run and the users it created —
    and a harness that had to go through the fenced writers could not clean up
    after a run it deliberately abandoned mid-flight.

    That exclusion is only safe while neither is importable by the application,
    which the test below asserts rather than assumes.
    """
    tables = (
        "verification_runs",
        "run_leases",
        "node_attempts",
        "idempotency_records",
        "workflow_events",
    )
    verbs = ("insert into ", "update ", "delete from ")
    offenders = []
    for path in BACKEND.rglob("*.py"):
        if any(part in {".venv", "__pycache__", "tests", "workflow", "acceptance"}
               for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8").lower()
        for table in tables:
            if any(f"{verb}{table}" in text for verb in verbs):
                offenders.append(f"{path.relative_to(BACKEND)} -> {table}")
    assert offenders == [], f"workflow tables written outside workflow/: {offenders}"


def test_the_store_re_exports_the_event_writers_rather_than_duplicating_them():
    """`workflow/events.py` stays the only module naming `workflow_events`, and
    the store gives callers one import site without giving the table a second
    writer."""
    source = (BACKEND / "workflow" / "store.py").read_text(encoding="utf-8").lower()
    # Prose mentions are fine and in fact required -- the module docstring
    # explains the split. What must not appear is SQL against the table.
    for statement in (
        "insert into workflow_events",
        "update workflow_events",
        "delete from workflow_events",
        "from workflow_events",
    ):
        assert statement not in source, f"store.py issues SQL against the event log: {statement}"
    for writer in (
        "create_run",
        "append_worker_event",
        "append_api_event",
        "pause_run_for_review",
        "finalize_run_success",
        "finalize_run_failure",
    ):
        assert writer in store.__all__


def test_the_acceptance_harness_is_not_reachable_from_the_application():
    """The exclusion above is only safe while this holds.

    `acceptance/` writes workflow tables directly to clean up after itself. That
    is fine for a harness and would be a hole in the application, so nothing the
    application imports may reach it -- the same rule `tests/harness.py` lives
    under, asserted the same way.
    """
    import ast

    acceptance = BACKEND / "acceptance"
    if not acceptance.is_dir():
        return

    offenders = []
    for path in BACKEND.rglob("*.py"):
        if any(part in {".venv", "__pycache__", "tests", "acceptance"} for part in path.parts):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name.split(".")[0] == "acceptance":
                    offenders.append(f"{path.relative_to(BACKEND)} -> {name}")
    assert offenders == [], f"application code imports the acceptance harness: {offenders}"
