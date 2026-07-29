"""Description-level report cache: an exact resubmission of the same
invention (within REPORT_TTL) reuses the stored report instead of
re-running the pipeline -- zero LLM calls, zero provider calls.

Patches server.connect directly, overriding conftest's autouse stub (which
hands out a fresh FakePostgresDB per call): this test needs the SECOND
_run_pipeline call to see what the FIRST one persisted, so both must share
one instance.
"""

import json
from contextlib import contextmanager
from unittest.mock import patch

from models import PatentRecord
from server import _run_pipeline
from tests.fakes import FakePostgresDB
from tests.test_pipeline_parity import CORE_PATENT_FIELDS

DESCRIPTION = "A UV water sterilizer for a portable drinking bottle."
CORPUS = [PatentRecord(id="US1000001A", title="UV sterilizer", source="Google Patents")]


def _fake_llm_chat(prompt: str, **kwargs: object) -> str:
    if "PatentScout AI" in prompt:
        return json.dumps({"concepts": ["water sterilization"], "keywords": ["UV-C"], "queries": ["uv sterilizer query"]})
    if "expert patent examiner" in prompt:
        return json.dumps({"rationales": [{"index": 1, "text": "Deterministic rationale."}], "recommendations": ["Deterministic recommendation."]})
    raise AssertionError(f"unexpected prompt: {prompt[:80]!r}")


def test_identical_resubmission_reuses_the_stored_report():
    db = FakePostgresDB()

    @contextmanager
    def shared_connect():
        yield db

    calls = {"llm": 0, "provider": 0}

    def counting_llm_chat(prompt: str, **kwargs: object) -> str:
        calls["llm"] += 1
        return _fake_llm_chat(prompt, **kwargs)

    def counting_provider(query: str, size: int) -> list[PatentRecord]:
        calls["provider"] += 1
        return CORPUS

    with patch("server.connect", shared_connect), \
         patch("llm._llm_chat", side_effect=counting_llm_chat), \
         patch("search.search.SOURCES", (("Fake", counting_provider),)):

        first_events = list(_run_pipeline(DESCRIPTION, None))
        assert first_events[-1]["type"] == "result"
        first_data = first_events[-1]["data"]
        assert calls["llm"] > 0 and calls["provider"] > 0, "first run should genuinely call the LLM and provider"
        llm_calls_after_first = calls["llm"]
        provider_calls_after_first = calls["provider"]

        second_events = list(_run_pipeline(DESCRIPTION, None))

    assert calls["llm"] == llm_calls_after_first, "second run must make zero additional LLM calls"
    assert calls["provider"] == provider_calls_after_first, "second run must make zero additional provider calls"

    # Skips straight to the result -- no queries/search/enrich/compare/analyze
    # progress events, since none of that work actually happened.
    assert [e["type"] for e in second_events] == ["progress", "result"]

    second_data = second_events[-1]["data"]
    # Only the fields the frontend actually reads (CORE_PATENT_FIELDS) are
    # persisted and restored -- a live run's entries also carry
    # full_text_chars (a debug-only field, never part of the contract; see
    # test_pipeline_parity.py's CORE_PATENT_FIELDS comment), which a cache
    # hit legitimately doesn't have since it isn't stored.
    strip = lambda patents: [{k: v for k, v in p.items() if k in CORE_PATENT_FIELDS} for p in patents]
    assert strip(second_data["patents"]) == strip(first_data["patents"])
    assert second_data["top_score"] == first_data["top_score"]
    assert second_data["verdict"] == first_data["verdict"]
    assert second_data["recommendations"] == first_data["recommendations"]
    assert second_data["concepts"] == first_data["concepts"]


def test_different_description_is_a_genuine_miss():
    db = FakePostgresDB()

    @contextmanager
    def shared_connect():
        yield db

    calls = {"llm": 0}

    def counting_llm_chat(prompt: str, **kwargs: object) -> str:
        calls["llm"] += 1
        return _fake_llm_chat(prompt, **kwargs)

    def provider(query: str, size: int) -> list[PatentRecord]:
        return CORPUS

    with patch("server.connect", shared_connect), \
         patch("llm._llm_chat", side_effect=counting_llm_chat), \
         patch("search.search.SOURCES", (("Fake", provider),)):

        list(_run_pipeline(DESCRIPTION, None))
        calls_after_first = calls["llm"]

        list(_run_pipeline("A completely different invention about solar panels.", None))

    assert calls["llm"] > calls_after_first, "a genuinely different description must not hit the cache"
