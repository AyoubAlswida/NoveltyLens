"""The section 10 API contract: admission, idempotency, and the resume boundary.

**The property this module exists for: an approval endpoint never resumes the
graph.** It commits the human decision, the audit row, the idempotency record, the
`resumed` event and the `WAITING_FOR_REVIEW -> RESUMING` transition -- five
writes, one transaction, all or none -- and returns. The executor picks the run up
on its next poll.

That is not a stylistic separation. It is what makes a duplicate approval harmless
and a crash mid-approval recoverable, and it is asserted here by patching the
graph and proving it was never built or called.

Real PostgreSQL throughout: idempotency is a transaction rather than a unique
index (spec section 8.2), and a double cannot exhibit a transaction.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from auth import issue_token
from workflow import events, lease, store
from workflow.types import RunStatus, WorkflowGate

pytestmark = pytest.mark.postgres

OWNER = "testnovelty@gmail.com"
WORKER = "worker-1"
TTL = 60


@pytest.fixture
def client(real_db):
    import server

    return TestClient(server.app)


def auth(email: str = OWNER) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token(email)}"}


@pytest.fixture
def paused_run(pg, make_run):
    """A run parked at Workflow Gate A, exactly as the executor leaves one."""
    run_id = make_run(created_by=OWNER)
    token = lease.acquire(pg, run_id=run_id, owner_id=WORKER, ttl_seconds=TTL)
    store.transition_status(
        pg,
        run_id=run_id,
        owner_id=WORKER,
        fence_token=token,
        expected_from=(RunStatus.QUEUED,),
        to_status=RunStatus.RUNNING,
    )
    events.pause_run_for_review(
        pg,
        run_id=run_id,
        owner_id=WORKER,
        fence_token=token,
        gate=WorkflowGate.A,
        review_payload={"gate": "A", "summary": {}, "payload_url": f"/api/runs/{run_id}/gates/A"},
        pause_payload={"gate": "A", "status": "WAITING_FOR_REVIEW"},
        checkpoint_id="ckpt-1",
    )
    return run_id


DECOMPOSITION = {
    "version": 1,
    # The understanding stage approves a technical summary and the capabilities
    # together, at one version -- `workflow/review.py` refuses an empty summary
    # and `approve_understanding_by_owner` refuses to write one. A fixture
    # without it would be approving half an artifact.
    "summary": "A closed-loop controller recovers exhaust heat via a duct-mounted exchanger.",
    "features": [
        {
            "feature_id": "F2",
            "feature_version": "3",
            "feature_text": "closed-loop control of exhaust heat recovery",
            "elements": [
                {"element_id": "E1", "text": "a heat exchanger in the exhaust duct",
                 "core": True, "order": 1}
            ],
            "relationships": [],
        }
    ],
}


def approve(client, run_id: str, key: str, *, decomposition=None):
    return client.post(
        f"/api/runs/{run_id}/gates/A/approve",
        json={"idempotency_key": key, "decomposition": decomposition or DECOMPOSITION},
        headers=auth(),
    )


# --- the approval never resumes the graph ----------------------------------


def test_an_approval_never_builds_or_calls_the_graph(client, paused_run, pg):
    """**Patch the graph and prove it was not invoked.** The executor resumes;
    the endpoint only commits."""
    import graph.adapter as adapter

    with patch.object(adapter, "build") as build, \
         patch("graph.pipeline.build_graph_with_checkpointer") as compiled:
        response = approve(client, paused_run, "key-1")

    assert response.status_code == 200, response.text
    build.assert_not_called()
    compiled.assert_not_called()


def test_an_approval_moves_the_run_to_resuming_and_stops_there(client, paused_run, pg):
    """`RESUMING` is a request for the executor, not execution. The run does not
    become `RUNNING` until a worker claims it."""
    assert approve(client, paused_run, "key-1").status_code == 200
    row = pg.execute(
        "SELECT status FROM verification_runs WHERE id = %s", (paused_run,)
    ).fetchone()
    assert row[0] == "RESUMING"


def test_an_approval_appends_exactly_one_resumed_event(client, paused_run, pg):
    assert approve(client, paused_run, "key-1").status_code == 200
    stored = [
        row for row in events.read_events(pg, run_id=paused_run, after_sequence=0)
        if row["event_type"] == "resumed"
    ]
    assert len(stored) == 1


def test_an_approval_writes_an_audit_row_naming_the_authenticated_actor(client, paused_run, pg):
    """The approver is the authenticated caller, never a field a request can set."""
    assert approve(client, paused_run, "key-1").status_code == 200
    rows = pg.execute(
        "SELECT reviewer_id, field FROM review_audit WHERE run_id = %s", (paused_run,)
    ).fetchall()
    assert rows
    assert all(row[0] == OWNER for row in rows)


# --- idempotency -------------------------------------------------------------


def test_a_duplicate_approval_produces_one_mutation_and_replays_the_response(
    client, paused_run, pg
):
    """**Spec section 8.2.** Same key, same payload: one mutation, one `resumed`
    event, one transition, and the second call returns the stored response."""
    first = approve(client, paused_run, "key-1")
    second = approve(client, paused_run, "key-1")

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()

    resumed = [
        row for row in events.read_events(pg, run_id=paused_run, after_sequence=0)
        if row["event_type"] == "resumed"
    ]
    assert len(resumed) == 1, "a duplicate approval must not append a second event"

    versions = pg.execute(
        "SELECT count(*) FROM feature_decompositions WHERE run_id = %s", (paused_run,)
    ).fetchone()[0]
    assert versions == 1, "a duplicate approval must not write a second version"


def test_the_same_key_with_a_different_payload_is_a_conflict(client, paused_run):
    """Not a silent replay of an unrelated result: the caller reused a key for a
    different request, and being told so is the only safe answer."""
    assert approve(client, paused_run, "key-1").status_code == 200

    other = {**DECOMPOSITION, "version": 2}
    response = approve(client, paused_run, "key-1", decomposition=other)
    assert response.status_code == 409
    assert response.json()["code"] == "IDEMPOTENCY_CONFLICT"


def test_a_mutating_request_without_an_idempotency_key_is_422(client, paused_run):
    """A server-generated key would make every retry a new mutation."""
    response = client.post(
        f"/api/runs/{paused_run}/gates/A/approve",
        json={"decomposition": DECOMPOSITION},
        headers=auth(),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "MISSING_IDEMPOTENCY_KEY"


# Every POST under `/api/runs/{run_id}` that mutates, and the minimum body each
# needs to get past FastAPI's own validation and reach the key check. Gate A is
# covered by its own test above; the four below it were the whole list until the
# work-request and overlay routes were added, at which point this test's name
# overstated what it checked -- it said "every mutating endpoint" while covering
# three of eight. `test_the_list_above_is_actually_every_mutating_route` now
# fails if a new route is added without being listed here.
MUTATING_ROUTES = [
    ("gates/A/approve", {"decomposition": DECOMPOSITION}),
    ("gates/STRATEGY/approve", {"strategy": {"version": 1, "queries": []}}),
    ("gates/B/approve", {"publication_numbers": []}),
    ("gates/C/complete", {}),
    ("gates/D/approve", {"report_version": 1}),
    ("understanding/regenerate", {"from_version": 1}),
    ("strategy/regenerate", {"from_version": 1}),
    ("candidates", {"publication_number": "EP1234567"}),
    ("cells/cell-1/review", {"overlay": {}}),
    ("cells/cell-1/waiver", {"waiver": {}}),
    ("cells/cell-1/rerun", {"source_result_version": 1, "context_requests": []}),
]


@pytest.mark.parametrize("path,body", MUTATING_ROUTES)
def test_every_mutating_endpoint_requires_an_idempotency_key(client, paused_run, path, body):
    response = client.post(f"/api/runs/{paused_run}/{path}", json=body, headers=auth())
    assert response.status_code == 422
    assert response.json()["code"] == "MISSING_IDEMPOTENCY_KEY"


def test_the_list_above_is_actually_every_mutating_route(client):
    """`MUTATING_ROUTES` is the whole POST surface, not a sample of it.

    A route added without an entry here would be a mutation nobody had checked
    for the key requirement, and the parametrized test above would keep passing
    and keep claiming "every". The router is the authority; this compares against
    it rather than against a second hand-written list.
    """
    from api.runs import router

    on_router = {
        route.path.split("/{run_id}/", 1)[1]
        for route in router.routes
        if "POST" in getattr(route, "methods", set()) and "/{run_id}/" in route.path
    }
    listed = {path for path, _ in MUTATING_ROUTES}

    def shape(path: str) -> str:
        # `cells/cell-1/review` and `cells/{cell_id}/review` are the same route.
        return "/".join(
            "{}" if part.startswith("{") or part == "cell-1" else part
            for part in path.split("/")
        )

    assert {shape(path) for path in listed} == {shape(path) for path in on_router}, (
        "MUTATING_ROUTES has drifted from the router: "
        f"only on the router {sorted(on_router)}, only listed {sorted(listed)}"
    )


# --- wrong gate --------------------------------------------------------------


def test_approving_a_gate_the_run_is_not_at_is_refused(client, pg, make_run):
    """A `QUEUED` run is not waiting for anybody's approval."""
    run_id = make_run(created_by=OWNER)
    response = approve(client, run_id, "key-1")
    assert response.status_code in (409, 422)
    assert response.json()["code"] in {"WRONG_GATE", "RUN_NOT_RESUMABLE", "REVIEW_REJECTED"}


