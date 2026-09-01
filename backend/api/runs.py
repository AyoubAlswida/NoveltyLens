"""The verified-run HTTP surface (spec section 10).

## The one rule that shapes every handler in this file

**No endpoint here resumes the graph.** An approval commits a human decision, an
audit row, an idempotency record, a `resumed` event and the
`WAITING_FOR_REVIEW -> RESUMING` transition -- all five in one transaction, all
five or none. Then it returns. The **executor** notices the run on its next poll,
acquires the lease and issues `Command(resume=...)`.

That separation is not an implementation detail; it is what makes two properties
true at once. A duplicate approval is harmless, because the second one matches
zero rows and the idempotency record hands back the first one's response. And a
crash mid-approval is recoverable, because either the transaction committed and
the executor will find it, or it did not and nothing happened.

`tests/test_workflow_api.py` patches the graph and asserts an approval never
invokes it.

## Ownership, on every single path

Every read, stream and mutation resolves the run with `actor_email` **in the same
query that fetches it**. A run the caller does not own answers `404
RUN_NOT_FOUND`, identical to an unknown run, so ids cannot be enumerated. There is
no `403` anywhere in this file, and no `_system_*` store function is reachable
from it.
"""

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Body, Request
from fastapi.responses import StreamingResponse

import db
from api.deps import ApiError, CurrentUser, LastEventId, require_idempotency_key, to_http
from workflow import approvals, evidence_store, review as review_store, store, stream
from workflow.idempotency import idempotent
from workflow.types import IllegalStateError, RunNotFoundError, RunStatus, WorkflowGate

router = APIRouter(prefix="/api/runs", tags=["verified runs"])

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    # Nginx and most ingress controllers buffer proxied responses by default,
    # which turns a live stream into one delivery at the end. The existing
    # `/api/analyze/stream` sets the same header for the same reason.
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_view(run: dict[str, Any], *, last_event_id: Optional[int] = None) -> dict[str, Any]:
    """The public shape of a run. Carries no lease, owner or checkpoint internal."""
    view: dict[str, Any] = {
        "run_id": run["run_id"],
        "status": str(run["status"]),
        "current_gate": None if run["current_gate"] is None else str(run["current_gate"]),
        "description": run["description"],
        "created_at": run["created_at"].isoformat() if run.get("created_at") else None,
        "updated_at": run["updated_at"].isoformat() if run.get("updated_at") else None,
    }
    # **Why a failed run says why.** `verification_runs.failure_detail` is written
    # through `workflow/redaction.py`, so what is stored is already the sanitized
    # form -- provider, HTTP status, workflow step, and nothing credential
    # bearing. Withholding it left the client with a status and no reason, and
    # the error screen exists precisely to render this string verbatim: "SerpAPI
    # returned 429" is the whole point of showing the user anything.
    #
    # Only on a failed run, and only when there is one: an empty key would be a
    # field the client has to distinguish from a missing one.
    if run.get("failure_code"):
        view["failure_code"] = str(run["failure_code"])
    if run.get("failure_detail"):
        view["failure_detail"] = str(run["failure_detail"])
    if last_event_id is not None:
        view["last_event_id"] = last_event_id
    return view


def _own_at_gate(conn, run_id: str, email: str, gate: WorkflowGate) -> dict:
    """Ownership **and** the run actually being parked at this gate.

    Spec section 10.1: an approval for a gate the run is not at is
    `409 WRONG_GATE`. Checked here, before any write, and that ordering is the
    point rather than a nicety.

    Without it the handler ran the approval writer first and only discovered the
    problem at the transition, by which time the approval had already attempted
    a second version of an artifact -- surfacing as a unique-constraint violation
    and a `500`. A caller approving the wrong gate has made an ordinary mistake
    and deserves to be told which one, not handed an internal error.

    `WAITING_FOR_REVIEW` is the only status at which any gate can be approved: a
    `QUEUED` run is waiting for a worker, a `RUNNING` run is mid-execution, a
    `RESUMING` run has already been approved once, and a terminal run is over.
    """
    run = _own(conn, run_id, email)
    if run["status"] is not RunStatus.WAITING_FOR_REVIEW:
        raise IllegalStateError(
            f"run {run_id} is {run['status']}, not WAITING_FOR_REVIEW: there is no "
            f"gate {gate.value} approval to make."
        )
    if run["current_gate"] is not None and run["current_gate"] is not gate:
        raise IllegalStateError(
            f"run {run_id} is waiting at gate {run['current_gate']}, not {gate.value}."
        )
    return run


def _own(conn, run_id: str, email: str) -> dict:
    """Resolve the run **before anything is written**, or raise `RunNotFoundError`.

    Ownership is checked first on every mutating path, ahead of the idempotency
    record, and that ordering is load-bearing rather than tidy. `idempotent()`
    inserts its record as the first statement of the transaction; entering it
    before the ownership check would let a caller who owns nothing write a row
    keyed to somebody else's run -- a write on another user's resource, performed
    by a request that is about to be told the run does not exist.

    It also fixes what the caller is told. Without this, the ownership failure
    surfaced from deep inside an approval writer as an unmapped exception and
    became `500 INTERNAL_ERROR`; a non-owner must get the same `404
    RUN_NOT_FOUND` every other path gives them.
    """
    return store.get_run(conn, run_id=run_id, actor_email=email)


# --- admission --------------------------------------------------------------


