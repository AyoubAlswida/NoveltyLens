"""Providers return more per call than the pipeline enriches, and the surplus is
indexed rather than discarded.

SerpAPI bills per call, not per result: ``search_google`` was already asking for
10 and throwing away all but ``results_per_query``. Those extras now grow the
Qdrant corpus for free, which is what makes semantic search useful sooner. The
constraint is that they must never be mistaken for candidates - they are
abstract-only, never enriched - and must never overwrite a richer stored vector.
"""

from unittest.mock import patch

import pytest

from graph.searching import index_patents_node, keyword_search_node
from models import PatentRecord
from search.retrieve import retrieve_similar_patents
from search.search import search_all_providers_strict
from search.vector_store import known_ids, store_patents


def _provider(records: list[PatentRecord]):
    def search(query: str, size: int) -> list[PatentRecord]:
        return records[:size]
    return search


def _corpus(n: int, prefix: str = "US") -> list[PatentRecord]:
    return [
        PatentRecord(
            id=f"{prefix}{1000000 + i}A",
            title=f"UV water sterilizer variant {i}",
            abstract=f"Sterilizes water with ultraviolet light, configuration {i}.",
        )
        for i in range(n)
    ]


# --------------------------------------------------------------------------
# The split
# --------------------------------------------------------------------------

def test_surplus_results_are_returned_for_indexing_not_discarded():
    ten = _corpus(10)
    with patch("search.search.SOURCES", (("Google Patents", _provider(ten)),)):
        found = search_all_providers_strict(["a query"], results_per_query=4, fetch_per_query=10)

    assert len(found.candidates) == 4
    assert len(found.index_only) == 6, "the 6 already-paid-for extras were dropped"
    assert not ({p.id for p in found.candidates} & {p.id for p in found.index_only})


def test_candidates_are_taken_per_query_not_from_the_flattened_head():
    """Taking the first N overall would fill the candidate set from query 1 and
    silently discard the perspectives the other generated queries exist to cover."""
    a, b = _corpus(10, "AA"), _corpus(10, "BB")
    sources = (("P", lambda q, size: (a if q == "q1" else b)[:size]),)

    with patch("search.search.SOURCES", sources):
        found = search_all_providers_strict(["q1", "q2"], results_per_query=4, fetch_per_query=10)

    ids = {p.id[:2] for p in found.candidates}
    assert ids == {"AA", "BB"}, f"one query dominated the candidate set: {ids}"


@pytest.mark.skip(
    reason="graph/searching.py keeps RESULTS_PER_QUERY at 10 (a wide net feeding "
    "a title/abstract filter, not upstream's narrower RESULTS_PER_QUERY=4), which "
    "equals FETCH_PER_QUERY - so keyword_search_node promotes everything it "
    "fetches to `candidates` and never produces `index_only` extras through this "
    "call path. The underlying split is still tested directly against "
    "search_all_providers_strict above, with explicit results_per_query= / "
    "fetch_per_query= - this test's premise only applies to keyword_search_node "
    "with a narrower RESULTS_PER_QUERY."
)
def test_extras_are_not_enriched():
    """Enrichment costs a provider credit per patent - that is the whole reason
    the candidate set stays small and separate."""
    ten = _corpus(10)
    enriched: list[list[PatentRecord]] = []

    with patch("search.search.SOURCES", (("Google Patents", _provider(ten)),)), \
         patch("graph.searching.emit_progress"), \
         patch("graph.searching.enrich_with_full_text", side_effect=lambda p: enriched.append(p) or p):
        result = keyword_search_node({"queries": ["a query"], "invention_text": "an invention"})

    assert len(enriched) == 1
    assert len(enriched[0]) == 4, "extras were sent to the paid enrichment step"
    assert len(result["index_only_patents"]) == 6


def test_extras_never_become_ranking_candidates():
    """They are abstract-only. Ranking them against enriched patents would report
    the difference in available text as a difference in similarity."""
    ten = _corpus(10)
    with patch("search.search.SOURCES", (("Google Patents", _provider(ten)),)), \
         patch("graph.searching.emit_progress"), \
         patch("graph.searching.enrich_with_full_text", side_effect=lambda p: p):
        result = keyword_search_node({"queries": ["a query"], "invention_text": "an invention"})

    candidate_ids = {p.id for p in result["candidates"]}
    assert not (candidate_ids & {p.id for p in result["index_only_patents"]})


# --------------------------------------------------------------------------
# Indexing
# --------------------------------------------------------------------------

def test_extras_reach_the_corpus():
    extras = _corpus(3, "EX")
    with patch("graph.searching.emit_progress"):
        index_patents_node({"keyword_patents": [], "index_only_patents": extras})

    found = {p.id for p in retrieve_similar_patents("Sterilizes water with ultraviolet light")}
    assert {p.id for p in extras} <= found


def test_already_indexed_extras_are_not_re_embedded():
    """A patent Qdrant already holds costs an embedding call to write a vector
    identical to the one it replaces."""
    extras = _corpus(3, "EX")
    store_patents(extras)

    embedded: list[list[str]] = []
    with patch("search.vector_store.embed_texts", side_effect=lambda texts: embedded.append(texts) or [[1.0] * 32] * len(texts)):
        index_patents_node({"keyword_patents": [], "index_only_patents": extras})

    assert embedded == [], f"re-embedded {len(embedded)} already-indexed batches"


def test_an_abstract_only_extra_does_not_overwrite_an_enriched_record():
    """The corpus must not degrade the more it is used.

    A patent stored with full text by an earlier request will keep showing up as
    a cheap extra in later searches. Blindly upserting would replace its rich
    vector with an abstract-only one.
    """
    patent_id = "US1000042A"
    enriched = PatentRecord(
        id=patent_id, title="UV water sterilizer",
        abstract="Short abstract.",
        full_text="A long and detailed description of ultraviolet sterilization " * 40,
    )
    store_patents([enriched])

    stale_extra = PatentRecord(id=patent_id, title="UV water sterilizer", abstract="Short abstract.")

    stored: list[str] = []
    with patch("search.vector_store.embed_texts", side_effect=lambda texts: stored.extend(texts) or [[1.0] * 32] * len(texts)):
        index_patents_node({"keyword_patents": [], "index_only_patents": [stale_extra]})

    assert stored == [], "an abstract-only record overwrote the enriched vector"


def test_enriched_candidates_are_always_reindexed():
    """The inverse: a candidate carries full text this time, so rewriting it can
    only improve the stored vector, even if the id is already present."""
    patent = PatentRecord(id="US1000043A", title="UV sterilizer", abstract="Short.")
    store_patents([patent])
    assert known_ids([patent]) == {"US1000043A"}

    enriched = PatentRecord(
        id="US1000043A", title="UV sterilizer", abstract="Short.",
        full_text="Now with a full description of the sterilization mechanism. " * 20,
    )
    stored: list[str] = []
    with patch("search.vector_store.embed_texts", side_effect=lambda texts: stored.extend(texts) or [[1.0] * 32] * len(texts)):
        index_patents_node({"keyword_patents": [enriched], "index_only_patents": []})

    assert len(stored) == 1
    assert "full description" in stored[0]


def test_known_ids_on_a_cold_collection_is_empty_not_an_error():
    assert known_ids(_corpus(2)) == set()
