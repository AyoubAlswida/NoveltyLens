"""Gate G section 6, the failure-injection rows, driven one at a time.

Every row in the specification's section 6 matrix must exist as a *named* test
before its gate is complete, and `tests/test_matrix_traceability.py` enforces
that mapping. This module holds the rows that had no named test of their own:
the ones about what happens at a boundary the happy path never touches.

They are grouped here rather than scattered because they share one property, and
it is the project's central one: **a failure is reported as itself.** Not as an
empty result, not as a completed run, not as a gate that happened to look clear.
"""

import threading
from unittest.mock import patch

import pytest

from workflow import lease, review as review_store, store
from workflow.review import GateBlocked
from workflow.types import RunStatus, WorkflowGate

pytestmark = pytest.mark.postgres

OWNER = "testnovelty@gmail.com"
WORKER = "gate-g-worker"
TTL = 60


def _running_run(pg, make_run) -> tuple[str, int]:
    """A run this test holds the lease on, in `RUNNING`."""
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
    return run_id, token


def _result(**overrides) -> dict:
    base = {
        "outcome": "FULLY_DISCLOSED_IN_SUPPLIED_PASSAGES",
        "feature_id": "F1",
        "feature_version": "1",
        "publication_number": "EP1000000",
        "elements": [{"element_id": "E1", "status": "FOUND", "evidence": []}],
        "relationships": [],
        "missing_element_ids": [],
        "requests_for_more_context": [],
    }
    base.update(overrides)
    return base


CHUNK = "PAT-EP1000000-CLM-001"
PASSAGE_TEXT = "A heat exchanger disposed in the exhaust duct of the engine."


def _passage(pg, run_id: str, token: int):
    """One real `evidence_passages` row, and the hash a citation to it must
    carry.

    Built explicitly rather than through `build_passages` so the test controls
    the hash: the point of the staleness rule is what happens when a stored
    citation and the passage of record disagree, and that comparison cannot be
    exercised through a path that always makes them agree.
    """
    import hashlib

    from evidence.passages import BuiltPassage
    from evidence.segmenter import SEGMENTER_VERSION
    from verification.passages import Passage
    from workflow import evidence_store

    digest = hashlib.sha256(PASSAGE_TEXT.encode("utf-8")).hexdigest()
    built = BuiltPassage(
        passage=Passage(
            chunk_id=CHUNK,
            publication_number="EP1000000",
            source_document_id="epo:EP1000000",
            source="EPO OPS",
            section="CLAIMS",
            locator="claim 1",
            language="en",
            text_version_date="2019-05-01",
            evidence_eligible=True,
            text=PASSAGE_TEXT,
        ),
        locator_kind="CLAIM_NUMBER",
        locator_source="PROVIDER_CLAIM_NUMBER",
        section_text_sha256=digest,
        text_sha256=digest,
        segmenter_version=SEGMENTER_VERSION,
        retrieved_at="2026-08-30T00:00:00+00:00",
        text_version_original="2019-05-01",
    )
    evidence_store.record_passages(
        pg, run_id=run_id, owner_id=WORKER, fence_token=token, passages=[built]
    )
    return digest


def _verified_cell(
    pg, run_id: str, token: int, machine_result: dict, text_hash: str = ""
) -> str:
    """One `VERIFIED` cell carrying the supplied machine result.

    It cites the passage `_passage` stored, with that passage's own hash unless
    the caller supplies a different one. A cell that cited nothing would dodge
    the staleness rule rather than satisfy it.
    """
    cell_id = run_id + ":F1:EP1000000"
    store.save_cell_result(
        pg,
        run_id=run_id,
        owner_id=WORKER,
        fence_token=token,
        cell={
            "cell_id": cell_id,
            "feature_id": "F1",
            "feature_version": "1",
            "publication_number": "EP1000000",
            "decomposition_version": 1,
            "candidate_set_version": 1,
            "state": "VERIFIED",
            "outcome": machine_result["outcome"],
            "machine_result": machine_result,
            "raw_response": "{}",
            "model": "stub",
            "prompt_version": "1",
            "passage_chunk_ids": [CHUNK],
            "passage_text_hashes": [text_hash],
            "attempts": 1,
        },
    )
    return cell_id


# --- row 34: resume with an unresolved context request ------------------------


def test_an_outstanding_context_request_blocks_gate_c(pg, make_run):
    """Section 12.3 and case AA: the gate is not clear while the model has asked
    for material it was never given.

    The request is the model saying its answer is limited by what it was shown.
    Completing the gate over it would turn "I could not see enough" into a
    finding, which is the substitution the verification vocabulary exists to
    prevent.
    """
    run_id, token = _running_run(pg, make_run)
    digest = _passage(pg, run_id, token)
    cell_id = _verified_cell(
        pg,
        run_id,
        token,
        _result(requests_for_more_context=["the figure referenced in claim 4"]),
        text_hash=digest,
    )

    blocking = review_store.blocking_conditions(pg, run_id=run_id, gate=WorkflowGate.C)
    assert "OUTSTANDING_CONTEXT_REQUEST" in {item["code"] for item in blocking}, blocking
    assert any(item["cell_id"] == cell_id for item in blocking)

    with pytest.raises(GateBlocked) as raised:
        review_store.require_gate_clear(pg, run_id=run_id, gate=WorkflowGate.C)
    # **Every** condition, not the first: a reviewer who fixes one and is then
    # told about the next has been made to discover the list one round trip at a
    # time.
    assert raised.value.blocking == blocking


