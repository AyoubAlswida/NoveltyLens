"""The Human-in-the-Loop gate nodes.

Spec §11.1 and §12: a verified run is not a black box that emits a report. It
**pauses**, and a person decides whether it continues. These nodes are where it
pauses.

Four of them came from Implementation Gate D. The fifth, `gate_strategy_node`,
is the SEARCH STRATEGY stage, and it sits between the approved understanding and
the search: nothing is dispatched to a provider until a person has approved both
what the invention is and what the run will go looking for.

## What an `interrupt()` here does and does not do

Each node calls LangGraph's `interrupt()` with a payload describing *what the
human is being asked to look at*. LangGraph commits a checkpoint, raises out of
`graph.stream()`, and `workflow/executor.py` catches the pending interrupt and
runs the §3.3.2 pause sequence: checkpoint (LangGraph's own transaction), then one
transaction of ours appending `review_required` and `paused` with the
`RUNNING -> WAITING_FOR_REVIEW` transition and the lease release, all through
`pause_run_for_review`.

**The node does not decide anything and does not write an approval.** It cannot:
`workflow/approvals.py` is the only module that writes an `approved_*` field, and
`tests/test_approval_boundary.py` asserts that at the source level.

## Resume: the payload is a signal, never the decision

When the run resumes, `interrupt()` returns whatever `Command(resume=...)` carried.
**These nodes do not trust it.** They re-read the approved artifact from the
database through `workflow/approvals.py`'s loaders, which filter `is_approved` **in
SQL** — an unapproved row is not fetched and then rejected, it is not fetched.

That is the whole point. The resume value travels through the executor, through a
LangGraph checkpoint, and originates in an HTTP request; if it were the decision,
then forging it would be approving. It is not the decision. It is a note saying
"a human decision was committed for gate X — go and read it."

## No auto-approval, anywhere

- No node advances a gate on a timeout. There is no timeout.
- No configuration disables a gate. There is no setting.
- A resume that arrives with no committed approval raises
  :class:`UnapprovedArtifactError` and fails the run, rather than continuing with
  machine output. `tests/test_workflow_gates.py` asserts each of these.
"""

from typing import Any

from langgraph.types import interrupt

import db
from graph.events import emit_progress
from graph.state import GraphState
from workflow import approvals
from workflow.types import UnapprovedArtifactError, WorkflowGate


def _run_id(state: GraphState, gate: WorkflowGate) -> str:
    """The durable run this pause belongs to.

    A gate node cannot run without one: the approval it will read back is stored
    against a run, and a pause nobody can resume is worse than no pause at all.
    A search-only run never reaches here — `_route_after_understanding` sends it
    straight past the whole branch.
    """
    run_id = (state.get("run_id") or "").strip()
    if not run_id:
        raise UnapprovedArtifactError(
            f"workflow gate {gate} needs a durable run to record its approval "
            f"against, and this graph execution has no run_id. A search-only run "
            f"must never reach a gate node."
        )
    return run_id


# --- Workflow Gate A: feature decomposition -------------------------------


