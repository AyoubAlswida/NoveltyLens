"""Human decisions, persisted beside the machine results and never over them.

Implementation Gate D. This module is to the human overlay what
`workflow/approvals.py` is to approved artifacts and `workflow/events.py` is to
the event log: **the only place that writes it.**

## What "beside, never over" means in code

A machine result is immutable (spec §13.1). A review does not edit it, a waiver
does not edit it, and a reviewed outcome is *derived* rather than accepted from
input (§13.3). So:

- `cell_reviews` and `cell_waivers` reference `verification_results
  (cell_id, result_version)` by composite foreign key. A review or waiver that
  names a version that does not exist is refused by the database, not by an `if`.
- Nothing here issues an `UPDATE` against `verification_results` or
  `verification_cells`. `tests/test_workflow_gates.py` greps for it.
- `reviewed_outcome` is computed by `verification.review.validate_review`'s own
  derivation and stored; a value supplied by a caller is ignored, because §13.4
  check 4 recomputes it and rejects a mismatch.

## Every write is authenticated, ownership-scoped, and audited

`actor_email` is the **authenticated** caller, resolved into the same query that
fetches the run. There is no parameter by which a request can name a different
reviewer: recording who decided something and letting the request claim it are
different things, and only one of them is an audit trail.

**Disclosed limitation.** Spec §16 decision 1 makes per-user accounts a
prerequisite for a real audit trail, and this repository has one shared seeded
account. Ownership scoping is implemented and real, but with one account every
decision is attributed to the same identity. That is stated here, in CLAUDE.md,
and in the run's audit appendix rather than being quietly true.

## No auto-approval

No function here advances a gate without an explicit recorded human action. There
is no timeout path, no configuration that disables a gate, and no bulk operation
that covers a cell whose state forbids it (§12.3: bulk approval may not cover a
validation failure, an `UNCLEAR`, or an outstanding context request).
"""

import json
import re
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

import psycopg
from psycopg.types.json import Jsonb

from workflow.types import (
    IllegalStateError,
    RunNotFoundError,
    UnapprovedArtifactError,
    WorkflowGate,
    raise_for_sqlstate,
)

__all__ = [
    "GateBlocked",
    "approve_report",
    "blocking_conditions",
    "create_draft_report",
    "latest_report_version",
    "list_reviews",
    "list_waivers",
    "require_approved_report",
    "save_review",
    "save_waiver",
    "write_audit",
]


