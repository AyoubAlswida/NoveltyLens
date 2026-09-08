"""The Advanced Search HTTP surface.

Advanced Search is a standard prior-art search preceded by guided editing: the
run pauses three times for a person -- first on the summary, then on limitations
generated from that approved summary, and finally on the search queries -- and then executes
the ordinary search pipeline. It performs no evidence verification, reaches no
legal conclusion, and presents no numeric score.

The Gate B/C/D surface that used to live here -- candidate curation, the
verification matrix and its cells, the draft report and its approval, and both
regeneration endpoints -- was removed when feature verification was withdrawn
from the product. The tables, migrations and rows behind it are all retained
(nothing is deleted to make a change tidy), and so are the modules under
`backend/verification/`, `backend/evidence/` and `graph/work_requests.py`; there
is simply no route to any of it. Editing the summary, the elements and the
queries replaces regeneration.

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
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict

import db
from api.deps import ApiError, CurrentUser, LastEventId, require_idempotency_key, to_http
from workflow import approvals, events, review as review_store, store, stream
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


POLICY_NOT_APPLICABLE = "advanced_search_not_applicable"
"""Written to the legacy `date_policy` / `text_version_policy` columns.

Both are `NOT NULL` (migration 0006) and both were frozen per run so an evidence
verdict could name the policy it was produced under. Advanced Search screens no
dates and selects no text version, so there is no policy to freeze -- and reusing
a real policy name here would record a screening decision no run ever made.

An explicit constant, not `''`: an empty string reads as "nobody filled this in",
while this says the question does not apply to how this run works. Existing rows
keep the values they were admitted under (spec section 10.4).
"""


class CreateRunRequest(BaseModel):
    """The Advanced Search admission body: a description, optionally claims.

    **Strict.** `extra="forbid"` refuses an unknown field rather than ignoring
    it, which is what makes the removal of `problem_solved`,
    `inventor_novelty_statement` and `cutoff_date` visible. A client still
    sending them has not been updated, and silently accepting the request would
    hide that while quietly dropping what it sent -- the caller would believe
    context reached the run when nothing did.
    """

    model_config = ConfigDict(extra="forbid")

    description: str
    claims: Optional[str] = None


@router.post("", status_code=201)
async def create_run(email: CurrentUser, body: CreateRunRequest) -> dict[str, Any]:
    """Admit a durable Advanced Search run. **Returns immediately**; the worker executes.

    ## What admission checks

    A description, and nothing else. Advanced Search is a standard prior-art
    search preceded by guided editing: it runs no verifier, screens no dates and
    selects no text version, so there is no deployment policy it could be
    inadmissible under.

    The verifier-policy gate that used to stand here is **gone** (spec section
    10.4). It called `evidence/policy.py::validate_admission_policies` and could
    refuse admission because `VERIFIER_CAPABILITY_ENFORCEMENT`,
    `DATE_SCREENING_POLICY` or `TEXT_VERSION_POLICY` was unset -- refusing a
    search over configuration for a feature the product no longer has. The module
    is retained as standalone legacy tooling; nothing on this path calls it, and
    no `capability_qualified` field is returned.

    ## What admission does not do

    It does not execute anything, and it does not call the graph. `create_run`
    inserts the run as `QUEUED`, creates its permanent lease row at token 0 and
    appends the seq-1 `queued` event, all in one transaction. The **worker**
    claims it on its next poll. Without `python -m workflow.worker` running, an
    admitted run stays `QUEUED` -- adding API replicas does not help, because the
    API never executes a graph.
    """
    description = body.description.strip()
    if not description:
        raise ApiError(
            422, "INVALID_REQUEST", "A run needs a description of the invention."
        )

    with db.connect() as conn:
        try:
            # One transaction: a run never exists without the submission it was
            # created from. `description` is the required source input and is
            # never rewritten.
            with conn.transaction():
                run_id = store.create_run(
                    conn,
                    description=description,
                    claims=body.claims,
                    # Legacy column, kept per constraint 7 and never collected.
                    cutoff_date=None,
                    date_policy=POLICY_NOT_APPLICABLE,
                    text_version_policy=POLICY_NOT_APPLICABLE,
                    created_by=email,
                )
                # The two legacy context columns, written empty. Kept as a call
                # so the run row is still fully initialized in one transaction;
                # nothing collects or exposes either value any more.
                store.set_submission_context(
                    conn,
                    run_id=run_id,
                    problem_solved="",
                    inventor_novelty_statement="",
                )
        except Exception as error:
            raise to_http(error) from error

    return {
        "run_id": run_id,
        "status": "QUEUED",
        "stream_url": f"/api/runs/{run_id}/stream",
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


@router.delete("/{run_id}", status_code=204)
async def cancel_run(run_id: str, email: CurrentUser) -> Response:
    """Cancel one live Advanced Search owned by the caller.

    Removing the live run also removes its lease, fencing any worker that held
    it. Completed and failed runs are retained so refresh cannot erase history.
    """
    with db.connect() as conn:
        try:
            store.cancel_run(conn, run_id=run_id, actor_email=email)
        except Exception as error:
            raise to_http(error, run_id=run_id) from error
    return Response(status_code=204)


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

    A dropped SSE connection by itself does nothing to the run. Page lifecycle
    cancellation is an explicit authenticated ``DELETE /api/runs/{run_id}``,
    not an inference from this stream disappearing.
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


# --- Workflow Gate A: the invention understanding (stages 1 and 2) ----------


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
    from graph.gates import GATE_LIMITATIONS_RULES, GATE_SUMMARY_RULES
    from graph.limits import MAX_ELEMENTS_TOTAL
    from workflow import gate_state

    with db.connect() as conn:
        try:
            run = store.get_run(conn, run_id=run_id, actor_email=email)
            version = approvals.latest_approved_understanding_version(conn, run_id=run_id)
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

    review_stage = str(state.get("understanding_review_stage") or "summary")
    completion_rules = (
        GATE_LIMITATIONS_RULES if review_stage == "limitations" else GATE_SUMMARY_RULES
    )
    return {
        # --- the editable business payload (spec section 4.2.1) -------------
        #
        # Three fields, and nothing that revives a removed concept: no capability
        # objects, no `core`, no relationships, no per-element ids.
        **gate_state.gate_a_payload(state),
        "review_stage": review_stage,
        # The run row is the record of what was submitted and survives a
        # checkpoint compaction, so it wins over graph state for the description.
        # Read-only, and NOT accepted by the approval endpoint: the description is
        # never rewritten -- not by a proposal, not by an edit, not by an approval.
        "original_description": run["description"],
        # --- generic transport metadata, outside the business payload -------
        "editable": True,
        "approved_version": version,
        "completion_rules": list(completion_rules),
        "limits": {"max_elements_total": MAX_ELEMENTS_TOTAL},
    }


TECHNICAL_GROUP_ID = "F1"
"""The single group the flat element list is stored under (spec section 4.2.3).