def gate_a_node(state: GraphState) -> dict[str, Any]:
    """Pause for spec section 12.1 -- a human reviews the invention understanding.

    This is the inventor-facing **Invention Understanding** stage. The payload
    carries `candidate_decomposition`, which is **machine output**, alongside the
    inventor's own untouched submission. The field name is the guard:
    `verify_features_node` reads `approved_decomposition` and has no code path
    that falls back to this one.

    The original description travels in the payload as `original_description` and
    is read straight from state. It is never the summary, never edited, and never
    written back -- the approved summary is stored beside it, not over it.
    """
    run_id = _run_id(state, WorkflowGate.A)
    emit_progress("gate_a", "Awaiting your review of the invention understanding…")

    interrupt(
        {
            "gate": str(WorkflowGate.A),
            "run_id": run_id,
            "original_description": state.get("description") or "",
            "problem_solved": state.get("problem_solved") or "",
            "inventor_novelty_statement": state.get("inventor_novelty_statement") or "",
            "candidate_decomposition": state.get("candidate_decomposition"),
            "editable": True,
            "completion_rules": list(GATE_A_RULES),
        }
    )

    # A rejection is not an approval, and must not be read as one. If the
    # inventor asked for a re-analysis instead of approving, there is no approved
    # artifact to read and reading one would either fail or -- far worse -- pick
    # up a version approved earlier and treat it as this review's outcome. The
    # router sends the graph back to the proposing node from here.
    from graph.work_requests import pending_understanding_regeneration

    if pending_understanding_regeneration(run_id):
        return {}

    # The resume value is a signal; the artifact is read back from the database
    # through the approved-only readers. `latest_approved_*` filters `is_approved`
    # in SQL, so an unapproved proposal is not fetched and then rejected -- it is
    # not fetched.
    with db.connect() as conn:
        version = approvals.latest_approved_decomposition_version(conn, run_id=run_id)
        features = approvals.require_approved_decompositions(
            conn, run_id=run_id, version=version
        )
        # The summary is approved by the same act, at the same version, so a
        # missing one here means the two were written apart -- which
        # `approve_understanding_by_owner` makes impossible and this refuses to
        # paper over.
        understanding = approvals.require_approved_understanding(
            conn, run_id=run_id, version=version
        )
    first = features[0]
    return {
        "approved_summary": understanding["summary"],
        "approved_decomposition": {
            "version": version,
            "decomposition_id": first.decomposition_id,
            "approved_by": first.approved_by,
            "approved_at": first.approved_at,
            # Rebuilt into the spec section 2 wire shape `_require_approved`
            # reads. Built field by field rather than by serializing the model, so
            # a future field added to `ApprovedDecomposition` cannot silently
            # start travelling through graph state unexamined.
            "features": [
                {
                    "feature_id": feature.feature_id,
                    "feature_version": feature.feature_version,
                    "feature_text": feature.feature_text,
                    "elements": [
                        {
                            "element_id": element.element_id,
                            "text": element.text,
                            "core": element.core,
                            "order": element.order,
                        }
                        for element in feature.elements
                    ],
                    "relationships": [
                        {
                            "relationship_id": relationship.relationship_id,
                            "text": relationship.text,
                            "order": relationship.order,
                        }
                        for relationship in feature.relationships
                    ],
                }
                for feature in features
            ],
        },
        "decomposition_version": version,
        "understanding_version": version,
    }


GATE_A_RULES: tuple[str, ...] = (
    "a non-empty technical summary",
    "at least one technical capability, and at least one element in it",
    "at least one element marked as core",
    "every element and relationship carries non-empty text",
    "requirement ids unique across elements AND relationships",
    "no relationship refers to an element that is no longer present",
    "the approval names a version explicitly",
)
"""Spec section 12.1's completion rules plus the understanding stage's own,
surfaced to the reviewer.

Advisory in this payload and **binding** in `workflow/review.py`, which refuses an
approval that violates any of them. A rule shown but not enforced is a suggestion;
these are checked before anything is written.

The element and capability *limits* are deliberately not listed here. They are
enforced in the same place, but a reviewer meets them while editing rather than
at the moment they try to approve, and a completion rule that reads "at most 25
elements" invites the question this stage should not have to answer -- see
`graph/limits.py` on why the derivation stays out of the UI."""


# --- Workflow Gate B: candidate publications ------------------------------


def gate_b_node(state: GraphState) -> dict[str, Any]:
    """Pause for spec §12.2 — a human curates the candidate set.

    The payload carries `ranked_candidates` (machine output) **and** the
    exclusions, because §12.2 requires every excluded publication to be visible
    with its reason. An exclusion is not a candidate the reviewer can restore by
    ticking a box: `EXCLUDED_DATE` and `EXCLUDED_NO_FULL_TEXT` are not overridable
    (case AL, AK), and `workflow/review.py` refuses an approval naming one.
    """
    run_id = _run_id(state, WorkflowGate.B)
    emit_progress("gate_b", "Awaiting candidate publication review…")

    interrupt(
        {
            "gate": str(WorkflowGate.B),
            "run_id": run_id,
            "ranked_candidates": list(state.get("ranked_candidates") or ()),
            "excluded": list(state.get("publication_exclusions") or ()),
            "passages_built": {
                number: len(passages)
                for number, passages in (state.get("passages_by_publication") or {}).items()
            },
            "dates": state.get("publication_dates") or {},
            "completion_rules": list(GATE_B_RULES),
        }
    )

    with db.connect() as conn:
        version = approvals.latest_approved_candidate_set_version(conn, run_id=run_id)
        publications = approvals.require_approved_candidate_set(
            conn, run_id=run_id, version=version
        )
    # An approved-but-EMPTY set is legal and is not a failure: "none of these are
    # worth verifying" is a real decision. It produces a matrix with no cells,
    # which is a different thing from a matrix nobody approved.
    return {
        "approved_candidate_set": publications,
        "candidate_set_version": version,
    }


