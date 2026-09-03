"""The SEARCH STRATEGY stage: what may be searched for, and what may not.

This stage sits between the approved invention understanding and the first
provider call, and three properties dominate everything here:

1. **No provider is contacted until both approvals exist.** The graph pauses
   again after the understanding, and `search_orchestrator` is reachable on the
   verified branch only from this stage's gate.
2. **Only the queries a human approved and left enabled are dispatched.** A
   proposal is never dispatched, and a disabled query is kept as the record of
   what was deliberately not searched for.
3. **A core element nobody will search for blocks approval.** Reporting no prior
   art for something nobody looked for is not a finding.

Real PostgreSQL for everything that asserts a constraint, a version allocation or
an ownership scope -- the in-memory double is a dict and can exhibit none of them.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from auth import issue_token
from workflow import approvals, events, lease, store
from workflow import strategy as strategy_store
from workflow.review import _gate_strategy_blocking
from workflow.types import RunStatus, UnapprovedArtifactError, WorkflowGate

OWNER = "testnovelty@gmail.com"
OTHER = "someone-else@example.com"
WORKER = "worker-1"
TTL = 60


def _query(query_id="Q1", text="graphene oxide membrane", covers=("F1.E1",), enabled=True):
    return {
        "query_id": query_id,
        "query_text": text,
        "rationale": "looks for the membrane",
        "enabled": enabled,
        "covered_element_ids": list(covers),
    }


# =========================================================================
# The graph: where the stage sits, and what it guards
# =========================================================================


def test_the_strategy_stage_sits_between_the_approved_understanding_and_the_search():
    """`gate_a -> propose_strategy -> gate_strategy -> search_orchestrator`.

    Nothing is dispatched to a provider until a person has approved both what the
    invention is and what will be searched for.
    """
    from graph.pipeline import build_graph

    edges = {(edge.source, edge.target) for edge in build_graph().get_graph().edges}
    assert ("gate_a", "propose_strategy") in edges
    assert ("propose_strategy", "gate_strategy") in edges
    assert ("gate_strategy", "search_orchestrator") in edges


def test_an_approved_strategy_leads_to_the_search_and_nowhere_else():
    """Replaces `test_a_rejected_strategy_routes_back_to_the_planning_not_on_to_the_search`.

    Same reasoning as the understanding stage: regeneration is gone (spec section
    10.2), editing the queries replaces it, and `gate_strategy` is a plain edge.
    What must remain true is that the ONLY way out of the gate is the search --
    an approved strategy releases the run, and nothing else does.
    """
    from graph.pipeline import build_graph

    graph = build_graph().get_graph()
    edges = {(edge.source, edge.target) for edge in graph.edges}
    out_of_gate = {target for source, target in edges if source == "gate_strategy"}
    assert out_of_gate == {"search_orchestrator"}
    assert "regenerate_strategy" not in set(graph.nodes)


def test_a_read_failure_takes_the_branch_that_does_the_work():
    """A router cannot raise usefully, so a failed read must not silently skip a
    human's request. The regeneration node re-reads and fails loudly there."""
    from graph.work_requests import route_after_gate_strategy

    with patch(
        "graph.work_requests.pending_strategy_regeneration",
        side_effect=RuntimeError("database gone"),
    ):
        assert route_after_gate_strategy({"run_id": "r1"}) == "regenerate_strategy"


def test_a_search_only_run_never_reaches_the_strategy_stage():
    """The stage is on the verified branch only. A search-only run traverses
    exactly the nodes it did before, which the SSE contract depends on."""
    from graph.pipeline import _route_after_understanding

    assert _route_after_understanding({"advanced_search": False}) == "search_orchestrator"


def test_the_gate_refuses_to_pause_a_run_it_cannot_be_resumed_for():
    """A pause nobody can resume is worse than no pause."""
    from graph import gates

    with pytest.raises(UnapprovedArtifactError):
        gates.gate_strategy_node({})


