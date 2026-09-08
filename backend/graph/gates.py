"""The Human-in-the-Loop gate nodes.

An Advanced Search run is not a black box that emits a report. It **pauses**, and
a person decides whether it continues. These two nodes are where it pauses.

`gate_a_node` and `gate_limitations_node` independently review the summary and
limitations; `gate_strategy_node`
is the search strategy (stage 3), and it sits between the approved understanding
and the search: nothing is dispatched to a provider until a person has approved
both what the invention is and what the run will go looking for. Gates B, C and D
were removed with feature verification -- see the note where they used to be.

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
    A search-only run never reaches here — `_route_from_start` sends it into the
    Quick Search branch instead.
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
            "review_stage": "summary",
            "editable": True,
            "completion_rules": list(GATE_LIMITATIONS_RULES),
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
    version = int(state.get("understanding_version") or 0)
    with db.connect() as conn:
        understanding = approvals.require_approved_understanding(conn, run_id=run_id, version=version)
    return {
        "approved_summary": understanding["summary"],
        "understanding_version": version,
        "understanding_review_stage": "limitations",
    }


def gate_limitations_node(state: GraphState) -> dict[str, Any]:
    """Pause independently for Stage 2 after its input summary was approved."""
    run_id = _run_id(state, WorkflowGate.A)
    emit_progress("gate_a", "Awaiting your review of the technical limitations…")
    interrupt(
        {
            "gate": str(WorkflowGate.A),
            "run_id": run_id,
            "review_stage": "limitations",
            "candidate_decomposition": state.get("candidate_decomposition"),
            "editable": True,
            "completion_rules": list(GATE_SUMMARY_RULES),
        }
    )
    version = int(state.get("understanding_version") or 0)
    with db.connect() as conn:
        features = approvals.require_approved_decompositions(conn, run_id=run_id, version=version)
        understanding = approvals.require_approved_understanding(conn, run_id=run_id, version=version)
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
        "understanding_review_stage": "complete",
    }


GATE_SUMMARY_RULES: tuple[str, ...] = ("a non-empty technical summary",)
"""The one binding rule surfaced while Stage 1 is on screen."""


GATE_LIMITATIONS_RULES: tuple[str, ...] = (
    "at least one non-empty technical element",
)
"""The one binding rule surfaced while Stage 2 is on screen."""


GATE_A_RULES: tuple[str, ...] = GATE_SUMMARY_RULES + GATE_LIMITATIONS_RULES
"""The combined server-side rules for a fully approved understanding.

Advisory in this payload and **binding** in `workflow/review.py`, which refuses an
approval that violates any of them. A rule shown but not enforced is a suggestion;
these are checked before anything is written.

**Exactly two of the three rules in spec section 3.5** (the third is the strategy
stage's own). The list shrank from seven when capability grouping, Core flags and
relationships were removed from the product: a rule about relationship references
or requirement-id uniqueness across relationships cannot be broken by a flat list
of strings, and `core` is no longer something a reviewer sets -- every approved
element is written `core = TRUE`, which is what makes the internal
`NO_CORE_ELEMENT` condition unreachable rather than deleted and hoped for.

Each pause surfaces only its own subset above. The combined tuple remains the
binding list for code paths that validate the completed summary and limitations
together."""


# --- Workflow Gates B, C and D: REMOVED ------------------------------------
#
# `gate_b_node`, `gate_c_node`, `gate_d_node` and their `GATE_*_RULES` tuples were
# removed here when feature verification was withdrawn from the product. They
# paused for candidate curation, the evidence matrix review and the draft report
# approval; none of those exist, none of the nodes is in the compiled graph, and
# each emitted a phase (`gate_b`, `gate_c`, `draft_report`, `gate_d`) that is no
# longer in `graph/events.py`'s vocabulary -- so calling one would raise
# `SSEContractError` rather than pause anything.
#
# This file is NOT part of the legacy subsystem retained by spec section 10.3.
# `backend/verification/`, `backend/evidence/`, `graph/evidence_nodes.py`,
# `graph/verification_nodes.py`, `graph/report.py` and `graph/work_requests.py`
# are; the gate nodes that used to call into them are not, because a pause node
# with no gate, no route and no screen is not retained code, it is dead code.


def gate_strategy_node(state: GraphState) -> dict[str, Any]:
    """Pause for the search strategy -- a human approves what will be searched for.

    Placed between the approved understanding and `search_orchestrator`, so
    **no provider is contacted until all three approvals exist**: the summary,
    the limitations, and what the run will go looking for.

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
    "at least one non-empty search query",
)
"""What stage 3 requires, surfaced to the reviewer.

Advisory here and **binding** in `workflow/review.py`. The third and last of spec
section 3.5's rules; nothing else blocks.

The list shrank from five with the concepts behind it. Enable/disable switches are
gone, so "at least one of them enabled" has no control to describe -- every stored
query is enabled. Query ids are gone from the public shape, so uniqueness has
nothing to apply to. Element-to-query coverage is gone, so the two coverage rules
describe a mapping the reviewer can no longer see or set: a rule the screen cannot
show is one an approval can fail on invisibly."""


GATE_RULES: dict[WorkflowGate, tuple[str, ...]] = {
    WorkflowGate.A: GATE_A_RULES,
    WorkflowGate.STRATEGY: GATE_STRATEGY_RULES,
}


GATE_NODES: dict[WorkflowGate, str] = {
    WorkflowGate.A: "gate_a",
    WorkflowGate.STRATEGY: "gate_strategy",
}
"""**Live** gate -> graph node name. Used by the adapter to describe a pause and
by the tests to assert every gate here is actually wired into the graph — an
unwired gate is a review nobody is ever asked for.

`WorkflowGate.B`, `.C` and `.D` are deliberately absent: their nodes were removed
with feature verification, so mapping them would claim a pause the product can no
longer reach. They stay in the `WorkflowGate` enum so a run parked at one before
the withdrawal can still be read back and reported as uncontinuable (spec §6.3);
removing the members would turn a legacy row into an enum error at exactly the
moment the product is supposed to explain itself."""


__all__ = [
    "GATE_A_RULES",
    "GATE_SUMMARY_RULES",
    "GATE_LIMITATIONS_RULES",
    "GATE_STRATEGY_RULES",
    "GATE_NODES",
    "GATE_RULES",
    "gate_a_node",
    "gate_limitations_node",
    "gate_strategy_node",
]

