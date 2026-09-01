"""The draft report a human approves at Workflow Gate D.

Spec §12.4 and case AC: **a report cannot be approved into existence.** A draft
must exist and have been presented before Gate D can complete, so this node runs
between Gate C and Gate D and persists a versioned draft.

## What a draft must say, and what it must never say

The draft is the place where this product's honesty about its own coverage either
holds or fails, so the contents are a contract rather than a layout choice:

- **Every excluded publication is listed**, with its state and reason. An
  exclusion means *no comparison was attempted* — §1.2.1 — and a report that
  omitted them would present partial coverage as complete.
- **Excluded publications are stated to have NOT BEEN EVALUATED** for feature
  disclosure. They are not "no prior art found".
- **Every waiver appears with its unresolved limitation.** A waiver is a disclosed
  gap; printing the cell without it would turn a disclosure into an omission.
- **A `VALIDATION_FAILURE` renders as a validation failure**, with its failing
  check, waived or not (case AJ). It never becomes a verdict.
- **`NOT_EVALUATED` renders as itself** (case AZ), never as `NO_DISCLOSURE_FOUND`.
- **No claim of exhaustive coverage, and no legal conclusion.** The measured EPO
  coverage on this project's own sample was 17.6%; the draft says what was
  searched and what was not, and says nothing about novelty as a legal matter.

## Nothing here is a verdict

The draft is assembled from stored cells, stored reviews and stored waivers. It
computes no statuses, promotes nothing, and fills no gap. Where a cell has no
verdict the draft says there is no verdict.
"""

from typing import Any

import db
from graph.events import emit_progress
from graph.state import GraphState
from workflow import review as review_store
from workflow.types import UnapprovedArtifactError

COVERAGE_STATEMENT = (
    "This report covers only the publications listed as evaluated, and only the "
    "passages retrieved for them. Publications listed as excluded were NOT "
    "EVALUATED for feature disclosure. This is not an exhaustive novelty search "
    "and is not a legal opinion."
)
"""Printed verbatim on every draft.

Not a disclaimer bolted on at the end: it is the difference between "we found no
prior art" and "we compared this much and this is what we found", and the second
is the only one the evidence supports."""


def draft_report_node(state: GraphState) -> dict[str, Any]:
    """Assemble and persist the next draft report version.

    Runs after Workflow Gate C and before Gate D. It always writes a **new**
    version rather than updating one, so an edited caveat produces a draft that
    needs fresh approval while the previously approved version stays immutable
    (case AD).
    """
    run_id = (state.get("run_id") or "").strip()
    if not run_id:
        raise UnapprovedArtifactError(
            "a draft report belongs to a durable run and this graph execution has "
            "no run_id"
        )

    emit_progress("draft_report", "Assembling the draft report…")

    with db.connect() as conn:
        reviews = review_store.list_reviews(conn, run_id=run_id)
        waivers = review_store.list_waivers(conn, run_id=run_id)
        draft = build_draft(state, reviews=reviews, waivers=waivers)
        version = review_store.create_draft_report(conn, run_id=run_id, draft=draft)

    return {"report_version": version["version"]}