GATE_B_RULES: tuple[str, ...] = (
    "every approved publication is date-eligible",
    "every approved publication completed enrichment",
    "every approved publication has at least one built passage",
    "EXCLUDED_DATE and EXCLUDED_NO_FULL_TEXT are not overridable",
)
"""Spec §12.2 plus §3.7. The third rule is the one that makes "eligible with
nothing to cite" impossible: eligibility is *derived* from passages actually
built, so approving a publication with none would approve a comparison that cannot
be made."""


# --- Workflow Gate C: evidence and verdicts -------------------------------


def gate_c_node(state: GraphState) -> dict[str, Any]:
    """Pause for spec §12.3 — a human reviews every cell.

    Nothing is written back into state on resume. Reviews, waivers and audit rows
    live in the database beside the machine results (§13), never over them, and
    the report reads them from there. A resumed Gate C returns `{}` precisely
    because a human review must not be able to edit the machine result it reviews.
    """
    run_id = _run_id(state, WorkflowGate.C)
    cells = list(state.get("cells") or ())
    emit_progress("gate_c", "Awaiting evidence and verdict review…")

    interrupt(
        {
            "gate": str(WorkflowGate.C),
            "run_id": run_id,
            "cells_total": len(cells),
            "excluded_publications": len(state.get("publication_exclusions") or ()),
            "completion_rules": list(GATE_C_RULES),
        }
    )
    return {}


GATE_C_RULES: tuple[str, ...] = (
    "no cell may remain PENDING",
    "no UNCLEAR requirement may remain unresolved",
    "no outstanding context request",
    "every VALIDATION_FAILURE is rerun or waived — never overridden or converted",
    "every NOT_EVALUATED is acknowledged or waived",
)
"""Spec §12.3. The fourth is the one case AI and AJ pin: an invalid model output
cannot be laundered into a verdict, and a waiver discloses the gap rather than
filling it."""


# --- Workflow Gate D: the draft report ------------------------------------


def gate_d_node(state: GraphState) -> dict[str, Any]:
    """Pause for spec §12.4 — a human approves the draft report.

    A draft must already exist and have been presented (case AC). The node reads
    it from `report_versions` rather than from state, because a draft is a
    persisted, versioned artifact: an edited caveat creates a **new** version
    requiring fresh approval, and the previously approved one stays immutable
    (case AD).
    """
    run_id = _run_id(state, WorkflowGate.D)
    emit_progress("gate_d", "Awaiting draft report approval…")

    from workflow import review as review_store

    with db.connect() as conn:
        draft = review_store.latest_report_version(conn, run_id=run_id)
    if draft is None:
        raise UnapprovedArtifactError(
            "workflow gate D was reached with no draft report to present. Spec "
            "§12.4 and case AC: approval requires a draft that exists and was "
            "shown, and a report cannot be approved into existence."
        )

    interrupt(
        {
            "gate": str(WorkflowGate.D),
            "run_id": run_id,
            "report_version": draft["version"],
            "status": draft["status"],
            "completion_rules": list(GATE_D_RULES),
        }
    )

    with db.connect() as conn:
        approved = review_store.require_approved_report(conn, run_id=run_id)
    return {"report_version": approved["version"]}


GATE_D_RULES: tuple[str, ...] = (
    "a draft exists and was presented",
    "every unresolved cell is listed",
    "every excluded publication is listed",
    "every waiver appears with its unresolved limitation",
    "the draft carries 'DRAFT — NOT APPROVED' until approved",
)
"""Spec §12.4. The last four are why a report can be honest about what it does not
cover: the exclusions and the waivers are part of the deliverable, not omissions
from it."""


# --- The SEARCH STRATEGY stage --------------------------------------------


