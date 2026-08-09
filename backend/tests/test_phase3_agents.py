"""Focused tests for the three new Phase 3 agents: Semantic Search, Result
Fusion, and Validation.

Not the full P3.8 failure-path matrix - just enough to pin each agent's own
Definition of Done. ``tests/conftest.py``'s autouse fixture stubs embeddings and
Qdrant for every test here, same as the rest of the suite.
"""

from unittest.mock import patch

import pytest
import requests

from compare import rank_by_title_abstract
from graph.searching import keyword_search_node, semantic_search_node
from graph.validation import validate_node
from models import PatentRecord
from ranking import reciprocal_rank_fusion
from search.epo import search_epo
from search.retrieve import retrieve_similar_patents
from search.vector_store import store_patents

# --------------------------------------------------------------------------
# Keyword search: drop candidates with no full text
# --------------------------------------------------------------------------


def test_keyword_search_node_drops_candidates_with_no_full_text():
    """A candidate that comes out of enrichment with no full text at all is
    dropped, not silently left to fall back to scoring against its abstract."""
    has_text = PatentRecord(id="US1", title="Has text", abstract="abstract", source="Google Patents")
    no_text = PatentRecord(id="US2", title="No text", abstract="abstract", source="Google Patents")

    def fake_enrich(patents):
        for patent in patents:
            if patent.id == "US1":
                patent.full_text = "real full text"
        return patents

    with patch("graph.searching.emit_progress"), \
         patch("graph.searching.search_all_providers_strict", return_value=[has_text, no_text]), \
         patch("graph.searching.rank_by_title_abstract", side_effect=lambda text, patents, top_n: patents), \
         patch("graph.searching.enrich_with_full_text", side_effect=fake_enrich), \
         patch("graph.searching.enrich_epo_full_text", side_effect=lambda patents: patents):
        result = keyword_search_node({"queries": ["q"], "invention_text": "an invention"})

    assert [p.id for p in result["keyword_patents"]] == ["US1"]
    assert result["candidates"] == result["keyword_patents"]


# --------------------------------------------------------------------------
# Title/abstract relevance pre-filter
# --------------------------------------------------------------------------


def test_rank_by_title_abstract_keeps_the_closer_matches():
    """The filter that runs before full-text enrichment: ranks by title +
    abstract similarity alone and truncates to top_n."""
    related = PatentRecord(id="US1", title="UV water bottle sterilizer", abstract="Sterilizes water with UV-C light in a portable bottle.")
    unrelated = PatentRecord(id="US2", title="Tractor gearbox", abstract="A mechanical gearbox assembly for tractor power take-off.")

    kept = rank_by_title_abstract("A portable UV-C water bottle sterilizer", [related, unrelated], top_n=1)

    assert [p.id for p in kept] == ["US1"]


def test_rank_by_title_abstract_returns_empty_for_no_candidates():
    assert rank_by_title_abstract("anything", [], top_n=20) == []


def test_epo_search_builds_kind_free_epodoc_id():
    """The id must be country+doc-number with NO kind suffix - EPO OPS's
    full-text endpoints 404 on a kind-suffixed id (verified against the live
    API during development)."""
    fake_xml = b"""<?xml version="1.0"?>
    <ops:world-patent-data xmlns:ops="http://ops.epo.org" xmlns="http://www.epo.org/exchange">
      <exchange-documents>
        <exchange-document country="EP" doc-number="1234567" kind="A1">
          <bibliographic-data>
            <invention-title lang="en">Test Invention</invention-title>
            <abstract><p>Test abstract text.</p></abstract>
          </bibliographic-data>
        </exchange-document>
      </exchange-documents>
    </ops:world-patent-data>"""

    class _FakeResponse:
        status_code = 200
        content = fake_xml

        def raise_for_status(self):
            pass

    with patch("search.epo.get_access_token", return_value="fake-token"), \
         patch("search.epo.requests.get", return_value=_FakeResponse()):
        patents = search_epo("test query", size=10)

    assert len(patents) == 1
    assert patents[0].id == "EP1234567"