def build_draft(
    state: GraphState,
    *,
    reviews: dict[str, dict[str, Any]],
    waivers: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """The draft document, as data. Pure, so its contents can be asserted directly.

    Separated from the node for the same reason the evidence modules are pure:
    behaviour reachable only through a compiled graph is behaviour that is hard to
    pin, and every rule in this module's docstring is a rule a test needs to check.
    """
    cells = list(state.get("cells") or ())
    exclusions = list(state.get("publication_exclusions") or ())

    matrix: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []

    for cell in cells:
        cell_id = str(cell.get("cell_id", ""))
        review = reviews.get(cell_id)
        cell_waivers = waivers.get(cell_id, [])
        state_name = str(cell.get("state", ""))

        entry = {
            "cell_id": cell_id,
            "feature_id": cell.get("feature_id"),
            "publication_number": cell.get("publication_number"),
            "state": state_name,
            # The machine result is carried verbatim and is never edited by a
            # review; §13.1. The overlay sits beside it.
            "machine_outcome": cell.get("outcome"),
            "failed_check": cell.get("failed_check"),
            "not_evaluated_reason": cell.get("not_evaluated_reason"),
            "result_version": cell.get("result_version"),
            "review": review,
            "reviewed_outcome": (review or {}).get("reviewed_outcome"),
            "waivers": cell_waivers,
            "class": _cell_class(state_name, review, cell_waivers),
        }
        matrix.append(entry)

        if state_name in ("VALIDATION_FAILURE", "NOT_EVALUATED"):
            unresolved.append(
                {
                    "cell_id": cell_id,
                    "state": state_name,
                    "failed_check": cell.get("failed_check"),
                    "reason": cell.get("not_evaluated_reason")
                    or cell.get("failure_reason"),
                    # Waived or not, it still renders as what it is. The waiver is
                    # printed beside it, never instead of it.
                    "waived": bool(cell_waivers),
                    "unresolved_limitations": [
                        waiver["unresolved_limitation"] for waiver in cell_waivers
                    ],
                }
            )

    return {
        "status": "DRAFT — NOT APPROVED",
        "coverage_statement": COVERAGE_STATEMENT,
        # **The provenance of the report, carried in the report.**
        #
        # A reviewed verification report is an audit document: which approved
        # decomposition it verified, which approved candidate set it verified
        # against, and under which recorded date and text-version policies. The
        # rendered report showed "v—" for all five because the draft never
        # carried them, so the one artifact a reader keeps could not say what it
        # was built from.
        #
        # Read from state rather than re-derived: these are the versions the
        # cells were actually built with, and `verification_cells` records the
        # same two per row.
        "decomposition_version": state.get("decomposition_version"),
        "candidate_set_version": state.get("candidate_set_version"),
        "date_policy": state.get("date_policy"),
        "text_version_policy": state.get("text_version_policy"),
        "matrix": matrix,
        "unresolved": unresolved,
        "excluded_publications": [_excluded_entry(item) for item in exclusions],
        "counts": {
            "cells_total": len(matrix),
            "unresolved": len(unresolved),
            "excluded_publications": len(exclusions),
        },
    }


def _excluded_entry(item: dict[str, Any]) -> dict[str, Any]:
    """One excluded publication, **named**.

    The number was read as ``publication_number``, and the exclusion payloads in
    `GraphState` do not have that key -- `graph/evidence_nodes.py::_exclusion_payload`
    writes ``requested_number`` and ``normalized_number``. So every draft ever
    built recorded ``"publication_number": null`` for every publication it
    excluded, and the report listed a count of publications it could not name.

    That is the most consequential blank this report could carry. Measured EPO
    coverage on this project's own sample was 17.6%: the exclusion list is where
    a reader learns which documents this run could not reach, and the
    obligation this product accepted in return for shipping EPO-only enrichment
    is that every exclusion is *visible and reasoned*. A reason attached to no
    identifier discharges neither half.

    Both keys are emitted. ``publication_number`` is what already-stored drafts
    and their readers expect and is now populated; ``requested_number`` is what
    the exclusion itself is keyed by everywhere else, so the report can be
    matched back to `publication_exclusions` without a translation step. They are
    the same value except where the number could not be normalized, and in that
    case the requested form is the honest one to show -- it is what was asked
    for, and no normalized form exists.
    """
    requested = item.get("requested_number") or ""
    normalized = item.get("normalized_number") or ""
    return {
        "publication_number": normalized or requested,
        "requested_number": requested,
        "normalized_number": normalized,
        "state": item.get("state"),
        "reason": item.get("reason"),
        # Kept because it is what makes a derived exclusion auditable: an
        # EXCLUDED_INELIGIBLE row retrieved text and still built nothing.
        "sections_retrieved": list(item.get("sections_retrieved") or ()),
        "passages_built": item.get("passages_built", 0),
        "evaluated": False,
        "note": "NOT EVALUATED for feature disclosure.",
    }


def _cell_class(
    state_name: str,
    review: dict[str, Any] | None,
    waivers: list[dict[str, Any]],
) -> str:
    """Spec §12.4's rendering class, derived and never asserted.

    The vocabulary is closed (§9.8): `machine-generated`, `human-reviewed`,
    `human-overridden`, `unresolved`, `validation-failure`, `unevaluated`. A
    validation failure and an unevaluated cell keep their own class **whether or
    not they were waived** — a waiver is a disclosed gap, and a class that changed
    on waiving would hide it in exactly the view a reviewer scans fastest.
    """
    if state_name == "VALIDATION_FAILURE":
        return "validation-failure"
    if state_name == "NOT_EVALUATED":
        return "unevaluated"
    if review is None:
        return "machine-generated"
    if str(review.get("status", "")).upper() in ("OVERRIDDEN", "MODIFIED"):
        return "human-overridden"
    return "human-reviewed"


__all__ = ["COVERAGE_STATEMENT", "build_draft", "draft_report_node"]