`feature_decompositions` is keyed per group and `decomposition_requirement_ids`
enforces uniqueness within one; storing everything under one group is what lets a
flat list of strings reuse that schema unchanged. The id never reaches a screen
or a payload -- it exists so the storage layer has a group to key on.
"""


def _one_technical_group(summary: str, elements: list[Any]) -> list[dict[str, Any]]:
    """The flat public list as the one technical group storage expects.

    **Ids are minted here, never submitted.** The public shape is a list of
    strings, so `E1..En` are assigned in the order the reviewer left them --
    which is why duplicate and missing ids are unreachable rather than validated
    against.

    **Every element is written `core = TRUE`**, and that is the honest encoding
    rather than a convenient default: `decomposition_elements.core` is NOT NULL
    with no default, the reviewer no longer marks anything as core, and every
    element they approved counts. Writing them all core is what makes the
    internal `NO_CORE_ELEMENT` condition unreachable instead of deleted and
    hoped for.

    `relationships` is empty and stays empty -- relationships are gone from the
    product, so `decomposition_relationships` gets no rows.
    """
    return [
        {
            "feature_id": TECHNICAL_GROUP_ID,
            "feature_version": "1",
            "feature_text": summary,
            "elements": [
                {
                    "element_id": f"E{index}",
                    "text": str(text or ""),
                    "core": True,
                    "order": index,
                }
                for index, text in enumerate(elements, start=1)
            ],
            "relationships": [],
        }
    ]


@router.post("/{run_id}/gates/A/approve")
async def approve_gate_a(
    run_id: str, email: CurrentUser, body: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    """Approve exactly the Stage-1 or Stage-2 artifact currently on screen."""
    from workflow import gate_state
    from workflow import understanding as understanding_store

    key = _key(body)
    requested_stage = str(body.get("review_stage") or "")
    summary = str(body.get("technical_summary") or "")
    elements = list(body.get("technical_elements") or ())

    with db.connect() as conn:
        try:
            # Ownership must be established before reading the actor-less graph
            # checkpoint. The idempotency record is checked next: a successful
            # Stage-1 request retried after the graph reaches Stage 2 must replay
            # its original response instead of failing as the "wrong" stage.
            _own(conn, run_id, email)
            with idempotent(
                conn,
                actor_email=email,
                scope=f"gate_a_{requested_stage or 'unspecified'}_approve",
                resource_id=run_id,
                idempotency_key=key,
                request_body=body,
            ) as slot:
                if slot.replayed:
                    return slot.response
                state = gate_state.read_state(run_id)
                if state is None:
                    raise ApiError(
                        503,
                        "CHECKPOINT_UNAVAILABLE",
                        "The review state could not be read.",
                    )
                review_stage = str(
                    state.get("understanding_review_stage") or "summary"
                )
                if requested_stage != review_stage:
                    raise ApiError(
                        409,
                        "WRONG_REVIEW_STAGE",
                        f"The run is awaiting {review_stage} review, not "
                        f"{requested_stage or 'an unspecified stage'} review.",
                    )
                version = int(state.get("understanding_version") or 0)
                if version < 1:
                    raise ApiError(
                        409,
                        "NO_UNDERSTANDING_VERSION",
                        "The review has no proposal version.",
                    )
                _own_at_gate(conn, run_id, email, WorkflowGate.A)
                approved_at = _now()
                if review_stage == "summary":
                    if not summary.strip():
                        raise ApiError(422, "EMPTY_SUMMARY", "The technical summary cannot be empty.")
                    understanding_store.record_proposal_if_absent(
                        conn,
                        run_id=run_id,
                        version=version,
                        source=understanding_store.SOURCE_USER_EDIT,
                        summary=summary,
                        features=[],
                        created_by=email,
                    )
                    approvals.approve_summary_by_owner(
                        conn,
                        run_id=run_id,
                        actor_email=email,
                        version=version,
                        summary=summary,
                        approved_at=approved_at,
                    )
                    field = "summary_version"
                    reason = "Technical summary approved."
                    response = {
                        "understanding_version": version,
                        "review_stage": "summary",
                        "status": "RESUMING",
                    }
                elif review_stage == "limitations":
                    approved = approvals.require_approved_understanding(conn, run_id=run_id, version=version)
                    features = _one_technical_group(approved["summary"], elements)
                    review_store.require_gate_clear(
                        conn,
                        run_id=run_id,
                        gate=WorkflowGate.A,
                        payload={"decomposition": features, "summary": approved["summary"]},
                    )
                    understanding_store.attach_features_to_proposal(
                        conn,
                        run_id=run_id,
                        version=version,
                        source=understanding_store.SOURCE_USER_EDIT,
                        features=features,
                    )
                    written = approvals.approve_limitations_by_owner(
                        conn,
                        run_id=run_id,
                        actor_email=email,
                        version=version,
                        features=features,
                        approved_at=approved_at,
                    )
                    field = "limitations_version"
                    reason = "Technical limitations approved."
                    response = {
                        "decomposition_version": written,
                        "understanding_version": version,
                        "review_stage": "limitations",
                        "status": "RESUMING",
                    }
                else:
                    raise ApiError(409, "WRONG_REVIEW_STAGE", f"Unknown review stage {review_stage!r}.")
                review_store.write_audit(
                    conn,
                    run_id=run_id,
                    actor_email=email,
                    workflow_stage=str(WorkflowGate.A),
                    field=field,
                    previous_value="",
                    new_value=str(version),
                    reason=reason,
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
                    dedupe_key=f"resumed:A:{review_stage}:v{version}",
                )
                slot.record(200, response)
                return response
        except ApiError:
            raise
        except Exception as error:
            raise to_http(error, run_id=run_id) from error


# --- Workflow Gate STRATEGY: the search queries (stage 3) -------------------


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
        # --- the editable business payload (spec section 4.2.2) -------------
        #
        # One field, a list of strings. No `enabled`, no `rationale`, no
        # `covered_element_ids`, no `understanding_version` coverage claim.
        **gate_state.gate_strategy_payload(state),
        # --- generic transport metadata, outside the business payload -------
        "editable": True,
        "approved_version": version,
        "completion_rules": list(GATE_STRATEGY_RULES),
        "limits": {"max_queries": MAX_STRATEGY_QUERIES},
    }


def _numbered_queries(texts: list[Any]) -> list[dict[str, Any]]:
    """The flat public query list as the rows storage expects.

    **Ids are minted here, never submitted** (`Q1..Qn`, in the reviewer's order),
    which is why duplicate and missing query ids are unreachable rather than
    validated against.

    **Every query is written enabled**, because enable/disable is gone from the
    product: a stored query the reviewer cannot switch off must not pretend to be
    switchable. `covered_element_ids` is empty -- coverage was removed with the
    element-to-query mapping, and an empty list is the honest value where a
    fabricated one would be a coverage claim nobody made.
    """
    return [
        {
            "query_id": f"Q{index}",
            "query_text": str(text or ""),
            "enabled": True,
            "rationale": "",
            "covered_element_ids": [],
        }
        for index, text in enumerate(texts, start=1)
    ]


def _next_strategy_versions(run_id: str) -> tuple[int, int]:
    """`(the strategy version this writes, the understanding it was built on)`.

    Both are supplied by the server. The reviewer edits query text, not versions,
    and the understanding version is read from what was actually approved rather
    than taken from the request -- a client-supplied one could name a version the
    run never approved.
    """
    with db.connect() as conn:
        current = approvals.latest_approved_search_strategy_version(conn, run_id=run_id)
        understanding = approvals.latest_approved_decomposition_version(
            conn, run_id=run_id
        )
    return int(current or 0) + 1, int(understanding or 0)


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
    version, understanding_version = _next_strategy_versions(run_id)
    queries = _numbered_queries(list(body.get("queries") or ()))

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


# --- the completed run's report ---------------------------------------------


@router.get("/{run_id}/report")
async def get_report(run_id: str, email: CurrentUser) -> dict[str, Any]:
    """The completed run's report: the **standard** `AnalysisReport`.

    Repurposed from the Gate D verification report rather than added beside it,
    so there is one path to a run's result and it cannot drift from the other
    two. What comes back is the same object the terminal `result` event carries
    and the same object `services/history.ts` maps a stored run into, so a
    durable run, a quick search and a run reopened from History all render
    through the same `ReportScreen`.

    **Read from the terminal event, not from `search_runs`.** The event is the
    run's own record of what it produced, it is owner-scoped through the run row
    resolved above, and `search_runs` has no ownership column at all -- looking
    the report up there would mean matching on description or timestamp, which is
    guessing. The two are written in one transaction (`executor._succeed`), so
    the event existing means the History row exists too.

    Ownership first, always: `get_run` filters `created_by` **in the same query**
    that fetches the run, so a non-owner and an unknown run are indistinguishable
    and both answer `404 RUN_NOT_FOUND`.
    """
    with db.connect() as conn:
        try:
            run = store.get_run(conn, run_id=run_id, actor_email=email)
            terminal = events.terminal_event(conn, run_id=run_id)
        except Exception as error:
            raise to_http(error, run_id=run_id) from error

    status = run["status"]
    if status is not RunStatus.COMPLETED:
        # Naming the actual status rather than a bare "not ready": a run parked
        # for review, still queued, or failed are three different situations, and
        # the caller can act on each of them differently.
        raise ApiError(
            409,
            "RUN_NOT_COMPLETED",
            f"run {run_id} is {status}, not COMPLETED: it has no report yet.",
        )

    if terminal is None or not isinstance(terminal.get("payload"), dict):
        # A COMPLETED run without its terminal event should be impossible --
        # `finalize_run_success` writes the event and the transition in one
        # transaction. Refusing loudly beats inventing an empty report, which
        # would render as a search that genuinely found nothing.
        raise ApiError(
            500,
            "REPORT_NOT_FOUND",
            f"run {run_id} is COMPLETED but has no terminal result to report.",
        )

    return terminal["payload"].get("data") or {}


def _key(body: dict[str, Any]) -> str:
    try:
        return require_idempotency_key(body)
    except Exception as error:
        raise to_http(error) from error


__all__ = ["router"]