def test_epo_search_returns_empty_on_zero_results_fault():
    """EPO OPS uses HTTP 404 with a SERVER.EntityNotFound fault to mean
    'zero matches for this query' - a genuine empty result, not a provider
    failure. Confirmed against the live API: a query that legitimately
    matches nothing returns exactly this shape."""
    fault_xml = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
    <fault xmlns="http://ops.epo.org">
        <code>SERVER.EntityNotFound</code>
        <message>No results found</message>
    </fault>"""

    class _FakeNotFoundResponse:
        status_code = 404
        content = fault_xml

        def raise_for_status(self):
            raise AssertionError("should not be called for a zero-results fault")

    with patch("search.epo.get_access_token", return_value="fake-token"), \
         patch("search.epo.requests.get", return_value=_FakeNotFoundResponse()):
        patents = search_epo("a query that matches nothing", size=10)

    assert patents == []


def test_epo_search_raises_on_a_genuine_404_that_is_not_the_zero_results_fault():
    """A 404 whose body isn't the SERVER.EntityNotFound shape is a real
    failure and must still raise - the zero-results carve-out is narrow."""
    class _FakeUnexpectedResponse:
        status_code = 404
        content = b"<html>not the fault shape we expect</html>"

        def raise_for_status(self):
            raise requests.HTTPError("404 Client Error")

    with patch("search.epo.get_access_token", return_value="fake-token"), \
         patch("search.epo.requests.get", return_value=_FakeUnexpectedResponse()):
        with pytest.raises(requests.HTTPError):
            search_epo("some query", size=10)


# --------------------------------------------------------------------------
# P3.3 - Semantic Search
# --------------------------------------------------------------------------


def test_repeated_query_retrieves_earlier_indexed_patents():
    """P3.3's own DoD: a repeated query retrieves patents indexed by an
    earlier search."""
    store_patents([
        PatentRecord(
            id="US1000001A",
            title="UV water sterilizer",
            abstract="Sterilizes water with ultraviolet light in a portable vessel.",
        ),
    ])

    hits = retrieve_similar_patents("UV water sterilizer bottle")

    assert any(hit.id == "US1000001A" for hit in hits)


def test_semantic_search_returns_empty_before_anything_is_indexed():
    """A cold, empty index is a legitimate empty result, not a failure."""
    assert retrieve_similar_patents("nothing has been indexed yet") == []


def test_semantic_search_drops_weak_matches_instead_of_returning_the_least_bad_option():
    """Qdrant's nearest-neighbor search always returns *something* up to
    `limit`, even if the closest thing in the index is still unrelated. Without
    a similarity floor, a small or unrelated corpus would hand back "the
    least-bad options" dressed up as real candidates. `score_threshold` in
    `search/vector_store.py` exists to cut those off at retrieval instead of
    relying on validation to catch them."""
    store_patents([
        PatentRecord(
            id="US9999999A",
            title="Tractor gearbox",
            abstract="A mechanical gearbox assembly for tractor power take-off.",
        ),
    ])

    hits = retrieve_similar_patents(
        "A portable UV-C water bottle sterilizer with a graphene-oxide filtration membrane"
    )

    assert hits == []


def test_semantic_search_node_sets_semantic_patents():
    found = [PatentRecord(id="US5", title="Found")]
    with patch("graph.searching.emit_progress"), \
         patch("graph.searching.retrieve_similar_patents", return_value=found):
        result = semantic_search_node({"invention_text": "a widget"})

    assert result == {"semantic_patents": found}


# --------------------------------------------------------------------------
# P3.4 - Result Fusion
# --------------------------------------------------------------------------


def test_fusion_surfaces_semantic_only_matches():
    """P3.4's own DoD: fused ordering differs from keyword-only ordering when
    the semantic branch found something keyword search didn't."""
    keyword_only = [
        PatentRecord(id="US1", title="Keyword hit 1"),
        PatentRecord(id="US2", title="Keyword hit 2"),
    ]
    semantic_only = [PatentRecord(id="US9", title="Semantic-only hit")]

    fused_ids = [p.id for p in reciprocal_rank_fusion([keyword_only, semantic_only])]

    assert "US9" in fused_ids
    assert fused_ids != [p.id for p in keyword_only]


def test_fusion_dedupes_and_ranks_patents_both_branches_found_ahead_of_single_hits():
    keyword = [PatentRecord(id="US1", title="A"), PatentRecord(id="US2", title="B")]
    semantic = [PatentRecord(id="US2", title="B"), PatentRecord(id="US3", title="C")]

    fused_ids = [p.id for p in reciprocal_rank_fusion([keyword, semantic])]

    assert fused_ids.count("US2") == 1
    assert fused_ids.index("US2") < fused_ids.index("US3")


# --------------------------------------------------------------------------
# P3.5 - Validation
# --------------------------------------------------------------------------


def test_validate_node_drops_patents_the_llm_marks_irrelevant():
    candidates = [
        PatentRecord(id="US1", title="Relevant"),
        PatentRecord(id="US2", title="Irrelevant"),
    ]
    state = {"invention_text": "a widget", "fused_patents": candidates}

    with patch("graph.validation.emit_progress"), \
         patch("graph.validation.validate_relevance", return_value=[True, False]):
        result = validate_node(state)

    assert [p.id for p in result["validated_patents"]] == ["US1"]
    assert result["candidates"] == result["validated_patents"]


def test_validate_node_skips_the_llm_call_when_nothing_was_fused():
    state = {"invention_text": "a widget", "fused_patents": []}

    with patch("graph.validation.emit_progress"), \
         patch("graph.validation.validate_relevance") as fake_validate:
        result = validate_node(state)

    fake_validate.assert_not_called()
    assert result == {"validated_patents": [], "candidates": []}


def test_validate_node_raises_when_the_llm_omits_a_verdict():
    """Zero tolerance: a missing verdict fails the request rather than being
    treated as an implicit relevant/irrelevant default."""
    candidates = [PatentRecord(id="US1", title="A"), PatentRecord(id="US2", title="B")]
    state = {"invention_text": "a widget", "fused_patents": candidates}

    with patch("graph.validation.emit_progress"), \
         patch(
             "graph.validation.validate_relevance",
             side_effect=ValueError("LLM omitted a relevance verdict for patent index/indices: [2]"),
         ):
        with pytest.raises(ValueError, match="omitted a relevance verdict"):
            validate_node(state)
