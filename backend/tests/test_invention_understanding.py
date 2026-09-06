"""The Invention Understanding stage: what a person may approve, and what they may not.

This stage is the first thing an inventor sees and the last point before any
metered provider call. Two properties dominate everything here:

1. **Nothing is searched for until a human approves an understanding.** The graph
   pauses before `search_orchestrator`, and a rejection routes *back* to the
   analysis rather than forward to the search.
2. **The inventor's own words are never rewritten.** The description is the source
   input; the approved summary is stored beside it, at its own version, and the
   machine's original proposal survives the human's edits so the two can be
   compared afterwards.

Real PostgreSQL for everything that asserts a constraint, a version allocation or
an ownership scope -- the in-memory double is a dict and can exhibit none of them.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from auth import issue_token
from workflow import approvals, events, lease, store
from workflow import understanding as understanding_store
from workflow.review import _gate_a_blocking
from workflow.types import RunStatus, UnapprovedArtifactError, WorkflowGate

OWNER = "testnovelty@gmail.com"
OTHER = "someone-else@example.com"
WORKER = "worker-1"
TTL = 60


def _feature(feature_id="F1", elements=None, relationships=None):
    return {
        "feature_id": feature_id,
        "feature_version": "1",
        "feature_text": "flow-triggered self-cleaning of a filtration membrane",
        "elements": elements
        if elements is not None
        else [{"element_id": "E1", "text": "a membrane", "core": True, "order": 1}],
        "relationships": relationships if relationships is not None else [],
    }


def _payload(summary="A device that filters and sterilises water.", features=None):
    # `is None`, not `or`: an explicitly EMPTY feature list is the thing several
    # of these tests are about, and `or` would silently substitute the default
    # and make the assertion pass for the wrong reason.
    return {
        "summary": summary,
        "decomposition": [_feature()] if features is None else features,
    }


# =========================================================================
# Completion rules -- pure, no database
# =========================================================================


def test_a_clean_understanding_has_no_blocking_conditions():
    assert _gate_a_blocking(_payload()) == []


def test_the_blocking_list_is_exactly_the_two_rules_this_stage_has():
    """**Spec section 8.1 row 9: no other condition blocks.**

    The reviewer's on-screen list pairs one-to-one and in order with
    `GATE_A_RULES`, so a check with no rule beside it refuses an approval for a
    reason the screen never showed. Asserting the whole set, rather than each
    surviving rule individually, is what makes re-adding a hidden check fail.
    """
    from graph.gates import GATE_A_RULES

    every_code = {
        item["code"]
        for payload in (
            _payload(summary="   "),
            _payload(features=[]),
            _payload(features=[_feature(elements=[])]),
            _payload(features=[
                _feature(elements=[{"element_id": "E1", "text": "  ", "core": True, "order": 1}])
            ]),
            _payload(features=[
                _feature(elements=[{"element_id": "E1", "text": "a membrane", "core": False, "order": 1}])
            ]),
        )
        for item in _gate_a_blocking(payload)
    }
    assert every_code == {"NO_SUMMARY", "NO_ELEMENTS"}
    assert len(GATE_A_RULES) == 2, "the screen and the server must shrink together"


def test_an_empty_summary_blocks_approval():
    """Rule 1. The summary is what the inventor confirmed the system understood.
    An empty one is not an interpretation anybody can correct."""
    codes = [item["code"] for item in _gate_a_blocking(_payload(summary="   "))]
    assert codes == ["NO_SUMMARY"]


def test_an_empty_summary_after_trimming_also_blocks():
    """Whitespace is not a summary. Section 3.5 says "after trimming"."""
    for blank in ("", " ", "\t", "\n  \n"):
        codes = [item["code"] for item in _gate_a_blocking(_payload(summary=blank))]
        assert "NO_SUMMARY" in codes, f"{blank!r} was accepted as a summary"


def test_zero_elements_blocks_approval():
    """Rule 2, in each of the shapes that can produce it."""
    for payload in (
        _payload(features=[]),
        _payload(features=[_feature(elements=[])]),
    ):
        codes = [item["code"] for item in _gate_a_blocking(payload)]
        assert "NO_ELEMENTS" in codes


def test_elements_that_are_only_whitespace_are_zero_elements():
    """A reviewer who blanked every element's text has approved no elements, and
    an approval of nothing must not read as an approval of something."""
    blanked = _payload(
        features=[_feature(elements=[
            {"element_id": "E1", "text": "  ", "core": True, "order": 1},
            {"element_id": "E2", "text": "", "core": True, "order": 2},
        ])]
    )
    assert "NO_ELEMENTS" in [item["code"] for item in _gate_a_blocking(blanked)]


def test_one_non_empty_element_is_enough():
    kept = _payload(
        features=[_feature(elements=[
            {"element_id": "E1", "text": "  ", "core": True, "order": 1},
            {"element_id": "E2", "text": "a graphene membrane", "core": True, "order": 2},
        ])]
    )
    assert _gate_a_blocking(kept) == []


# The following completion-rule tests were REMOVED with the rules they covered.
# Each is listed with the reason the rule has nothing left to check -- a removed
# test is a removed guarantee, and these are guarantees whose subject the product
# no longer has (spec sections 3.3, 3.5 and 4.2.3):
#
# * `test_no_capability_blocks_approval`,
#   `test_too_many_capabilities_blocks_approval`,
#   `test_exactly_the_capability_limit_is_allowed`,
#   `test_too_many_elements_in_one_capability_blocks_approval`,
#   `test_too_many_elements_in_total_blocks_approval`,
#   `test_the_capability_limit_is_derived_from_the_reviewed_matrix_constants` --
#   capability grouping is gone. There is one flat list of strings, so there are
#   no groups to count and no per-group limit; the total limit was derived from
#   MAX_CAPABILITIES x the matrix cell cap, and there is no matrix.
#
# * `test_no_core_element_anywhere_blocks_approval`,
#   `test_one_core_element_anywhere_is_enough` -- `core` is no longer something a
#   reviewer sets. Every approved element is written `core = TRUE`, which makes
#   NO_CORE_ELEMENT unreachable rather than deleted and hoped for. That is
#   asserted directly by `test_every_approved_element_is_written_core` below.
#
# * `test_a_requirement_id_shared_by_an_element_and_a_relationship_blocks_approval`
#   -- ids are minted on write and never submitted, so a caller cannot collide
#   them.
#
# * `test_a_relationship_naming_a_deleted_element_blocks_approval`,
#   `test_a_relationship_naming_only_present_elements_is_clean` -- relationships
#   are gone from the product and no row is written to
#   `decomposition_relationships`.


def test_a_proposal_with_no_summary_fails_rather_than_rendering_empty():
    import llm

    with patch.object(llm, "_llm_json", return_value={"features": [_feature()]}):
        with pytest.raises(llm.LLMResponseError) as caught:
            llm.propose_understanding("an invention")
    assert "summary" in str(caught.value).lower()


def test_a_proposal_with_no_features_fails():
    import llm

    with patch.object(llm, "_llm_json", return_value={"summary": "s", "features": []}):
        with pytest.raises(llm.LLMResponseError):
            llm.propose_understanding("an invention")


def test_the_inventor_context_reaches_the_prompt_and_is_never_called_evidence():
    """Problem and novelty statements steer the analysis. The prompt says so in
    as many words -- what the inventor believes is new is a claim this run exists
    to test, and a model told otherwise would be told to assume the conclusion."""
    import llm

    captured = {}

    def capture(prompt, **kwargs):
        captured["prompt"] = prompt
        return {"summary": "s", "features": [_feature()]}

    with patch.object(llm, "_llm_json", side_effect=capture):
        llm.propose_understanding(
            "a water filter",
            problem_solved="membranes foul and nobody notices",
            novelty_statement="automatic detection of fouling",
        )
    assert "membranes foul and nobody notices" in captured["prompt"]
    assert "automatic detection of fouling" in captured["prompt"]
    assert "a claim to be tested, not a fact" in captured["prompt"]


def test_regeneration_feedback_reaches_the_prompt_as_a_correction():
    import llm

    captured = {}

    def capture(prompt, **kwargs):
        captured["prompt"] = prompt
        return {"summary": "s", "features": [_feature()]}

    with patch.object(llm, "_llm_json", side_effect=capture):
        llm.propose_understanding("a water filter", feedback="you missed the UV stage")
    assert "you missed the UV stage" in captured["prompt"]
    assert "Do not repeat the previous one." in captured["prompt"]


def test_the_proposal_prompt_asks_for_compound_searchable_limitations():
    import llm

    captured = {}

    def capture(prompt, **kwargs):
        captured["prompt"] = prompt
        return {"summary": "s", "features": [_feature()]}

    with patch.object(llm, "_llm_json", side_effect=capture):
        llm.propose_understanding("a water filter")

    prompt = captured["prompt"]
    assert "Return exactly one feature, F1" in prompt
    assert "Aim for 4 to 7 limitations" in prompt
    assert "single generic component" in prompt
    assert "Merge related nouns with the function, arrangement, trigger, or condition" in prompt
    assert "relationships as an empty array" in prompt


def test_duplicate_machine_proposed_limitations_fail_instead_of_being_hidden():
    import llm

    duplicate = _feature(elements=[
        {"element_id": "E1", "text": "flow-triggered membrane cleaning", "core": True, "order": 1},
        {"element_id": "E2", "text": "  FLOW-TRIGGERED   MEMBRANE CLEANING  ", "core": True, "order": 2},
    ])
    with patch.object(llm, "_llm_json", return_value={"summary": "s", "features": [duplicate]}):
        with pytest.raises(llm.LLMResponseError, match="duplicate"):
            llm.propose_understanding("a water filter")


def test_separate_machine_proposed_relationships_fail_instead_of_being_flattened():
    import llm

    feature = _feature(relationships=[
        {"relationship_id": "R1", "text": "a sensor triggers cleaning", "order": 1}
    ])
    with patch.object(llm, "_llm_json", return_value={"summary": "s", "features": [feature]}):
        with pytest.raises(llm.LLMResponseError, match="separate relationships"):
            llm.propose_understanding("a water filter")


# `test_relationships_are_regenerated_against_the_current_elements_only` and
# `test_an_absent_relationships_array_is_not_read_as_an_empty_one` were REMOVED
# with `llm.regenerate_relationships`. They asserted that the regeneration prompt
# listed only the elements that exist now, and that an absent relationships array
# was not read as an empty one. Relationships are gone from the product: the
# understanding is a flat list of element strings, no row is ever written to
# `decomposition_relationships`, and no prompt asks for them.


# =========================================================================
# The graph: nothing is searched for before a human approves
# =========================================================================


def test_the_understanding_stage_sits_before_the_search_in_the_graph():
    """The approved artifacts are prepared before the shared search pipeline.

    The gate is an `interrupt()`, and no provider is contacted until a person has
    approved what the run is looking for. The path gained the SEARCH STRATEGY
    stage between this approval and the search, so what is asserted is that the
    understanding still precedes the search and never reaches it directly --
    `tests/test_search_strategy.py` asserts the new stage's own edges, and
    `test_no_provider_call_is_reachable_before_both_approvals` asserts that
    `search_orchestrator` has no other way in.
    """
    from graph.pipeline import build_graph

    edges = {(edge.source, edge.target) for edge in build_graph().get_graph().edges}
    assert ("decompose", "gate_a") in edges
    assert ("gate_a", "propose_strategy") in edges
    assert ("gate_strategy", "prepare_approved_search") in edges
    assert ("prepare_approved_search", "search_orchestrator") in edges
    assert ("gate_a", "search_orchestrator") not in edges
    assert ("decompose", "search_orchestrator") not in edges


def test_an_approved_understanding_leads_to_the_strategy_stage_and_nowhere_else():
    """Replaces `test_a_rejected_analysis_routes_back_to_the_analysis_not_on_to_the_search`.

    That test asserted the regeneration loop `gate_a -> regenerate_understanding
    -> decompose`. Regeneration was removed from the product (spec section 10.2):
    editing the summary and the elements replaces asking the model for a new
    version, so there is no rejection branch and `gate_a` is a plain edge with one
    destination.

    The guarantee that survives -- and the reason the loop existed -- is that an
    approved understanding leads to the SEARCH STRATEGY stage rather than to a
    provider, so there are still two approvals in front of any search.
    """
    from graph.pipeline import build_graph

    edges = {(edge.source, edge.target) for edge in build_graph().get_graph().edges}
    out_of_gate_a = {target for source, target in edges if source == "gate_a"}
    assert out_of_gate_a == {"propose_strategy"}
    assert "regenerate_understanding" not in set(build_graph().get_graph().nodes)


def test_a_read_failure_takes_the_branch_that_does_the_work():
    """A router cannot raise usefully, so a failed read must not silently skip a
    human's request. The regeneration node re-reads and fails loudly there."""
    from graph.work_requests import route_after_gate_a

    with patch(
        "graph.work_requests.pending_understanding_regeneration",
        side_effect=RuntimeError("database gone"),
    ):
        assert route_after_gate_a({"run_id": "r1"}) == "regenerate_understanding"


