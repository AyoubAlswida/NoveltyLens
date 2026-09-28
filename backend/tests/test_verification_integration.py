"""The verifier inside the graph, driven by explicitly approved fixtures.

**What this gate can and cannot demonstrate.** A *harness-driven* matrix run
completes here, end to end, over artifacts `tests/harness.py` approved by name.
A *production* run cannot: Workflow Gates A and B do not exist until
Implementation Gate D, and there is no auto-approval standing in for them. That
limit is asserted in `test_approval_boundary.py` rather than worked around here.

**The existing pipeline must be untouched.** Several tests below exist only to
prove a search-only run traverses exactly the nodes it did before this gate and
emits exactly the phases it did before. The SSE contract is live production
behaviour a browser depends on; adding a branch beside it is the whole job, and
adding one *through* it would be a regression nothing else would catch.

**No outbound call is made.** `_llm_chat` and `SOURCES` are stubbed here;
embeddings, Qdrant and Postgres by `conftest.py`.
"""

import json
import re
from pathlib import Path
from unittest.mock import patch

import psycopg
import pytest

from evidence.eligibility import ExclusionState
from evidence.passages import build_passages
from graph import evidence_nodes
from graph import verification_nodes
from graph.events import ADVANCED_SEARCH_PHASES, PHASES, QUICK_SEARCH_PHASES
from graph.pipeline import run_graph_pipeline
from graph.verification_nodes import (
    MATRIX_CELL_CAP,
    MATRIX_CONCURRENCY,
    RETRIEVAL_FLOOR,
    verify_features_node,
)
from models import (
    EnrichmentResult,
    EnrichmentStatus,
    PatentRecord,
    ProviderClaim,
    SectionText,
    TextVersionSource,
)
from run_mock import _mock_verification
from verification.prompt import PROMPT_MARKER as VERIFIER_MARKER
from workflow.types import UnapprovedArtifactError

from tests.test_pipeline_contract import (  # the existing, unchanged doubles
    CLAIMS,
    DESCRIPTION,
    FAKE_SOURCES,
    RESULT_KEYS,
    _fake_llm_chat,
)

ACTOR = "testnovelty@gmail.com"

CLAIM_TEXT = (
    "A waste heat recovery system comprising a heat exchanger disposed within an "
    "exhaust duct, and a controller configured to vary a coolant flow rate."
)

APPROVED_FEATURE = {
    "feature_id": "F1",
    "feature_version": "1",
    "feature_text": "closed-loop control of exhaust heat recovery",
    "elements": [
        {"element_id": "E1", "text": "a heat exchanger within the exhaust duct", "core": True, "order": 1},
        {"element_id": "E2", "text": "a controller varying coolant flow rate", "core": True, "order": 2},
    ],
    "relationships": [
        {"relationship_id": "R1", "text": "the controller varies flow through the exchanger", "order": 1},
    ],
}

SECOND_FEATURE = {
    **APPROVED_FEATURE,
    "feature_id": "F2",
    "feature_text": "temperature sensing upstream of the exchanger",
}


