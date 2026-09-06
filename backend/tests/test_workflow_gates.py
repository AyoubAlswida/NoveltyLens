"""The Human-in-the-Loop gates: wiring, vocabulary, and the approval boundary.

**A gate that is not in the graph is a review nobody is ever asked for**, and that
failure is silent: the run simply finishes without pausing, and the report reads
as though a person approved every step. So the first thing this module asserts is
that every gate is a reachable node, derived from the enum rather than from a list
somebody kept in step by hand.

The second thing it asserts is the boundary. `graph/gates.py` may pause a run and
may *read* an approved artifact; it may never write one, never decide anything,
and never accept the resume value as a decision. Those are properties of the
source, so they are checked against the source -- at the AST level where a
substring check would be fooled by a docstring that merely names the thing it
refuses to do.

No test here makes a network call or an LLM call.
"""

import ast
from pathlib import Path

import pytest

from graph import gates
from graph.adapter import ADAPTER, gate_of, initial_input, resume_value, review_payload
from graph.events import PHASES
from graph.pipeline import build_graph
from workflow.types import UnapprovedArtifactError, WorkflowGate

BACKEND = Path(__file__).resolve().parent.parent
GATES_SOURCE = BACKEND / "graph" / "gates.py"


# --- every gate is wired, and the list is derived ---------------------------


def test_the_live_gates_are_exactly_the_two_advanced_search_pauses():
    """Advanced Search pauses twice: the understanding, then the search queries.

    Was `set(GATE_NODES) == set(WorkflowGate)`. The enum is deliberately WIDER
    than the graph now -- `B`/`C`/`D` remain members so a legacy run parked at one
    of them can still be read back and reported as uncontinuable (spec section
    6.3) -- so equality against the enum would now assert the withdrawn gates back
    into the topology. The live set is named explicitly instead, which fails just
    as loudly if a gate is added and not wired.
    """
    assert set(gates.GATE_NODES) == {WorkflowGate.A, WorkflowGate.STRATEGY}


def test_the_withdrawn_gates_remain_readable_but_have_no_node():
    """The other half: B/C/D are still nameable, and reach nothing.

    A legacy `verification_runs.current_gate` of `B` has to parse -- otherwise the
    "this run cannot be continued" path fails with an enum error instead of the
    explanation it exists to give.
    """
    for withdrawn in (WorkflowGate.B, WorkflowGate.C, WorkflowGate.D):
        assert WorkflowGate(str(withdrawn)) is withdrawn, "no longer readable"
        assert withdrawn not in gates.GATE_NODES

    nodes = set(build_graph().get_graph().nodes)
    assert not ({"gate_b", "gate_c", "gate_d"} & nodes)


def test_every_gate_node_is_registered_in_the_compiled_graph():
    """**The anti-silence test.** An unwired gate does not error -- the run just
    never pauses, and a report that nobody approved reads exactly like one that
    was approved."""
    nodes = set(build_graph().get_graph().nodes)
    for gate, node_name in gates.GATE_NODES.items():
        assert node_name in nodes, f"workflow gate {gate} is not in the graph"


def test_an_advanced_search_run_ends_after_explain():
    """Replaces `test_the_draft_report_node_sits_between_gate_c_and_gate_d`.

    That test guarded case AC -- a report cannot be approved into existence --
    for the Gate D draft report. Feature verification was withdrawn, so there is
    no draft, no Gate D and no case AC to guard; the guarantee did not weaken,
    the feature it protected no longer exists.

    What replaces it is the property that took its place: an Advanced Search run
    terminates after `explain`, exactly as a search-only run does, and reaches
    none of the withdrawn evidence nodes. Asserted against the compiled node set
    rather than a run transcript, so it holds without executing anything.
    """
    graph = build_graph().get_graph()
    edges = {(edge.source, edge.target) for edge in graph.edges}
    assert ("explain", "__end__") in edges

    withdrawn = {
        "normalize_publications", "screen_dates", "enrich_epo", "build_passages",
        "confirm_eligibility", "rank_and_reduce", "gate_b", "verify_features",
        "gate_c", "draft_report", "gate_d", "absorb_manual_candidates",
        "rerun_cells", "regenerate_understanding", "regenerate_strategy",
    }
    assert not (withdrawn & set(graph.nodes)), "a withdrawn node is back in the graph"
    assert not any(
        source in withdrawn or target in withdrawn for source, target in edges
    ), "a withdrawn node is still referenced by an edge"


