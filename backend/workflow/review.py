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
    """Stages 1 and 2: exactly two of spec section 3.5's three rules.

    The stage shows a read-only description, an editable technical summary and a
    flat editable list of technical elements. Everything a reviewer can change is
    checked here rather than in the endpoint, so the executor, a test harness and
    the API all get the same answer.

    **Nothing else blocks, and that is a requirement rather than an omission.**
    Section 3.5 fixes the blocking list at three rules; the reviewer's on-screen
    list pairs one-to-one and in order with `GATE_A_RULES`, so a check here with
    no rule beside it would refuse an approval for a reason the screen never
    showed.

    What was removed, and why each has nothing left to check:

    * `TOO_MANY_CAPABILITIES`, `NO_ELEMENTS`, `TOO_MANY_ELEMENTS` -- capability
      grouping is gone; there is one flat list, not groups to count or bound.
    * `TOO_MANY_ELEMENTS_TOTAL` -- the total limit was derived from
      `MAX_CAPABILITIES` x the matrix cell cap, and there is no matrix.
    * `DUPLICATE_REQUIREMENT_ID` -- ids are minted on write, never submitted, so
      a caller cannot produce a duplicate.
    * `DANGLING_RELATIONSHIP_REFERENCE` -- relationships are gone from the
      product and no row is ever written to `decomposition_relationships`.
    * `NO_VERSION` -- the version is supplied by the endpoint, not the reviewer.
    * `NO_CORE_ELEMENT` -- `core` is no longer something a reviewer sets. Every
      approved element is written `core = TRUE`, which makes this condition
      **unreachable** rather than deleted and hoped for.
    """
    blocking: list[dict[str, Any]] = []

    # Rule 1: a non-empty technical summary. Refused here as well as by
    # `approve_understanding_by_owner`, so the reviewer is told which rule they
    # tripped instead of getting a write error.
    if "summary" in payload and not str(payload.get("summary") or "").strip():
        blocking.append(
            {
                "code": "NO_SUMMARY",
                "reason": "the technical summary is the system's interpretation of "
                "the invention and cannot be approved empty",
            }
        )

    # Rule 2: at least one non-empty technical element. Emptiness is measured
    # after trimming, and a list of nothing but blanks is an empty list -- a
    # reviewer who deleted every element's text has approved no elements.
    elements = [
        element
        for feature in (payload.get("decomposition") or [])
        for element in (feature.get("elements") or [])
    ]
    if not any(str(element.get("text") or "").strip() for element in elements):
        blocking.append(
            {
                "code": "NO_ELEMENTS",
                "reason": "at least one non-empty technical element is required",
            }
        )

    return blocking


def _gate_strategy_blocking(
    conn: psycopg.Connection, *, run_id: str, payload: dict[str, Any]
) -> list[dict[str, Any]]:
    """Stage 3: the last of spec section 3.5's three rules, and nothing else.

    The stage shows a plain editable list of query strings. One rule blocks: at
    least one non-empty query. A strategy with no queries would search for
    nothing and then report that nothing was found, which is the one failure here
    that would look exactly like a genuine empty result.

    **Nothing else blocks**, and each removal is a rule whose subject the product
    no longer has:

    * `NO_ENABLED_QUERY` -- enable/disable switches are gone. Every stored query
      is enabled, so "all of them are off" is unreachable.
    * `DUPLICATE_QUERY_ID`, `NO_QUERY_ID` -- ids are minted on write, never
      submitted, so a caller cannot produce a missing or duplicated one.
    * `TOO_MANY_QUERIES` -- a reviewer meets the limit while editing.
    * `NO_UNDERSTANDING_VERSION`, `STALE_UNDERSTANDING_VERSION`,
      `UNKNOWN_ELEMENT_REFERENCE`, `UNCOVERED_CORE_ELEMENT` -- all four are about
      the element-to-query coverage mapping, which is gone from the public shape.
      A reviewer can no longer see or set coverage, and a rule the screen cannot
      show is one an approval fails on invisibly.

    The coverage rule is the significant loss and it is worth naming plainly: it
    guaranteed that every core element was searched for, so that "no prior art
    found" could not rest on a search nobody performed for part of the invention.
    Advanced Search makes no such guarantee, states no such conclusion, and
    presents its queries to a person to approve instead. `conn` and `run_id` are
    still taken because `require_gate_clear` dispatches uniformly on gate.
    """
    queries = list(payload.get("queries") or ())
    if not any(str(query.get("query_text") or "").strip() for query in queries):
        return [
            {
                "code": "NO_QUERIES",
                "reason": "a search strategy with no queries would search for "
                "nothing and then report that nothing was found",
            }
        ]
    return []


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
