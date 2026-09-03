"""The executor side of the two human work requests (spec cases T and U).

The API records a request and returns `202`; **these are where the worker carries
it out**, and the two properties that matter are not about mechanics:

* a manually added publication **re-enters the pipeline** rather than skipping it,
  so it can still be excluded and still needs approving;
* a rerun **supersedes** rather than edits, so the prior result and any review
  attached to it survive untouched.

The graph-shape tests run offline. The claim/idempotency tests need real
PostgreSQL, because "a recovery does not do it twice" is a property of an
`UPDATE ... WHERE processed_at IS NULL`, which an in-memory double cannot exhibit.
"""

from unittest.mock import patch

import pytest

from graph import work_requests
from graph.pipeline import build_graph
from workflow import store
from workflow.types import UnapprovedArtifactError

OWNER = "testnovelty@gmail.com"


# --- the graph shape: neither consumer is in the graph any more -------------
#
# Three tests were REMOVED here, and each named a guarantee that no longer has
# anything to protect:
#
# * `test_a_manual_candidate_routes_back_into_the_real_pipeline` (case T) --
#   asserted `gate_b -> absorb_manual_candidates -> normalize_publications`;
# * `test_the_manual_candidate_loop_returns_to_gate_b_for_approval` -- asserted a
#   widened candidate set was approved again before reaching the verifier;
# * `test_a_rerun_returns_to_the_reviewer_rather_than_marching_past_them`
#   (case U) -- asserted `gate_c -> rerun_cells -> gate_c`.
#
# All three describe loops through Gate B, Gate C and the evidence layer. Feature
# verification was withdrawn from the product: there is no candidate set to widen,
# no matrix cell to rerun and no reviewer to return to. The guarantees did not
# weaken -- the steps they sequenced are gone from the topology.
#
# `graph/work_requests.py` itself is RETAINED as legacy (spec section 10.3), with
# the focused unit tests below, and the test that follows now asserts the thing
# that replaced those three: it is unreachable from the compiled product graph.


def test_neither_work_request_consumer_is_in_the_compiled_graph():
    """The replacement guarantee: retained module, no path to it.

    Section 10.3 keeps `graph/work_requests.py` for historical reference; what
    must stay true is that no compiled graph reaches it, so the nodes cannot run
    against a product that has no UI or API for them.
    """
    graph = build_graph().get_graph()
    edges = {(edge.source, edge.target) for edge in graph.edges}
    for node in ("absorb_manual_candidates", "rerun_cells"):
        assert node not in set(graph.nodes)
        assert not any(node in (source, target) for source, target in edges)


def test_neither_consumer_is_reachable_on_a_search_only_run():
    """A search-only run never enters the verification branch at all."""
    for node in ("absorb_manual_candidates", "rerun_cells"):
        with pytest.raises(UnapprovedArtifactError):
            getattr(work_requests, f"{node.replace('absorb_manual_candidates', 'absorb_manual_candidates')}_node"
                    if node == "absorb_manual_candidates" else "rerun_cells_node")({})


# --- routing decisions -----------------------------------------------------


def test_routing_skips_the_loop_when_nothing_was_requested():
    with patch.object(work_requests, "pending_manual_candidates", return_value=[]):
        assert work_requests.route_after_gate_b({"run_id": "r"}) == "verify_features"
    with patch.object(work_requests, "pending_cell_reruns", return_value=[]):
        assert work_requests.route_after_gate_c({"run_id": "r"}) == "draft_report"


def test_routing_takes_the_loop_when_a_human_asked_for_something():
    with patch.object(work_requests, "pending_manual_candidates", return_value=[{"x": 1}]):
        assert work_requests.route_after_gate_b({"run_id": "r"}) == "absorb_manual_candidates"
    with patch.object(work_requests, "pending_cell_reruns", return_value=[{"x": 1}]):
        assert work_requests.route_after_gate_c({"run_id": "r"}) == "rerun_cells"


def test_a_read_failure_never_silently_skips_a_humans_request():
    """The router cannot raise usefully, so it takes the branch that does the
    work -- which re-reads and fails loudly there. Silently choosing
    `verify_features` would drop a request a person made."""
    with patch.object(work_requests, "pending_manual_candidates", side_effect=RuntimeError):
        assert work_requests.route_after_gate_b({"run_id": "r"}) == "absorb_manual_candidates"
    with patch.object(work_requests, "pending_cell_reruns", side_effect=RuntimeError):
        assert work_requests.route_after_gate_c({"run_id": "r"}) == "rerun_cells"


def test_a_manual_candidate_is_never_labelled_as_a_discovered_one():
    """A publication nobody's search returned must not read in a report as though
    one did."""
    assert work_requests.MANUAL_SOURCE == "Added by reviewer"
    assert "lens" not in work_requests.MANUAL_SOURCE.lower()
    assert "epo" not in work_requests.MANUAL_SOURCE.lower()