def _sha(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _enrichment(publication="EP4778509", *, claim_text=CLAIM_TEXT) -> EnrichmentResult:
    section = SectionText(
        section="CLAIMS",
        text=claim_text,
        text_sha256=_sha(claim_text),
        language="en",
        claims=(ProviderClaim(number=1, text=claim_text, char_start=0, char_end=len(claim_text)),),
    )
    return EnrichmentResult(
        requested_publication=f"{publication}A1",
        normalized_publication=publication,
        served_publication=publication,
        source_document_id=f"epo:{publication}",
        status=EnrichmentStatus.TEXT,
        sections=(section,),
        sections_available=("CLAIMS",),
        sections_retrieved=("CLAIMS",),
        language_attempted="en",
        text_version_original="20190601",
        text_version_normalized="2019-06-01",
        text_version_source=TextVersionSource.PROVIDER_PUBLICATION_DATE,
        retrieved_at="2026-08-28T12:00:00+00:00",
    )


def _approved_state(*, features=(APPROVED_FEATURE,), publications=("EP4778509",)):
    """A state exactly as it looks after Workflow Gates A and B have completed.

    Built here rather than through `tests/harness.py` because these tests do not
    touch PostgreSQL -- but built to the same shape the harness returns, and the
    postgres-marked `test_approval_boundary.py` proves the harness produces it.
    """
    enrichment = {pub: {"result": _enrichment(pub)} for pub in publications}
    return {
        "run_id": "11111111-1111-1111-1111-111111111111",
        "advanced_search": True,
        "approved_decomposition": {
            "decomposition_id": "dec-1",
            "version": 1,
            "approved_by": "testnovelty@gmail.com",
            "approved_at": "2026-08-28T10:00:00+00:00",
            "features": list(features),
        },
        "decomposition_version": 1,
        "approved_candidate_set": [
            {"publication_number": f"{pub}A1", "normalized_number": pub}
            for pub in publications
        ],
        "candidate_set_version": 1,
        "enrichment": enrichment,
        "passages_by_publication": {
            pub: build_passages(entry["result"]) for pub, entry in enrichment.items()
        },
    }


def _verify(state, llm_double=None):
    """Run the matrix node with `emit_progress` stubbed (no graph context)."""
    double = llm_double or _fake_llm_chat
    # `verification/verify.py` imports `_llm_chat` by name, so patching
    # `llm._llm_chat` alone would leave the verifier calling the real gateway --
    # which is exactly how a "stubbed" test spends quota. Both names, always.
    with patch("llm._llm_chat", side_effect=double), \
         patch("verification.verify._llm_chat", side_effect=double), \
         patch("graph.verification_nodes.emit_progress"):
        return verify_features_node(state)


# --- the existing pipeline is untouched ------------------------------------


@pytest.fixture
def deterministic():
    with patch("llm._llm_chat", side_effect=_fake_llm_chat), \
         patch("search.search.SOURCES", FAKE_SOURCES):
        yield


def test_a_search_only_run_emits_exactly_the_phases_it_did_before(deterministic):
    """**The regression this gate could most easily cause.** A verification phase
    leaking into a search-only stream would stall the frontend checklist on a
    step that never completes."""
    events = list(run_graph_pipeline(DESCRIPTION, CLAIMS))
    phases = {event["phase"] for event in events if event["type"] == "progress"}

    verification_phases = {
        "decompose", "normalize", "screen_dates", "enrich_epo",
        "build_passages", "confirm_eligibility", "rank_candidates", "verify",
    }
    assert not (phases & verification_phases)
    assert {"understand", "queries", "search", "enrich", "validate", "compare", "analyze"} <= phases


def test_a_search_only_run_returns_exactly_the_result_keys_it_did_before(deterministic):
    result = list(run_graph_pipeline(DESCRIPTION, CLAIMS))[-1]
    assert result["type"] == "result"
    assert result["data"].keys() == RESULT_KEYS


def test_advanced_search_is_absent_by_default(deterministic):
    """A run that does not ask for verification does not get it. The branch is
    opt-in at the state level, not opt-out."""
    events = list(run_graph_pipeline(DESCRIPTION, CLAIMS))
    assert not any(event.get("phase") == "decompose" for event in events)


def test_every_advanced_search_phase_is_in_the_shared_vocabulary():
    """`emit_progress` raises on an unknown phase, so a node emitting one would
    fail the run rather than stalling a checklist. This asserts the tuple itself
    covers the branch.

    The list shrank with the topology: `normalize`, `screen_dates`, `enrich_epo`,
    `build_passages`, `confirm_eligibility`, `rank_candidates` and `verify` named
    evidence-layer nodes that were removed, so keeping them here would assert a
    vocabulary for work the product can no longer do.
    """
    for phase in ("decompose", "strategy", "gate_a", "gate_strategy"):
        assert phase in PHASES


def test_the_withdrawn_phases_are_gone_from_the_vocabulary():
    """The other direction: a phase whose node was removed must not linger.

    `emit_progress` validates against `PHASES`, so a name left here would stay
    emittable -- and the frontend, which mirrors this tuple, would render a
    checklist step for work nothing performs. Unlike `semantic_search`/`fuse`,
    whose nodes are disabled but coming back, these are withdrawn for good.
    """
    withdrawn = {
        "normalize", "screen_dates", "enrich_epo", "build_passages",
        "confirm_eligibility", "rank_candidates", "verify",
        "gate_b", "gate_c", "draft_report", "gate_d",
    }
    assert not (withdrawn & set(PHASES))


def test_the_backend_and_frontend_phase_lists_agree():
    """The one direction nothing else catches: a phase the backend emits and the
    frontend has never heard of renders as nothing at all."""
    from pathlib import Path

    report = (
        Path(__file__).resolve().parent.parent.parent
        / "frontend" / "src" / "app" / "models" / "report.ts"
    ).read_text(encoding="utf-8")
    block = report.split("export const PHASE_ORDER = [", 1)[1].split("] as const;", 1)[0]
    frontend = set(re.findall(r"'([a-z_]+)'", block))

    # The backend's tuple is wider by exactly two: `semantic_search` and `fuse`
    # are phases whose NODES ARE DISABLED in `graph/pipeline.py`, so the backend
    # never emits them and the frontend omits them rather than rendering steps
    # that never complete.
    #
    # `filter` is deliberately NOT in that set. Its node is live --
    # `graph/searching.py`'s `keyword_search_node` emits it on every search run --
    # and it was missing from the frontend list while the backend emitted it, so
    # the checklist skipped a step the run genuinely performed. Listing a phase
    # the backend cannot emit stalls the checklist; omitting one it does emit
    # hides real work. Both directions are wrong, and this assertion checks both.
    disabled = {"semantic_search", "fuse"}
    assert set(PHASES) - disabled == frontend


def test_each_frontend_checklist_matches_its_backend_route():
    """The two graph branches need two chronological checklists."""
    from pathlib import Path

    report = (
        Path(__file__).resolve().parent.parent.parent
        / "frontend" / "src" / "app" / "models" / "report.ts"
    ).read_text(encoding="utf-8")

    advanced_block = report.split(
        "export const ADVANCED_SEARCH_PHASE_ORDER: readonly Phase[] = [", 1
    )[1].split("];", 1)[0]
    quick_block = report.split(
        "export const QUICK_SEARCH_PHASE_ORDER: readonly Phase[] = [", 1
    )[1].split("];", 1)[0]

    def phases(block: str) -> tuple[str, ...]:
        return tuple(re.findall(r"'([a-z_]+)'", block))

    assert phases(quick_block) == QUICK_SEARCH_PHASES
    assert phases(advanced_block) == ADVANCED_SEARCH_PHASES


# --- the nodes are thin wrappers -------------------------------------------


def test_build_passages_node_equals_the_pure_function():
    """A node that contained logic of its own would have behaviour reachable only
    through a compiled graph."""
    state = _approved_state()
    with patch("graph.evidence_nodes.emit_progress"):
        produced = evidence_nodes.build_passages_node(
            {"enrichment": state["enrichment"], "candidates": []}
        )
    expected = build_passages(state["enrichment"]["EP4778509"]["result"])
    assert produced["passages_by_publication"]["EP4778509"] == expected


def test_normalize_publications_node_excludes_rather_than_guesses():
    """A publication number quietly corrected into a different real document
    would attach that document's text to this candidate."""
    with patch("graph.evidence_nodes.emit_progress"):
        result = evidence_nodes.normalize_publications_node(
            {
                "candidates": [
                    PatentRecord(id="EP4778509A1", title="a", source="Lens.org"),
                    PatentRecord(id="not-a-number", title="b", source="Lens.org"),
                ]
            }
        )
    assert result["normalized_publications"] == {"EP4778509A1": "EP4778509"}
    assert [c.id for c in result["candidates"]] == ["EP4778509A1"]
    assert len(result["publication_exclusions"]) == 1
    assert result["publication_exclusions"][0]["state"] == str(
        ExclusionState.EXCLUDED_INELIGIBLE
    )


def test_a_no_text_enrichment_becomes_an_exclusion_and_never_a_cell():
    """The scope rule: `EXCLUDED_*` is a property of a publication."""
    no_text = EnrichmentResult(
        requested_publication="US1234567B2",
        normalized_publication="US1234567",
        served_publication="US1234567",
        source_document_id="epo:US1234567",
        status=EnrichmentStatus.NO_TEXT,
        sections=(),
        sections_available=(),
        sections_retrieved=(),
        language_attempted="en",
        text_version_original="",
        text_version_normalized="",
        text_version_source=TextVersionSource.NONE,
        retrieved_at="2026-08-28T12:00:00+00:00",
    )
    from search.enrichment import EnrichmentOutcome

    record = PatentRecord(id="US1234567B2", title="a", source="Lens.org")
    with patch("graph.evidence_nodes.emit_progress"), patch(
        "graph.evidence_nodes.enrich_candidates",
        return_value=[EnrichmentOutcome(record=record, result=no_text)],
    ):
        result = evidence_nodes.enrich_epo_node({"candidates": [record]})

    assert result["candidates"] == []
    assert result["enrichment"] == {}
    assert result["publication_exclusions"][0]["state"] == str(
        ExclusionState.EXCLUDED_NO_FULL_TEXT
    )


def test_a_provider_failure_propagates_rather_than_becoming_an_exclusion():
    """Gate B's rule, carried into the graph unchanged: a failed request is not
    an absence, and converting one would understate coverage using the
    provider's own downtime."""
    from search.epo_fulltext import EpoEnrichmentFailed

    record = PatentRecord(id="EP4778509A1", title="a", source="Lens.org")
    with patch("graph.evidence_nodes.emit_progress"), patch(
        "graph.evidence_nodes.enrich_candidates",
        side_effect=EpoEnrichmentFailed("EPO OPS returned HTTP 503"),
    ):
        with pytest.raises(EpoEnrichmentFailed):
            evidence_nodes.enrich_epo_node({"candidates": [record]})


def test_rank_and_reduce_caps_the_set_a_human_will_curate():
    records = [
        PatentRecord(id=f"EP{4000000 + i}A1", title="a", source="Lens.org")
        for i in range(evidence_nodes.MAX_APPROVED_CANDIDATES + 5)
    ]
    with patch("graph.evidence_nodes.emit_progress"):
        result = evidence_nodes.rank_and_reduce_node(
            {
                "candidates": records,
                "normalized_publications": {r.id: r.id[:-2] for r in records},
            }
        )
    assert len(result["ranked_candidates"]) == evidence_nodes.MAX_APPROVED_CANDIDATES


def test_rank_and_reduce_produces_a_ranked_list_and_never_an_approved_set():
    """The field it writes is machine output. Nothing here can write the other
    one -- `workflow/approvals.py` is the only module that can."""
    record = PatentRecord(id="EP4778509A1", title="a", source="Lens.org")
    with patch("graph.evidence_nodes.emit_progress"):
        result = evidence_nodes.rank_and_reduce_node(
            {"candidates": [record], "normalized_publications": {record.id: "EP4778509"}}
        )
    assert result["ranked_candidates"] == ["EP4778509"]
    assert "approved_candidate_set" not in result


# --- the matrix ------------------------------------------------------------


def test_the_matrix_is_exactly_features_times_publications():
    result = _verify(_approved_state(
        features=(APPROVED_FEATURE, SECOND_FEATURE),
        publications=("EP4778509", "EP3010333"),
    ))
    assert len(result["cells"]) == 4
    assert {(c["feature_id"], c["publication_number"]) for c in result["cells"]} == {
        ("F1", "EP4778509"), ("F1", "EP3010333"),
        ("F2", "EP4778509"), ("F2", "EP3010333"),
    }


def test_every_cell_records_the_versions_it_was_built_from():
    """A cell that cannot name its decomposition and candidate-set versions
    cannot be bound to a review, and a review of one version would attach
    cleanly to another."""
    for cell in _verify(_approved_state())["cells"]:
        assert cell["decomposition_version"] == 1
        assert cell["candidate_set_version"] == 1
        assert cell["cell_id"].endswith(":F1:EP4778509")


def test_a_matrix_over_the_reviewed_cap_raises_rather_than_truncating():
    """A silently shortened matrix presents partial coverage as complete."""
    features = tuple(
        {**APPROVED_FEATURE, "feature_id": f"F{i}"} for i in range(MATRIX_CELL_CAP + 1)
    )
    with pytest.raises(ValueError) as caught:
        _verify(_approved_state(features=features))
    assert "not truncated" in str(caught.value)


def test_the_concurrency_and_retrieval_floor_are_the_reviewed_values():
    """Both are blocked on measurements nobody has taken. Pinned so a later
    change is deliberate rather than incidental."""
    assert MATRIX_CONCURRENCY == 1, "gateway concurrency has not been measured"
    assert RETRIEVAL_FLOOR is None, "spec §16 decision 6 blocks the floor on recalibration"


def test_a_harness_shaped_approved_run_produces_real_cells():
    """The gate's headline acceptance: a matrix run completes over explicitly
    approved artifacts."""
    result = _verify(_approved_state())
    assert len(result["cells"]) == 1
    cell = result["cells"][0]
    assert cell["state"] == "VERIFIED"
    assert cell["outcome"] == "NO_DISCLOSURE_FOUND_IN_SUPPLIED_PASSAGES"
    assert cell["machine_result"]["publication_number"] == "EP4778509"
    assert cell["passage_chunk_ids"] == ["PAT-EP4778509-CLM-001"]


# --- what a bad response produces ------------------------------------------


def _malformed(prompt: str, **kwargs: object) -> str:
    if VERIFIER_MARKER in str(kwargs.get("system", "")):
        return "{not json at all"
    return _fake_llm_chat(prompt, **kwargs)


def _fenced(prompt: str, **kwargs: object) -> str:
    if VERIFIER_MARKER in str(kwargs.get("system", "")):
        return "```json\n" + _mock_verification(prompt) + "\n```"
    return _fake_llm_chat(prompt, **kwargs)


def test_malformed_json_is_a_validation_failure_with_the_raw_response_retained():
    cell = _verify(_approved_state(), _malformed)["cells"][0]
    assert cell["state"] == "VALIDATION_FAILURE"
    assert cell["failed_check"]
    assert cell["raw_response"], "the rejected response is a diagnostic artifact"
    assert cell["machine_result"] is None


def test_a_fenced_response_is_a_validation_failure_and_is_not_unwrapped():
    """§3 says one JSON object and nothing else. Stripping the fences would
    repair the most visible symptom of a provider ignoring structured output --
    which is exactly the evidence §14.2.1's capability test needs."""
    cell = _verify(_approved_state(), _fenced)["cells"][0]
    assert cell["state"] == "VALIDATION_FAILURE"
    assert cell["machine_result"] is None
    assert "```" in cell["raw_response"]


def test_two_malformed_responses_produce_one_failed_cell_and_exactly_two_calls():
    """The one permitted re-ask (§9.1), held to the same contract. No repair."""
    calls = []

    def counting(prompt: str, **kwargs: object) -> str:
        if VERIFIER_MARKER in str(kwargs.get("system", "")):
            calls.append(prompt)
            return "{still not json"
        return _fake_llm_chat(prompt, **kwargs)

    result = _verify(_approved_state(), counting)
    assert len(result["cells"]) == 1
    assert result["cells"][0]["state"] == "VALIDATION_FAILURE"
    assert len(calls) == 2, "one call plus one re-ask, and no third"


def test_a_validation_failure_is_a_cell_and_never_an_exclusion():
    """Spec §16 decision 9: `VALIDATION_FAILURE` is a property of a *cell* and
    always appears in the matrix. Folding it into an exclusion would report "we
    did not compare this" for a comparison that was attempted and broke."""
    result = _verify(_approved_state(), _malformed)
    assert len(result["cells"]) == 1
    assert result["publication_exclusions"] == []


# --- ineligible passages remove a publication ------------------------------


def test_passage_set_ineligible_removes_the_publication_and_creates_no_cells():
    """Spec cases S and AH. Caught at the matrix level only: the run continues
    and the publication leaves the matrix with a reason."""
    state = _approved_state()
    # A passage whose document id belongs to a different publication fails §7.3
    # check 3 -- a family member is a different document with different content
    # and a different date.
    state["enrichment"]["EP4778509"]["result"] = _enrichment("EP4778509")
    state["passages_by_publication"]["EP4778509"] = build_passages(_enrichment("EP9999999"))

    result = _verify(state)
    assert result["cells"] == []
    assert len(result["publication_exclusions"]) == 1
    assert result["publication_exclusions"][0]["state"] == str(
        ExclusionState.EXCLUDED_INELIGIBLE
    )


def test_one_ineligible_publication_does_not_remove_the_others():
    state = _approved_state(publications=("EP4778509", "EP3010333"))
    state["passages_by_publication"]["EP4778509"] = build_passages(_enrichment("EP9999999"))

    result = _verify(state)
    assert {cell["publication_number"] for cell in result["cells"]} == {"EP3010333"}
    assert len(result["publication_exclusions"]) == 1


def test_a_publication_is_never_both_excluded_and_carrying_cells():
    state = _approved_state(
        features=(APPROVED_FEATURE, SECOND_FEATURE), publications=("EP4778509",)
    )
    state["passages_by_publication"]["EP4778509"] = build_passages(_enrichment("EP9999999"))

    result = _verify(state)
    excluded = {row["normalized_number"] for row in result["publication_exclusions"]}
    cited = {cell["publication_number"] for cell in result["cells"]}
    assert not (excluded & cited)


# --- a mid-matrix failure fails the run ------------------------------------


def test_a_provider_outage_mid_matrix_fails_the_run():
    """**No partial matrix presented as complete.** The cells already computed
    are discarded with the exception rather than returned as a result."""
    calls = []

    def dies_on_the_third(prompt: str, **kwargs: object) -> str:
        if VERIFIER_MARKER in str(kwargs.get("system", "")):
            calls.append(prompt)
            if len(calls) == 3:
                raise RuntimeError("gateway 503")
            return _mock_verification(prompt)
        return _fake_llm_chat(prompt, **kwargs)

    with pytest.raises(RuntimeError):
        _verify(
            _approved_state(
                features=(APPROVED_FEATURE, SECOND_FEATURE),
                publications=("EP4778509", "EP3010333"),
            ),
            dies_on_the_third,
        )


def test_an_unapproved_run_produces_no_cells_and_no_partial_matrix():
    with pytest.raises(UnapprovedArtifactError):
        _verify({**_approved_state(), "approved_decomposition": None})


# --- the machine result is immutable ---------------------------------------


def test_a_stored_machine_result_is_a_faithful_round_trip():
    """Serialized with `mode="json"`, which renames nothing and drops nothing.
    A reviewer is shown this document, and a stored verdict is checked against
    it."""
    cell = _verify(_approved_state())["cells"][0]
    restored = json.loads(json.dumps(cell["machine_result"]))
    assert restored == cell["machine_result"]
    assert restored["verification_scope"] == "SUPPLIED_PASSAGES_ONLY"
    assert restored["feature_id"] == "F1"


def test_the_validated_result_itself_refuses_collection_mutation():
    """`frozen=True` blocks reassignment only; every collection is a tuple of
    frozen models, so evidence cannot be attached to a requirement no model
    cited."""
    from verification.verify import verify_feature_disclosure
    from verification.decomposition import ApprovedDecomposition

    state = _approved_state()
    decomposition = ApprovedDecomposition.from_json(
        {
            **APPROVED_FEATURE,
            "decomposition_id": "dec-1",
            "decomposition_version": "1",
            "approved_by": "testnovelty@gmail.com",
            "approved_at": "2026-08-28T10:00:00+00:00",
        }
    )
    from verification.verify import PublicationUnderEvaluation

    with patch("verification.verify._llm_chat", side_effect=_fake_llm_chat):
        result = verify_feature_disclosure(
            decomposition=decomposition,
            publication=PublicationUnderEvaluation(
                publication_number="EP4778509",
                title="t",
                jurisdiction="EP",
                document_ids=frozenset({"epo:EP4778509"}),
            ),
            passages=[e.passage for e in state["passages_by_publication"]["EP4778509"]],
            cell_id="run:F1:EP4778509",
        )
    machine = result.machine_result
    assert machine is not None
    with pytest.raises(AttributeError):
        machine.elements.append(object())
    with pytest.raises(AttributeError):
        machine.elements[0].evidence.append(object())


# --- the mock serves the guided search, free -------------------------------


def test_run_mock_serves_guided_search_with_no_outbound_http():
    """`run_mock.py` follows the live flat-limitation proposal without HTTP."""
    from run_mock import MOCK_TECHNICAL_SUMMARY, _mock_decomposition, _mock_llm_chat

    decomposition = json.loads(_mock_decomposition())
    assert decomposition["summary"] == MOCK_TECHNICAL_SUMMARY
    assert decomposition["summary"].startswith("An automatic dry-pet-food dispenser includes")
    assert decomposition["summary"].endswith("low-appetite alert to the owner’s phone.")
    assert len(decomposition["features"]) == 1
    assert len(decomposition["features"][0]["elements"]) == 4
    assert decomposition["features"][0]["relationships"] == []

    import llm as llm_module

    routed = _mock_llm_chat(
        f"...{llm_module.DECOMPOSITION_PROMPT_MARKER}...", system=""
    )
    assert json.loads(routed)["features"], "the decomposition prompt reaches its double"


def test_run_mock_keeps_quick_at_eight_matches_and_advanced_at_nine():
    from run_mock import ADVANCED_PET_RESULT_COUNT, PET_REPORT, _pet_result

    description = PET_REPORT["description"]
    quick = _pet_result(description)["data"]["patents"]
    advanced = _pet_result(
        description, result_count=ADVANCED_PET_RESULT_COUNT
    )["data"]["patents"]

    assert len(quick) == 8
    assert len(advanced) == 9
    assert advanced[:8] == quick
    assert advanced[8]["num"] == "US11013214B2"


def test_the_mock_verifier_never_fabricates_a_finding():
    """A double that returned `FOUND` would have to fabricate a verbatim
    quotation to go with it.

    Driven through the **real** prompt renderer, not a hand-written string: the
    double reads its requirement ids back out of the rendered prompt, so a
    hand-written approximation would test the test rather than the double.
    """
    from verification.decomposition import ApprovedDecomposition
    from verification.render import build_user_prompt

    state = _approved_state()
    decomposition = ApprovedDecomposition.from_json(
        {
            **APPROVED_FEATURE,
            "decomposition_id": "dec-1",
            "decomposition_version": "1",
            "approved_by": "testnovelty@gmail.com",
            "approved_at": "2026-08-28T10:00:00+00:00",
        }
    )
    prompt = build_user_prompt(
        feature_id=decomposition.feature_id,
        feature_version=decomposition.feature_version,
        feature_text=decomposition.feature_text,
        elements=decomposition.elements,
        relationships=decomposition.relationships,
        publication_number="EP4778509",
        publication_title="A closed-loop exhaust heat recovery system",
        jurisdiction="EP",
        passages=[e.passage for e in state["passages_by_publication"]["EP4778509"]],
    )
    response = json.loads(_mock_verification(prompt))
    assert response["outcome"] == "NO_DISCLOSURE_FOUND_IN_SUPPLIED_PASSAGES"
    assert all(
        entry["status"] == "NOT_FOUND_IN_SUPPLIED_PASSAGES"
        for entry in response["elements"]
    )
    assert all(entry["evidence"] == [] for entry in response["elements"])


# --- persistence: the machine result, verbatim -----------------------------


@pytest.mark.postgres
class TestCellPersistence:
    """Cells and results through `workflow/store.py`, against real PostgreSQL.

    The three CHECK constraints on `verification_results` are the schema's half
    of spec §16 decision 9 -- `VERIFIED` must carry a machine result,
    `VALIDATION_FAILURE` must carry a failing check, `NOT_EVALUATED` must carry a
    reason -- and the in-memory double cannot exhibit any of them.
    """

    OWNER = "verification-worker"

    def _leased(self, pg, make_run):
        from workflow import lease

        run_id = make_run()
        return run_id, lease.acquire(
            pg, run_id=run_id, owner_id=self.OWNER, ttl_seconds=60.0
        )

    def _cell(self, run_id, **overrides):
        cell = _verify(_approved_state())["cells"][0]
        cell["run_id"] = run_id
        cell["cell_id"] = f"{run_id}:F1:EP4778509"
        cell.update(overrides)
        return cell

    def test_a_machine_result_round_trips_byte_identical(self, pg, make_run):
        """**Verbatim** means verbatim. A reviewer is shown this document and a
        stored verdict is checked against it, so a re-serialization that
        reordered or renamed anything would silently change what was reviewed."""
        from workflow import store

        run_id, token = self._leased(pg, make_run)
        cell = self._cell(run_id)
        store.save_cell_result(
            pg, run_id=run_id, owner_id=self.OWNER, fence_token=token, cell=cell
        )

        stored = store.list_cells(pg, run_id=run_id, actor_email=ACTOR)
        assert len(stored) == 1
        assert stored[0]["machine_result"] == cell["machine_result"]
        assert stored[0]["state"] == "VERIFIED"
        assert stored[0]["decomposition_version"] == 1

    def test_a_fenced_worker_cannot_write_a_cell(self, pg, make_run):
        from workflow import lease
        from workflow.types import FencedError
        from workflow import store

        run_id, token = self._leased(pg, make_run)
        pg.execute(
            """
            UPDATE run_leases SET acquired_at = now() - interval '120 seconds',
                   expires_at = now() - interval '60 seconds' WHERE run_id = %s
            """,
            (run_id,),
        )
        lease.acquire(pg, run_id=run_id, owner_id="replacement", ttl_seconds=60.0)

        with pytest.raises(FencedError):
            store.save_cell_result(
                pg, run_id=run_id, owner_id=self.OWNER, fence_token=token,
                cell=self._cell(run_id),
            )
        assert pg.execute(
            "SELECT count(*) FROM verification_cells WHERE run_id = %s", (run_id,)
        ).fetchone()[0] == 0

    def test_a_validation_failure_is_stored_as_a_cell_with_its_failing_check(
        self, pg, make_run
    ):
        """It appears in the matrix as itself. There is no path that turns it
        into an exclusion or a blank."""
        from workflow import store

        run_id, token = self._leased(pg, make_run)
        with patch("llm._llm_chat", side_effect=_malformed), \
             patch("verification.verify._llm_chat", side_effect=_malformed), \
             patch("graph.verification_nodes.emit_progress"):
            cell = verify_features_node(_approved_state())["cells"][0]
        cell["cell_id"] = f"{run_id}:F1:EP4778509"

        store.save_cell_result(
            pg, run_id=run_id, owner_id=self.OWNER, fence_token=token, cell=cell
        )
        stored = store.list_cells(pg, run_id=run_id, actor_email=ACTOR)[0]
        assert stored["state"] == "VALIDATION_FAILURE"
        assert stored["failed_check"]
        assert stored["machine_result"] is None

    def test_the_schema_refuses_a_verified_cell_with_no_machine_result(self, pg, make_run):
        """A `VERIFIED` state is a claim that a validated result exists."""
        run_id, _ = self._leased(pg, make_run)
        pg.execute(
            """
            INSERT INTO verification_cells
                (cell_id, run_id, feature_id, feature_version, publication_number,
                 decomposition_version, candidate_set_version)
            VALUES (%s, %s, 'F1', '1', 'EP4778509', 1, 1)
            """,
            (f"{run_id}:F1:EP4778509", run_id),
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            pg.execute(
                """
                INSERT INTO verification_results
                    (cell_id, result_version, state, machine_result, model, prompt_version)
                VALUES (%s, 1, 'VERIFIED', NULL, 'm', 'p')
                """,
                (f"{run_id}:F1:EP4778509",),
            )

    def test_an_excluded_publication_has_no_cell_row_at_all(self, pg, make_run):
        """Not an empty one, not a greyed one. The scope rule, at the schema."""
        from workflow import store

        run_id, token = self._leased(pg, make_run)
        state = _approved_state()
        state["passages_by_publication"]["EP4778509"] = build_passages(
            _enrichment("EP9999999")
        )
        result = _verify(state)
        assert result["cells"] == []
        for cell in result["cells"]:  # pragma: no cover - empty by assertion above
            store.save_cell_result(
                pg, run_id=run_id, owner_id=self.OWNER, fence_token=token, cell=cell
            )
        assert pg.execute(
            "SELECT count(*) FROM verification_cells WHERE run_id = %s", (run_id,)
        ).fetchone()[0] == 0

    def test_re_saving_the_same_cell_does_not_duplicate_it(self, pg, make_run):
        """Recovery re-runs the node, so every write is replayed eventually."""
        from workflow import store

        run_id, token = self._leased(pg, make_run)
        cell = self._cell(run_id)
        for _ in range(2):
            store.save_cell_result(
                pg, run_id=run_id, owner_id=self.OWNER, fence_token=token, cell=cell
            )
        assert len(store.list_cells(pg, run_id=run_id, actor_email=ACTOR)) == 1

    def test_a_second_user_cannot_read_another_runs_matrix(self, pg, make_run, second_user):
        from workflow import store
        from workflow.types import RunNotFoundError

        run_id, token = self._leased(pg, make_run)
        store.save_cell_result(
            pg, run_id=run_id, owner_id=self.OWNER, fence_token=token,
            cell=self._cell(run_id),
        )
        assert store.list_cells(pg, run_id=run_id, actor_email=ACTOR)
        with pytest.raises(RunNotFoundError):
            store.list_cells(pg, run_id=run_id, actor_email=second_user)

    def test_cells_disappear_with_their_run(self, pg, make_run):
        from workflow import store

        run_id, token = self._leased(pg, make_run)
        store.save_cell_result(
            pg, run_id=run_id, owner_id=self.OWNER, fence_token=token,
            cell=self._cell(run_id),
        )
        pg.execute("DELETE FROM verification_runs WHERE id = %s", (run_id,))
        assert pg.execute(
            "SELECT count(*) FROM verification_cells WHERE run_id = %s", (run_id,)
        ).fetchone()[0] == 0


# --- the approved MVP constants --------------------------------------------
#
# APPROVED 2026-08-28 by the repository owner, in the request that authorized
# Gate C's completion:
#
#     MATRIX_CELL_CAP   = 75
#     MATRIX_CONCURRENCY = 1
#     RETRIEVAL_FLOOR   = None
#
#     "More than 75 cells must fail clearly. Never truncate the matrix."
#     "Verifier calls remain sequential until gateway concurrency is measured."
#
# These are reviewed constants (spec §16 decision 5), not tuning knobs, so they
# are pinned by value AND by behaviour. A test that only asserted the numbers
# would pass against an implementation that truncated at 75 instead of raising.


def test_the_approved_constants_are_exactly_the_approved_values():
    assert MATRIX_CELL_CAP == 75
    assert MATRIX_CONCURRENCY == 1
    assert RETRIEVAL_FLOOR is None


def _n_features(count: int):
    return tuple(
        {**APPROVED_FEATURE, "feature_id": f"F{index}"} for index in range(count)
    )


def test_exactly_seventy_five_cells_are_accepted():
    """The boundary from below. 75 x 1 is the largest matrix the Gate B cap can
    produce, and it must run rather than be refused off-by-one."""
    result = _verify(_approved_state(features=_n_features(MATRIX_CELL_CAP)))
    assert len(result["cells"]) == MATRIX_CELL_CAP


def test_seventy_six_cells_fail_before_any_llm_call():
    """The boundary from above -- and it must fail **before** spending anything.
    A matrix that raised after 40 calls would have burned 40 calls to learn a
    fact available from the approved artifacts alone."""
    with patch("llm._llm_chat") as chat, \
         patch("verification.verify._llm_chat") as verifier_chat, \
         patch("graph.verification_nodes.emit_progress"):
        with pytest.raises(ValueError) as caught:
            verify_features_node(_approved_state(features=_n_features(MATRIX_CELL_CAP + 1)))
    chat.assert_not_called()
    verifier_chat.assert_not_called()
    assert "76 cells" in str(caught.value)
    assert str(MATRIX_CELL_CAP) in str(caught.value)


def test_an_over_cap_matrix_is_never_truncated():
    """The failure mode the cap exists to prevent is not a crash -- it is a
    **silently shortened matrix**, which presents partial coverage as complete."""
    with patch("llm._llm_chat"), patch("verification.verify._llm_chat"), \
         patch("graph.verification_nodes.emit_progress"):
        with pytest.raises(ValueError) as caught:
            verify_features_node(_approved_state(features=_n_features(MATRIX_CELL_CAP + 1)))
    message = str(caught.value)
    assert "not truncated" in message
    assert "partial coverage as complete" in message


def test_an_over_cap_matrix_stores_no_partial_result():
    """Nothing is returned and nothing is written. The exception is the only
    outcome, so there is no half-matrix for a caller to persist."""
    outcome = None
    with patch("llm._llm_chat"), patch("verification.verify._llm_chat"), \
         patch("graph.verification_nodes.emit_progress"):
        try:
            outcome = verify_features_node(
                _approved_state(features=_n_features(MATRIX_CELL_CAP + 1))
            )
        except ValueError:
            pass
    assert outcome is None


@pytest.mark.postgres
def test_an_over_cap_matrix_leaves_no_cell_rows(pg, make_run):
    """The database half of the same statement, because "no partial matrix is
    stored" is a claim about rows, not about a return value."""
    from workflow import lease

    run_id = make_run()
    token = lease.acquire(pg, run_id=run_id, owner_id="cap-worker", ttl_seconds=60.0)
    state = _approved_state(features=_n_features(MATRIX_CELL_CAP + 1))
    state["run_id"] = run_id

    with patch("llm._llm_chat"), patch("verification.verify._llm_chat"), \
         patch("graph.verification_nodes.emit_progress"):
        with pytest.raises(ValueError):
            verify_features_node(state)

    assert pg.execute(
        "SELECT count(*) FROM verification_cells WHERE run_id = %s", (run_id,)
    ).fetchone()[0] == 0
    assert pg.execute(
        "SELECT count(*) FROM verification_results", ()
    ).fetchone()[0] >= 0  # the table exists; this run contributed nothing to it
    assert token > 0


def test_at_most_one_verifier_call_is_ever_in_flight():
    """**Sequential until gateway concurrency is measured.** Lens's trial tier
    already forced a global request lock in this codebase, so "probably fine"
    has been wrong here before.

    Asserted by observing overlap rather than by reading the constant: a future
    change that introduced a thread pool while leaving `MATRIX_CONCURRENCY = 1`
    unread would pass a value check and fail this one.
    """
    import threading

    active = 0
    peak = 0
    guard = threading.Lock()

    def observing(prompt, **kwargs):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        try:
            return _fake_llm_chat(prompt, **kwargs)
        finally:
            with guard:
                active -= 1

    _verify(
        _approved_state(
            features=(APPROVED_FEATURE, SECOND_FEATURE),
            publications=("EP4778509", "EP3010333"),
        ),
        observing,
    )
    assert peak == 1, f"maximum concurrent verifier calls was {peak}, expected exactly 1"


def test_the_matrix_node_spawns_no_executor_or_thread_pool():
    """Source-level, because a pool sized 1 would satisfy the overlap test above
    while making a later bump to 8 a one-character change nobody reviews."""
    import ast

    source = (
        Path(__file__).resolve().parent.parent / "graph" / "verification_nodes.py"
    ).read_text(encoding="utf-8")
    called = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Name):
                called.add(target.id)
            elif isinstance(target, ast.Attribute):
                called.add(target.attr)
    forbidden = {"ThreadPoolExecutor", "ProcessPoolExecutor", "Thread", "gather", "map"}
    assert not (called & forbidden), f"the matrix runs concurrently: {called & forbidden}"


def test_the_retrieval_floor_is_passed_explicitly_and_is_none():
    """§16 decision 6 blocks the uncalibrated passage-level floor. Passing it
    **explicitly** matters: relying on
    `verify_feature_disclosure`'s own default would mean a later change to that
    default silently altered what this run evaluated."""
    captured = {}
    real = verification_nodes.verify_feature_disclosure

    def capturing(**kwargs):
        captured.update(kwargs)
        return real(**kwargs)

    with patch("llm._llm_chat", side_effect=_fake_llm_chat), \
         patch("verification.verify._llm_chat", side_effect=_fake_llm_chat), \
         patch("graph.verification_nodes.emit_progress"), \
         patch.object(verification_nodes, "verify_feature_disclosure", capturing):
        verify_features_node(_approved_state())

    assert "retrieval_floor" in captured, "the floor must be passed, not defaulted"
    assert captured["retrieval_floor"] is None


def test_no_module_substitutes_a_numeric_retrieval_floor():
    """A guessed floor would drop passages from the prompt and report the result
    as `NOT_EVALUATED` -- an evaluation that did not happen, attributed to a
    threshold nobody established."""
    import ast

    backend = Path(__file__).resolve().parent.parent
    offenders = []
    for path in backend.rglob("*.py"):
        if any(part in {".venv", "__pycache__", "tests"} for part in path.parts):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # A numeric LITERAL, specifically. Passing the reviewed `None`
            # constant through, or forwarding a caller's parameter, is the
            # designed shape; a number written into the source is the invention.
            if isinstance(node, ast.keyword) and node.arg == "retrieval_floor":
                if isinstance(node.value, ast.Constant) and isinstance(
                    node.value.value, (int, float)
                ):
                    offenders.append(
                        f"{path.relative_to(backend)}:{node.value.lineno} -> "
                        f"{node.value.value}"
                    )
            # And no module may give the constant itself a number.
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.target.id == "RETRIEVAL_FLOOR" and isinstance(
                    node.value, ast.Constant
                ) and isinstance(node.value.value, (int, float)):
                    offenders.append(
                        f"{path.relative_to(backend)}:{node.lineno} -> "
                        f"RETRIEVAL_FLOOR = {node.value.value}"
                    )
    assert offenders == [], f"a retrieval floor was invented: {offenders}"