def test_each_search_mode_starts_with_its_own_understanding_step():
    """Advanced Search skips the Quick-only LLM/query expansion prelude."""
    from graph.pipeline import _route_from_start

    assert _route_from_start({"advanced_search": False}) == "understand"
    assert _route_from_start({"advanced_search": True}) == "decompose"


def test_approved_search_preparation_uses_no_unreviewed_machine_analysis():
    from graph.understanding import prepare_approved_search_node

    state = {
        "description": "original description",
        "claims": "original claims",
        "approved_summary": "approved summary",
        "approved_decomposition": {
            "features": [{
                "elements": [
                    {"text": "approved limitation one"},
                    {"text": "approved limitation two"},
                ]
            }]
        },
        "queries": ["approved query"],
    }
    prepared = prepare_approved_search_node(state)

    assert prepared["analysis"].queries == ["approved query"]
    assert prepared["analysis"].keywords == []
    assert prepared["analysis"].concepts == [
        "approved limitation one",
        "approved limitation two",
    ]
    assert prepared["invention_text"] == (
        "original description\n\noriginal claims\n\napproved summary\n\n"
        "approved limitation one\n\napproved limitation two"
    )


# =========================================================================
# Persistence, versioning and ownership -- real PostgreSQL
# =========================================================================


@pytest.mark.postgres
class TestPersistence:
    @pytest.fixture
    def client(self, real_db):
        import server

        return TestClient(server.app)

    @staticmethod
    def auth(email: str = OWNER) -> dict[str, str]:
        return {"Authorization": f"Bearer {issue_token(email)}"}

    @pytest.fixture
    def paused_run(self, pg, make_run):
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
        events.pause_run_for_review(
            pg,
            run_id=run_id,
            owner_id=WORKER,
            fence_token=token,
            gate=WorkflowGate.A,
            review_payload={"gate": "A", "summary": {}, "payload_url": f"/api/runs/{run_id}/gates/A"},
            pause_payload={"gate": "A", "status": "WAITING_FOR_REVIEW"},
            checkpoint_id="ckpt-1",
        )
        return run_id

    # --- the original description is never rewritten -----------------------

    def test_approving_a_summary_never_touches_the_original_description(self, pg, paused_run):
        """The source input survives every proposal, edit and approval.

        The whole point of the side-by-side screen is that the description and
        the summary are two different things. A stage that overwrote the first
        with the second would delete what the inventor actually wrote.
        """
        before = pg.execute(
            "SELECT description FROM verification_runs WHERE id = %s", (paused_run,)
        ).fetchone()[0]

        approvals.approve_understanding_by_owner(
            pg,
            run_id=paused_run,
            actor_email=OWNER,
            version=1,
            summary="An entirely different technical restatement.",
            features=[_feature()],
            approved_at="2026-08-30T10:00:00Z",
        )

        after = pg.execute(
            "SELECT description FROM verification_runs WHERE id = %s", (paused_run,)
        ).fetchone()[0]
        assert after == before

    def test_the_approved_summary_is_stored_beside_the_description_not_over_it(
        self, pg, paused_run
    ):
        approvals.approve_understanding_by_owner(
            pg,
            run_id=paused_run,
            actor_email=OWNER,
            version=1,
            summary="The approved restatement.",
            features=[_feature()],
            approved_at="2026-08-30T10:00:00Z",
        )
        approved = approvals.load_approved_understanding(pg, run_id=paused_run, version=1)
        assert approved["summary"] == "The approved restatement."
        assert approved["approved_by"] == OWNER

    # --- an approval names an approver -------------------------------------

    def test_an_empty_summary_cannot_be_approved_even_by_the_writer(self, pg, paused_run):
        with pytest.raises(UnapprovedArtifactError):
            approvals.approve_understanding_by_owner(
                pg,
                run_id=paused_run,
                actor_email=OWNER,
                version=1,
                summary="   ",
                features=[_feature()],
                approved_at="2026-08-30T10:00:00Z",
            )

    def test_an_unapproved_understanding_is_not_fetched_rather_than_fetched_and_refused(
        self, pg, paused_run
    ):
        """`is_approved` is filtered in SQL. A row that is not approved does not
        come back at all, so a missed comparison in Python cannot leak one."""
        pg.execute(
            """
            INSERT INTO approved_understandings (run_id, version, summary, is_approved)
            VALUES (%s, 1, 'not approved by anybody', FALSE)
            """,
            (paused_run,),
        )
        assert approvals.load_approved_understanding(pg, run_id=paused_run, version=1) is None
        with pytest.raises(UnapprovedArtifactError):
            approvals.require_approved_understanding(pg, run_id=paused_run, version=1)

    # --- proposals are versioned and never overwritten ----------------------

    def test_a_proposal_is_stored_and_is_not_an_approval(self, pg, paused_run):
        understanding_store.record_proposal(
            pg,
            run_id=paused_run,
            version=1,
            source=understanding_store.SOURCE_LLM,
            summary="machine summary",
            features=[_feature()],
            created_by=OWNER,
        )
        latest = understanding_store.latest_proposal(pg, run_id=paused_run, actor_email=OWNER)
        assert latest["summary"] == "machine summary"
        assert latest["source"] == understanding_store.SOURCE_LLM
        # It is not, and cannot become, an approved artifact.
        assert approvals.latest_approved_understanding_version(pg, run_id=paused_run) is None

    def test_the_machine_proposal_survives_the_human_edit(self, pg, paused_run):
        """Both halves of the audit question. A stage that kept only the human's
        version could not show that the human changed anything."""
        understanding_store.record_proposal(
            pg, run_id=paused_run, version=1, source=understanding_store.SOURCE_LLM,
            summary="what the machine said", features=[_feature()], created_by=OWNER,
        )
        understanding_store.record_proposal(
            pg, run_id=paused_run, version=1, source=understanding_store.SOURCE_USER_EDIT,
            summary="what the human approved", features=[_feature()], created_by=OWNER,
        )
        latest = understanding_store.latest_proposal(pg, run_id=paused_run, actor_email=OWNER)
        assert latest["summary"] == "what the human approved"
        assert latest["machine_summary"] == "what the machine said"
        assert latest["edited"] is True

    def test_a_regeneration_takes_a_new_version_and_leaves_the_old_one_intact(
        self, pg, paused_run
    ):
        understanding_store.record_proposal(
            pg, run_id=paused_run, version=1, source=understanding_store.SOURCE_LLM,
            summary="first attempt", features=[_feature()], created_by=OWNER,
        )
        second = understanding_store.next_version(pg, run_id=paused_run)
        assert second == 2
        understanding_store.record_proposal(
            pg, run_id=paused_run, version=second, source=understanding_store.SOURCE_LLM,
            summary="second attempt", features=[_feature()], created_by=OWNER,
            feedback="you missed the UV stage",
        )

        history = understanding_store.proposal_history(pg, run_id=paused_run, actor_email=OWNER)
        summaries = {(item["version"], item["summary"]) for item in history}
        assert (1, "first attempt") in summaries, "the rejected version was not preserved"
        assert (2, "second attempt") in summaries
        assert any(item["feedback"] == "you missed the UV stage" for item in history)

    def test_an_approved_version_is_never_reused_by_a_later_proposal(self, pg, paused_run):
        approvals.approve_understanding_by_owner(
            pg, run_id=paused_run, actor_email=OWNER, version=1,
            summary="approved", features=[_feature()], approved_at="2026-08-30T10:00:00Z",
        )
        assert understanding_store.next_version(pg, run_id=paused_run) == 2

    def test_a_replayed_proposal_write_is_a_no_op(self, pg, paused_run):
        """Recovery re-runs the proposing node. The same version arriving twice is
        an ordinary replay, not a second proposal."""
        first = understanding_store.record_proposal_if_absent(
            pg, run_id=paused_run, version=1, source=understanding_store.SOURCE_LLM,
            summary="s", features=[_feature()], created_by=OWNER,
        )
        again = understanding_store.record_proposal_if_absent(
            pg, run_id=paused_run, version=1, source=understanding_store.SOURCE_LLM,
            summary="s", features=[_feature()], created_by=OWNER,
        )
        assert first is not None and again is None
        count = pg.execute(
            "SELECT count(*) FROM understanding_proposals WHERE run_id = %s", (paused_run,)
        ).fetchone()[0]
        assert count == 1

    # --- ownership ---------------------------------------------------------

    def test_another_users_run_answers_not_found_rather_than_forbidden(self, pg, paused_run):
        from workflow.types import RunNotFoundError

        with pytest.raises(RunNotFoundError):
            understanding_store.latest_proposal(pg, run_id=paused_run, actor_email=OTHER)

    # --- the regeneration request ------------------------------------------

    def test_a_regeneration_request_is_recorded_and_does_no_work(self, pg, paused_run):
        requested = store.request_understanding_regeneration(
            pg, run_id=paused_run, actor_email=OWNER, from_version=1, feedback="wrong"
        )
        assert requested["already_requested"] is False
        assert requested["processed"] is False
        # No proposal was produced by asking for one.
        assert understanding_store.latest_proposal(pg, run_id=paused_run, actor_email=OWNER) is None

    def test_asking_twice_for_the_same_version_is_the_same_request(self, pg, paused_run):
        store.request_understanding_regeneration(
            pg, run_id=paused_run, actor_email=OWNER, from_version=1, feedback="wrong"
        )
        again = store.request_understanding_regeneration(
            pg, run_id=paused_run, actor_email=OWNER, from_version=1, feedback="wrong"
        )
        assert again["already_requested"] is True
        count = pg.execute(
            "SELECT count(*) FROM understanding_regeneration_requests WHERE run_id = %s",
            (paused_run,),
        ).fetchone()[0]
        assert count == 1

    def test_claiming_a_regeneration_twice_carries_it_out_once(self, pg, paused_run):
        """What stops at-least-once execution producing two re-analyses for one
        request. A recovery that re-runs the node finds nothing left to claim."""
        store.request_understanding_regeneration(
            pg, run_id=paused_run, actor_email=OWNER, from_version=1
        )
        assert store.claim_understanding_regeneration(pg, run_id=paused_run, from_version=1) is True
        assert store.claim_understanding_regeneration(pg, run_id=paused_run, from_version=1) is False

    def test_a_claimed_request_is_kept_as_evidence_that_a_person_asked(self, pg, paused_run):
        store.request_understanding_regeneration(
            pg, run_id=paused_run, actor_email=OWNER, from_version=1, feedback="wrong"
        )
        store.claim_understanding_regeneration(pg, run_id=paused_run, from_version=1)
        rows = store.list_understanding_regenerations(pg, run_id=paused_run, actor_email=OWNER)
        assert len(rows) == 1
        assert rows[0]["processed"] is True
        assert rows[0]["feedback"] == "wrong"

    # --- the HTTP surface ---------------------------------------------------

    def test_the_stage_payload_is_the_description_summary_and_flat_elements(
        self, client, pg, paused_run
    ):
        understanding_store.record_proposal(
            pg, run_id=paused_run, version=1, source=understanding_store.SOURCE_LLM,
            summary="machine summary", features=[_feature()], created_by=OWNER,
        )
        with patch(
            "workflow.gate_state.read_state",
            return_value={"description": "the inventor's own words",
                          "candidate_decomposition": {"summary": "machine summary",
                                                      "features": [_feature()], "version": 1}},
        ):
            response = client.get(f"/api/runs/{paused_run}/gates/A", headers=self.auth())
        assert response.status_code == 200, response.text
        body = response.json()
        # Read from the RUN ROW, not from graph state: the row survives a
        # checkpoint compaction and is the record of what was submitted. It is
        # never rewritten -- not by a proposal, not by an edit, not by an
        # approval -- and the approval endpoint does not accept it.
        assert body["original_description"] == pg.execute(
            "SELECT description FROM verification_runs WHERE id = %s", (paused_run,)
        ).fetchone()[0]
        # The flat business payload (spec section 4.2.1): three fields, and
        # nothing that revives a removed concept.
        assert body["technical_summary"] == "machine summary"
        assert body["technical_elements"] == ["a membrane"]
        assert "candidate_decomposition" not in body
        assert "proposal" not in body

    def test_an_unreadable_checkpoint_is_not_rendered_as_an_empty_analysis(
        self, client, paused_run
    ):
        with patch("workflow.gate_state.read_state", return_value=None):
            response = client.get(f"/api/runs/{paused_run}/gates/A", headers=self.auth())
        assert response.status_code == 503
        assert response.json()["code"] == "CHECKPOINT_UNAVAILABLE"

    def test_an_invalid_understanding_is_refused_with_the_rules_it_broke(
        self, client, paused_run
    ):
        response = client.post(
            f"/api/runs/{paused_run}/gates/A/approve",
            json={
                "idempotency_key": "k1",
                "technical_summary": "",
                "technical_elements": ["a membrane"],
            },
            headers=self.auth(),
        )
        assert response.status_code == 409, response.text
        assert "NO_SUMMARY" in response.text

    def test_an_approval_records_both_the_edit_and_the_approved_artifact(
        self, client, pg, paused_run
    ):
        response = client.post(
            f"/api/runs/{paused_run}/gates/A/approve",
            json={
                "idempotency_key": "k1",
                "technical_summary": "the approved restatement",
                "technical_elements": ["a membrane"],
            },
            headers=self.auth(),
        )
        assert response.status_code == 200, response.text
        assert approvals.load_approved_understanding(pg, run_id=paused_run, version=1)[
            "summary"
        ] == "the approved restatement"
        history = understanding_store.proposal_history(pg, run_id=paused_run, actor_email=OWNER)
        assert any(item["source"] == understanding_store.SOURCE_USER_EDIT for item in history)

    def test_the_review_state_survives_a_refresh(self, client, pg, paused_run):
        """Nothing about the stage lives in the browser. A second GET after a
        simulated reload returns the same artifact, read from the database."""
        understanding_store.record_proposal(
            pg, run_id=paused_run, version=1, source=understanding_store.SOURCE_LLM,
            summary="machine summary", features=[_feature()], created_by=OWNER,
        )
        state = {
            "description": "the inventor's own words",
            "candidate_decomposition": {
                "summary": "machine summary", "features": [_feature()], "version": 1
            },
        }
        with patch("workflow.gate_state.read_state", return_value=state):
            first = client.get(f"/api/runs/{paused_run}/gates/A", headers=self.auth()).json()
            second = client.get(f"/api/runs/{paused_run}/gates/A", headers=self.auth()).json()
        # The business payload is what has to survive a refresh -- it is what
        # the reviewer is editing.
        for field in ("original_description", "technical_summary", "technical_elements"):
            assert first[field] == second[field]

    # The three regeneration tests were REMOVED with the endpoint they covered
    # (`POST /api/runs/{id}/understanding/regenerate`). They asserted that asking
    # for a re-analysis was recorded as a request and called no model on the
    # request thread, that repeating it replayed, and that another user could not
    # ask. Regeneration is gone from the product (spec section 10.2): editing the
    # summary and the elements replaces asking the model for a new version, so
    # there is no request to record. The `*_regeneration_requests` table, its
    # migration and its existing rows are all retained.

    # --- historical runs stay readable --------------------------------------

    def test_a_run_admitted_before_this_change_is_still_readable(self, pg, make_run):
        """The two Stage 1 columns are nullable and additive. A NULL means "this
        run predates the field", which a reader must be able to tell from an
        inventor leaving it blank."""
        run_id = make_run(created_by=OWNER)
        pg.execute(
            """
            UPDATE verification_runs
               SET problem_solved = NULL, inventor_novelty_statement = NULL
             WHERE id = %s
            """,
            (run_id,),
        )
        run = store.get_run(pg, run_id=run_id, actor_email=OWNER)
        assert run["problem_solved"] is None
        assert run["inventor_novelty_statement"] is None

    def test_a_blank_answer_is_distinguishable_from_an_absent_one(self, pg, make_run):
        run_id = make_run(created_by=OWNER)
        store.set_submission_context(
            pg, run_id=run_id, problem_solved="", inventor_novelty_statement=""
        )
        run = store.get_run(pg, run_id=run_id, actor_email=OWNER)
        assert run["problem_solved"] == ""
        assert run["inventor_novelty_statement"] == ""