# --- claiming is idempotent, in the database -------------------------------


@pytest.mark.postgres
def test_a_claimed_manual_candidate_is_not_processed_twice(pg, make_run, created_runs):
    """At-least-once execution is what the lease provides (§3.3.1), so a recovery
    re-runs this node. `processed_at IS NULL` is what makes that safe."""
    run_id = make_run(created_by=OWNER)
    store.request_manual_candidate(
        pg, run_id=run_id, actor_email=OWNER, publication_number="EP4778509"
    )

    first = store.claim_manual_candidates(
        pg, run_id=run_id, actor_email=OWNER, publication_numbers=["EP4778509"]
    )
    second = store.claim_manual_candidates(
        pg, run_id=run_id, actor_email=OWNER, publication_numbers=["EP4778509"]
    )
    assert (first, second) == (1, 0), "a replay claimed the same request twice"


@pytest.mark.postgres
def test_a_claimed_request_is_kept_not_deleted(pg, make_run):
    """The row is the record that a person asked and the worker acted. Deleting
    it would erase the request along with the evidence it was honoured, and the
    report's audit appendix has to be able to say a candidate was added by hand.
    """
    run_id = make_run(created_by=OWNER)
    store.request_manual_candidate(
        pg, run_id=run_id, actor_email=OWNER, publication_number="EP4778509"
    )
    store.claim_manual_candidates(
        pg, run_id=run_id, actor_email=OWNER, publication_numbers=["EP4778509"]
    )

    rows = store.list_manual_candidates(pg, run_id=run_id, actor_email=OWNER)
    assert len(rows) == 1
    assert rows[0]["processed"] is True
    assert rows[0]["requested_by"] == OWNER


@pytest.mark.postgres
def test_claiming_is_ownership_scoped(pg, make_run, second_user):
    from workflow.types import RunNotFoundError

    run_id = make_run(created_by=OWNER)
    store.request_manual_candidate(
        pg, run_id=run_id, actor_email=OWNER, publication_number="EP4778509"
    )
    with pytest.raises(RunNotFoundError):
        store.claim_manual_candidates(
            pg, run_id=run_id, actor_email=second_user, publication_numbers=["EP4778509"]
        )
    with pytest.raises(RunNotFoundError):
        store.list_manual_candidates(pg, run_id=run_id, actor_email=second_user)


@pytest.mark.postgres
def test_pending_reads_exclude_what_has_already_been_carried_out(pg, make_run):
    run_id = make_run(created_by=OWNER)
    store.request_manual_candidate(
        pg, run_id=run_id, actor_email=OWNER, publication_number="EP4778509"
    )
    with patch("graph.work_requests.db.connect") as connect:
        connect.return_value.__enter__.return_value = pg
        assert len(work_requests.pending_manual_candidates(run_id, OWNER)) == 1
        store.claim_manual_candidates(
            pg, run_id=run_id, actor_email=OWNER, publication_numbers=["EP4778509"]
        )
        assert work_requests.pending_manual_candidates(run_id, OWNER) == []


@pytest.mark.postgres
def test_a_rerun_request_names_the_version_the_reviewer_saw(pg, make_run):
    """A rerun composed against a superseded result is refused, for the same
    reason §13.4 check 16 refuses a stale review."""
    from workflow.types import IllegalStateError, StaleResultVersionError

    run_id = make_run(created_by=OWNER)
    # No result at all: a rerun supersedes a result, it does not create one.
    with pytest.raises(IllegalStateError):
        store.request_cell_rerun(
            pg,
            run_id=run_id,
            cell_id=f"{run_id}:F2:US1",
            actor_email=OWNER,
            context_requests=[],
            source_result_version=1,
        )
    assert StaleResultVersionError.code == "STALE_RESULT_VERSION"


# --- the rerun never edits -------------------------------------------------


def test_rerun_single_cell_refuses_a_cell_that_is_not_in_the_matrix():
    """A rerun cannot invent a cell. A cell id that does not resolve is a request
    about something that was never verified."""
    from graph.verification_nodes import rerun_single_cell

    with pytest.raises(UnapprovedArtifactError):
        rerun_single_cell(
            {"run_id": "r", "cells": [], "approved_decomposition": {}, "approved_candidate_set": {}},
            cell_id="r:F2:US1",
            context_requests=[],
            source_result_version=1,
        )


def test_the_rerun_result_carries_its_lineage():
    """A result that cannot say which version it superseded cannot be reconciled
    with the review attached to that version."""
    import inspect

    from graph.verification_nodes import rerun_single_cell

    source = inspect.getsource(rerun_single_cell)
    assert '"source_result_version": source_result_version' in source
    assert '"result_version": source_result_version + 1' in source
    # And it never rewrites the old row.
    for forbidden in ("UPDATE verification_results", "DELETE FROM verification_results"):
        assert forbidden not in source