def gate_strategy_node(state: GraphState) -> dict[str, Any]:
    """Pause for the search strategy -- a human approves what will be searched for.

    Placed between the approved understanding and `search_orchestrator`, so
    **no provider is contacted until both approvals exist**: what the invention
    is, and what the run will go looking for.

    The payload carries `candidate_search_strategy`, which is **machine output**.
    The field name is the guard, exactly as at the understanding stage: the search
    reads `queries`, and this node is the only thing that writes `queries` on the
    verified branch -- from the **approved** strategy, filtered to the queries the
    reviewer left enabled.

    Returning the enabled query text is the whole mechanism behind "only enabled
    queries from the approved strategy execute". A disabled query is not removed
    from the record; it is simply not among the queries the search is given.
    """
    run_id = _run_id(state, WorkflowGate.STRATEGY)
    emit_progress("gate_strategy", "Awaiting your review of the search strategy…")

    interrupt(
        {
            "gate": str(WorkflowGate.STRATEGY),
            "run_id": run_id,
            "candidate_search_strategy": state.get("candidate_search_strategy"),
            "approved_summary": state.get("approved_summary") or "",
            "understanding_version": state.get("decomposition_version"),
            "editable": True,
            "completion_rules": list(GATE_STRATEGY_RULES),
        }
    )

    # A rejection is not an approval. If the reviewer asked for fresh queries
    # instead of approving, there is no approved strategy to read and reading one
    # would either fail or -- far worse -- pick up a version approved earlier and
    # treat it as this review's outcome. The router sends the graph back to the
    # proposing node from here.
    from graph.work_requests import pending_strategy_regeneration

    if pending_strategy_regeneration(run_id):
        return {}

    # The resume value is a signal; the artifact is read back from the database
    # through the approved-only reader, which filters `is_approved` in SQL.
    with db.connect() as conn:
        version = approvals.latest_approved_search_strategy_version(conn, run_id=run_id)
        approved = approvals.require_approved_search_strategy(
            conn, run_id=run_id, version=version
        )
    return {
        "approved_search_strategy": approved,
        "strategy_approved_version": version,
        # **This is what the search dispatches.** Only the enabled queries of the
        # approved strategy, and nothing else: not the proposal, and not the
        # queries `expand_queries_node` derived from the raw description.
        "queries": list(approved["enabled_queries"]),
    }


GATE_STRATEGY_RULES: tuple[str, ...] = (
    "at least one query, and at least one of them enabled",
    "every query carries non-empty text and a unique id",
    "every core technical element is covered by at least one enabled query",
    "no query claims to cover an element the approved analysis does not contain",
    "the strategy names the approved analysis version it covers",
)
"""What this stage requires, surfaced to the reviewer.

Advisory here and **binding** in `workflow/review.py`, which refuses an approval
that violates any of them. The third is the one the stage exists for: finding no
prior art for something nobody searched for is not a finding.

The query *limit* is deliberately absent, for the same reason the element limits
are absent from `GATE_A_RULES` -- a reviewer meets it while editing rather than at
the moment they try to approve."""


GATE_RULES: dict[WorkflowGate, tuple[str, ...]] = {
    WorkflowGate.A: GATE_A_RULES,
    WorkflowGate.STRATEGY: GATE_STRATEGY_RULES,
    WorkflowGate.B: GATE_B_RULES,
    WorkflowGate.C: GATE_C_RULES,
    WorkflowGate.D: GATE_D_RULES,
}


GATE_NODES: dict[WorkflowGate, str] = {
    WorkflowGate.A: "gate_a",
    WorkflowGate.STRATEGY: "gate_strategy",
    WorkflowGate.B: "gate_b",
    WorkflowGate.C: "gate_c",
    WorkflowGate.D: "gate_d",
}
"""Gate -> graph node name. Used by the adapter to describe a pause and by the
tests to assert every gate in the enum is actually wired into the graph — an
unwired gate is a review nobody is ever asked for."""


__all__ = [
    "GATE_A_RULES",
    "GATE_STRATEGY_RULES",
    "GATE_B_RULES",
    "GATE_C_RULES",
    "GATE_D_RULES",
    "GATE_NODES",
    "GATE_RULES",
    "gate_a_node",
    "gate_strategy_node",
    "gate_b_node",
    "gate_c_node",
    "gate_d_node",
]

