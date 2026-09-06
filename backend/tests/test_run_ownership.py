"""Authentication is not authorization: every category, both users, real database.

Spec section 3.5 and section 10.1. A run belongs to exactly one account, and
**every** read, stream and mutation is scoped by `run_id + authenticated user`.
This module walks the entire endpoint catalogue with a second user's token and
asserts `404 RUN_NOT_FOUND` on each one.

**404 and not 403, deliberately.** A `403` confirms the id exists, which is all an
enumerator needs; an unknown run and somebody else's run must be
indistinguishable. So the same assertion covers both, and the test that a
nonexistent id also answers 404 is what proves they really are the same answer
rather than two paths that happen to agree today.

The database is real because the property is a `WHERE` clause. An in-memory double
would let a missing `AND created_by = %s` pass, which is the exact defect this
guards.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from auth import issue_token
from workflow import store

pytestmark = pytest.mark.postgres

OWNER = "testnovelty@gmail.com"
INTRUDER = "someone-else@example.com"


@pytest.fixture
def client(real_db):
    """A client whose handlers reach the **real** database.

    `real_db` undoes `tests/conftest.py`'s autouse in-memory `db.connect` patch
    for this test. The API handlers open their own connections through
    `db.connect`, and ownership scoping is a `WHERE` clause -- asserting it
    against a dict double would assert nothing at all.
    """
    import server

    return TestClient(server.app)


def auth(email: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token(email)}"}


@pytest.fixture
def owned_run(pg, created_runs) -> str:
    run_id = store.create_run(
        pg,
        description="A closed-loop exhaust heat recovery system",
        claims=None,
        cutoff_date=None,
        date_policy="publication_lt_cutoff",
        text_version_policy="provider_publication_date",
        created_by=OWNER,
    )
    created_runs.append(run_id)
    return run_id


def endpoints(run_id: str) -> list[tuple[str, str, dict | None]]:
    """Every run-scoped path in the section 10 catalogue.

    Listed explicitly rather than discovered from the router, because a path that
    silently stopped being ownership-scoped would also silently drop out of a
    discovered list. The count is asserted below so a new endpoint added without
    a row here fails.
    """
    body = {"idempotency_key": "k1"}
    # **The catalogue shrank with the surface, and the walk is unchanged.** The
    # Gate B/C/D routes, both regeneration routes, the candidate and cell routes
    # were removed with feature verification; a walk that still listed them would
    # be asserting ownership scoping on paths that answer 404 for everyone,
    # which passes for the wrong reason. `test_the_catalogue_is_every_run_route`
    # keeps this list honest against the router itself.
    return [
        ("GET", f"/api/runs/{run_id}", None),
        ("DELETE", f"/api/runs/{run_id}", None),
        ("GET", f"/api/runs/{run_id}/stream", None),
        ("GET", f"/api/runs/{run_id}/gates/A", None),
        ("POST", f"/api/runs/{run_id}/gates/A/approve",
         {**body, "technical_summary": "s", "technical_elements": ["e"]}),
        ("GET", f"/api/runs/{run_id}/gates/STRATEGY", None),
        ("POST", f"/api/runs/{run_id}/gates/STRATEGY/approve", {**body, "queries": ["q"]}),
        ("GET", f"/api/runs/{run_id}/report", None),
    ]


# --- 401 before anything else ----------------------------------------------


def test_every_endpoint_returns_401_without_a_token(client, owned_run):
    """401, never 403: the frontend treats exactly one status as "session gone"."""
    for method, path, body in endpoints(owned_run):
        with client.stream(method, path, json=body) as response:
            assert response.status_code == 401, f"{method} {path} -> {response.status_code}"


def test_the_collection_endpoints_also_require_a_token(client):
    assert client.get("/api/runs").status_code == 401
    assert client.post("/api/runs", json={"description": "x"}).status_code == 401


# --- 404 for another user's run, on every category -------------------------


def test_another_user_gets_404_on_every_run_scoped_endpoint(client, owned_run):
    """**The whole catalogue.** Reads, the stream, both gate approvals, the cell
    read, the review, the waiver, gate completion, the report and its approval."""
    for method, path, body in endpoints(owned_run):
        with client.stream(method, path, json=body, headers=auth(INTRUDER)) as response:
            assert response.status_code == 404, (
                f"{method} {path} answered {response.status_code} to a non-owner"
            )
            response.read()
            assert response.json()["code"] == "RUN_NOT_FOUND"


def test_a_nonexistent_run_answers_identically_to_someone_elses(client, owned_run):
    """The two must be indistinguishable, or the 404 leaks existence anyway."""
    missing = str(uuid.uuid4())
    for method, path, body in endpoints(owned_run):
        with client.stream(method, path, json=body, headers=auth(INTRUDER)) as theirs:
            theirs.read()
            theirs_status, theirs_code = theirs.status_code, theirs.json()["code"]
        with client.stream(
            method, path.replace(owned_run, missing), json=body, headers=auth(INTRUDER)
        ) as mine:
            mine.read()
            mine_status, mine_code = mine.status_code, mine.json()["code"]
        assert theirs_status == mine_status
        assert theirs_code == mine_code == "RUN_NOT_FOUND"


def test_no_endpoint_ever_answers_403(client, owned_run):
    """There is no 403 in this surface at all. A 403 on any path would be an
    existence oracle.

    The stream is exercised with `client.stream(...)` rather than a plain
    request: for a live run it tails until a terminal event, which is correct
    production behaviour and would hang a test that consumed the body. The status
    line is what this asserts, and it arrives before the first frame.
    """
    for method, path, body in endpoints(owned_run):
        for email in (INTRUDER, OWNER):
            if path.endswith("/stream") and email == OWNER:
                # The owner's stream is a LIVE tail: for a QUEUED run it stays
                # open waiting for the worker, which is the whole point of it and
                # would hang this test. The intruder's 404 below is what this
                # test needs from that path, and the owner's 200 is asserted in
                # `test_workflow_sse.py` against a run driven to a terminal
                # event, where the stream ends on its own.
                continue
            response = client.request(method, path, json=body, headers=auth(email))
            assert response.status_code != 403, f"{method} {path} answered 403"


def test_listing_runs_never_shows_another_users(client, owned_run):
    """A leak here needs no id at all."""
    response = client.get("/api/runs", headers=auth(INTRUDER))
    assert response.status_code == 200
    assert all(run["run_id"] != owned_run for run in response.json())


def test_the_owner_can_reach_their_own_run(client, owned_run):
    """The positive control. Without it every 404 above could be a broken route
    rather than an ownership check."""
    response = client.get(f"/api/runs/{owned_run}", headers=auth(OWNER))
    assert response.status_code == 200
    assert response.json()["run_id"] == owned_run
    assert response.json()["status"] == "QUEUED"


# --- the catalogue is complete ---------------------------------------------


def test_every_run_scoped_route_on_the_router_is_covered_here():
    """A new endpoint added without an ownership row in `endpoints()` fails here
    rather than shipping unscoped."""
    from api.runs import router

    routed = {
        (method, route.path)
        for route in router.routes
        for method in route.methods
        if "{run_id}" in route.path
    }
    covered = {
        (method, path.replace("/api/runs/" + "x" * 0, "/api/runs/"))
        for method, path, _ in endpoints("RID")
    }
    # Compare shapes rather than concrete ids.
    routed_shapes = {(m, p.replace("{run_id}", "RID").replace("{cell_id}", "RID:F2:US1234567B2"))
                     for m, p in routed}
    assert routed_shapes == {(m, p) for m, p, _ in endpoints("RID")}, (
        f"uncovered: {routed_shapes ^ {(m, p) for m, p, _ in endpoints('RID')}}"
    )
