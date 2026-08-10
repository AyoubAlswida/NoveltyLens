"""Runs are persisted before their result is sent, and read back by History.

These run against ``tests/fakes.py``'s ``FakeReportStore``, which stands in at
the ``reports.py`` seam - so they pin the *call* contract (what gets stored,
when, in what order) and say nothing about the SQL. See that class's docstring
for what that leaves uncovered.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import server
from auth import issue_token
from graph.pipeline import run_graph_pipeline
from tests.test_pipeline_contract import (
    CLAIMS,
    DESCRIPTION,
    FAKE_SOURCES,
    _fake_llm_chat,
)

AUTH = {"Authorization": f"Bearer {issue_token('testnovelty@gmail.com')}"}


@pytest.fixture
def deterministic():
    with patch("llm._llm_chat", side_effect=_fake_llm_chat), \
         patch("search.search.SOURCES", FAKE_SOURCES):
        yield


def test_a_completed_run_is_stored(deterministic, report_store):
    events = list(run_graph_pipeline(DESCRIPTION, CLAIMS))
    result = events[-1]["data"]

    assert len(report_store.runs) == 1
    stored = report_store.runs[0]
    assert stored["description"] == DESCRIPTION
    assert stored["claims"] == CLAIMS
    assert stored["top_score"] == result["top_score"]
    assert stored["verdict"] == result["verdict"]
    assert stored["concepts"] == result["concepts"]
    assert stored["query_count"] == result["query_count"]
    assert stored["recommendations"] == result["recommendations"]
    assert stored["matches"] == result["patents"]


def test_matches_are_stored_in_displayed_rank_order(deterministic, report_store):
    list(run_graph_pipeline(DESCRIPTION, CLAIMS))
    stored = [match["pct"] for match in report_store.runs[0]["matches"]]
    assert stored == sorted(stored, reverse=True)


def test_the_result_event_is_not_sent_when_the_run_cannot_be_stored(deterministic):
    """The zero-tolerance policy applied to persistence: a report the database
    refused must not be reported to the user as a completed search."""
    with patch("graph.pipeline.save_report", side_effect=RuntimeError("database is down")):
        with pytest.raises(RuntimeError, match="database is down"):
            list(run_graph_pipeline(DESCRIPTION, CLAIMS))


def test_a_run_that_found_nothing_is_still_stored(report_store):
    """"We searched and there was no prior art" is a real answer and belongs in
    History next to the others."""
    empty_source = (("Google Patents", lambda query, size: []),)
    with patch("llm._llm_chat", side_effect=_fake_llm_chat), \
         patch("search.search.SOURCES", empty_source):
        events = list(run_graph_pipeline(DESCRIPTION, CLAIMS))

    assert events[-1]["data"]["patents"] == []
    assert len(report_store.runs) == 1
    assert report_store.runs[0]["matches"] == []
    assert report_store.runs[0]["top_score"] == 0


def test_history_returns_stored_runs_newest_first(deterministic, report_store):
    list(run_graph_pipeline("an older invention " + DESCRIPTION, None))
    list(run_graph_pipeline("a newer invention " + DESCRIPTION, None))

    response = TestClient(server.app, headers=AUTH).get("/api/history")
    assert response.status_code == 200
    entries = response.json()
    assert [entry["description"].split()[1] for entry in entries] == ["newer", "older"]


def test_history_entries_carry_a_real_timestamp_not_the_hardcoded_placeholder(
    deterministic, report_store
):
    """The SSE payload's ``date`` is the literal string "Today". A history entry
    is read back long after the fact, so it ships ``created_at`` instead and
    lets the frontend format it."""
    list(run_graph_pipeline(DESCRIPTION, CLAIMS))
    entry = TestClient(server.app, headers=AUTH).get("/api/history").json()[0]
    assert "date" not in entry
    assert entry["created_at"].startswith("2026-")


def test_history_resolves_each_match_title_from_the_patents_table(
    deterministic, report_store
):
    list(run_graph_pipeline(DESCRIPTION, CLAIMS))
    entry = TestClient(server.app, headers=AUTH).get("/api/history").json()[0]
    assert entry["patents"]
    assert all(match["title"] for match in entry["patents"])


def test_history_is_empty_before_any_run(report_store):
    assert TestClient(server.app, headers=AUTH).get("/api/history").json() == []