def test_gate_a_precedes_the_search_that_uses_its_decomposition():
    """Nothing is searched for against requirements nobody approved.

    The path is now longer by one approved stage: an approved understanding leads
    to `propose_strategy`, and the search is reached only through that stage's own
    gate. The invariant is unchanged and stronger -- there are two approvals in
    front of the first provider call rather than one -- so this asserts the path
    rather than a single edge that no longer exists.
    """
    edges = {(edge.source, edge.target) for edge in build_graph().get_graph().edges}
    assert ("decompose", "gate_a") in edges
    assert ("gate_a", "propose_strategy") in edges
    assert ("propose_strategy", "gate_strategy") in edges
    assert ("gate_strategy", "prepare_approved_search") in edges
    assert ("prepare_approved_search", "search_orchestrator") in edges
    assert ("decompose", "search_orchestrator") not in edges
    assert ("gate_a", "search_orchestrator") not in edges


def test_no_provider_call_is_reachable_before_both_approvals():
    """**The property the SEARCH STRATEGY stage exists for.**

    `search_orchestrator` is the first node that can reach a provider. On the
    verified branch it must be reachable only from `prepare_approved_search`,
    which is reachable only from `gate_strategy`, then `propose_strategy`, then
    `gate_a`. Any other incoming edge would be a path to a provider call that
    skips one of the two approvals -- and it would be silent, because a run that
    searched early would look exactly like one that searched on time.

    `expand_queries` is the search-only branch and is asserted separately: that
    path has no gates at all, by design, and `POST /api/analyze` depends on it.
    """
    edges = {(edge.source, edge.target) for edge in build_graph().get_graph().edges}
    into_search = {source for source, target in edges if target == "search_orchestrator"}
    assert into_search == {"expand_queries", "prepare_approved_search"}

    into_prepare = {
        source for source, target in edges if target == "prepare_approved_search"
    }
    assert into_prepare == {"gate_strategy"}

    into_strategy_gate = {source for source, target in edges if target == "gate_strategy"}
    assert into_strategy_gate == {"propose_strategy"}

    into_propose = {source for source, target in edges if target == "propose_strategy"}
    # `gate_a` alone. This set used to also carry `regenerate_strategy`; with
    # regeneration removed from the product (spec section 10.2) the chain is
    # strictly TIGHTER than it was, not looser -- one way in, from the gate.
    assert into_propose == {"gate_a"}

    into_gate_a = {source for source, target in edges if target == "gate_a"}
    assert into_gate_a == {"decompose"}


# `test_gate_b_precedes_the_verifier_that_uses_its_candidate_set` was REMOVED,
# not weakened. It guaranteed that no publication was verified against unless a
# person put it in the approved candidate set. There is no verifier and no
# candidate set any more -- Gate B, `rank_and_reduce` and `verify_features` are
# all out of the topology -- so the guarantee has nothing left to protect.
# `test_an_advanced_search_run_ends_after_explain` asserts those nodes are
# unreachable, which is the stronger statement: not "the approval comes first"
# but "the step does not exist".


# --- the phase vocabulary is synchronized -----------------------------------


GATE_PHASES = ("gate_a", "gate_strategy")
"""The phases the two live gates emit. Was six; `gate_b`, `gate_c`,
`draft_report` and `gate_d` went with their nodes."""


@pytest.mark.parametrize("phase", GATE_PHASES)
def test_every_gate_phase_is_in_the_backend_vocabulary(phase):
    """`emit_progress` raises `SSEContractError` on an unknown phase, so a gate
    node emitting one that is not in `PHASES` fails the run at the pause."""
    assert phase in PHASES


@pytest.mark.parametrize("phase", GATE_PHASES)
def test_every_gate_phase_is_in_the_frontend_vocabulary(phase):
    """Spec §9.8: a new phase goes into `PHASES` **and** `PHASE_ORDER` in the same
    commit. The backend fails loudly on the first; nothing fails on the second
    except a checklist that silently stalls in a browser, which is why this is
    asserted rather than trusted."""
    report_ts = (BACKEND.parent / "frontend" / "src" / "app" / "models" / "report.ts")
    text = report_ts.read_text(encoding="utf-8")
    order = text.split("PHASE_ORDER = [", 1)[1].split("]", 1)[0]
    assert f"'{phase}'" in order, f"{phase} is missing from the frontend PHASE_ORDER"
    assert f"  {phase}:" in text, f"{phase} has no PHASE_LABELS entry"


def test_the_two_phase_lists_have_not_drifted_apart():
    """Both directions. A phase the frontend lists but the backend cannot emit is
    a checklist step that never lights up."""
    report_ts = (BACKEND.parent / "frontend" / "src" / "app" / "models" / "report.ts")
    order_block = report_ts.read_text(encoding="utf-8").split("PHASE_ORDER = [", 1)[1].split("]", 1)[0]
    frontend = {line.strip().strip(",").strip("'") for line in order_block.splitlines()
                if line.strip().startswith("'")}
    # `semantic_search` and `fuse` are deliberately backend-only: their nodes are
    # disabled, so the backend never emits them and listing them would mark them
    # complete once `validate` fires.
    assert frontend <= set(PHASES), f"frontend lists phases the backend cannot emit: {frontend - set(PHASES)}"
    assert set(PHASES) - frontend == {"semantic_search", "fuse"}