@router.post("", status_code=201)
async def create_run(email: CurrentUser, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Admit a durable verified run. **Returns immediately**; the worker executes.

    ## Everything is validated before anything is written

    Spec section 10: the date and text-version policies, their recorded
    decisions, and the verifier capability decision are all checked **before**
    the first insert. On failure this returns `422 POLICY_NOT_APPROVED` with
    `{missing: [...]}` and creates **no run, no lease, no checkpoint, no event and
    no successful idempotency record** -- there is nothing to clean up because
    nothing was made.

    That ordering is the whole design. A run admitted and then found
    unadmissible would sit `QUEUED` forever, or worse, be picked up by a worker
    and executed under a policy nobody recorded.

    ## What admission does not do

    It does not execute anything, and it does not call the graph. `create_run`
    inserts the run as `QUEUED`, creates its permanent lease row at token 0 and
    appends the seq-1 `queued` event, all in one transaction. The **worker**
    claims it on its next poll. Without `python -m workflow.worker` running, an
    admitted run stays `QUEUED` -- adding API replicas does not help, because the
    API never executes a graph.
    """
    from evidence.policy import PolicyNotApproved, validate_admission_policies

    description = str(body.get("description") or "").strip()
    if not description:
        raise ApiError(
            422, "INVALID_REQUEST", "A run needs a description of the invention."
        )

    try:
        policies = validate_admission_policies()
    except PolicyNotApproved as error:
        # Structured, safe fields. `missing` names configuration, never a value.
        raise ApiError(
            422,
            "POLICY_NOT_APPROVED",
            "This deployment cannot admit a verified run yet.",
            missing=list(getattr(error, "missing", []) or []),
        ) from error
    except Exception as error:  # CapabilityNotQualified and anything else
        raise ApiError(
            422,
            "POLICY_NOT_APPROVED",
            "This deployment cannot admit a verified run yet.",
            missing=[type(error).__name__],
        ) from error

    with db.connect() as conn:
        try:
            # One transaction: a run never exists without the submission it was
            # created from. `description` is the required source input and is
            # never rewritten; the two optional fields are context for the
            # analysis and are never treated as evidence.
            with conn.transaction():
                run_id = store.create_run(
                    conn,
                    description=description,
                    claims=body.get("claims"),
                    cutoff_date=body.get("cutoff_date"),
                    date_policy=policies.date_screening_policy,
                    text_version_policy=policies.text_version_policy,
                    created_by=email,
                )
                store.set_submission_context(
                    conn,
                    run_id=run_id,
                    problem_solved=str(body.get("problem_solved") or "").strip(),
                    inventor_novelty_statement=str(
                        body.get("inventor_novelty_statement") or ""
                    ).strip(),
                )
        except Exception as error:
            raise to_http(error) from error

    return {
        "run_id": run_id,
        "status": "QUEUED",
        "stream_url": f"/api/runs/{run_id}/stream",
        # Stated at admission rather than discovered in the report. A run
        # admitted under a recorded deferral produces verdicts from a verifier
        # this runtime has never qualified, and the person starting it is the one
        # who should know.
        "capability_qualified": policies.capability_qualified,
    }


# --- reads ------------------------------------------------------------------


@router.get("")
async def list_runs(email: CurrentUser) -> list[dict[str, Any]]:
    """The caller's runs, newest first. Never anybody else's."""
    with db.connect() as conn:
        runs = store.list_runs(conn, actor_email=email)
    return [_run_view(run) for run in runs]


@router.get("/{run_id}")
async def get_run(run_id: str, email: CurrentUser) -> dict[str, Any]:
    with db.connect() as conn:
        try:
            run = store.get_run(conn, run_id=run_id, actor_email=email)
            from workflow import events as event_log

            last = event_log.max_sequence(conn, run_id=run_id)
        except Exception as error:
            raise to_http(error, run_id=run_id) from error
    return _run_view(run, last_event_id=last)


@router.get("/{run_id}/stream")
async def stream_run(
    run_id: str, request: Request, email: CurrentUser, last_event_id: LastEventId
) -> StreamingResponse:
    """Replay what the client missed, then tail live (spec section 9).

    **Ownership is checked before the response starts.** A `StreamingResponse`
    whose generator raises after the first byte has already sent `200`, so the
    404 for somebody else's run would arrive as a broken stream instead of a
    status. The check happens here, synchronously, and the generator only runs
    once it has passed.

    **Disconnecting does nothing to the run** -- no cancellation, no status
    change, no event. `request.is_disconnected()` is deliberately not consulted
    anywhere: execution lives in a worker that has never heard of this
    connection.
    """
    with db.connect() as conn:
        try:
            store.get_run(conn, run_id=run_id, actor_email=email)
        except Exception as error:
            raise to_http(error, run_id=run_id) from error

    def frames():
        yield from stream.stream_frames(
            run_id=run_id, actor_email=email, last_event_id=last_event_id
        )

    return StreamingResponse(frames(), media_type="text/event-stream", headers=SSE_HEADERS)


# --- Workflow Gate A: the feature decomposition -----------------------------


@router.get("/{run_id}/gates/A")
async def gate_a(run_id: str, email: CurrentUser) -> dict[str, Any]:
    """The **candidate** invention understanding. Note the field name.

    This is not approved. `verify_features_node` reads `approved_decomposition`
    and has no code path that falls back to this one, and
    `tests/test_approval_boundary.py` asserts that at the AST level.

    The payload carries the inventor's untouched submission beside the machine's
    proposal, plus the limits the stage enforces, so the screen refuses an edit
    at the moment it is made rather than at the moment it is submitted. The
    backend enforces the same limits in `workflow/review.py`; that is the binding
    check and this is the courtesy.
    """
    from graph.gates import GATE_A_RULES
    from graph.limits import (
        MAX_CAPABILITIES,
        MAX_ELEMENTS_PER_CAPABILITY,
        MAX_ELEMENTS_TOTAL,
    )
    from workflow import gate_state
    from workflow import understanding as understanding_store

    with db.connect() as conn:
        try:
            run = store.get_run(conn, run_id=run_id, actor_email=email)
            version = approvals.latest_approved_decomposition_version(conn, run_id=run_id)
            proposal = understanding_store.latest_proposal(
                conn, run_id=run_id, actor_email=email
            )
            regenerations = store.list_understanding_regenerations(
                conn, run_id=run_id, actor_email=email
            )
        except Exception as error:
            raise to_http(error, run_id=run_id) from error

    # Ownership first, ALWAYS, then the state read. `gate_state` takes no actor,
    # so the resolve above is what stands between this payload and another
    # account's run.
    state = gate_state.read_state(run_id)
    if state is None:
        # Not an empty gate. The reviewer must be able to tell "the machine
        # proposed nothing" from "we could not read what it proposed".
        raise ApiError(
            503,
            "CHECKPOINT_UNAVAILABLE",
            "The run's state could not be read, so there is nothing to review "
            "yet. This is not an empty analysis.",
        )

    return {
        **gate_state.gate_a_payload(state),
        # The run row is the record of what was submitted and survives a
        # checkpoint compaction, so it wins over graph state for these three.
        "original_description": run["description"],
        "problem_solved": run.get("problem_solved") or "",
        "inventor_novelty_statement": run.get("inventor_novelty_statement") or "",
        # What the machine originally said, kept even after the inventor edits,
        # so the screen can show the comparison rather than only the result.
        "proposal": proposal,
        "regenerations": [
            {
                "from_version": item["from_version"],
                "feedback": item["feedback"],
                "processed": item["processed"],
            }
            for item in regenerations
        ],
        "editable": True,
        "approved_version": version,
        "completion_rules": list(GATE_A_RULES),
        "limits": {
            "max_capabilities": MAX_CAPABILITIES,
            "max_elements_per_capability": MAX_ELEMENTS_PER_CAPABILITY,
            "max_elements_total": MAX_ELEMENTS_TOTAL,
        },
    }


@router.post("/{run_id}/gates/A/approve")
async def approve_gate_a(
    run_id: str, email: CurrentUser, body: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    """Commit the invention-understanding approval. **Does not call the graph.**

    One human act approves the summary and the technical capabilities together,
    at one version. The inventor's original description is untouched by this and
    by everything else -- an approved summary is stored beside it, never over it.

    The edited proposal is recorded as a `USER_EDIT` proposal row in the same
    transaction, so "what the machine said" and "what the human approved" are
    both retrievable afterwards. A stage that kept only the second could not show
    that the human changed anything.
    """
    key = _key(body)
    decomposition = body.get("decomposition") or {}
    features = decomposition.get("features") or decomposition.get("decompositions") or []
    summary = str(decomposition.get("summary") or body.get("summary") or "")
    version = int(decomposition.get("version") or 0)

    from workflow import understanding as understanding_store

    with db.connect() as conn:
        try:
            _own(conn, run_id, email)
            with idempotent(
                conn,
                actor_email=email,
                scope="gate_a_approve",
                resource_id=run_id,
                idempotency_key=key,
                request_body=body,
            ) as slot:
                if slot.replayed:
                    return slot.response
                _own_at_gate(conn, run_id, email, WorkflowGate.A)
                # Every completion rule, checked before anything is written, so an
                # invalid understanding is refused with the rules it broke rather
                # than by a constraint violation surfacing as a 500.
                review_store.require_gate_clear(
                    conn,
                    run_id=run_id,
                    gate=WorkflowGate.A,
                    payload={"decomposition": features, "summary": summary},
                )
                approved_at = _now()
                understanding_store.record_proposal_if_absent(
                    conn,
                    run_id=run_id,
                    version=version,
                    source=understanding_store.SOURCE_USER_EDIT,
                    summary=summary,
                    features=features,
                    created_by=email,
                )
                written = approvals.approve_understanding_by_owner(
                    conn,
                    run_id=run_id,
                    actor_email=email,
                    version=version,
                    summary=summary,
                    features=features,
                    approved_at=approved_at,
                )
                review_store.write_audit(
                    conn,
                    run_id=run_id,
                    actor_email=email,
                    workflow_stage=str(WorkflowGate.A),
                    field="understanding_version",
                    previous_value="",
                    new_value=str(version),
                    reason="Invention understanding approved.",
                )
                store.begin_resume(
                    conn,
                    run_id=run_id,
                    actor_email=email,
                    gate=WorkflowGate.A,
                    resume_payload={
                        "gate": str(WorkflowGate.A),
                        "status": str(store.RunStatus.RESUMING),
                        "at": approved_at,
                        "approved_by": email,
                    },
                    dedupe_key=f"resumed:A:v{version}",
                )
                response = {
                    "decomposition_version": written,
                    "understanding_version": version,
                    "status": "RESUMING",
                }
                slot.record(200, response)
                return response
        except Exception as error:
            raise to_http(error, run_id=run_id) from error


@router.post("/{run_id}/understanding/regenerate", status_code=202)
async def regenerate_understanding(
    run_id: str, email: CurrentUser, body: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    """Ask for a fresh analysis of the invention. **202 -- the worker does it.**

    Records a **request** and does none of the work. Generating here would put an
    LLM call on a request thread with no lease, no fencing and no recovery; the
    executor re-enters the proposing node under the run's lease, exactly as it
    does for a manual candidate or a cell rerun.

    A regeneration produces a **new version** and never edits the version it was
    asked to improve on. `from_version` must name the version the inventor was
    looking at, so a request composed against a proposal that has since been
    replaced is refused rather than silently applied to a different one.

    The run is moved out of `WAITING_FOR_REVIEW` in the same transaction, because
    a rejection is a decision too: the run has to resume for the worker to act on
    it. Nothing about this is an approval -- no approved artifact is written, and
    the gate node re-reads the pending request rather than the resume signal.
    """
    key = _key(body)
    from_version = int(body.get("from_version") or 0)
    feedback = str(body.get("feedback") or "").strip()

    with db.connect() as conn:
        try:
            _own(conn, run_id, email)
            with idempotent(
                conn,
                actor_email=email,
                scope="understanding_regenerate",
                resource_id=run_id,
                idempotency_key=key,
                request_body=body,
            ) as slot:
                if slot.replayed:
                    return slot.response
                _own_at_gate(conn, run_id, email, WorkflowGate.A)
                requested = store.request_understanding_regeneration(
                    conn,
                    run_id=run_id,
                    actor_email=email,
                    from_version=from_version,
                    feedback=feedback,
                )
                review_store.write_audit(
                    conn,
                    run_id=run_id,
                    actor_email=email,
                    workflow_stage=str(WorkflowGate.A),
                    field="understanding_regeneration",
                    previous_value=str(from_version),
                    new_value="requested",
                    reason=feedback or "Re-analysis requested with no feedback.",
                )
                store.begin_resume(
                    conn,
                    run_id=run_id,
                    actor_email=email,
                    gate=WorkflowGate.A,
                    resume_payload={
                        "gate": str(WorkflowGate.A),
                        "status": str(store.RunStatus.RESUMING),
                        "at": _now(),
                        "requested_by": email,
                        # Named so a reader of the event log can tell a rejection
                        # from an approval without inferring it from what is absent.
                        "action": "REGENERATE",
                    },
                    dedupe_key=f"resumed:A:regenerate:v{from_version}",
                )
                response = {
                    "status": "RESUMING",
                    "from_version": from_version,
                    "already_requested": requested["already_requested"],
                }
                slot.record(202, response)
                return response
        except Exception as error:
            raise to_http(error, run_id=run_id) from error


# --- The SEARCH STRATEGY stage ----------------------------------------------


@router.get("/{run_id}/gates/STRATEGY")
async def gate_strategy(run_id: str, email: CurrentUser) -> dict[str, Any]:
    """The **candidate** search strategy. Note the field name.

    This is not approved. The search reads `queries`, which is written from the
    approved strategy by `gate_strategy_node` and by nothing else, so a proposal
    read from here can never become a dispatched query by being looked at.

    The payload carries the approved understanding beside it -- the summary and
    the feature-qualified element ids, with the core ones marked -- because
    coverage is what this stage is for and a reviewer cannot judge it against ids
    they cannot see.
    """
    from graph.gates import GATE_STRATEGY_RULES
    from graph.limits import MAX_STRATEGY_QUERIES
    from workflow import gate_state
    from workflow import strategy as strategy_store

    with db.connect() as conn:
        try:
            store.get_run(conn, run_id=run_id, actor_email=email)
            version = approvals.latest_approved_search_strategy_version(
                conn, run_id=run_id
            )
            proposal = strategy_store.latest_proposal(
                conn, run_id=run_id, actor_email=email
            )
            regenerations = store.list_strategy_regenerations(
                conn, run_id=run_id, actor_email=email
            )
        except Exception as error:
            raise to_http(error, run_id=run_id) from error

    # Ownership first, ALWAYS, then the state read. `gate_state` takes no actor,
    # so the resolve above is what stands between this payload and another
    # account's run.
    state = gate_state.read_state(run_id)
    if state is None:
        # Not an empty strategy. The reviewer must be able to tell "the machine
        # proposed no queries" from "we could not read what it proposed".
        raise ApiError(
            503,
            "CHECKPOINT_UNAVAILABLE",
            "The run's state could not be read, so the search strategy cannot be "
            "shown. This is not an empty strategy.",
        )

    return {
        **gate_state.gate_strategy_payload(state),
        # What the machine originally proposed, kept even after the reviewer
        # edits, so the screen can show the comparison rather than only the result.
        "proposal": proposal,
        "regenerations": [
            {
                "from_version": item["from_version"],
                "feedback": item["feedback"],
                "processed": item["processed"],
            }
            for item in regenerations
        ],
        "editable": True,
        "approved_version": version,
        "completion_rules": list(GATE_STRATEGY_RULES),
        "limits": {"max_queries": MAX_STRATEGY_QUERIES},
    }


@router.post("/{run_id}/gates/STRATEGY/approve")
async def approve_gate_strategy(
    run_id: str, email: CurrentUser, body: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    """Commit the search-strategy approval. **Does not call the graph.**

    The three checks run in the order every mutating endpoint here uses:
    ownership before any write, idempotency before the gate precondition so a
    retry of an identical approval replays, and the gate precondition before the
    mutation so approving the wrong stage is a `409 WRONG_GATE` rather than a
    constraint violation surfacing as a 500.

    Every completion rule is checked before anything is written, so a strategy
    that leaves a core element uncovered is refused **with the elements it left
    uncovered** rather than by a write error.

    The edited strategy is recorded as a `USER_EDIT` proposal row in the same
    transaction, so what the machine proposed and what the human approved are both
    retrievable afterwards. A stage that kept only the second could not show that
    the reviewer changed anything -- which, for the queries a run was built on, is
    the whole audit question.
    """
    key = _key(body)
    strategy = body.get("strategy") or {}
    queries = list(strategy.get("queries") or [])
    version = int(strategy.get("version") or 0)
    understanding_version = int(strategy.get("understanding_version") or 0)

    from workflow import strategy as strategy_store

    with db.connect() as conn:
        try:
            _own(conn, run_id, email)
            with idempotent(
                conn,
                actor_email=email,
                scope="gate_strategy_approve",
                resource_id=run_id,
                idempotency_key=key,
                request_body=body,
            ) as slot:
                if slot.replayed:
                    return slot.response
                _own_at_gate(conn, run_id, email, WorkflowGate.STRATEGY)
                review_store.require_gate_clear(
                    conn,
                    run_id=run_id,
                    gate=WorkflowGate.STRATEGY,
                    payload={
                        "queries": queries,
                        "understanding_version": understanding_version,
                    },
                )
                approved_at = _now()
                strategy_store.record_proposal_if_absent(
                    conn,
                    run_id=run_id,
                    version=version,
                    understanding_version=understanding_version,
                    source=strategy_store.SOURCE_USER_EDIT,
                    queries=queries,
                    created_by=email,
                )
                written = approvals.approve_search_strategy_by_owner(
                    conn,
                    run_id=run_id,
                    actor_email=email,
                    version=version,
                    understanding_version=understanding_version,
                    queries=queries,
                    approved_at=approved_at,
                )
                enabled = sum(1 for query in queries if query.get("enabled"))
                review_store.write_audit(
                    conn,
                    run_id=run_id,
                    actor_email=email,
                    workflow_stage=str(WorkflowGate.STRATEGY),
                    field="strategy_version",
                    previous_value="",
                    new_value=str(version),
                    reason=f"Search strategy approved: {enabled} of {written} "
                    f"queries enabled.",
                )
                store.begin_resume(
                    conn,
                    run_id=run_id,
                    actor_email=email,
                    gate=WorkflowGate.STRATEGY,
                    resume_payload={
                        "gate": str(WorkflowGate.STRATEGY),
                        "status": str(store.RunStatus.RESUMING),
                        "at": approved_at,
                        "approved_by": email,
                    },
                    dedupe_key=f"resumed:STRATEGY:v{version}",
                )
                response = {
                    "strategy_version": version,
                    "queries_approved": written,
                    "queries_enabled": enabled,
                    "status": "RESUMING",
                }
                slot.record(200, response)
                return response
        except Exception as error:
            raise to_http(error, run_id=run_id) from error


@router.post("/{run_id}/strategy/regenerate", status_code=202)
async def regenerate_strategy(
    run_id: str, email: CurrentUser, body: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    """Ask for fresh search queries. **202 -- the worker does it.**

    Records a **request** and does none of the work, for the same reason the
    understanding stage's regeneration does: generating here would put an LLM call
    on a request thread with no lease, no fencing and no recovery. The executor
    re-enters the proposing node under the run's lease.

    A regeneration produces a **new version** and never edits the version it was
    asked to improve on. `from_version` must name the version the reviewer was
    looking at, so a request composed against a strategy that has since been
    replaced is refused rather than silently applied to a different one.

    The run is moved out of `WAITING_FOR_REVIEW` in the same transaction, because
    a rejection is a decision too. Nothing about it is an approval: no approved
    artifact is written, and the gate node re-reads the pending request rather
    than the resume signal.
    """
    key = _key(body)
    from_version = int(body.get("from_version") or 0)
    feedback = str(body.get("feedback") or "").strip()

    with db.connect() as conn:
        try:
            _own(conn, run_id, email)
            with idempotent(
                conn,
                actor_email=email,
                scope="strategy_regenerate",
                resource_id=run_id,
                idempotency_key=key,
                request_body=body,
            ) as slot:
                if slot.replayed:
                    return slot.response
                _own_at_gate(conn, run_id, email, WorkflowGate.STRATEGY)
                requested = store.request_strategy_regeneration(
                    conn,
                    run_id=run_id,
                    actor_email=email,
                    from_version=from_version,
                    feedback=feedback,
                )
                review_store.write_audit(
                    conn,
                    run_id=run_id,
                    actor_email=email,
                    workflow_stage=str(WorkflowGate.STRATEGY),
                    field="strategy_regeneration",
                    previous_value=str(from_version),
                    new_value="requested",
                    reason=feedback or "Fresh queries requested with no feedback.",
                )
                store.begin_resume(
                    conn,
                    run_id=run_id,
                    actor_email=email,
                    gate=WorkflowGate.STRATEGY,
                    resume_payload={
                        "gate": str(WorkflowGate.STRATEGY),
                        "status": str(store.RunStatus.RESUMING),
                        "at": _now(),
                        "requested_by": email,
                        # Named so a reader of the event log can tell a rejection
                        # from an approval without inferring it from what is absent.
                        "action": "REGENERATE",
                    },
                    dedupe_key=f"resumed:STRATEGY:regenerate:v{from_version}",
                )
                response = {
                    "status": "RESUMING",
                    "from_version": from_version,
                    "already_requested": requested["already_requested"],
                }
                slot.record(202, response)
                return response
        except Exception as error:
            raise to_http(error, run_id=run_id) from error


# --- Workflow Gate B: the candidate set -------------------------------------


@router.get("/{run_id}/gates/B")
async def gate_b(run_id: str, email: CurrentUser) -> dict[str, Any]:
    from graph.gates import GATE_B_RULES
    from workflow import gate_state

    with db.connect() as conn:
        try:
            store.get_run(conn, run_id=run_id, actor_email=email)
            version = approvals.latest_approved_candidate_set_version(conn, run_id=run_id)
            manual = store.list_manual_candidates(conn, run_id=run_id, actor_email=email)
        except Exception as error:
            raise to_http(error, run_id=run_id) from error

    state = gate_state.read_state(run_id)
    if state is None:
        raise ApiError(
            503,
            "CHECKPOINT_UNAVAILABLE",
            "The run's state could not be read, so the candidate set cannot be "
            "shown. This is not an empty candidate set.",
        )

    return {
        **gate_state.gate_b_payload(state),
        # Every manual request and whether the worker has carried it out yet, so
        # a reviewer can see that adding is not approving.
        "manual_candidates": manual,
        "approved_version": version,
        "completion_rules": list(GATE_B_RULES),
    }


@router.post("/{run_id}/gates/B/approve")
async def approve_gate_b(
    run_id: str, email: CurrentUser, body: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    key = _key(body)
    numbers = list(body.get("publication_numbers") or [])
    version = int(body.get("version") or 0)

    with db.connect() as conn:
        try:
            _own(conn, run_id, email)
            with idempotent(
                conn,
                actor_email=email,
                scope="gate_b_approve",
                resource_id=run_id,
                idempotency_key=key,
                request_body=body,
            ) as slot:
                if slot.replayed:
                    return slot.response
                _own_at_gate(conn, run_id, email, WorkflowGate.B)
                approved_at = _now()
                # `publications` is a sequence of `(requested, normalized)`
                # pairs, not bare strings: the approval records BOTH identities,
                # because a number quietly corrected into a different real
                # document would attach that document's text to this candidate.
                # The client sends normalized numbers, which are their own
                # requested form here.
                manual = {
                    str(row["publication_number"]).strip().upper()
                    for row in store.list_manual_candidates(
                        conn, run_id=run_id, actor_email=email
                    )
                }
                pairs = [(str(number), str(number)) for number in numbers]
                written = approvals.approve_candidate_set_by_owner(
                    conn,
                    run_id=run_id,
                    actor_email=email,
                    version=version,
                    publications=pairs,
                    approved_at=approved_at,
                    # Recorded so a report never presents a publication nobody's
                    # search returned as though one did.
                    manually_added=[n for n, _ in pairs if n.strip().upper() in manual],
                )
                review_store.write_audit(
                    conn,
                    run_id=run_id,
                    actor_email=email,
                    workflow_stage=str(WorkflowGate.B),
                    field="candidate_set_version",
                    previous_value="",
                    new_value=str(written),
                    reason="Workflow Gate B approval.",
                )
                store.begin_resume(
                    conn,
                    run_id=run_id,
                    actor_email=email,
                    gate=WorkflowGate.B,
                    resume_payload={
                        "gate": str(WorkflowGate.B),
                        "status": str(store.RunStatus.RESUMING),
                        "at": approved_at,
                        "approved_by": email,
                    },
                    dedupe_key=f"resumed:B:v{written}",
                )
                response = {"candidate_set_version": written, "status": "RESUMING"}
                slot.record(200, response)
                return response
        except Exception as error:
            raise to_http(error, run_id=run_id) from error


@router.post("/{run_id}/candidates", status_code=202)
async def add_candidate(
    run_id: str, email: CurrentUser, body: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    """Add a publication by hand at Workflow Gate B. **202 -- never a bypass.**

    Spec case T: a manually added publication "passes normalization, date
    screening, enrichment, and full-text eligibility **identically** to a
    discovered candidate. If it fails any, it is recorded with that state and
    cannot be approved into the set."

    So this records a **request** and does none of the work. The `202` is the
    design rather than a courtesy: the API cannot fetch a publication, cannot
    build a passage, and above all cannot make anything eligible. The executor
    puts it through the same nodes every discovered candidate goes through, and
    eligibility is still derived from passages actually built (§3.7).

    Approving it into the candidate set is still a separate human act through
    `workflow/approvals.py`. Nothing here shortens that path.
    """
    key = _key(body)
    number = str(body.get("publication_number") or "").strip()

    with db.connect() as conn:
        try:
            _own(conn, run_id, email)
            with idempotent(
                conn,
                actor_email=email,
                scope="add_candidate",
                resource_id=f"{run_id}:{number}",
                idempotency_key=key,
                request_body=body,
            ) as slot:
                if slot.replayed:
                    return slot.response
                recorded = store.request_manual_candidate(
                    conn,
                    run_id=run_id,
                    actor_email=email,
                    publication_number=number,
                )
                review_store.write_audit(
                    conn,
                    run_id=run_id,
                    actor_email=email,
                    workflow_stage=str(WorkflowGate.B),
                    field="manual_candidate",
                    previous_value="",
                    new_value=recorded["publication_number"],
                    reason="Publication added by hand at Workflow Gate B.",
                )
                response = {
                    "publication_number": recorded["publication_number"],
                    "accepted": True,
                    "already_requested": recorded["already_requested"],
                    # Stated so nobody reads a 202 as an approval.
                    "note": (
                        "Queued for the same normalization, date screening, "
                        "enrichment and eligibility checks a discovered candidate "
                        "goes through. It is not in the candidate set and is not "
                        "approved."
                    ),
                }
                slot.record(202, response)
                return response
        except Exception as error:
            raise to_http(error, run_id=run_id) from error


@router.post("/{run_id}/cells/{cell_id}/rerun", status_code=202)
async def rerun_cell(
    run_id: str, cell_id: str, email: CurrentUser, body: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    """Re-run ONE cell with the context the model asked for. **202.**

    Spec case U: "Only the named surrounding material is fetched; only that cell
    re-runs; every other cell's result is byte-identical; new result links to the
    prior one via `source_result_version`."

    **Nothing is edited, replaced or deleted.** The existing machine result stays
    immutable and retrievable; the rerun produces a *new* `result_version`, and
    the response returns that next version rather than the current one so a
    caller cannot mistake one for the other.

    `source_result_version` is required and is checked against what the cell
    actually holds. A rerun composed against a superseded result is
    `409 STALE_RESULT_VERSION` -- the same rule §13.4 check 16 applies to a
    review, and for the same reason: the reviewer was looking at specific
    evidence, and a decision cannot be silently transplanted onto different
    evidence.
    """
    key = _key(body)
    context_requests = list(body.get("context_requests") or [])
    source_version = body.get("source_result_version")
    if source_version is None:
        raise ApiError(
            422,
            "INVALID_REQUEST",
            "source_result_version is required: a rerun must name the result the "
            "reviewer was looking at, so it cannot attach to one that superseded it.",
        )

    with db.connect() as conn:
        try:
            _own(conn, run_id, email)
            with idempotent(
                conn,
                actor_email=email,
                scope="cell_rerun",
                resource_id=f"{run_id}:{cell_id}",
                idempotency_key=key,
                request_body=body,
            ) as slot:
                if slot.replayed:
                    return slot.response
                recorded = store.request_cell_rerun(
                    conn,
                    run_id=run_id,
                    cell_id=cell_id,
                    actor_email=email,
                    context_requests=context_requests,
                    source_result_version=int(source_version),
                )
                review_store.write_audit(
                    conn,
                    run_id=run_id,
                    actor_email=email,
                    workflow_stage=str(WorkflowGate.C),
                    field="cell_rerun",
                    previous_value=str(recorded["source_result_version"]),
                    new_value=str(recorded["result_version"]),
                    reason="Reviewer requested more context and a re-run.",
                    cell_id=cell_id,
                    source_result_version=recorded["source_result_version"],
                )
                response = {
                    "cell_id": cell_id,
                    "result_version": recorded["result_version"],
                    "source_result_version": recorded["source_result_version"],
                    "already_requested": recorded["already_requested"],
                    "note": (
                        "The existing result is unchanged and still retrievable. "
                        "Only this cell re-runs, and only the named context is "
                        "fetched."
                    ),
                }
                slot.record(202, response)
                return response
        except ApiError:
            raise
        except Exception as error:
            raise to_http(error, run_id=run_id) from error


# --- Workflow Gate C: the evidence matrix -----------------------------------


@router.get("/{run_id}/gates/C")
async def gate_c(run_id: str, email: CurrentUser) -> dict[str, Any]:
    """The matrix and **every** blocking condition, not just the first.

    A reviewer who fixes one blocker and is refused again for the next has been
    told half the truth twice.
    """
    from graph.gates import GATE_C_RULES

    with db.connect() as conn:
        try:
            store.get_run(conn, run_id=run_id, actor_email=email)
            cells = store.list_cells(conn, run_id=run_id, actor_email=email)
            blocking = review_store.blocking_conditions(
                conn, run_id=run_id, gate=WorkflowGate.C
            )
        except Exception as error:
            raise to_http(error, run_id=run_id) from error
    return {"matrix": cells, "blocking": blocking, "completion_rules": list(GATE_C_RULES)}


@router.get("/{run_id}/cells/{cell_id}")
async def get_cell(run_id: str, cell_id: str, email: CurrentUser) -> dict[str, Any]:
    """One cell: machine result, passages, review overlay and waiver, kept apart.

    **Machine output and human output are never merged** (constraint 7). They are
    separate keys here for the same reason they are separate rows in the
    database: collapsing them presents a model guess and an expert judgment as
    the same thing.
    """
    with db.connect() as conn:
        try:
            store.get_run(conn, run_id=run_id, actor_email=email)
            cells = {
                cell["cell_id"]: cell
                for cell in store.list_cells(conn, run_id=run_id, actor_email=email)
            }
            cell = cells.get(cell_id)
            if cell is None:
                raise ApiError(404, "CELL_NOT_FOUND", f"No cell {cell_id} in this run.")
            reviews = review_store.list_reviews(conn, run_id=run_id)
            waivers = review_store.list_waivers(conn, run_id=run_id)
            # **The passages this result actually cited**, read from the passage
            # of record rather than from the model's own response. A reviewer's
            # job at Gate C is to check a quotation against its source, and a
            # cell that showed a verdict with no citable text made that
            # impossible -- the endpoint returned no `passages` key at all.
            #
            # Filtered to the cited chunk ids, in the order the result cited
            # them: the publication's other passages were not evidence for this
            # cell, and showing them would suggest the model had seen more than
            # it did.
            everything = evidence_store.list_passages(
                conn,
                run_id=run_id,
                actor_email=email,
                publication_number=cell.get("publication_number"),
            )
            by_chunk = {str(row["chunk_id"]): row for row in everything}
            cited = [
                by_chunk[str(chunk_id)]
                for chunk_id in (cell.get("passage_chunk_ids") or ())
                if str(chunk_id) in by_chunk
            ]
        except ApiError:
            raise
        except Exception as error:
            raise to_http(error, run_id=run_id) from error
    return {
        **cell,
        "passages": cited,
        "review": reviews.get(cell_id),
        "waiver": waivers.get(cell_id, []),
    }


@router.post("/{run_id}/cells/{cell_id}/review")
async def review_cell(
    run_id: str, cell_id: str, email: CurrentUser, body: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    """Save a review overlay, through spec section 13.4's checks 0-18.

    Rejected **wholesale** with the failing check named -- never partially saved.
    Checks 15-18 are the binding ones here: they compare the overlay's declared
    cell, result version, feature, feature version and publication against the
    cell it is being saved into, which is what makes a stale or wrong-cell review
    a rejection rather than a silent misattachment.
    """
    key = _key(body)
    overlay = body.get("overlay") or {}

    with db.connect() as conn:
        try:
            _own(conn, run_id, email)
            with idempotent(
                conn,
                actor_email=email,
                scope="cell_review",
                resource_id=f"{run_id}:{cell_id}",
                idempotency_key=key,
                request_body=body,
            ) as slot:
                if slot.replayed:
                    return slot.response
                saved = review_store.save_review(
                    conn,
                    run_id=run_id,
                    cell_id=cell_id,
                    actor_email=email,
                    overlay=overlay,
                )
                response = {
                    "status": saved.get("status"),
                    "reviewed_outcome": saved.get("reviewed_outcome"),
                }
                slot.record(200, response)
                return response
        except Exception as error:
            raise to_http(error, run_id=run_id) from error


@router.post("/{run_id}/cells/{cell_id}/waiver")
async def waive_cell(
    run_id: str, cell_id: str, email: CurrentUser, body: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    """Record a waiver. **The cell state is unchanged and the response says so.**

    A waiver is a disclosed gap, never a verdict. A waived `VALIDATION_FAILURE`
    is still a `VALIDATION_FAILURE`, with its failing check, in the matrix, the
    draft and the final report (cases AJ and AZ).
    """
    key = _key(body)
    waiver = body.get("waiver") or {}

    with db.connect() as conn:
        try:
            _own(conn, run_id, email)
            with idempotent(
                conn,
                actor_email=email,
                scope="cell_waiver",
                resource_id=f"{run_id}:{cell_id}",
                idempotency_key=key,
                request_body=body,
            ) as slot:
                if slot.replayed:
                    return slot.response
                saved = review_store.save_waiver(
                    conn,
                    run_id=run_id,
                    cell_id=cell_id,
                    actor_email=email,
                    waiver=waiver,
                )
                response = {"cell_state": saved.get("cell_state"), "waived": True}
                slot.record(200, response)
                return response
        except Exception as error:
            raise to_http(error, run_id=run_id) from error


@router.post("/{run_id}/gates/C/complete")
async def complete_gate_c(
    run_id: str, email: CurrentUser, body: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    """Finish Gate C, or refuse with **every** blocking condition named."""
    key = _key(body)
    with db.connect() as conn:
        try:
            _own(conn, run_id, email)
            with idempotent(
                conn,
                actor_email=email,
                scope="gate_c_complete",
                resource_id=run_id,
                idempotency_key=key,
                request_body=body,
            ) as slot:
                if slot.replayed:
                    return slot.response
                # §10.1, and the same order every other mutation uses: own,
                # replay short-circuit, THEN gate. Completing a gate the run is
                # not parked at is `409 WRONG_GATE` -- this handler was the one
                # mutation missing the check, so a completion aimed at a running
                # or terminal run reached `require_gate_clear` and answered
                # about blocking cells instead of about the gate.
                run = _own_at_gate(conn, run_id, email, WorkflowGate.C)
                review_store.require_gate_clear(conn, run_id=run_id, gate=WorkflowGate.C)
                at = _now()
                review_store.write_audit(
                    conn,
                    run_id=run_id,
                    actor_email=email,
                    workflow_stage=str(WorkflowGate.C),
                    field="gate_c_completed_at",
                    previous_value="",
                    new_value=at,
                    reason="Workflow Gate C completed after review.",
                )
                store.begin_resume(
                    conn,
                    run_id=run_id,
                    actor_email=email,
                    gate=WorkflowGate.C,
                    resume_payload={
                        "gate": str(WorkflowGate.C),
                        "status": str(store.RunStatus.RESUMING),
                        "at": at,
                        "approved_by": email,
                    },
                    # **The pause being answered, not the gate.**
                    #
                    # Gate C is the one gate a run can legitimately reach more
                    # than once: completing it with a cell rerun outstanding
                    # sends the run through `rerun_cells` and straight back to
                    # `gate_c`. A fixed `"resumed:C"` made the second completion
                    # collide with the first and answer 409
                    # IDEMPOTENCY_CONFLICT -- refusing a resume that was not a
                    # replay at all, and stranding the run at a gate it had
                    # already satisfied.
                    #
                    # While the run is `WAITING_FOR_REVIEW` nothing is appended
                    # after `paused`, so the run's sequence allocator does not
                    # move: `next_sequence` is stable for as long as this pause
                    # lasts (a genuine retry still dedupes to the same key) and
                    # has advanced by the next one (a second completion is
                    # correctly a second completion).
                    dedupe_key=f"resumed:C:s{int(run.get('next_sequence') or 0)}",
                )
                response = {"status": "RESUMING"}
                slot.record(200, response)
                return response
        except Exception as error:
            raise to_http(error, run_id=run_id) from error


# --- Workflow Gate D: the report --------------------------------------------


@router.get("/{run_id}/report")
async def get_report(run_id: str, email: CurrentUser) -> dict[str, Any]:
    """The latest report version. A draft carries `DRAFT - NOT APPROVED`."""
    with db.connect() as conn:
        try:
            store.get_run(conn, run_id=run_id, actor_email=email)
            latest = review_store.latest_report_version(conn, run_id=run_id)
        except Exception as error:
            raise to_http(error, run_id=run_id) from error
    if latest is None:
        raise ApiError(404, "REPORT_NOT_FOUND", "No draft report exists for this run yet.")
    return latest


@router.post("/{run_id}/gates/D/approve")
async def approve_gate_d(
    run_id: str, email: CurrentUser, body: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    """Approve the draft. Case AC: a report cannot be approved into existence."""
    key = _key(body)
    version = int(body.get("report_version") or 0)

    with db.connect() as conn:
        try:
            _own(conn, run_id, email)
            with idempotent(
                conn,
                actor_email=email,
                scope="gate_d_approve",
                resource_id=run_id,
                idempotency_key=key,
                request_body=body,
            ) as slot:
                if slot.replayed:
                    return slot.response
                _own_at_gate(conn, run_id, email, WorkflowGate.D)
                at = _now()
                review_store.approve_report(
                    conn,
                    run_id=run_id,
                    actor_email=email,
                    version=version,
                    # `or ""`, not `body.get(...)`: both columns are `TEXT NOT
                    # NULL DEFAULT ''`, and passing an explicit `None` overrides
                    # the Python default and sets the column NULL, which the
                    # constraint refuses. An omitted optional field means "the
                    # reviewer wrote nothing", which is the empty string.
                    comments=body.get("comments") or "",
                    caveats=body.get("caveats") or "",
                )
                store.begin_resume(
                    conn,
                    run_id=run_id,
                    actor_email=email,
                    gate=WorkflowGate.D,
                    resume_payload={
                        "gate": str(WorkflowGate.D),
                        "status": str(store.RunStatus.RESUMING),
                        "at": at,
                        "approved_by": email,
                    },
                    dedupe_key=f"resumed:D:v{version}",
                )
                response = {"status": "RESUMING", "report_version": version}
                slot.record(200, response)
                return response
        except Exception as error:
            raise to_http(error, run_id=run_id) from error


def _key(body: dict[str, Any]) -> str:
    try:
        return require_idempotency_key(body)
    except Exception as error:
        raise to_http(error) from error


__all__ = ["router"]