# =========================================================================
# The gate node never approves anything
# =========================================================================


def test_the_gate_node_reads_a_pending_rejection_rather_than_the_resume_signal():
    """A resume value travels through an HTTP request, an event row and a
    checkpoint. If it carried the decision, forging it at any of those three
    points would be approving -- so the gate re-reads the database instead."""
    import inspect

    from graph import gates

    source = inspect.getsource(gates.gate_a_node)
    assert "pending_understanding_regeneration" in source
    # The interrupt's return value is discarded, never bound to a name.
    assert "= interrupt(" not in source


def test_carrying_out_a_regeneration_does_not_raise_on_its_own_logging():
    """`regenerate_understanding_node` logs the version it is re-analysing, and
    `graph/work_requests.py` had no `logger` at all -- so the first regeneration a
    worker actually carried out would have died with `NameError` *after* claiming
    the request, leaving a request marked processed and no new proposal.

    Latent because nothing drove the node past the claim: the router and the claim
    are both covered, and neither reaches the log line. This drives it.
    """
    from graph import work_requests

    with patch.object(
        work_requests, "pending_understanding_regeneration",
        return_value={"from_version": 1, "feedback": "wrong"},
    ), patch.object(
        work_requests.store, "claim_understanding_regeneration", return_value=True
    ), patch.object(work_requests, "db"):
        written = work_requests.regenerate_understanding_node({"run_id": "r1"})

    assert written == {"understanding_feedback": "wrong", "understanding_version": 0}
