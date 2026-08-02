"""P2.2: an exact repeat of a query must skip every provider call.

Uses FakePostgresDB directly (tests/fakes.py) rather than the
server.connect-patching autouse fixture, since this test asserts on the
cache's own read/write behavior, not on _run_pipeline.
"""

from models import PatentRecord
from search.search import search_patents
from tests.fakes import FakePostgresDB

RECORD = PatentRecord(id="US1234567A", title="Widget", abstract="A widget.", source="Google Patents")


def _once_only_provider(calls: list[str]):
    """Raises if called a second time for the same query -- a cache bug
    that still returns correct data would otherwise pass silently."""
    seen: set[str] = set()

    def provider(query: str, size: int) -> list[PatentRecord]:
        if query in seen:
            raise AssertionError(f"provider called twice for query {query!r} -- cache did not skip it")
        seen.add(query)
        calls.append(query)
        return [RECORD]

    return provider


def test_repeat_query_skips_the_live_provider(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr("search.search.SOURCES", (("Fake", _once_only_provider(calls)),))

    db = FakePostgresDB()
    first = search_patents(db, ["widget with a sensor"], results_per_query=4)
    second = search_patents(db, ["widget with a sensor"], results_per_query=4)

    assert calls == ["widget with a sensor"], "provider should only be hit once"
    assert [p.id for p in first] == [p.id for p in second] == ["US1234567A"]


def test_cache_persists_full_text_written_after_the_initial_cache_write():
    """search_patents caches patents before enrichment runs (server.py calls
    enrich_with_full_text after search_patents returns), so a naive upsert
    would always cache an empty full_text. store_enriched_patents' later
    write must not be clobbered by search_patents caching the same patent
    again for an unrelated query."""
    from cache import store_enriched_patents

    db = FakePostgresDB()

    def provider(query: str, size: int) -> list[PatentRecord]:
        return [RECORD]

    import search.search as search_module
    orig_sources = search_module.SOURCES
    search_module.SOURCES = (("Fake", provider),)
    try:
        search_patents(db, ["first query"], results_per_query=4)
        enriched = PatentRecord(id="US1234567A", title="Widget", abstract="A widget.",
                                 source="Google Patents", full_text="full claims and description text")
        store_enriched_patents(db, [enriched])

        # A second, different query re-caching the same patent ID with an
        # empty full_text (the pre-enrichment state) must not blank it out.
        search_patents(db, ["second query"], results_per_query=4)

        assert db.patents["US1234567A"]["full_text"] == "full claims and description text"
    finally:
        search_module.SOURCES = orig_sources
