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


SUMMARY = "A closed-loop controller recovers exhaust heat via a duct-mounted exchanger."
ELEMENTS = ["a heat exchanger in the exhaust duct"]
"""The flat understanding body (spec section 4.2.1).

A technical summary and a list of element **strings**. No version, no capability
objects, no `core`, no relationships and no per-element ids: the version is
minted by the endpoint and the ids on write.
"""


def approve(client, run_id: str, key: str, *, summary=None, elements=None):
    return client.post(
        f"/api/runs/{run_id}/gates/A/approve",
        json={
            "idempotency_key": key,
            "technical_summary": SUMMARY if summary is None else summary,
            "technical_elements": ELEMENTS if elements is None else elements,
        },
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

    response = approve(client, paused_run, "key-1", summary="a different summary")
    assert response.status_code == 409
    assert response.json()["code"] == "IDEMPOTENCY_CONFLICT"


def test_a_mutating_request_without_an_idempotency_key_is_422(client, paused_run):
    """A server-generated key would make every retry a new mutation."""
    response = client.post(
        f"/api/runs/{paused_run}/gates/A/approve",
        json={"technical_summary": SUMMARY, "technical_elements": ELEMENTS},
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
    ("gates/A/approve", {"technical_summary": SUMMARY, "technical_elements": ELEMENTS}),
    ("gates/STRATEGY/approve", {"queries": ["a query"]}),
]
"""Two approvals, and that is the entire mutating surface of a run.

It was eleven. The nine that went -- Gate B/C/D, both regeneration routes, the
candidate route and the three cell routes -- were removed with feature
verification, not merely delisted here;
`test_every_removed_route_is_gone_from_the_router` asserts they answer 404 as
unknown paths, and `test_the_list_above_is_actually_every_mutating_route`
compares this list against the router so a re-added route cannot slip past.
"""


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
        json={"technical_summary": SUMMARY, "technical_elements": ELEMENTS},
        headers=auth(),
    )
    body = response.json()
    assert set(body) >= {"code", "detail"}
    assert isinstance(body["detail"], str)


def test_no_error_body_carries_a_traceback_or_a_provider_body(client, paused_run):
    response = client.post(
        f"/api/runs/{paused_run}/gates/A/approve",
        json={"technical_summary": SUMMARY, "technical_elements": ELEMENTS},
        headers=auth(),
    )
    rendered = response.text.lower()
    for leaked in ("traceback", "file \"", "psycopg", "api_key", "bearer ", "sk-"):
        assert leaked not in rendered


# --- admission ----------------------------------------------------------------


def test_admission_runs_no_verifier_policy_check(client, pg, created_runs):
    """Replaces `test_admission_refuses_before_writing_anything_when_a_policy_is_unrecorded`.

    That test asserted `422 POLICY_NOT_APPROVED` when a verifier policy was
    unrecorded. Advanced Search screens no dates, selects no text version and
    runs no verifier, so refusing a search over that configuration would block a
    feature on settings for a feature that no longer exists (spec section 10.4).

    The guarantee is inverted rather than dropped: admission must **not** consult
    the policy module at all. Patching it to explode proves the call is gone --
    if anything on this path still reached it, this admission would fail.
    """
    before = pg.execute("SELECT count(*) FROM verification_runs").fetchone()[0]

    with patch("evidence.policy.validate_admission_policies") as validate:
        validate.side_effect = AssertionError(
            "admission must not consult the verifier policy module"
        )
        response = client.post(
            "/api/runs", json={"description": "an exhaust heat recovery system"},
            headers=auth(),
        )

    assert response.status_code == 201
    body = response.json()
    created_runs.append(body["run_id"])
    assert body["status"] == "QUEUED"
    assert "capability_qualified" not in body, "the withdrawn field is back"
    after = pg.execute("SELECT count(*) FROM verification_runs").fetchone()[0]
    assert after == before + 1


def test_admission_records_the_legacy_policy_columns_as_not_applicable(
    client, pg, created_runs
):
    """The columns are NOT NULL and survive (constraint 7), so something must go
    in them. An explicit sentinel says the question does not apply to this run,
    where a real policy name would record a screening decision nobody made."""
    run_id = client.post(
        "/api/runs", json={"description": "a graphene desalination membrane"},
        headers=auth(),
    ).json()["run_id"]
    created_runs.append(run_id)

    row = pg.execute(
        "SELECT date_policy, text_version_policy, cutoff_date, problem_solved,"
        " inventor_novelty_statement FROM verification_runs WHERE id = %s",
        (run_id,),
    ).fetchone()
    assert row[0] == "advanced_search_not_applicable"
    assert row[1] == "advanced_search_not_applicable"
    assert row[2] is None, "cutoff_date is no longer collected"
    assert not row[3] and not row[4], "the context columns are no longer collected"


def test_admission_requires_a_description(client):
    response = client.post("/api/runs", json={}, headers=auth())
    assert response.status_code == 422