def test_an_unresolved_unclear_requirement_blocks_gate_c(pg, make_run):
    """Section 12.3: an `UNCLEAR` requirement is not a negative finding, and the
    gate stays shut until a human resolves it or waives it."""
    run_id, token = _running_run(pg, make_run)
    digest = _passage(pg, run_id, token)
    _verified_cell(
        pg,
        run_id,
        token,
        _result(
            outcome="UNCLEAR",
            elements=[{"element_id": "E1", "status": "UNCLEAR", "evidence": []}],
        ),
        text_hash=digest,
    )

    blocking = review_store.blocking_conditions(pg, run_id=run_id, gate=WorkflowGate.C)
    assert {item["code"] for item in blocking} == {"UNRESOLVED_UNCLEAR"}, blocking
    assert blocking[0]["requirement_ids"] == ["E1"]


# --- row 132: no final report before the required gates complete --------------


def test_no_path_to_the_end_of_the_graph_skips_the_three_approvals():
    """A `result` cannot be produced while a mandatory gate is outstanding.

    Asserted on the topology rather than by running a graph: the property is
    "there is no such path", and a passing execution only shows that one path did
    not take it.

    This used to read `into_end == {"gate_d"}`. With feature verification
    withdrawn, `END` is reached from `explain` -- the ordinary end of the search
    pipeline -- for both kinds of run. The gate that guarded the exit is gone, so
    the property is re-expressed where it still bites: on an Advanced Search run
    the only route into the search half of the graph passes all three approvals, so no
    terminal result exists that skipped them. `explain` reaching END is not a
    bypass; it is the pipeline finishing.
    """
    from langgraph.graph import END

    from graph.pipeline import _assemble

    builder = _assemble()
    into_end = {source for source, target in builder.edges if target == END}
    assert into_end == {"explain"}, (
        "something other than the end of the pipeline reaches END: "
        + str(sorted(into_end - {"explain"}))
    )

    # The approvals still fence the search: `search_orchestrator` -- the first
    # node that can reach a provider, and therefore the first that can produce
    # anything a result is built from -- is entered either by the search-only
    # branch or through `prepare_approved_search`, which can only follow both
    # Advanced Search approvals. There is no third route.
    #
    # Read off the COMPILED graph, not `builder.edges`: the search-only arm is a
    # conditional edge, and an uncompiled builder does not list those, so the
    # same check against `builder` would silently see one incoming edge instead
    # of two and assert a topology that is not the one that runs.
    from graph.pipeline import build_graph

    compiled = {(edge.source, edge.target) for edge in build_graph().get_graph().edges}
    assert {s for s, t in compiled if t == "search_orchestrator"} == {
        "expand_queries",
        "prepare_approved_search",
    }
    assert ("gate_strategy", "prepare_approved_search") in compiled


def test_the_draft_must_exist_before_it_can_be_approved(pg, make_run):
    """Case AC, from the database's side: `approve_report` on a run with no draft
    refuses rather than creating one.

    Approving a report into existence would make the approved artifact and the
    reviewed artifact the same act, and nobody would have read anything.
    """
    from workflow.types import UnapprovedArtifactError

    run_id, _ = _running_run(pg, make_run)
    with pytest.raises(UnapprovedArtifactError):
        review_store.approve_report(
            pg, run_id=run_id, actor_email=OWNER, version=1, comments="", caveats=""
        )


# --- row 131: database / checkpoint outage ------------------------------------


def _stub_adapter():
    """The smallest thing `Executor` accepts. It is never invoked: the test
    patches the claim loop, and an adapter that ran would mean the outage was not
    injected where it was supposed to be."""
    from workflow.executor import GraphAdapter

    def unreachable(*args, **kwargs):  # pragma: no cover - asserted unreachable
        raise AssertionError("the graph must not be built during an outage")

    return GraphAdapter(
        build=unreachable,
        initial_input=unreachable,
        result_payload=unreachable,
        # `unreachable` like the rest, not a no-op: this adapter asserts nothing
        # runs during an outage, and a silent no-op here would let a persistence
        # call slip through unnoticed.
        persist_result=unreachable,
        gate_of=unreachable,
        review_payload=unreachable,
        resume_value=unreachable,
    )


