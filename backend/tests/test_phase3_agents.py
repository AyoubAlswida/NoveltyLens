"""Focused tests for the three new Phase 3 agents: Semantic Search, Result
Fusion, and Validation.

Not the full P3.8 failure-path matrix - just enough to pin each agent's own
Definition of Done. ``tests/conftest.py``'s autouse fixture stubs embeddings and
Qdrant for every test here, same as the rest of the suite.
"""

from unittest.mock import patch

import pytest

from graph.pipeline import run_graph_pipeline
from graph.searching import semantic_search_node
from graph.validation import validate_node
from models import InventionAnalysis, MatchAnalysis, PatentRecord
from ranking import reciprocal_rank_fusion
from search.retrieve import retrieve_similar_patents
from search.search import KeywordSearchResults
from search.vector_store import QUERY_INSTRUCTION, store_patents
from tests.fakes import fake_embed_texts

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


def test_query_is_instruction_prefixed_and_documents_are_not():
    """bge-large-en-v1.5 is asymmetric: the instruction goes on the query side
    only.

    Prefixing documents too would undo the benefit - the shared prefix pulls
    every stored vector toward the same region, flattening the gap between a
    relevant hit and an unrelated one that ``MIN_SIMILARITY_SCORE`` has to cut
    through. Since ``store_patents`` and ``semantic_search`` call the same
    ``embed_texts``, that mistake is a one-line edit away and would show up only
    as quietly worse ranking, so it is pinned here rather than left to review.
    """
    seen: list[list[str]] = []

    def spy_embed(texts: list[str]) -> list[list[float]]:
        seen.append(texts)
        return fake_embed_texts(texts)

    with patch("search.vector_store.embed_texts", side_effect=spy_embed):
        store_patents([PatentRecord(id="US1000002A", title="UV water sterilizer")])
        document_texts = seen.pop()

        retrieve_similar_patents("UV water sterilizer bottle")
        query_texts = seen.pop()

    assert not any(text.startswith(QUERY_INSTRUCTION) for text in document_texts)
    assert query_texts == [QUERY_INSTRUCTION + "UV water sterilizer bottle"]


def test_semantic_search_node_sets_semantic_patents():
    found = [PatentRecord(id="US5", title="Found")]
    with patch("graph.searching.emit_progress"), \
         patch("graph.searching.retrieve_similar_patents", return_value=found):
        result = semantic_search_node({"invention_text": "a widget"})

    assert result == {"semantic_patents": found}


def test_semantic_search_does_not_retrieve_this_requests_own_keyword_hits():
    """Semantic search must only ever see patents indexed by *earlier* requests.

    The two search branches fan out from ``search_orchestrator`` in parallel,
    but LangGraph runs a superstep's sync nodes sequentially - ``keyword_search``
    always executes first. If it indexes its own hits into Qdrant before the
    semantic branch reads, semantic search retrieves the patents keyword search
    just handed over instead of genuinely new prior art, and Result Fusion then
    awards those echoes credit from *both* branches - inflating their fused rank
    over patents only one branch found.

    Indexing must therefore happen after both branches have read, which is a
    property of the graph's topology, not of the scheduler's ordering.
    """
    keyword_hits = [
        PatentRecord(
            id="US1000042A",
            title="UV water sterilizer",
            abstract="Sterilizes water with ultraviolet light in a portable vessel.",
        ),
    ]
    invention = "Sterilizes water with ultraviolet light in a portable vessel."
    analysis = InventionAnalysis(
        queries=["uv water sterilizer"], keywords=["ultraviolet"], concepts=["uv sterilization"]
    )
    seen_by_semantic: list[list[PatentRecord]] = []

    def spy_retrieve(invention_text: str, limit: int = 20) -> list[PatentRecord]:
        hits = retrieve_similar_patents(invention_text, limit=limit)
        seen_by_semantic.append(hits)
        return hits

    with patch("graph.understanding.understand_invention", return_value=analysis), \
         patch(
             "graph.searching.search_all_providers_strict",
             return_value=KeywordSearchResults(candidates=keyword_hits, index_only=[]),
         ), \
         patch("graph.searching.enrich_with_full_text", side_effect=lambda patents: patents), \
         patch("graph.searching.retrieve_similar_patents", side_effect=spy_retrieve), \
         patch("graph.validation.validate_relevance", side_effect=lambda text, c: [True] * len(c)), \
         patch(
             "graph.ranking.analyze_all_matches",
             side_effect=lambda description, patents: MatchAnalysis(
                 rationales=["r"] * len(patents), recommendations=["rec"]
             ),
         ):
        list(run_graph_pipeline(invention, None))

    # Qdrant was cold: no earlier request indexed anything, so the semantic
    # branch had nothing legitimate to find.
    assert seen_by_semantic == [[]]

    # ...but this request's hits must still be indexed by the time it ends, or
    # the corpus would never grow for the next request.
    assert any(hit.id == "US1000042A" for hit in retrieve_similar_patents(invention))


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


def test_fusion_dedupes_on_the_same_key_merge_patents_uses():
    """`search.search.merge_patents` dedupes on `id.strip().upper()`. Fusion must
    agree with it: it receives keyword results merge_patents already deduped
    alongside semantic results rebuilt from Qdrant payloads, so a looser key here
    would let one patent through as two separate cards in the final report."""
    keyword = [PatentRecord(id="US1234567B2", title="A")]
    semantic = [PatentRecord(id=" us1234567b2 ", title="A")]

    fused = reciprocal_rank_fusion([keyword, semantic])

    assert len(fused) == 1


def test_fusion_does_not_replace_an_enriched_record_with_a_stale_one():
    """`compare.py` scores on `full_text or abstract`, so which copy of a patent
    survives fusion decides whether it is ranked on its full description or just
    its abstract. The semantic branch rebuilds records from Qdrant payloads that
    may have been indexed before enrichment ran; those must not clobber the copy
    this request just enriched."""
    enriched = PatentRecord(id="US1", title="A", abstract="abs", full_text="full description and claims")
    stale = PatentRecord(id="US1", title="A", abstract="abs", full_text="")

    fused = reciprocal_rank_fusion([[enriched], [stale]])

    assert fused[0].full_text == "full description and claims"


def test_fusion_upgrades_a_bare_record_when_a_later_list_has_full_text():
    """The mirror of the above: whichever branch supplied the text, fusion keeps
    the richer record rather than whichever happened to be seen first."""
    bare = PatentRecord(id="US1", title="A", abstract="abs", full_text="")
    enriched = PatentRecord(id="US1", title="A", abstract="abs", full_text="full description and claims")

    fused = reciprocal_rank_fusion([[bare], [enriched]])

    assert fused[0].full_text == "full description and claims"


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