def test_an_owner_can_cancel_a_live_run(client, pg, make_run):
    run_id = make_run(created_by=OWNER)

    response = client.delete(f"/api/runs/{run_id}", headers=auth())

    assert response.status_code == 204
    assert pg.execute(
        "SELECT 1 FROM verification_runs WHERE id = %s", (run_id,)
    ).fetchone() is None
    assert pg.execute(
        "SELECT 1 FROM run_leases WHERE run_id = %s", (run_id,)
    ).fetchone() is None


def test_cancelling_a_running_run_fences_its_worker(client, pg, make_run):
    run_id = make_run(created_by=OWNER)
    token = lease.acquire(pg, run_id=run_id, owner_id=WORKER, ttl_seconds=TTL)
    assert token is not None
    store.transition_status(
        pg,
        run_id=run_id,
        owner_id=WORKER,
        fence_token=token,
        expected_from=(RunStatus.QUEUED,),
        to_status=RunStatus.RUNNING,
    )

    response = client.delete(f"/api/runs/{run_id}", headers=auth())

    assert response.status_code == 204
    assert not lease.heartbeat(
        pg,
        run_id=run_id,
        owner_id=WORKER,
        fence_token=token,
        ttl_seconds=TTL,
    )


def test_cancel_keeps_a_terminal_run_in_history(client, pg, make_run):
    run_id = make_run(created_by=OWNER)
    token = lease.acquire(pg, run_id=run_id, owner_id=WORKER, ttl_seconds=TTL)
    assert token is not None
    events.finalize_run_failure(
        pg,
        run_id=run_id,
        owner_id=WORKER,
        fence_token=token,
        payload={"detail": "The provider failed."},
        failure_code="PROVIDER_ERROR",
    )

    response = client.delete(f"/api/runs/{run_id}", headers=auth())

    assert response.status_code == 204
    assert pg.execute(
        "SELECT status FROM verification_runs WHERE id = %s", (run_id,)
    ).fetchone()[0] == RunStatus.FAILED


def test_cancel_is_ownership_scoped(client, pg, make_run, second_user):
    run_id = make_run(created_by=OWNER)

    response = client.delete(f"/api/runs/{run_id}", headers=auth(second_user))

    assert response.status_code == 404
    assert pg.execute(
        "SELECT 1 FROM verification_runs WHERE id = %s", (run_id,)
    ).fetchone() is not None


@pytest.mark.parametrize(
    "withdrawn",
    ["problem_solved", "inventor_novelty_statement", "cutoff_date", "anything_else"],
)
def test_admission_refuses_an_unknown_field_rather_than_ignoring_it(client, withdrawn):
    """A client still sending the withdrawn fields has not been updated.

    Accepting the request would drop what it sent while reporting success, so the
    caller would believe context reached the run when nothing did.
    """
    response = client.post(
        "/api/runs",
        json={"description": "an exhaust heat recovery system", withdrawn: "x"},
        headers=auth(),
    )
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


# --- the removed surface -----------------------------------------------------
#
# The candidate and rerun test blocks were REMOVED here with the routes they
# covered (`POST /candidates`, `POST /cells/{id}/rerun`). They asserted that a
# manually added publication was a *request* rather than an approval and never
# touched a provider, and that a rerun produced a new version rather than
# editing one. Both are guarantees about human work requests against the
# verification matrix; feature verification was withdrawn, so there is no matrix,
# no candidate set and no cell to rerun. `graph/work_requests.py` is retained as
# legacy (spec section 10.3) with its own focused unit tests, and
# `tests/test_work_requests.py` asserts no compiled graph can reach it.
#
# What replaces them is the assertion below: the routes are gone from the router
# entirely, so a client calling one gets an unknown path rather than a handler
# that half-works.


REMOVED_ROUTES = [
    ("POST", "understanding/regenerate"),
    ("POST", "strategy/regenerate"),
    ("GET", "gates/B"),
    ("POST", "gates/B/approve"),
    ("GET", "gates/C"),
    ("POST", "gates/C/complete"),
    ("POST", "gates/D/approve"),
    ("POST", "candidates"),
    ("GET", "cells/cell-1"),
    ("POST", "cells/cell-1/review"),
    ("POST", "cells/cell-1/waiver"),
    ("POST", "cells/cell-1/rerun"),
]


@pytest.mark.parametrize("method,path", REMOVED_ROUTES)
def test_every_removed_route_is_gone_from_the_router(client, paused_run, method, path):
    """**Spec section 8.5 row 30.** Not disabled, not 403, not a stub: absent.

    A 404 from an unknown path is the honest answer for a route the product no
    longer has. Asserted with the OWNER's token, so a 404 here cannot be the
    ownership 404 in disguise -- this caller owns the run.
    """
    response = client.request(
        method, f"/api/runs/{paused_run}/{path}",
        json={"idempotency_key": "k1"} if method == "POST" else None,
        headers=auth(),
    )
    assert response.status_code == 404
    # The ownership 404 carries a code; an unknown path does not.
    assert response.json().get("code") != "RUN_NOT_FOUND"


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