def test_the_proposing_node_refuses_an_unapproved_understanding():
    """It reads `approved_decomposition` and has no fallback to the candidate one.
    Queries built from a reading of the invention nobody approved would put the
    search back in front of the approval it was moved behind."""
    from graph.strategy import propose_strategy_node

    with pytest.raises(UnapprovedArtifactError):
        propose_strategy_node({"candidate_decomposition": {"features": [{"feature_id": "F1"}]}})


# =========================================================================
# Completion rules -- these need the approved decomposition, so real PostgreSQL
# =========================================================================


@pytest.mark.postgres
class TestCompletionRules:
    """The blocking conditions, against real rows.

    `_gate_strategy_blocking` reads the approved decomposition to resolve
    coverage, so it cannot be exercised against the in-memory double: the double
    is a dict and has no `feature_decompositions` to read.
    """

    @pytest.fixture
    def approved_run(self, pg, make_run):
        """A run with an APPROVED understanding at version 1: two core elements
        in F1, one non-core, and one core element in F2 whose id collides with
        F1's -- which is legal, and is why coverage ids are feature-qualified."""
        run_id = make_run(created_by=OWNER)
        approvals.approve_understanding_by_owner(
            pg,
            run_id=run_id,
            actor_email=OWNER,
            version=1,
            summary="A device that filters and sterilises water.",
            features=[
                {
                    "feature_id": "F1",
                    "feature_version": "1",
                    "feature_text": "filtration",
                    "elements": [
                        {"element_id": "E1", "text": "a membrane", "core": True, "order": 1},
                        {"element_id": "E2", "text": "a flow sensor", "core": True, "order": 2},
                        {"element_id": "E3", "text": "an actuator", "core": False, "order": 3},
                    ],
                    "relationships": [],
                },
                {
                    "feature_id": "F2",
                    "feature_version": "1",
                    "feature_text": "sterilisation",
                    "elements": [
                        {"element_id": "E1", "text": "a UV-C emitter", "core": True, "order": 1},
                    ],
                    "relationships": [],
                },
            ],
            approved_at="2026-08-30T00:00:00+00:00",
        )
        return run_id

    def _blocking(self, pg, run_id, queries, version=1):
        return _gate_strategy_blocking(
            pg,
            run_id=run_id,
            payload={"queries": queries, "understanding_version": version},
        )

    def test_a_strategy_with_queries_is_clean(self, pg, approved_run):
        blocking = self._blocking(pg, approved_run, [_query("Q1"), _query("Q2")])
        assert blocking == []

    def test_no_queries_at_all_blocks_approval(self, pg, approved_run):
        """The one rule this stage has. A strategy with no queries would search
        for nothing and then report that nothing was found -- the single failure
        here that would look exactly like a genuine empty result."""
        blocking = self._blocking(pg, approved_run, [])
        assert [item["code"] for item in blocking] == ["NO_QUERIES"]

    def test_queries_that_are_only_whitespace_are_no_queries(self, pg, approved_run):
        blocking = self._blocking(
            pg, approved_run, [_query("Q1", text="   "), _query("Q2", text="")]
        )
        assert [item["code"] for item in blocking] == ["NO_QUERIES"]

    def test_one_non_empty_query_is_enough(self, pg, approved_run):
        blocking = self._blocking(
            pg, approved_run, [_query("Q1", text="  "), _query("Q2", text="graphene membrane")]
        )
        assert blocking == []

    def test_the_blocking_list_is_exactly_the_one_rule_this_stage_has(
        self, pg, approved_run
    ):
        """**Spec section 8.1 row 9: no other condition blocks.**

        The reviewer's on-screen list pairs one-to-one with `GATE_STRATEGY_RULES`,
        so a check with no rule beside it refuses an approval for a reason the
        screen never showed.
        """
        from graph.gates import GATE_STRATEGY_RULES

        every_code = {
            item["code"]
            for queries in ([], [_query("Q1", text=" ")], [_query("Q1"), _query("Q1")])
            for item in self._blocking(pg, approved_run, queries)
        }
        assert every_code == {"NO_QUERIES"}
        assert len(GATE_STRATEGY_RULES) == 1, "the screen and the server shrink together"