# --- the approval boundary, at the source ----------------------------------


def _gates_ast() -> ast.Module:
    return ast.parse(GATES_SOURCE.read_text(encoding="utf-8"))


def test_no_gate_node_writes_an_approval():
    """`workflow/approvals.py` is the only module that writes an `approved_*`
    field, because a row saying a human approved something must exist only
    because a named human, at a recorded time, said so.

    Checked at the AST level: the docstrings in `gates.py` *name* the approval
    writers in order to say it never calls them, and a substring check would be
    fooled by exactly that.
    """
    forbidden = {
        "approve_decomposition",
        "approve_decomposition_by_owner",
        "approve_candidate_set",
        "approve_candidate_set_by_owner",
        "approve_report",
        "save_review",
        "save_waiver",
        "write_audit",
    }
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(_gates_ast())
        if isinstance(node, ast.Call)
    }
    assert not (called & forbidden), f"a gate node calls an approval writer: {called & forbidden}"


def test_gate_nodes_read_approvals_only_through_the_approved_only_readers():
    """The readers filter `is_approved` **in SQL**: an unapproved row is not
    fetched and then rejected, it is not fetched."""
    called = {
        node.func.attr
        for node in ast.walk(_gates_ast())
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    # The two live gates' readers. `require_approved_candidate_set` and
    # `require_approved_report` went with Gates B and D; the readers themselves
    # are retained in `workflow/approvals.py`, but no gate node calls them, so
    # asserting a call would assert the withdrawn gates back into this file.
    assert "require_approved_decompositions" in called
    assert "require_approved_understanding" in called
    assert "require_approved_search_strategy" in called


def test_no_gate_node_returns_the_resume_value_as_a_decision():
    """The resume value travels through an HTTP request, an event row and a
    checkpoint. If it were the decision, forging it at any of those three points
    would be approving. Every gate node discards it: `interrupt(...)` is called as
    a bare statement, never assigned and never returned."""
    for node in ast.walk(_gates_ast()):
        if isinstance(node, (ast.Assign, ast.Return)):
            for call in ast.walk(node):
                if isinstance(call, ast.Call) and getattr(call.func, "id", "") == "interrupt":
                    raise AssertionError(
                        "a gate node captured interrupt()'s return value; the resume "
                        "value is a signal, never the decision"
                    )


def test_no_auto_approval_switch_exists_anywhere_in_the_gate_path():
    """No timeout advances a gate, no configuration disables one. Grepped, because
    each would be added in a single line and would remove the whole guarantee."""
    text = GATES_SOURCE.read_text(encoding="utf-8").lower()
    for phrase in ("auto_approve", "autoapprove", "skip_gate", "bypass_gate",
                   "gate_timeout", "timeout_approve", "disable_gate"):
        assert phrase not in text, f"gates.py mentions {phrase}"


# --- a gate cannot pause a run it cannot be resumed for --------------------


@pytest.mark.parametrize("gate_node", ["gate_a_node", "gate_strategy_node"])
def test_a_gate_without_a_run_id_refuses_rather_than_pausing(gate_node):
    """A search-only run must never reach a gate. If one somehow did, a pause
    nobody can resume is worse than no pause -- so it raises instead."""
    with pytest.raises(UnapprovedArtifactError):
        getattr(gates, gate_node)({})


# --- the adapter describes the pause without deciding anything -------------


def test_gate_of_reads_the_gate_the_node_named():
    for gate in WorkflowGate:
        assert gate_of({"gate": gate.value}) is gate


def test_an_interrupt_naming_no_gate_is_refused():
    """The executor is about to persist `review_required` and transition the run
    to `WAITING_FOR_REVIEW`. A pause whose gate cannot be named cannot be resumed
    by any endpoint."""
    with pytest.raises(ValueError):
        gate_of({})
    with pytest.raises(ValueError):
        gate_of({"gate": "Z"})


def test_the_review_event_carries_counts_and_a_url_but_not_the_artifact():
    """The event log is replayed to every reconnecting client and retained for the
    life of the run. Putting a decomposition or a matrix in it would copy mutable
    state into an append-only store, where the copy starts going stale at once."""
    body = review_payload(
        {"run_id": "run-1"},
        WorkflowGate.C,
        {"gate": "C", "cells_total": 12, "excluded_publications": 4},
    )
    assert body["payload_url"] == "/api/runs/run-1/gates/C"
    assert body["summary"] == {"cells_total": 12, "excluded_publications": 4}
    assert all(isinstance(value, int) for value in body["summary"].values())


def test_the_review_event_carries_no_executor_internal():
    """Spec §9.8: nothing carries a lease token, an owner id, or a checkpoint blob."""
    body = review_payload({"run_id": "r"}, WorkflowGate.A, {"gate": "A"})
    rendered = repr(body)
    for leaked in ("fence", "token", "owner_id", "checkpoint", "lease"):
        assert leaked not in rendered.lower()


def test_the_resume_value_carries_no_approval():
    """It says "a decision was committed -- go and read it", and nothing else."""
    value = resume_value({"run_id": "r"}, {"payload": {"gate": "A"}})
    assert value["resumed"] is True
    rendered = repr(value).lower()
    for forbidden in ("approved_by", "decomposition", "version", "is_approved", "reviewer"):
        assert forbidden not in rendered


def test_the_adapter_derives_advanced_search_from_the_run_row_existing():
    """Every row in `verification_runs` is an Advanced Search run, so the flag is
    derived from that fact rather than passed in beside it -- the two cannot
    disagree. (The flag was `verification_requested`; renamed with no alias and
    no dual-read period, spec section 10.5.)"""
    graph_input = initial_input(
        {
            "run_id": "r",
            "description": "d",
            "claims": None,
            "cutoff_date": None,
            "date_policy": "publication_lt_cutoff",
            "text_version_policy": "provider_publication_date",
        }
    )
    assert graph_input["advanced_search"] is True
    assert graph_input["run_id"] == "r"


def test_the_adapter_supplies_no_policy_default():
    """Spec §3.6: the policies have no default anywhere. Re-supplying one here
    would let a run execute under a policy nobody recorded."""
    with pytest.raises(KeyError):
        initial_input({"run_id": "r", "description": "d"})


def test_the_production_adapter_is_the_one_the_executor_would_load():
    from workflow.executor import load_adapter

    assert load_adapter("graph.adapter:ADAPTER") is ADAPTER


# --- the interrupt payload arrives in more than one shape ------------------


def test_gate_of_unwraps_every_shape_langgraph_hands_back():
    """**The shape that bit.** The executor unwraps `.value` once; for a task
    carrying a sequence of interrupts that returns the sequence unchanged, and
    `gate_of` then correctly refused a payload with no `gate` -- failing the run
    at its first pause. Four shapes appear in real traces and all four resolve."""

    class FakeInterrupt:
        def __init__(self, value):
            self.value = value

    payload = {"gate": "C", "run_id": "r"}

    assert gate_of(payload) is WorkflowGate.C
    assert gate_of(FakeInterrupt(payload)) is WorkflowGate.C
    assert gate_of((FakeInterrupt(payload),)) is WorkflowGate.C
    assert gate_of([FakeInterrupt(payload)]) is WorkflowGate.C
    assert gate_of(FakeInterrupt(FakeInterrupt(payload))) is WorkflowGate.C
    # **The shape that actually failed in the four-gate acceptance run.** An
    # `Interrupt` serialized as a dict is still a dict, so the unwrapper returned
    # the WRAPPER and the payload was never reached -- and `gate_of` correctly
    # refused something with no `gate`.
    assert gate_of({"value": payload, "id": "abc"}) is WorkflowGate.C
    assert gate_of([{"value": payload, "id": "abc"}]) is WorkflowGate.C


def test_an_empty_or_unnameable_interrupt_is_still_refused():
    """The refusal itself is not weakened by the extra unwrapping: a pause whose
    gate cannot be named cannot be resumed by any endpoint."""
    with pytest.raises(ValueError):
        gate_of(())
    with pytest.raises(ValueError):
        gate_of([{"no_gate": True}])
    with pytest.raises(ValueError):
        gate_of(object())


def test_the_cutoff_date_reaches_the_graph_as_a_string():
    """`verification_runs.cutoff_date` is a SQL `DATE`, so psycopg returns a
    `datetime.date` -- and `evidence/dates.py::parse_patent_date` calls `.strip()`
    on it. Passing the row value straight through failed the run between Gate A
    and Gate B with `AttributeError: 'datetime.date' object has no attribute
    'strip'`."""
    from datetime import date

    run = {
        "run_id": "r", "description": "d", "claims": None,
        "date_policy": "publication_lt_cutoff",
        "text_version_policy": "provider_publication_date",
        "cutoff_date": date(2024, 1, 1),
    }
    assert initial_input(run)["cutoff_date"] == "2024-01-01"

    run["cutoff_date"] = None
    assert initial_input(run)["cutoff_date"] is None

    run["cutoff_date"] = "2023-06-30"
    assert initial_input(run)["cutoff_date"] == "2023-06-30"