def test_a_second_approval_after_resuming_does_not_transition_again(client, paused_run, pg):
    """The run has already left `WAITING_FOR_REVIEW`; a different key must not
    move it a second time."""
    assert approve(client, paused_run, "key-1").status_code == 200
    response = approve(client, paused_run, "key-2")
    assert response.status_code in (409, 422)

    row = pg.execute(
        "SELECT status FROM verification_runs WHERE id = %s", (paused_run,)
    ).fetchone()
    assert row[0] == "RESUMING"


# --- error bodies ------------------------------------------------------------


def test_every_error_body_carries_code_and_detail_at_the_top_level(client, paused_run):
    """Spec section 10.2. A client branches on `code`; nesting it under FastAPI's
    `detail` would hide it exactly where that branch happens."""
    response = client.post(
        f"/api/runs/{paused_run}/gates/A/approve",
        json={"decomposition": DECOMPOSITION},
        headers=auth(),
    )
    body = response.json()
    assert set(body) >= {"code", "detail"}
    assert isinstance(body["detail"], str)


def test_no_error_body_carries_a_traceback_or_a_provider_body(client, paused_run):
    response = client.post(
        f"/api/runs/{paused_run}/gates/A/approve",
        json={"decomposition": DECOMPOSITION},
        headers=auth(),
    )
    rendered = response.text.lower()
    for leaked in ("traceback", "file \"", "psycopg", "api_key", "bearer ", "sk-"):
        assert leaked not in rendered