# The following completion-rule tests were REMOVED with the rules they covered.
# Each names the reason the rule has nothing left to check (spec sections 3.4,
# 3.5 and 4.2.2):
#
# * `test_an_uncovered_core_element_blocks_approval`,
#   `test_an_uncovered_non_core_element_does_not_block`,
#   `test_a_disabled_query_covers_nothing`,
#   `test_a_bare_element_id_is_not_a_coverage_claim`,
#   `test_an_element_the_understanding_does_not_contain_is_refused`,
#   `test_a_strategy_naming_no_understanding_version_is_refused`,
#   `test_a_strategy_naming_a_version_that_was_never_approved_is_stale` -- all
#   seven are about element-to-query coverage, which is gone from the public
#   shape. This is the significant loss and is worth stating plainly: the
#   coverage rule guaranteed every core element was searched for, so that "no
#   prior art found" could not rest on a search nobody performed for part of the
#   invention. Advanced Search makes no such guarantee and states no such
#   conclusion -- it shows the queries to a person and asks them to approve them.
#
# * `test_every_query_switched_off_blocks_approval` -- enable/disable switches
#   are gone; every stored query is enabled, so "all of them are off" is
#   unreachable.
#
# * `test_duplicate_query_ids_block_approval`, and the id half of
#   `test_an_empty_query_text_blocks_approval` -- query ids are minted on write
#   and never submitted, so a caller cannot produce a missing or duplicated one.
#
# * `test_too_many_queries_blocks_approval`, `test_exactly_the_limit_is_allowed`
#   -- a reviewer meets the limit while editing rather than at approval.