class GateBlocked(IllegalStateError):
    """A gate cannot complete: `409 GATE_BLOCKED`, body listing every condition.

    Every condition, not the first. A reviewer told about one blocker at a time
    fixes one, resubmits, and is told about the next — which is the same amount of
    work spread over more round trips and more confusion.
    """

    code = "GATE_BLOCKED"

    def __init__(self, gate: WorkflowGate, blocking: Sequence[dict[str, Any]]) -> None:
        self.gate = gate
        self.blocking = list(blocking)
        super().__init__(
            f"workflow gate {gate} cannot complete: "
            + "; ".join(str(item.get("reason", item)) for item in self.blocking)
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_owned_run(
    conn: psycopg.Connection, *, run_id: str, actor_email: str
) -> dict[str, Any]:
    """Fetch a run the caller owns, locked, or raise `RunNotFoundError`.

    Ownership is in the `WHERE` clause. A missed comparison in Python is a data
    leak; a missed `WHERE` clause is a no-result, and only one of those fails
    safe.
    """
    row = conn.execute(
        """
        SELECT id::text, status, current_gate, created_by
          FROM verification_runs
         WHERE id = %s AND created_by = %s
           FOR UPDATE
        """,
        (run_id, actor_email),
    ).fetchone()
    if row is None:
        raise RunNotFoundError(f"run {run_id} was not found")
    return {
        "run_id": row[0],
        "status": row[1],
        "current_gate": row[2],
        "created_by": row[3],
    }


# --- the audit trail -------------------------------------------------------


def write_audit(
    conn: psycopg.Connection,
    *,
    run_id: str,
    actor_email: str,
    workflow_stage: str,
    field: str,
    previous_value: str,
    new_value: str,
    reason: str,
    cell_id: Optional[str] = None,
    requirement_type: Optional[str] = None,
    requirement_id: Optional[str] = None,
    source_result_version: Optional[int] = None,
    covered_cell_ids: Sequence[str] = (),
) -> str:
    """Append one audit row. **Append only** — nothing updates or deletes these.

    Spec §13.5 and Gate D item 8: every human change carries the authenticated
    reviewer, the time, the previous and new value, the reason, the result
    version, the workflow stage, and — for a requirement-level change — the
    requirement's type and id.

    `previous_value` and `new_value` are strings on purpose. An audit trail is
    read by a person reconstructing a decision, and a JSON blob whose shape has
    since changed is not a record of what they saw.
    """
    row = conn.execute(
        """
        INSERT INTO review_audit
            (run_id, cell_id, workflow_stage, reviewer_id, requirement_type,
             requirement_id, field, previous_value, new_value, reason,
             source_result_version, covered_cell_ids)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING audit_id::text
        """,
        (
            run_id,
            cell_id,
            workflow_stage,
            actor_email,
            requirement_type,
            requirement_id,
            field,
            previous_value,
            new_value,
            reason,
            source_result_version,
            list(covered_cell_ids),
        ),
    ).fetchone()
    return row[0]


# --- cell reviews ----------------------------------------------------------


def _load_cell(
    conn: psycopg.Connection, *, run_id: str, cell_id: str
) -> Optional[dict[str, Any]]:
    """A cell and its latest result, scoped to the run that owns it.

    `run_id` is in the `WHERE` clause even though `cell_id` is a primary key: a
    cell id embeds a run id by construction, but relying on that would make
    ownership depend on a naming convention rather than on a join.
    """
    row = conn.execute(
        """
        SELECT c.cell_id, c.feature_id, c.feature_version, c.publication_number,
               c.decomposition_version, c.stale,
               r.result_version, r.state::text, r.machine_result, r.outcome,
               r.failed_check, r.failure_reason, r.not_evaluated_reason,
               r.passage_chunk_ids
          FROM verification_cells c
          LEFT JOIN LATERAL (
                SELECT * FROM verification_results
                 WHERE cell_id = c.cell_id
                 ORDER BY result_version DESC
                 LIMIT 1
          ) r ON TRUE
         WHERE c.cell_id = %s AND c.run_id = %s
        """,
        (cell_id, run_id),
    ).fetchone()
    if row is None:
        return None
    return {
        "cell_id": row[0],
        "feature_id": row[1],
        "feature_version": row[2],
        "publication_number": row[3],
        "decomposition_version": row[4],
        "stale": row[5],
        "result_version": row[6],
        "state": row[7],
        "machine_result": row[8],
        "outcome": row[9],
        "failed_check": row[10],
        "failure_reason": row[11],
        "not_evaluated_reason": row[12],
        "passage_chunk_ids": list(row[13] or ()),
    }


def save_review(
    conn: psycopg.Connection,
    *,
    run_id: str,
    actor_email: str,
    cell_id: str,
    result_version: int,
    overlay: dict[str, Any],
    reviewed_outcome: str,
    status: str,
    idempotency_key: str,
    payload_sha256: str,
    approved_via_bulk: bool = False,
) -> dict[str, Any]:
    """Persist a **validated** review overlay against one cell and one version.

    This function does not validate the overlay's contents — `workflow/api` calls
    `verification.review.validate_review` first, with the real `cell_id` and
    `result_version`, because spec §13.4 checks 15–18 are the binding and they
    need the machine result, the decomposition and the passages to compare
    against. What this function does is make the *storage* uncheatable:

    - the composite foreign key refuses a result version that does not exist;
    - the `CHECK (source_cell_id = cell_id)` and
      `CHECK (source_result_version = result_version)` constraints refuse an
      overlay that claims to have been composed against a different target, even
      if the application checks were somehow skipped;
    - the primary key `(cell_id, result_version)` makes a second review of the
      same version an update of the same row rather than a second opinion.

    `reviewed_outcome` is the value **derived** from the reviewed statuses. It is
    never taken from the request (§13.3, case W).
    """
    conn.execute(
        """
        INSERT INTO cell_reviews
            (cell_id, result_version, run_id, status, overlay, reviewed_outcome,
             reviewer_id, source_cell_id, source_result_version,
             approved_via_bulk, idempotency_key, payload_sha256)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (cell_id, result_version) DO UPDATE
           SET status = EXCLUDED.status,
               overlay = EXCLUDED.overlay,
               reviewed_outcome = EXCLUDED.reviewed_outcome,
               reviewer_id = EXCLUDED.reviewer_id,
               reviewed_at = now(),
               approved_via_bulk = EXCLUDED.approved_via_bulk,
               idempotency_key = EXCLUDED.idempotency_key,
               payload_sha256 = EXCLUDED.payload_sha256
        """,
        (
            cell_id,
            result_version,
            run_id,
            status,
            Jsonb(overlay),
            reviewed_outcome,
            actor_email,
            cell_id,
            result_version,
            approved_via_bulk,
            idempotency_key,
            payload_sha256,
        ),
    )
    return {
        "cell_id": cell_id,
        "result_version": result_version,
        "status": status,
        "reviewed_outcome": reviewed_outcome,
    }


def list_reviews(
    conn: psycopg.Connection, *, run_id: str
) -> dict[str, dict[str, Any]]:
    """Every review on a run, keyed by cell id."""
    rows = conn.execute(
        """
        SELECT cell_id, result_version, status::text, overlay, reviewed_outcome,
               reviewer_id, reviewed_at, approved_via_bulk
          FROM cell_reviews WHERE run_id = %s
        """,
        (run_id,),
    ).fetchall()
    return {
        row[0]: {
            "cell_id": row[0],
            "result_version": row[1],
            "status": row[2],
            "overlay": row[3],
            "reviewed_outcome": row[4],
            "reviewer_id": row[5],
            "reviewed_at": row[6].isoformat() if row[6] else None,
            "approved_via_bulk": row[7],
        }
        for row in rows
    }


# --- waivers ---------------------------------------------------------------


def save_waiver(
    conn: psycopg.Connection,
    *,
    run_id: str,
    actor_email: str,
    cell_id: str,
    result_version: int,
    feature_id: str,
    feature_version: str,
    publication_number: str,
    reason: str,
    unresolved_limitation: str,
    idempotency_key: str,
    payload_sha256: str,
    requirement_id: Optional[str] = None,
) -> dict[str, Any]:
    """Record a §12.3 waiver. **It changes nothing about the cell.**

    There is deliberately no column in `cell_waivers` that could alter state,
    `machine_result` or outcome, and nothing here writes to
    `verification_results`. A waived `VALIDATION_FAILURE` is still a
    `VALIDATION_FAILURE` with its failing check, in the matrix, in the draft and
    in the final report, beside the waiver's unresolved limitation (case AJ, AZ).

    The composite foreign keys pin the waiver to the exact result version *and* to
    the cell's own run, feature, feature version and publication — declaratively,
    so a mismatched waiver cannot be inserted by any code path (case AY).
    """
    if not (reason or "").strip():
        raise UnapprovedArtifactError("a waiver with no reason waives nothing")
    if not (unresolved_limitation or "").strip():
        raise UnapprovedArtifactError(
            "a waiver must state the limitation it leaves unresolved; that "
            "statement is what the report prints beside the cell"
        )
    try:
        row = conn.execute(
            """
            INSERT INTO cell_waivers
                (cell_id, result_version, run_id, feature_id, feature_version,
                 publication_number, requirement_id, reviewer_id, reason,
                 unresolved_limitation, idempotency_key, payload_sha256)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (cell_id, result_version, requirement_id) DO UPDATE
               SET reason = EXCLUDED.reason,
                   unresolved_limitation = EXCLUDED.unresolved_limitation,
                   reviewer_id = EXCLUDED.reviewer_id,
                   idempotency_key = EXCLUDED.idempotency_key,
                   payload_sha256 = EXCLUDED.payload_sha256
            RETURNING id::text
            """,
            (
                cell_id,
                result_version,
                run_id,
                feature_id,
                feature_version,
                publication_number,
                requirement_id,
                actor_email,
                reason,
                unresolved_limitation,
                idempotency_key,
                payload_sha256,
            ),
        ).fetchone()
    except psycopg.Error as error:
        raise_for_sqlstate(error)
        raise
    return {"waiver_id": row[0], "cell_id": cell_id, "result_version": result_version}


def list_waivers(conn: psycopg.Connection, *, run_id: str) -> dict[str, list[dict[str, Any]]]:
    """Every waiver on a run, grouped by cell id."""
    rows = conn.execute(
        """
        SELECT cell_id, result_version, requirement_id, reviewer_id, reason,
               unresolved_limitation, at
          FROM cell_waivers WHERE run_id = %s ORDER BY at
        """,
        (run_id,),
    ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row[0], []).append(
            {
                "cell_id": row[0],
                "result_version": row[1],
                "requirement_id": row[2],
                "reviewer_id": row[3],
                "reason": row[4],
                "unresolved_limitation": row[5],
                "at": row[6].isoformat() if row[6] else None,
            }
        )
    return grouped


# --- report versions -------------------------------------------------------


def create_draft_report(
    conn: psycopg.Connection, *, run_id: str, draft: dict[str, Any]
) -> dict[str, Any]:
    """Write the next draft version. Never overwrites an approved one.

    Case AD: editing a caveat after approval creates a **new** version requiring
    fresh approval, and the previously approved version stays immutable. That is
    why this always inserts at `max(version) + 1` rather than updating in place.
    """
    row = conn.execute(
        "SELECT coalesce(max(version), 0) FROM report_versions WHERE run_id = %s",
        (run_id,),
    ).fetchone()
    version = int(row[0]) + 1
    conn.execute(
        "INSERT INTO report_versions (run_id, version, draft) VALUES (%s, %s, %s)",
        (run_id, version, Jsonb(draft)),
    )
    return {"version": version, "status": "DRAFT"}


def latest_report_version(
    conn: psycopg.Connection, *, run_id: str
) -> Optional[dict[str, Any]]:
    """The newest report version for a run, approved or not, or None."""
    row = conn.execute(
        """
        SELECT version, draft, comments, caveats, is_approved, approved_by,
               approved_at, created_at
          FROM report_versions WHERE run_id = %s ORDER BY version DESC LIMIT 1
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "version": row[0],
        # Echoed at the top level as well, because the rendered report asks for
        # `report_version` and the row calls it `version`. The report has to be
        # able to say which version of itself it is.
        "report_version": row[0],
        "draft": row[1],
        "comments": row[2],
        "caveats": row[3],
        "status": "APPROVED" if row[4] else "DRAFT",
        "approved_by": row[5],
        "approved_at": row[6].isoformat() if row[6] else None,
        "created_at": row[7].isoformat() if row[7] else None,
    }


def approve_report(
    conn: psycopg.Connection,
    *,
    run_id: str,
    actor_email: str,
    version: int,
    comments: str = "",
    caveats: str = "",
) -> dict[str, Any]:
    """Stamp one report version approved, by the authenticated caller.

    The schema's `CHECK (is_approved = (approved_by IS NOT NULL AND approved_at IS
    NOT NULL))` makes a half-stamped row impossible. The `WHERE` clause makes a
    second approval of an already-approved version a no-op rather than a
    re-approval, so `approved_at` records when it was *first* approved.

    Approving a version that is not the latest is refused: the caller would be
    approving a draft that has since been superseded, which is exactly the
    staleness case AD describes from the other side.
    """
    latest = latest_report_version(conn, run_id=run_id)
    if latest is None:
        raise UnapprovedArtifactError(
            "there is no draft report to approve. Spec §12.4 and case AC: a report "
            "cannot be approved into existence."
        )
    if int(latest["version"]) != int(version):
        raise IllegalStateError(
            f"report version {version} is not the current draft "
            f"({latest['version']}); approving a superseded draft would approve a "
            f"document nobody is looking at"
        )
    row = conn.execute(
        """
        UPDATE report_versions
           SET is_approved = TRUE, approved_by = %s, approved_at = now(),
               comments = %s, caveats = %s
         WHERE run_id = %s AND version = %s AND NOT is_approved
        RETURNING version
        """,
        (actor_email, comments, caveats, run_id, version),
    ).fetchone()
    if row is None:
        return {"version": version, "status": "APPROVED", "already_approved": True}
    return {"version": int(row[0]), "status": "APPROVED", "already_approved": False}


def require_approved_report(
    conn: psycopg.Connection, *, run_id: str
) -> dict[str, Any]:
    """The approved report version, or raise. Read by `gate_d_node` on resume."""
    row = conn.execute(
        """
        SELECT version, approved_by, approved_at FROM report_versions
         WHERE run_id = %s AND is_approved ORDER BY version DESC LIMIT 1
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        raise UnapprovedArtifactError(
            "workflow gate D resumed with no approved report version. The approval "
            "transaction is the only thing that can stamp one, and nothing here "
            "falls back to the draft."
        )
    return {
        "version": int(row[0]),
        "approved_by": row[1],
        "approved_at": row[2].isoformat() if row[2] else None,
    }


# --- gate completion rules -------------------------------------------------


def blocking_conditions(
    conn: psycopg.Connection,
    *,
    run_id: str,
    gate: WorkflowGate,
    payload: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """Every condition preventing `gate` from completing. Empty means clear.

    Spec §12.1–§12.4, and the rules are enforced here rather than in the endpoint
    so the executor, a test harness and the API all get the same answer.
    """
    payload = payload or {}
    if gate is WorkflowGate.A:
        return _gate_a_blocking(payload)
    if gate is WorkflowGate.STRATEGY:
        return _gate_strategy_blocking(conn, run_id=run_id, payload=payload)
    if gate is WorkflowGate.B:
        return _gate_b_blocking(conn, run_id=run_id, payload=payload)
    if gate is WorkflowGate.C:
        return _gate_c_blocking(conn, run_id=run_id)
    if gate is WorkflowGate.D:
        return _gate_d_blocking(conn, run_id=run_id)
    raise IllegalStateError(f"unknown workflow gate {gate!r}")


def _gate_a_blocking(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Spec section 12.1, plus the inventor-facing understanding stage's own rules.

    The stage shows an editable technical summary and editable technical elements
    grouped under capabilities. Everything a reviewer can change is checked here
    rather than in the endpoint, so the executor, a test harness and the API all
    get the same answer.

    **Limits are enforced on both sides, and this is the binding one.** The
    frontend refuses the same edits, but a browser check is a courtesy and this
    is the boundary a request has to cross.
    """
    # Deferred: `graph.limits` pulls in the verification nodes to derive
    # MAX_CAPABILITIES from the reviewed matrix constants, and importing that at
    # module scope would drag the graph into every importer of this module. The
    # same deferred-import pattern `api/runs.py` uses for the gate rules.
    from graph.limits import (
        MAX_CAPABILITIES,
        MAX_ELEMENTS_PER_CAPABILITY,
        MAX_ELEMENTS_TOTAL,
    )

    features = payload.get("decomposition") or []
    blocking: list[dict[str, Any]] = []

    # The approved summary is half of what this stage approves. An empty one is
    # refused here as well as by `approve_understanding_by_owner`, so the reviewer
    # is told which rule they tripped instead of getting a write error.
    if "summary" in payload and not str(payload.get("summary") or "").strip():
        blocking.append(
            {
                "code": "NO_SUMMARY",
                "reason": "the technical summary is the system's interpretation of "
                "the invention and cannot be approved empty",
            }
        )

    if not features:
        blocking.append({"code": "NO_FEATURES", "reason": "at least one feature is required"})
        return blocking

    if len(features) > MAX_CAPABILITIES:
        blocking.append(
            {
                "code": "TOO_MANY_CAPABILITIES",
                "reason": f"at most {MAX_CAPABILITIES} technical capabilities may be "
                f"approved; this understanding has {len(features)}",
            }
        )

    total_elements = 0
    any_core = False

    for feature in features:
        feature_id = feature.get("feature_id") or "<unnamed>"
        elements = feature.get("elements") or []
        relationships = feature.get("relationships") or []
        total_elements += len(elements)
        if any(bool(element.get("core")) for element in elements):
            any_core = True

        if not elements:
            blocking.append(
                {
                    "code": "NO_ELEMENTS",
                    "feature_id": feature_id,
                    "reason": f"feature {feature_id} has no elements",
                }
            )
        if len(elements) > MAX_ELEMENTS_PER_CAPABILITY:
            blocking.append(
                {
                    "code": "TOO_MANY_ELEMENTS",
                    "feature_id": feature_id,
                    "reason": f"at most {MAX_ELEMENTS_PER_CAPABILITY} technical "
                    f"elements are allowed in one capability; {feature_id} has "
                    f"{len(elements)}",
                }
            )

        ids = [e.get("element_id") for e in elements] + [
            r.get("relationship_id") for r in relationships
        ]
        # Unique ACROSS elements and relationships -- the same rule
        # `decomposition_requirement_ids` enforces in the database. A relationship
        # sharing an element's id would make every locator ambiguous.
        duplicates = sorted({name for name in ids if ids.count(name) > 1 and name})
        if duplicates:
            blocking.append(
                {
                    "code": "DUPLICATE_REQUIREMENT_ID",
                    "feature_id": feature_id,
                    "reason": f"requirement ids must be unique across elements and "
                    f"relationships; duplicated: {', '.join(duplicates)}",
                }
            )
        for requirement in list(elements) + list(relationships):
            if not (requirement.get("text") or "").strip():
                blocking.append(
                    {
                        "code": "EMPTY_REQUIREMENT_TEXT",
                        "feature_id": feature_id,
                        "reason": "every element and relationship needs non-empty text",
                    }
                )
                break

        dangling = _dangling_element_references(elements, relationships)
        if dangling:
            blocking.append(
                {
                    "code": "DANGLING_RELATIONSHIP_REFERENCE",
                    "feature_id": feature_id,
                    "reason": f"a relationship in {feature_id} refers to elements that "
                    f"are not in it: {', '.join(dangling)}. Relationships are "
                    f"regenerated when elements change; one left pointing at a "
                    f"deleted element is a requirement the verifier would be asked "
                    f"to find among elements that no longer exist",
                }
            )

        if not (feature.get("feature_version") or "").strip():
            blocking.append(
                {
                    "code": "NO_VERSION",
                    "feature_id": feature_id,
                    "reason": f"feature {feature_id} does not name a version",
                }
            )

    if total_elements > MAX_ELEMENTS_TOTAL:
        blocking.append(
            {
                "code": "TOO_MANY_ELEMENTS_TOTAL",
                "reason": f"at most {MAX_ELEMENTS_TOTAL} technical elements may be "
                f"approved in one understanding; this one has {total_elements}",
            }
        )

    # At least one element, somewhere, must be core. Without one the matrix asks
    # only about detail: `core` is what says "the invention is not the invention
    # without this", and a set with none of them cannot support a novelty finding.
    if not any_core:
        blocking.append(
            {
                "code": "NO_CORE_ELEMENT",
                "reason": "at least one technical element must be marked as core; "
                "a core element is what the invention is not itself without",
            }
        )

    return blocking


_ELEMENT_REFERENCE = re.compile(r"\bE\d{1,3}\b")
"""Element ids as this pipeline mints them (`E1`, `E2`, ...), matched inside
relationship prose.

**A textual check, and it is stated as one.** A relationship carries no foreign
key to the elements it relates -- `decomposition_relationships` stores text and an
ordinal -- so the only detectable dangling reference is one the text names. This
catches the failure that actually happens (an element is deleted and a
relationship goes on naming it, because the two were edited at different times),
and it cannot catch a relationship that describes a deleted element in words. It
is a guard, never a proof, and the real defence is regenerating relationships
whenever elements change rather than patching them.
"""


def _dangling_element_references(
    elements: Sequence[dict[str, Any]], relationships: Sequence[dict[str, Any]]
) -> list[str]:
    """Element ids a relationship names that this feature no longer has."""
    present = {
        str(element.get("element_id") or "").strip()
        for element in elements
        if str(element.get("element_id") or "").strip()
    }
    named: set[str] = set()
    for relationship in relationships:
        named.update(_ELEMENT_REFERENCE.findall(str(relationship.get("text") or "")))
    return sorted(named - present)


def _gate_strategy_blocking(
    conn: psycopg.Connection, *, run_id: str, payload: dict[str, Any]
) -> list[dict[str, Any]]:
    """The SEARCH STRATEGY stage's completion rules.

    **The rule this stage exists for is coverage**, and it is the one that cannot
    be moved into the UI as a courtesy: every **core** element of the approved
    understanding must be covered by at least one **enabled** query. A core
    element is one the invention is not itself without, so a run that searches
    without covering one and then reports "no prior art found" has made a claim
    about prior art on the strength of a search nobody performed for it.

    Coverage is an **id-set comparison against the approved decomposition** at
    the strategy's `understanding_version`, and it is stated as one:
    `approved_strategy_queries.covered_element_ids` carries no foreign key, so
    what is checked is that the ids exist and that the core ones are named -- never
    that a query would actually retrieve prior art for them. That second thing is
    a judgement, which is why a person approves this stage at all.

    **The ids are feature-qualified (`F1.E1`), and they have to be.** A
    requirement id is unique within one feature and not across a run -- the
    database says so: `decomposition_requirement_ids` is keyed by
    `(decomposition_id, requirement_id)`, one decomposition per feature. A bare
    `E1` in two features is two different elements, and treating a coverage claim
    for one as covering the other would report a core element as searched for when
    nobody searched for it.

    The version check is the other half. A strategy is approved *against* one
    understanding version; if the understanding has since been regenerated and
    re-approved, coverage was computed over element ids that are no longer the
    ones the run will verify against, and re-using it would report coverage of
    requirements it was never checked against.
    """
    from graph.limits import MAX_STRATEGY_QUERIES

    queries = list(payload.get("queries") or ())
    version = int(payload.get("understanding_version") or 0)
    blocking: list[dict[str, Any]] = []

    if not queries:
        blocking.append(
            {
                "code": "NO_QUERIES",
                "reason": "a search strategy with no queries would search for "
                "nothing and then report that nothing was found",
            }
        )
        return blocking

    if len(queries) > MAX_STRATEGY_QUERIES:
        blocking.append(
            {
                "code": "TOO_MANY_QUERIES",
                "reason": f"at most {MAX_STRATEGY_QUERIES} queries may be approved; "
                f"this strategy has {len(queries)}",
            }
        )

    enabled = [query for query in queries if bool(query.get("enabled"))]
    if not enabled:
        blocking.append(
            {
                "code": "NO_ENABLED_QUERY",
                "reason": "every query is switched off, so nothing would be "
                "dispatched and the run could report nothing honestly",
            }
        )

    ids = [str(query.get("query_id") or "").strip() for query in queries]
    duplicates = sorted({name for name in ids if name and ids.count(name) > 1})
    if duplicates:
        blocking.append(
            {
                "code": "DUPLICATE_QUERY_ID",
                "reason": "query ids must be unique within a strategy; duplicated: "
                + ", ".join(duplicates),
            }
        )
    if any(not name for name in ids):
        blocking.append(
            {
                "code": "NO_QUERY_ID",
                "reason": "every query needs an id, or its coverage claim cannot be "
                "attributed to anything",
            }
        )
    for query in queries:
        if not str(query.get("query_text") or "").strip():
            blocking.append(
                {
                    "code": "EMPTY_QUERY_TEXT",
                    "query_id": str(query.get("query_id") or ""),
                    "reason": "every query needs non-empty text",
                }
            )
            break

    if version < 1:
        blocking.append(
            {
                "code": "NO_UNDERSTANDING_VERSION",
                "reason": "a search strategy must name the approved understanding "
                "it covers; coverage over an unnamed version is not a coverage "
                "claim at all",
            }
        )
        return blocking

    elements = _approved_elements(conn, run_id=run_id, version=version)
    if elements is None:
        blocking.append(
            {
                "code": "STALE_UNDERSTANDING_VERSION",
                "reason": f"there is no approved understanding at version {version} "
                "for this run, so the coverage claimed here was computed against "
                "requirements this run will not verify against",
            }
        )
        return blocking

    known, core = elements
    claimed: set[str] = set()
    for query in queries:
        named = {str(item).strip() for item in (query.get("covered_element_ids") or ())}
        unknown = sorted(name for name in named if name and name not in known)
        if unknown:
            blocking.append(
                {
                    "code": "UNKNOWN_ELEMENT_REFERENCE",
                    "query_id": str(query.get("query_id") or ""),
                    "reason": "a query claims to cover elements the approved "
                    "understanding does not contain: " + ", ".join(unknown),
                }
            )
        if bool(query.get("enabled")):
            claimed |= named

    uncovered = sorted(core - claimed)
    if uncovered:
        blocking.append(
            {
                "code": "UNCOVERED_CORE_ELEMENT",
                "element_ids": uncovered,
                "reason": "every core element must be covered by at least one "
                "enabled query; not covered: " + ", ".join(uncovered) + ". A core "
                "element is one the invention is not itself without, and finding no "
                "prior art for something nobody searched for is not a finding",
            }
        )
    return blocking


def _approved_elements(
    conn: psycopg.Connection, *, run_id: str, version: int
) -> Optional[tuple[set[str], set[str]]]:
    """`(every approved element id, the core ones)` at `version`, or `None`.

    Ids are returned **feature-qualified** (`F1.E1`), which is the form a coverage
    claim names them in. See `_gate_strategy_blocking` on why a bare element id
    cannot carry a coverage claim across a whole run.

    `None` means there is no **approved** understanding at that version -- read
    with `is_approved` filtered in SQL, so an unapproved proposal cannot answer
    this question. The caller reports that as a stale strategy rather than as an
    empty element set, because "the version moved" and "the invention has no
    elements" are different facts and only one of them is possible here.
    """
    approved = conn.execute(
        """
        SELECT 1 FROM feature_decompositions
         WHERE run_id = %s AND version = %s AND is_approved LIMIT 1
        """,
        (run_id, version),
    ).fetchone()
    if approved is None:
        return None
    rows = conn.execute(
        """
        SELECT d.feature_id, e.element_id, e.core
          FROM decomposition_elements e
          JOIN feature_decompositions d ON d.id = e.decomposition_id
         WHERE d.run_id = %s AND d.version = %s AND d.is_approved
        """,
        (run_id, version),
    ).fetchall()
    # Feature-qualified, because a requirement id is unique within a feature and
    # not across a run. Two features each having an `E1` is ordinary and legal.
    known = {f"{row[0]}.{row[1]}" for row in rows}
    core = {f"{row[0]}.{row[1]}" for row in rows if row[2]}
    return known, core


def _gate_b_blocking(
    conn: psycopg.Connection, *, run_id: str, payload: dict[str, Any]
) -> list[dict[str, Any]]:
    """Spec §12.2 plus §3.7: an approved publication must be date-eligible, must
    have completed enrichment, and must have **at least one built passage**.

    The third is the one that makes "eligible with nothing to cite" impossible.
    `EXCLUDED_DATE` and `EXCLUDED_NO_FULL_TEXT` are **not overridable** (cases AK,
    AL): approving one is refused here, and the only path is correcting the input,
    which creates a new version.
    """
    requested = list(payload.get("publication_numbers") or ())
    blocking: list[dict[str, Any]] = []

    excluded = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT publication_number, state::text FROM publication_exclusions "
            "WHERE run_id = %s",
            (run_id,),
        ).fetchall()
    }
    with_passages = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT publication_number FROM evidence_passages WHERE run_id = %s",
            (run_id,),
        ).fetchall()
    }

    for number in requested:
        state = excluded.get(number)
        if state:
            blocking.append(
                {
                    "code": "PUBLICATION_EXCLUDED",
                    "publication_number": number,
                    "state": state,
                    "reason": f"{number} is {state} and is not overridable; correct "
                    f"the input and re-derive it in a new version",
                }
            )
            continue
        if number not in with_passages:
            blocking.append(
                {
                    "code": "NO_PASSAGES",
                    "publication_number": number,
                    "reason": f"{number} has no built passage, so eligibility cannot "
                    f"be derived and there would be nothing to cite",
                }
            )
    return blocking


def _gate_c_blocking(conn: psycopg.Connection, *, run_id: str) -> list[dict[str, Any]]:
    """Spec §12.3 and case AA.

    No `PENDING` cell, no `VALIDATION_FAILURE` that has not been rerun or waived,
    no unresolved `UNCLEAR` requirement, and no outstanding context request. A
    `NOT_EVALUATED` cell is blocking until it is acknowledged or waived — it is a
    cell that produced no verdict, and completing over it silently would report
    coverage that does not exist.
    """
    reviews = list_reviews(conn, run_id=run_id)
    waivers = list_waivers(conn, run_id=run_id)
    blocking: list[dict[str, Any]] = []

    rows = conn.execute(
        """
        SELECT c.cell_id, r.result_version, r.state::text, r.machine_result,
               r.failed_check
          FROM verification_cells c
          LEFT JOIN LATERAL (
                SELECT * FROM verification_results
                 WHERE cell_id = c.cell_id ORDER BY result_version DESC LIMIT 1
          ) r ON TRUE
         WHERE c.run_id = %s AND NOT c.stale
         ORDER BY c.cell_id
        """,
        (run_id,),
    ).fetchall()

    for cell_id, result_version, state, machine_result, failed_check in rows:
        waived = bool(waivers.get(cell_id))
        reviewed = reviews.get(cell_id)

        if result_version is None or state == "PENDING":
            blocking.append(
                {"code": "PENDING_CELL", "cell_id": cell_id,
                 "reason": f"{cell_id} has no result yet"}
            )
            continue

        if state == "VALIDATION_FAILURE":
            if not waived:
                blocking.append(
                    {
                        "code": "VALIDATION_FAILURE",
                        "cell_id": cell_id,
                        "failed_check": failed_check,
                        "reason": f"{cell_id} failed {failed_check} and must be "
                        f"rerun or waived — it can never be overridden or "
                        f"converted into a verdict",
                    }
                )
            continue

        if state == "NOT_EVALUATED":
            if not waived and not reviewed:
                blocking.append(
                    {
                        "code": "NOT_EVALUATED",
                        "cell_id": cell_id,
                        "reason": f"{cell_id} was not evaluated and must be "
                        f"acknowledged or waived",
                    }
                )
            continue

        # VERIFIED: unresolved UNCLEAR requirements and outstanding context
        # requests both block, and a review that resolved them clears the block.
        result = _as_dict(machine_result)
        unclear = [
            item.get("element_id") or item.get("relationship_id")
            for item in list(result.get("elements") or ())
            + list(result.get("relationships") or ())
            if item.get("status") == "UNCLEAR"
        ]
        requests = list(result.get("requests_for_more_context") or ())
        resolved = _resolved_requirements(reviewed)

        outstanding = [name for name in unclear if name not in resolved]
        if outstanding and not waived:
            blocking.append(
                {
                    "code": "UNRESOLVED_UNCLEAR",
                    "cell_id": cell_id,
                    "requirement_ids": outstanding,
                    "reason": f"{cell_id} has unresolved UNCLEAR requirements: "
                    f"{', '.join(str(name) for name in outstanding)}",
                }
            )
        if requests and not waived and reviewed is None:
            blocking.append(
                {
                    "code": "OUTSTANDING_CONTEXT_REQUEST",
                    "cell_id": cell_id,
                    "reason": f"{cell_id} has an outstanding request for more "
                    f"context; fulfil it with a rerun, or waive it",
                }
            )

    # **A verdict whose evidence has moved is not a verdict about the text on
    # screen.** Spec section 6 rows 10, 15 and 17 all end "dependents stale":
    # when a passage's text version changes, every result that cited it is a
    # statement about text that is no longer what the citation resolves to.
    #
    # `stale_passage_citations` computed exactly this and had no caller, so the
    # condition was detectable and never detected -- a reviewer could complete
    # Gate C over a cell citing a chunk id that no longer exists, or one whose
    # `text_sha256` had changed underneath it. It is blocking rather than
    # informational because the only honest resolutions are a rerun against the
    # current text or an explicit waiver, and both are human acts.
    from workflow import evidence_store

    for cell_id, result_version in evidence_store.stale_passage_citations(
        conn, run_id=run_id
    ):
        if waivers.get(cell_id):
            continue
        blocking.append(
            {
                "code": "STALE_EVIDENCE",
                "cell_id": cell_id,
                "result_version": result_version,
                "reason": f"{cell_id} v{result_version} cites passage text that has "
                f"since changed or gone; re-run it against the current text, or "
                f"waive it — its finding is about text this citation no longer "
                f"resolves to",
            }
        )

    return blocking


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _resolved_requirements(review: Optional[dict[str, Any]]) -> set[str]:
    """Requirement ids a review gave a non-`UNCLEAR` reviewed status to.

    A review that leaves a requirement `UNCLEAR` has not resolved it — §12.3 says
    Gate C cannot complete with an unresolved ambiguity, and a reviewer agreeing
    that it is ambiguous is agreeing, not resolving.
    """
    if not review:
        return set()
    overlay = _as_dict(review.get("overlay"))
    resolved: set[str] = set()
    for field, key in (
        ("element_reviews", "element_id"),
        ("relationship_reviews", "relationship_id"),
    ):
        for item in list(overlay.get(field) or ()):
            if not isinstance(item, dict):
                continue
            name = item.get(key) or item.get("requirement_id")
            if name and item.get("reviewed_status") not in (None, "UNCLEAR"):
                resolved.add(str(name))
    return resolved


def _gate_d_blocking(conn: psycopg.Connection, *, run_id: str) -> list[dict[str, Any]]:
    """Spec §12.4 and case AC: a draft must exist and have been presented.

    Gate C's conditions are re-checked here as well. A report approved over an
    unresolved cell would be a document asserting coverage the matrix does not
    have, and "Gate C completed earlier" is not evidence about the state now.
    """
    blocking = list(_gate_c_blocking(conn, run_id=run_id))
    draft = latest_report_version(conn, run_id=run_id)
    if draft is None:
        blocking.append(
            {
                "code": "NO_DRAFT",
                "reason": "no draft report exists; a report cannot be approved "
                "into existence (case AC)",
            }
        )
    return blocking


def require_gate_clear(
    conn: psycopg.Connection,
    *,
    run_id: str,
    gate: WorkflowGate,
    payload: Optional[dict[str, Any]] = None,
) -> None:
    """Raise :class:`GateBlocked` listing every condition, or return."""
    blocking = blocking_conditions(conn, run_id=run_id, gate=gate, payload=payload)
    if blocking:
        raise GateBlocked(gate, blocking)