# --- admission ----------------------------------------------------------------


def test_admission_refuses_before_writing_anything_when_a_policy_is_unrecorded(client, pg):
    """`422 POLICY_NOT_APPROVED`, and **no run, no lease, no event**. There is
    nothing to clean up because nothing was made."""
    before = pg.execute("SELECT count(*) FROM verification_runs").fetchone()[0]

    with patch("evidence.policy.validate_admission_policies") as validate:
        from evidence.policy import PolicyNotApproved

        validate.side_effect = PolicyNotApproved(["DATE_SCREENING_DECISION is unset"])
        response = client.post(
            "/api/runs", json={"description": "an exhaust heat recovery system"},
            headers=auth(),
        )

    assert response.status_code == 422
    assert response.json()["code"] == "POLICY_NOT_APPROVED"
    assert response.json()["missing"]
    after = pg.execute("SELECT count(*) FROM verification_runs").fetchone()[0]
    assert after == before, "a refused admission created a run"


def test_admission_requires_a_description(client):
    response = client.post("/api/runs", json={}, headers=auth())
    assert response.status_code == 422


# --- the search-only surface is untouched ------------------------------------


def test_the_existing_endpoints_are_still_registered_and_unchanged(client):
    """Spec: `POST /api/analyze`, `/api/analyze/stream`, `GET`/`DELETE
    /api/history` and the auth endpoints are not migrated onto the executor."""
    import server

    paths = {
        getattr(route, "path", "")
        for route in server.app.routes
    }
    for path in ("/api/analyze", "/api/analyze/stream", "/api/history",
                 "/api/auth/login", "/api/auth/me", "/healthz", "/readyz"):
        assert path in paths, f"{path} disappeared from the app"


# --- POST /candidates: a request, never an approval and never a bypass ------


def test_adding_a_candidate_is_accepted_but_grants_nothing(client, paused_run, pg):
    """Spec case T. A `202` records that a person asked for a publication to go
    through the pipeline. It does not put it in the candidate set, does not make
    it eligible, and does not fetch anything."""
    response = client.post(
        f"/api/runs/{paused_run}/candidates",
        json={"idempotency_key": "cand-1", "publication_number": "EP4778509"},
        headers=auth(),
    )
    assert response.status_code == 202
    body = response.json()
    assert body["accepted"] is True
    assert body["already_requested"] is False

    # It is a REQUEST row, and nothing else was created.
    requested = pg.execute(
        "SELECT requested_number, requested_by, processed_at FROM manual_candidates"
        " WHERE run_id = %s",
        (paused_run,),
    ).fetchall()
    assert requested == [("EP4778509", OWNER, None)]
    assert pg.execute(
        "SELECT count(*) FROM candidate_sets WHERE run_id = %s", (paused_run,)
    ).fetchone()[0] == 0, "a 202 must not create an approved candidate set"