class TestPersistence:
    @pytest.fixture
    def client(self, real_db):
        import server

        return TestClient(server.app)

    @staticmethod
    def auth(email: str = OWNER) -> dict[str, str]:
        return {"Authorization": f"Bearer {issue_token(email)}"}

    @pytest.fixture
    def parked_run(self, pg, make_run):
        """A run with an approved understanding, parked at the strategy stage."""
        run_id = make_run(created_by=OWNER)
        approvals.approve_understanding_by_owner(
            pg,
            run_id=run_id,
            actor_email=OWNER,
            version=1,
            summary="A device that filters and sterilises water.",
            features=[
                {
                    "feature_id": "F1",
                    "feature_version": "1",
                    "feature_text": "filtration",
                    "elements": [
                        {"element_id": "E1", "text": "a membrane", "core": True, "order": 1},
                    ],
                    "relationships": [],
                }
            ],
            approved_at="2026-08-30T00:00:00+00:00",
        )
        token = lease.acquire(pg, run_id=run_id, owner_id=WORKER, ttl_seconds=TTL)
        store.transition_status(
            pg,
            run_id=run_id,
            owner_id=WORKER,
            fence_token=token,
            expected_from=(RunStatus.QUEUED,),
            to_status=RunStatus.RUNNING,
        )
        events.pause_run_for_review(
            pg,
            run_id=run_id,
            owner_id=WORKER,
            fence_token=token,
            gate=WorkflowGate.STRATEGY,
            review_payload={
                "gate": "STRATEGY",
                "summary": {"queries": 1, "enabled_queries": 1},
                "payload_url": f"/api/runs/{run_id}/gates/STRATEGY",
            },
            pause_payload={"gate": "STRATEGY", "status": "WAITING_FOR_REVIEW"},
            checkpoint_id="ckpt-strategy-1",
        )
        return run_id

    # --- the approved artifact -------------------------------------------

    def test_only_enabled_queries_are_offered_for_dispatch(self, pg, parked_run):
        """The whole mechanism behind "only enabled queries execute": the
        approved artifact carries both, and `enabled_queries` is what the gate
        node writes into `queries`."""
        approvals.approve_search_strategy_by_owner(
            pg,
            run_id=parked_run,
            actor_email=OWNER,
            version=1,
            understanding_version=1,
            queries=[
                _query("Q1", text="membrane filtration", covers=["F1.E1"]),
                _query("Q2", text="uv sterilisation", covers=["F1.E1"], enabled=False),
            ],
            approved_at="2026-08-30T00:00:00+00:00",
        )
        approved = approvals.require_approved_search_strategy(pg, run_id=parked_run, version=1)
        assert [q["query_id"] for q in approved["queries"]] == ["Q1", "Q2"]
        assert approved["enabled_queries"] == ["membrane filtration"]

    def test_a_disabled_query_is_kept_rather_than_dropped(self, pg, parked_run):
        """"We deliberately did not search for this" is part of what was approved
        and part of what the report has to be able to state."""
        approvals.approve_search_strategy_by_owner(
            pg,
            run_id=parked_run,
            actor_email=OWNER,
            version=1,
            understanding_version=1,
            queries=[_query("Q1", covers=["F1.E1"]), _query("Q2", covers=["F1.E1"], enabled=False)],
            approved_at="2026-08-30T00:00:00+00:00",
        )
        rows = pg.execute(
            "SELECT query_id, enabled FROM approved_strategy_queries "
            "WHERE run_id = %s AND version = 1 ORDER BY ord",
            (parked_run,),
        ).fetchall()
        assert rows == [("Q1", True), ("Q2", False)]

    def test_a_strategy_with_no_enabled_query_cannot_be_approved_even_by_the_writer(
        self, pg, parked_run
    ):
        """The single writer refuses it too, not only the gate rules: an approved
        strategy that dispatches nothing would report "no prior art" about a
        search that never ran."""
        with pytest.raises(UnapprovedArtifactError):
            approvals.approve_search_strategy_by_owner(
                pg,
                run_id=parked_run,
                actor_email=OWNER,
                version=1,
                understanding_version=1,
                queries=[_query("Q1", covers=["F1.E1"], enabled=False)],
                approved_at="2026-08-30T00:00:00+00:00",
            )

    def test_an_unapproved_strategy_is_not_fetched_rather_than_fetched_and_refused(
        self, pg, parked_run
    ):
        """`is_approved` is filtered in SQL. A row that could not be fetched and a
        row refused after being fetched are the same outcome only when nobody
        makes a mistake."""
        pg.execute(
            "INSERT INTO approved_search_strategies (run_id, version, understanding_version) "
            "VALUES (%s, 9, 1)",
            (parked_run,),
        )
        assert approvals.load_approved_search_strategy(pg, run_id=parked_run, version=9) is None
        assert approvals.latest_approved_search_strategy_version(pg, run_id=parked_run) is None
        with pytest.raises(UnapprovedArtifactError):
            approvals.require_approved_search_strategy(pg, run_id=parked_run, version=None)

    # --- the proposal, and what it cannot record --------------------------

    def test_a_proposal_is_stored_and_is_not_an_approval(self, pg, parked_run):
        """`search_strategy_proposals` has no column in which an approval could be
        recorded, so a row here cannot be mistaken for one however it is written."""
        strategy_store.record_proposal_if_absent(
            pg,
            run_id=parked_run,
            version=1,
            understanding_version=1,
            source=strategy_store.SOURCE_LLM,
            queries=[_query("Q1", covers=["F1.E1"])],
            created_by=OWNER,
        )
        columns = {
            row[0]
            for row in pg.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'search_strategy_proposals'"
            ).fetchall()
        }
        assert not (columns & {"is_approved", "approved_by", "approved_at"})
        assert approvals.latest_approved_search_strategy_version(pg, run_id=parked_run) is None

    def test_the_machine_proposal_survives_the_human_edit(self, pg, parked_run):
        """Both halves of the audit question: what the machine proposed, and what
        the human changed it to."""
        strategy_store.record_proposal_if_absent(
            pg, run_id=parked_run, version=1, understanding_version=1,
            source=strategy_store.SOURCE_LLM,
            queries=[_query("Q1", text="machine query", covers=["F1.E1"])],
            created_by=OWNER,
        )
        strategy_store.record_proposal_if_absent(
            pg, run_id=parked_run, version=1, understanding_version=1,
            source=strategy_store.SOURCE_USER_EDIT,
            queries=[_query("Q1", text="human query", covers=["F1.E1"])],
            created_by=OWNER,
        )
        latest = strategy_store.latest_proposal(pg, run_id=parked_run, actor_email=OWNER)
        assert latest["edited"] is True
        assert latest["queries"][0]["query_text"] == "human query"
        assert latest["machine_queries"][0]["query_text"] == "machine query"

    def test_a_replayed_proposal_write_is_a_no_op(self, pg, parked_run):
        """Recovery re-runs the proposing node. The same version arriving twice is
        an ordinary replay, not a second proposal."""
        first = strategy_store.record_proposal_if_absent(
            pg, run_id=parked_run, version=1, understanding_version=1,
            source=strategy_store.SOURCE_LLM, queries=[_query()], created_by=OWNER,
        )
        second = strategy_store.record_proposal_if_absent(
            pg, run_id=parked_run, version=1, understanding_version=1,
            source=strategy_store.SOURCE_LLM, queries=[_query("Q9")], created_by=OWNER,
        )
        assert first and second is None
        rows = pg.execute(
            "SELECT count(*) FROM search_strategy_proposals WHERE run_id = %s", (parked_run,)
        ).fetchone()
        assert rows[0] == 1

    def test_an_approved_version_is_never_reused_by_a_later_proposal(self, pg, parked_run):
        approvals.approve_search_strategy_by_owner(
            pg, run_id=parked_run, actor_email=OWNER, version=1, understanding_version=1,
            queries=[_query("Q1", covers=["F1.E1"])],
            approved_at="2026-08-30T00:00:00+00:00",
        )
        assert strategy_store.next_version(pg, run_id=parked_run) == 2

    def test_another_users_run_answers_not_found_rather_than_forbidden(self, pg, parked_run):
        from workflow.types import RunNotFoundError

        with pytest.raises(RunNotFoundError):
            strategy_store.latest_proposal(pg, run_id=parked_run, actor_email=OTHER)

    # --- regeneration is a durable work request ---------------------------

    def test_a_regeneration_request_is_recorded_and_does_no_work(self, pg, parked_run):
        recorded = store.request_strategy_regeneration(
            pg, run_id=parked_run, actor_email=OWNER, from_version=1, feedback="too narrow"
        )
        assert recorded["already_requested"] is False
        assert recorded["processed"] is False
        pending = store._system_pending_strategy_regeneration(pg, run_id=parked_run)
        assert pending == {"from_version": 1, "feedback": "too narrow"}

    def test_asking_twice_for_the_same_version_is_the_same_request(self, pg, parked_run):
        store.request_strategy_regeneration(
            pg, run_id=parked_run, actor_email=OWNER, from_version=1, feedback="a"
        )
        again = store.request_strategy_regeneration(
            pg, run_id=parked_run, actor_email=OWNER, from_version=1, feedback="b"
        )
        assert again["already_requested"] is True
        # The FIRST request stands. A second one with different words is the same
        # request retried, not a new instruction, and silently replacing the
        # feedback would change what the worker was asked without anybody saying so.
        assert again["feedback"] == "a"

    def test_claiming_a_regeneration_twice_carries_it_out_once(self, pg, parked_run):
        store.request_strategy_regeneration(
            pg, run_id=parked_run, actor_email=OWNER, from_version=1
        )
        assert store.claim_strategy_regeneration(pg, run_id=parked_run, from_version=1) is True
        assert store.claim_strategy_regeneration(pg, run_id=parked_run, from_version=1) is False

    def test_a_claimed_request_is_kept_as_evidence_that_a_person_asked(self, pg, parked_run):
        store.request_strategy_regeneration(
            pg, run_id=parked_run, actor_email=OWNER, from_version=1
        )
        store.claim_strategy_regeneration(pg, run_id=parked_run, from_version=1)
        rows = store.list_strategy_regenerations(pg, run_id=parked_run, actor_email=OWNER)
        assert len(rows) == 1 and rows[0]["processed"] is True

    def test_a_regeneration_must_name_the_version_it_was_asked_about(self, pg, parked_run):
        from workflow.types import IllegalStateError

        with pytest.raises(IllegalStateError):
            store.request_strategy_regeneration(
                pg, run_id=parked_run, actor_email=OWNER, from_version=0
            )

    # --- the HTTP surface -------------------------------------------------

    def test_the_stage_payload_is_a_plain_list_of_query_strings(
        self, client, pg, parked_run
    ):
        """Read from the checkpoint, which is patched here because no executor has
        run: what is being asserted is the payload's shape and its ownership
        check, not the checkpointer."""
        state = {
            "description": "a bottle",
            "approved_summary": "A device that filters and sterilises water.",
            "decomposition_version": 1,
            "approved_decomposition": {
                "version": 1,
                "features": [
                    {
                        "feature_id": "F1",
                        "feature_text": "filtration",
                        "elements": [{"element_id": "E1", "text": "a membrane", "core": True}],
                    }
                ],
            },
            "candidate_search_strategy": {
                "version": 1,
                "understanding_version": 1,
                "queries": [_query("Q1", covers=["F1.E1"])],
            },
        }
        with patch("workflow.gate_state.read_state", return_value=state):
            response = client.get(f"/api/runs/{parked_run}/gates/STRATEGY", headers=self.auth())
        assert response.status_code == 200
        body = response.json()
        # The flat business payload (spec section 4.2.2): one field, a list of
        # strings. Everything the reviewer can no longer see or set is absent --
        # coverage, the approved summary it was judged against, query ids, the
        # enabled flag and the rationale.
        assert body["queries"] == ["graphene oxide membrane"]
        for withdrawn in (
            "approved_summary",
            "elements",
            "candidate_search_strategy",
            "understanding_version",
            "proposal",
        ):
            assert withdrawn not in body, f"{withdrawn} is back in the payload"
        assert body["limits"]["max_queries"] >= 1

    def test_an_unreadable_checkpoint_is_not_rendered_as_an_empty_strategy(
        self, client, parked_run
    ):
        """The reviewer must be able to tell "the machine proposed no queries"
        from "we could not read what it proposed"."""
        with patch("workflow.gate_state.read_state", return_value=None):
            response = client.get(f"/api/runs/{parked_run}/gates/STRATEGY", headers=self.auth())
        assert response.status_code == 503
        assert response.json()["code"] == "CHECKPOINT_UNAVAILABLE"

    def test_another_user_gets_the_same_404_an_unknown_run_gets(self, client, parked_run):
        response = client.get(
            f"/api/runs/{parked_run}/gates/STRATEGY", headers=self.auth(OTHER)
        )
        assert response.status_code == 404
        assert response.json()["code"] == "RUN_NOT_FOUND"

    def test_an_invalid_strategy_is_refused_with_the_rules_it_broke(
        self, client, pg, parked_run
    ):
        """`409 GATE_BLOCKED` listing every condition, and **nothing written**.

        The invalid strategy is now an empty one: with coverage gone, a query
        that covers nothing is perfectly valid, so the only way to break this
        stage's single rule is to approve no queries at all.
        """
        response = client.post(
            f"/api/runs/{parked_run}/gates/STRATEGY/approve",
            headers=self.auth(),
            json={"idempotency_key": "k-blocked", "queries": []},
        )
        assert response.status_code == 409
        body = response.json()
        assert body["code"] == "GATE_BLOCKED"
        assert "NO_QUERIES" in {item["code"] for item in body["blocking"]}
        assert approvals.latest_approved_search_strategy_version(pg, run_id=parked_run) is None

    def test_an_approval_records_both_the_edit_and_the_approved_artifact(
        self, client, pg, parked_run
    ):
        response = client.post(
            f"/api/runs/{parked_run}/gates/STRATEGY/approve",
            headers=self.auth(),
            json={
                "idempotency_key": "k-approve",
                "queries": ["membrane filtration", "uv sterilisation"],
            },
        )
        assert response.status_code == 200, response.json()
        assert response.json() == {
            "strategy_version": 1,
            "queries_approved": 2,
            # Every stored query is enabled: the reviewer has no switch to turn
            # one off, so a strategy cannot carry a query that will not be run.
            "queries_enabled": 2,
            "status": "RESUMING",
        }
        approved = approvals.require_approved_search_strategy(pg, run_id=parked_run, version=1)
        assert approved["enabled_queries"] == ["membrane filtration", "uv sterilisation"]
        edit = strategy_store.latest_proposal(pg, run_id=parked_run, actor_email=OWNER)
        assert edit["source"] == strategy_store.SOURCE_USER_EDIT

    def test_an_identical_approval_replays_rather_than_approving_twice(
        self, client, pg, parked_run
    ):
        body = {
            "idempotency_key": "k-replay",
            "queries": ["membrane filtration", "uv sterilisation"],
        }
        first = client.post(
            f"/api/runs/{parked_run}/gates/STRATEGY/approve", headers=self.auth(), json=body
        )
        second = client.post(
            f"/api/runs/{parked_run}/gates/STRATEGY/approve", headers=self.auth(), json=body
        )
        assert first.status_code == 200 and second.status_code == 200
        assert first.json() == second.json()
        count = pg.execute(
            "SELECT count(*) FROM approved_search_strategies WHERE run_id = %s", (parked_run,)
        ).fetchone()[0]
        assert count == 1

    def test_an_approval_never_builds_or_calls_the_graph(self, client, parked_run):
        """No endpoint resumes the graph. The executor picks the run up on its
        next poll and issues `Command(resume=...)`."""
        with patch("graph.pipeline.build_graph_with_checkpointer") as build:
            response = client.post(
                f"/api/runs/{parked_run}/gates/STRATEGY/approve",
                headers=self.auth(),
                json={
                    "idempotency_key": "k-nograph",
                    "queries": ["membrane filtration", "uv sterilisation"],
                },
            )
        assert response.status_code == 200
        build.assert_not_called()

    def test_another_user_cannot_approve_a_strategy(self, client, pg, parked_run):
        response = client.post(
            f"/api/runs/{parked_run}/gates/STRATEGY/approve",
            headers=self.auth(OTHER),
            json={
                "idempotency_key": "k-intruder",
                "queries": ["membrane filtration", "uv sterilisation"],
            },
        )
        assert response.status_code == 404
        assert response.json()["code"] == "RUN_NOT_FOUND"
        assert approvals.latest_approved_search_strategy_version(pg, run_id=parked_run) is None
        # Ownership is checked BEFORE the idempotency record, so a caller who owns
        # nothing never writes a row keyed to somebody else's run.
        rows = pg.execute(
            "SELECT count(*) FROM idempotency_records WHERE resource_id = %s AND actor_email = %s",
            (parked_run, OTHER),
        ).fetchone()[0]
        assert rows == 0

    # The regeneration tests were REMOVED with the endpoint they covered
    # (`POST /api/runs/{id}/strategy/regenerate`). They asserted that asking for
    # fresh queries was recorded as a request rather than an approval, called no
    # model on the request thread, replayed on repeat, and was refused to another
    # user. Regeneration is gone from the product (spec section 10.2): editing
    # the queries replaces asking the model for a new version, so there is no
    # request to record. `strategy_regeneration_requests`, its migration and its
    # existing rows are all retained.

    def test_approving_the_wrong_stage_is_a_conflict_not_an_internal_error(
        self, client, pg, make_run
    ):
        """The gate precondition runs before the mutation, so approving a stage
        the run is not at is a `409`, not a unique-constraint violation surfacing
        as a 500."""
        run_id = make_run(created_by=OWNER)
        response = client.post(
            f"/api/runs/{run_id}/gates/STRATEGY/approve",
            headers=self.auth(),
            json={
                "idempotency_key": "k-wrong",
                "queries": ["membrane filtration", "uv sterilisation"],
            },
        )
        assert response.status_code == 409
        assert response.json()["code"] == "WRONG_GATE"

    # --- what the search is actually given --------------------------------

    def test_the_gate_hands_the_search_only_the_enabled_approved_queries(
        self, real_db, pg, parked_run
    ):
        """**The claim the whole stage makes.** `queries` is what
        `search_orchestrator` and `keyword_search` dispatch, and `gate_strategy_node`
        is the only thing that writes it on the verified branch.

        `interrupt()` and `emit_progress` are patched because both need a running
        graph; what is under test is the resume half -- what the node reads back
        and hands forward -- not LangGraph's pause. `real_db` undoes the in-memory
        `db.connect` patch, because the node opens its own connection the way the
        worker does.
        """
        from graph import gates

        approvals.approve_search_strategy_by_owner(
            pg,
            run_id=parked_run,
            actor_email=OWNER,
            version=1,
            understanding_version=1,
            queries=[
                _query("Q1", text="membrane filtration", covers=["F1.E1"]),
                _query("Q2", text="never dispatched", covers=["F1.E1"], enabled=False),
                _query("Q3", text="uv sterilisation", covers=["F1.E1"]),
            ],
            approved_at="2026-08-30T00:00:00+00:00",
        )
        with patch("graph.gates.interrupt"), patch("graph.gates.emit_progress"):
            written = gates.gate_strategy_node({"run_id": parked_run})

        assert written["queries"] == ["membrane filtration", "uv sterilisation"]
        assert "never dispatched" not in written["queries"]
        assert written["strategy_approved_version"] == 1

    def test_the_gate_refuses_to_hand_the_search_a_strategy_nobody_approved(
        self, real_db, pg, parked_run
    ):
        """No approved strategy means no queries -- and a raise, not an empty list.
        An empty list would search for nothing and then report that nothing was
        found."""
        from graph import gates

        with patch("graph.gates.interrupt"), patch("graph.gates.emit_progress"):
            with pytest.raises(UnapprovedArtifactError):
                gates.gate_strategy_node({"run_id": parked_run})

    def test_a_pending_regeneration_is_read_before_any_approved_artifact(
        self, real_db, pg, parked_run
    ):
        """A rejection is not an approval. The node returns `{}` and the router
        sends the graph back to the planning; reading an approval here would
        either fail or pick up a version approved earlier."""
        from graph import gates

        store.request_strategy_regeneration(
            pg, run_id=parked_run, actor_email=OWNER, from_version=1, feedback="wrong"
        )
        with patch("graph.gates.interrupt"), patch("graph.gates.emit_progress"):
            assert gates.gate_strategy_node({"run_id": parked_run}) == {}


def test_carrying_out_a_strategy_regeneration_does_not_raise_on_its_own_logging():
    """The same shape as the understanding stage's regeneration, and the same trap:
    the node logs the version it is re-planning, and a module with no `logger`
    would die with `NameError` *after* claiming the request -- leaving a request
    marked processed and no new strategy."""
    from graph import work_requests

    with patch.object(
        work_requests, "pending_strategy_regeneration",
        return_value={"from_version": 1, "feedback": "too narrow"},
    ), patch.object(
        work_requests.store, "claim_strategy_regeneration", return_value=True
    ), patch.object(work_requests, "db"):
        written = work_requests.regenerate_strategy_node({"run_id": "r1"})

    assert written == {"strategy_feedback": "too narrow", "strategy_version": 0}


def test_a_claimed_regeneration_re_run_by_a_recovery_regenerates_nothing():
    """At-least-once execution: a recovery re-runs the node, finds nothing left to
    claim, and returns cleanly rather than producing a second strategy."""
    from graph import work_requests

    with patch.object(work_requests, "pending_strategy_regeneration", return_value=None):
        assert work_requests.regenerate_strategy_node({"run_id": "r1"}) == {
            "strategy_feedback": ""
        }