def test_a_checkpoint_outage_stops_the_worker_and_fabricates_no_terminal(pg, make_run):
    """The executor waits and retries; it does not finish the run in memory.

    **There is deliberately no `error` event here**, and claiming one would be a
    promise the system cannot keep: an `error` event cannot be persisted while
    the event log is unreachable. The run keeps its last committed state, and a
    later reconcile records the sanitized infrastructure failure when there is
    finally somewhere to write it.
    """
    from checkpointer import CheckpointUnavailable
    from workflow.executor import Executor

    run_id, _ = _running_run(pg, make_run)
    before = store._system_get_run(pg, run_id=run_id)

    executor = Executor(adapter=_stub_adapter(), poll_interval_seconds=0.01)
    stop = threading.Event()
    calls: list[int] = []

    def outage():
        calls.append(1)
        if len(calls) >= 3:
            stop.set()
        raise CheckpointUnavailable("postgres at 127.0.0.1:5432/agenticai is unreachable")

    with patch.object(executor, "claim_and_run_one", side_effect=outage):
        executor.run_forever(stop=stop)

    assert len(calls) >= 3, "the worker gave up instead of waiting and retrying"

    after = store._system_get_run(pg, run_id=run_id)
    assert after["status"] is before["status"], "the run's state moved during an outage"
    assert after["status"] is RunStatus.RUNNING
    events = store.list_events(pg, run_id=run_id, actor_email=OWNER)
    terminals = [event for event in events if event["event_type"] in ("result", "error")]
    assert not terminals, (
        "a terminal event was fabricated while the event log was supposedly "
        "unreachable: " + str(terminals)
    )


# --- rows 10, 15 and 17: a dependent result goes stale ------------------------


def test_a_cell_citing_changed_passage_text_blocks_gate_c(pg, make_run):
    """"Dependents stale" is enforced, not merely detectable.

    `stale_passage_citations` computed exactly this condition and had **no
    caller**, and `save_cell_result` never wrote `passage_text_hashes` at all --
    so a citation could be checked for existence and never for having changed,
    and a reviewer could complete Gate C over a verdict about text that had moved
    underneath it. Both halves are wired now, and this is the test that fails if
    either is unwired again.
    """
    run_id, token = _running_run(pg, make_run)
    _passage(pg, run_id, token)
    cell_id = _verified_cell(
        pg, run_id, token, _result(), text_hash="0" * 64
    )

    blocking = review_store.blocking_conditions(pg, run_id=run_id, gate=WorkflowGate.C)
    stale = [item for item in blocking if item["code"] == "STALE_EVIDENCE"]
    assert stale, blocking
    assert stale[0]["cell_id"] == cell_id


def test_a_matching_citation_does_not_block(pg, make_run):
    """The other direction, so the rule above is not simply "always blocked"."""
    run_id, token = _running_run(pg, make_run)
    digest = _passage(pg, run_id, token)
    _verified_cell(pg, run_id, token, _result(), text_hash=digest)

    blocking = review_store.blocking_conditions(pg, run_id=run_id, gate=WorkflowGate.C)
    assert blocking == [], blocking


# --- a run that vanishes mid-flight must not stop the worker ------------------


def test_a_deleted_run_is_abandoned_and_the_worker_keeps_going(pg, make_run):
    """One deleted run must not stop every future run.

    Every lease-sensitive SQL function begins with
    `_lock_run_and_require_lease`, which raises `unknown run <id>` as a foreign
    key violation once the row is gone. That propagated out of the poll loop and
    killed the worker thread outright -- observed live: a run deleted while the
    executor held it took the worker down, and the API kept serving as though
    nothing had happened while every later run sat `QUEUED` for ever.

    A disappearance is not a failure OF the run. There is no run left to fail,
    no status to transition, and nothing to write an `error` event to. The only
    honest response is to say so and carry on.
    """
    import psycopg
    from workflow.executor import Executor

    executor = Executor(adapter=_stub_adapter(), poll_interval_seconds=0.01)
    stop = threading.Event()
    calls: list[int] = []

    def vanished():
        calls.append(1)
        if len(calls) >= 3:
            stop.set()
            return None
        raise psycopg.errors.ForeignKeyViolation(
            "unknown run 00000000-0000-0000-0000-000000000000"
        )

    with patch.object(executor, "claim_and_run_one", side_effect=vanished):
        executor.run_forever(stop=stop)

    assert len(calls) >= 3, "the worker stopped polling after a run disappeared"


def test_an_integrity_error_that_is_not_a_missing_run_still_stops_the_worker(pg, make_run):
    """The carve-out is narrow on purpose.

    A foreign-key violation that is *not* about a missing run is a real defect --
    a cell pointing at no publication, an attempt pointing at no run row that
    should exist -- and polling past it would turn a corruption into a silent
    retry loop.
    """
    import psycopg
    from workflow.executor import Executor

    executor = Executor(adapter=_stub_adapter(), poll_interval_seconds=0.01)

    def corrupt():
        raise psycopg.errors.ForeignKeyViolation(
            'insert or update on table "verification_cells" violates foreign key'
        )

    with patch.object(executor, "claim_and_run_one", side_effect=corrupt):
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            executor.run_forever(stop=threading.Event())