def test_adding_a_candidate_does_not_touch_the_provider_or_the_graph(client, paused_run):
    """The endpoint fetches nothing. The outbound guard in `conftest.py` would
    fail the test if it tried, and the graph is patched to prove it is not run."""
    import graph.adapter as adapter

    with patch.object(adapter, "build") as build:
        response = client.post(
            f"/api/runs/{paused_run}/candidates",
            json={"idempotency_key": "cand-2", "publication_number": "EP4778509"},
            headers=auth(),
        )
    assert response.status_code == 202
    build.assert_not_called()


def test_the_same_publication_twice_is_the_same_request(client, paused_run, pg):
    """Asking twice is not a conflict; it is the same request, and the response
    says which happened."""
    first = client.post(
        f"/api/runs/{paused_run}/candidates",
        json={"idempotency_key": "cand-a", "publication_number": "EP4778509"},
        headers=auth(),
    )
    second = client.post(
        f"/api/runs/{paused_run}/candidates",
        json={"idempotency_key": "cand-b", "publication_number": "EP4778509"},
        headers=auth(),
    )
    assert first.status_code == second.status_code == 202
    assert second.json()["already_requested"] is True
    assert pg.execute(
        "SELECT count(*) FROM manual_candidates WHERE run_id = %s", (paused_run,)
    ).fetchone()[0] == 1


def test_a_candidate_with_no_publication_number_is_refused(client, paused_run):
    response = client.post(
        f"/api/runs/{paused_run}/candidates",
        json={"idempotency_key": "cand-3", "publication_number": "  "},
        headers=auth(),
    )
    assert response.status_code in (409, 422)


def test_adding_a_candidate_requires_an_idempotency_key(client, paused_run):
    response = client.post(
        f"/api/runs/{paused_run}/candidates",
        json={"publication_number": "EP4778509"},
        headers=auth(),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "MISSING_IDEMPOTENCY_KEY"


# --- POST /cells/{id}/rerun: a new version, never an edit -------------------


def test_a_rerun_on_a_cell_with_no_result_is_refused(client, paused_run):
    """A rerun supersedes a result; it does not create the first one."""
    response = client.post(
        f"/api/runs/{paused_run}/cells/{paused_run}:F2:US1234567B2/rerun",
        json={"idempotency_key": "rr-1", "context_requests": [], "source_result_version": 1},
        headers=auth(),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "WRONG_GATE"


def test_a_rerun_must_name_the_result_the_reviewer_saw(client, paused_run):
    """Without `source_result_version` a rerun could attach to a result that
    superseded the one under review while the request sat in the queue."""
    response = client.post(
        f"/api/runs/{paused_run}/cells/{paused_run}:F2:US1234567B2/rerun",
        json={"idempotency_key": "rr-2", "context_requests": []},
        headers=auth(),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_REQUEST"


def test_a_rerun_requires_an_idempotency_key(client, paused_run):
    response = client.post(
        f"/api/runs/{paused_run}/cells/{paused_run}:F2:US1234567B2/rerun",
        json={"context_requests": [], "source_result_version": 1},
        headers=auth(),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "MISSING_IDEMPOTENCY_KEY"


# --- error detail: trusted where it is safe, rebuilt where it is not -------


def test_a_workflow_error_keeps_its_message_so_the_caller_learns_something():
    """CLAUDE.md's rule. These messages are built by this codebase and by SQL
    functions that interpolate run ids, statuses and fence tokens and nothing
    else, so passing them through (redacted) is what makes a 409 actionable.

    Rebuilding them produced 'an unexpected IllegalStateError occurred', which is
    safe and useless -- the caller cannot tell WHICH gate the run is at.
    """
    from api.deps import to_http
    from workflow.types import IllegalStateError

    error = to_http(IllegalStateError("run abc is RESUMING, not WAITING_FOR_REVIEW"))
    assert "RESUMING" in error.detail["detail"]
    assert "unexpected" not in error.detail["detail"]


def test_an_unknown_exception_is_still_rebuilt_from_allow_listed_facts():
    """The other half, and the one that matters for secrets: pattern-matching an
    unknown exception's prose is never complete, so nothing of it is passed
    through."""
    from api.deps import to_http

    error = to_http(ValueError("provider said https://api.example/x?api_key=SECRETVALUE"))
    detail = error.detail["detail"]
    assert "SECRETVALUE" not in detail
    assert "api_key" not in detail
    assert "https://" not in detail


@pytest.mark.parametrize(
    "leak",
    ["sk-live-abcdefghijklmnop", "Bearer eyJhbGciOi", "postgresql://u:p@host/db"],
)
def test_no_credential_shape_survives_the_error_boundary(leak):
    from api.deps import to_http

    detail = to_http(RuntimeError(f"boom {leak}")).detail["detail"]
    assert leak not in detail
