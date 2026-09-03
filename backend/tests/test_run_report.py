"""`GET /api/runs/{id}/report`: the completed run's standard report.

Spec sections 4.4 and 3.8. The endpoint was the Gate D verification report and
is **repurposed** rather than joined by a second one, so a run's result has one
path to it and cannot drift from the two that already existed -- the terminal
`result` event a live client reads, and the History row `services/history.ts`
maps into the same `AnalysisReport`.

Real PostgreSQL: what is asserted here is ownership filtered in SQL, a status
read off the run row, and a terminal event written atomically with a History
row. The in-memory double is a dict and can exhibit none of them.
"""

from dataclasses import replace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from auth import issue_token
from workflow import events, lease, store
from workflow.types import RunStatus

pytestmark = pytest.mark.postgres

OWNER = "testnovelty@gmail.com"
OTHER = "someone-else@example.com"
WORKER = "worker-1"
TTL = 60

REPORT = {
    "title": "A graphene desalination membrane...",
    "date": "Today",
    "concepts": ["membrane", "desalination"],
    "patents": [
        {
            "num": "US1000001A",
            "title": "A filtration membrane",
            "risk": "Medium",
            "rationale": "Shares the membrane geometry.",
            "key_overlap": ["membrane"],
            "source": "Lens.org",
            "url": "https://example.invalid/1",
        }
    ],
    "recommendations": ["Claim the pore distribution explicitly."],
    "query_count": 3,
}


@pytest.fixture
def client(real_db):
    import server

    return TestClient(server.app)


def auth(email: str = OWNER) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token(email)}"}


def _complete(pg, run_id: str, data: dict = REPORT) -> None:
    """Drive a run to COMPLETED with a terminal `result`, as the executor does."""
    token = lease.acquire(pg, run_id=run_id, owner_id=WORKER, ttl_seconds=TTL)
    store.transition_status(
        pg,
        run_id=run_id,
        owner_id=WORKER,
        fence_token=token,
        expected_from=(RunStatus.QUEUED,),
        to_status=RunStatus.RUNNING,
    )
    events.finalize_run_success(
        pg,
        run_id=run_id,
        owner_id=WORKER,
        fence_token=token,
        payload={"type": "result", "run_id": run_id, "data": data},
    )


# --- what a completed run returns -------------------------------------------


def test_a_completed_run_returns_the_standard_report(client, pg, make_run):
    run_id = make_run(created_by=OWNER)
    _complete(pg, run_id)

    response = client.get(f"/api/runs/{run_id}/report", headers=auth())
    assert response.status_code == 200, response.text
    assert response.json() == REPORT


def test_the_report_is_the_shape_the_report_screen_reads(client, pg, make_run):
    """The same key set `graph/pipeline.py`'s `_result_envelope` produces.

    Asserted as a set rather than field by field, because the frontend's
    `AnalysisReport` is a whole shape: a missing key blanks a section, and an
    extra one is a field nothing renders.
    """
    from tests.test_pipeline_contract import RESULT_KEYS

    run_id = make_run(created_by=OWNER)
    _complete(pg, run_id)

    body = client.get(f"/api/runs/{run_id}/report", headers=auth()).json()
    assert set(body) == RESULT_KEYS


def test_the_report_publishes_no_score_or_verdict(client, pg, make_run):
    """The endpoint is not a way back to the numbers the product removed."""
    from tests.test_scores_are_not_published import _offending

    run_id = make_run(created_by=OWNER)
    _complete(pg, run_id)

    body = client.get(f"/api/runs/{run_id}/report", headers=auth()).json()
    assert _offending(body) == []


# --- every other status names itself ----------------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [(RunStatus.QUEUED, "QUEUED"), (RunStatus.RUNNING, "RUNNING")],
)
def test_a_run_that_is_not_completed_is_a_409_naming_its_real_status(
    client, pg, make_run, status, expected
):
    """Not a bare "not ready": queued, running, parked for review and failed are
    four different situations and a caller can act on each differently."""
    run_id = make_run(created_by=OWNER)
    if status is RunStatus.RUNNING:
        token = lease.acquire(pg, run_id=run_id, owner_id=WORKER, ttl_seconds=TTL)
        store.transition_status(
            pg,
            run_id=run_id,
            owner_id=WORKER,
            fence_token=token,
            expected_from=(RunStatus.QUEUED,),
            to_status=RunStatus.RUNNING,
        )

    response = client.get(f"/api/runs/{run_id}/report", headers=auth())
    assert response.status_code == 409
    body = response.json()
    # `{code, detail}` at the TOP level, not nested under `detail`.
    assert body["code"] == "RUN_NOT_COMPLETED"
    assert expected in body["detail"]


# --- ownership --------------------------------------------------------------


def test_another_users_completed_run_answers_not_found(client, pg, make_run):
    """404, never 403, and identical to an unknown run: a report is the most
    disclosive thing a run holds, and the endpoint must not confirm one exists."""
    run_id = make_run(created_by=OWNER)
    _complete(pg, run_id)

    response = client.get(f"/api/runs/{run_id}/report", headers=auth(OTHER))
    assert response.status_code == 404
    assert response.json()["code"] == "RUN_NOT_FOUND"


def test_an_unknown_run_answers_identically(client):
    unknown = "00000000-0000-0000-0000-000000000000"
    response = client.get(f"/api/runs/{unknown}/report", headers=auth())
    assert response.status_code == 404
    assert response.json()["code"] == "RUN_NOT_FOUND"


def test_the_report_requires_a_session(client, pg, make_run):
    run_id = make_run(created_by=OWNER)
    assert client.get(f"/api/runs/{run_id}/report").status_code == 401


# --- the report and the History row commit together -------------------------
#
# Spec section 8.5 row 34. `_succeed` opens ONE transaction and does three
# things in it: store the report through `reports.py`, write the terminal
# `result` event with the status transition, and release the lease. All three or
# none.
#
# The failure this guards is specific and would be invisible: a run that
# completes with its report dropped on the floor looks, to a client reading the
# stream, exactly like a run that completed. It is only when History is opened
# later that the report turns out not to exist.


class _LiveBeat:
    """A heartbeat that is simply not fenced.

    `_succeed` reads `beat.fenced` and nothing else. A real `Heartbeat` would
    start a thread and open its own connection to prove one boolean, and the
    lease races it exists for are covered against real PostgreSQL in
    `tests/test_lease_races.py`.
    """

    fenced = False


def _final_state() -> dict:
    """Graph state as `explain` leaves it, for the parts persistence reads.

    `persist_result` takes the `PatentRecord` behind each match out of
    `comparisons` and upserts `patents` first, so `search_run_matches`' foreign
    key resolves. A state without them fails on that key -- which is the
    persistence layer working correctly, and is why this fixture carries a real
    comparison rather than an empty dict.
    """
    from models import ComparisonResult, PatentRecord

    return {
        "comparisons": [
            ComparisonResult(
                patent=PatentRecord(
                    id="US1000001A",
                    title="A filtration membrane",
                    abstract="A membrane for desalination.",
                    source="Lens.org",
                    url="https://example.invalid/1",
                ),
                similarity_score=0.42,
            )
        ]
    }


def _succeed_with(executor, run, fence_token, final_state=None):
    state = _final_state() if final_state is None else final_state
    return executor._succeed(run, fence_token, state, _LiveBeat())


def test_a_durable_success_writes_the_report_and_the_terminal_together(
    pg, make_run, real_db
):
    """One History row, one terminal result, and the SAME object in both."""
    from workflow.executor import Executor
    from checkpointer import checkpointer
    from graph.adapter import ADAPTER

    run_id = make_run(created_by=OWNER)
    token = lease.acquire(pg, run_id=run_id, owner_id=WORKER, ttl_seconds=TTL)
    store.transition_status(
        pg, run_id=run_id, owner_id=WORKER, fence_token=token,
        expected_from=(RunStatus.QUEUED,), to_status=RunStatus.RUNNING,
    )

    # `dataclasses.replace`, not `patch.object`: `GraphAdapter` is frozen, which
    # is deliberate -- the executor's seam is data, and a test that could mutate
    # the production adapter in place could leak that mutation into another test.
    # The REAL `persist_result` is kept; only the payload is pinned.
    executor = Executor(
        adapter=replace(ADAPTER, result_payload=lambda run, state: REPORT),
        checkpointer_factory=checkpointer,
        owner_id=WORKER, ttl_seconds=TTL, heartbeat_seconds=5,
        poll_interval_seconds=0.05,
    )
    run = store._system_get_run(pg, run_id=run_id)

    before = pg.execute("SELECT count(*) FROM search_runs").fetchone()[0]
    _succeed_with(executor, run, token)

    after = pg.execute("SELECT count(*) FROM search_runs").fetchone()[0]
    assert after == before + 1, "exactly one standard History row"

    terminal = events.terminal_event(pg, run_id=run_id)
    assert terminal is not None
    assert terminal["payload"]["data"] == REPORT, "the stored and streamed reports differ"

    status = pg.execute(
        "SELECT status FROM verification_runs WHERE id = %s", (run_id,)
    ).fetchone()[0]
    assert status == "COMPLETED"


def test_a_report_persistence_failure_leaves_nothing_behind(pg, make_run, real_db):
    """**Inject a `reports.py` failure and assert the whole thing rolled back.**

    No History row, no `COMPLETED` transition, no terminal `result`. There is
    deliberately no `try` around `persist_result`: a run that reported success
    while its report was dropped is the substitution the zero-tolerance policy
    forbids, and here it would be silent -- the client reading the stream would
    see a completed run and only discover the gap when History was next opened.
    """
    from workflow.executor import Executor
    from checkpointer import checkpointer
    from graph.adapter import ADAPTER

    run_id = make_run(created_by=OWNER)
    token = lease.acquire(pg, run_id=run_id, owner_id=WORKER, ttl_seconds=TTL)
    store.transition_status(
        pg, run_id=run_id, owner_id=WORKER, fence_token=token,
        expected_from=(RunStatus.QUEUED,), to_status=RunStatus.RUNNING,
    )

    executor = Executor(
        adapter=replace(ADAPTER, result_payload=lambda run, state: REPORT),
        checkpointer_factory=checkpointer,
        owner_id=WORKER, ttl_seconds=TTL, heartbeat_seconds=5,
        poll_interval_seconds=0.05,
    )
    run = store._system_get_run(pg, run_id=run_id)

    before = pg.execute("SELECT count(*) FROM search_runs").fetchone()[0]
    # The failure is injected at `reports.py`'s own writer, which is where a real
    # database outage would surface -- not at the callback, which would prove
    # only that the callback is called.
    with patch("graph.adapter.save_report", side_effect=RuntimeError("database is down")):
        with pytest.raises(RuntimeError, match="database is down"):
            _succeed_with(executor, run, token)

    assert pg.execute("SELECT count(*) FROM search_runs").fetchone()[0] == before, (
        "a failed persistence left a partial History row"
    )
    assert events.terminal_event(pg, run_id=run_id) is None, (
        "a terminal result was committed for a report that was never stored"
    )
    status = pg.execute(
        "SELECT status FROM verification_runs WHERE id = %s", (run_id,)
    ).fetchone()[0]
    assert status == "RUNNING", "the run became terminal despite the failure"
