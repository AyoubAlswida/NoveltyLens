"""The SSE frames as the browser actually receives them.

``test_pipeline_contract`` compares the event dicts the generator yields.  This
compares what comes out the other side of ``server.py``'s wrapper: real
``data: {...}\\n\\n`` frames over a real request.  The two failure modes it
covers that the generator-level tests cannot are a node exception escaping as
something other than a single ``error`` frame, and the JSON encoding of the
non-ASCII characters in the progress messages.
"""

import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import server
from tests.test_pipeline_contract import (
    CLAIMS,
    DESCRIPTION,
    FAKE_SOURCES,
    _fake_llm_chat,
)


def _frames(response) -> list[dict]:
    """Parse an SSE body into the event dicts the frontend would see."""
    return [
        json.loads(line[len("data: "):])
        for line in response.text.split("\n\n")
        if line.startswith("data: ")
    ]


@pytest.fixture
def langgraph_client():
    """A client with both external boundaries pinned.

    No pipeline patching: there is one pipeline now, and ``server.py`` calls
    ``run_graph_pipeline`` directly.
    """
    with patch("llm._llm_chat", side_effect=_fake_llm_chat), \
         patch("search.search.SOURCES", FAKE_SOURCES):
        yield TestClient(server.app)


def test_stream_endpoint_emits_wellformed_frames(langgraph_client):
    response = langgraph_client.post(
        "/api/analyze/stream", json={"description": DESCRIPTION, "claims": CLAIMS}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    events = _frames(response)
    assert [e["type"] for e in events].count("result") == 1
    assert events[-1]["type"] == "result"
    assert all(e["type"] in {"progress", "result"} for e in events)


def test_non_ascii_progress_messages_survive_the_wire(langgraph_client):
    """The progress strings carry U+2026 and U+00B7. json.dumps escapes them by
    default; either way the frontend must decode the original characters."""
    response = langgraph_client.post(
        "/api/analyze/stream", json={"description": DESCRIPTION, "claims": CLAIMS}
    )
    messages = [e["message"] for e in _frames(response) if e["type"] == "progress"]
    assert any("…" in m for m in messages), "ellipsis lost in transport"
    assert any("·" in m for m in messages), "middle dot lost in transport"


def test_node_failure_becomes_exactly_one_error_frame(langgraph_client):
    """A raising node must not produce a partial result alongside the error."""
    with patch("llm._llm_chat", side_effect=RuntimeError("LLM request failed")):
        response = langgraph_client.post(
            "/api/analyze/stream", json={"description": DESCRIPTION, "claims": CLAIMS}
        )
    events = _frames(response)
    assert [e["type"] for e in events].count("error") == 1
    assert events[-1]["type"] == "error"
    assert not any(e["type"] == "result" for e in events), "degraded result alongside an error"


def test_json_endpoint_returns_the_result_payload(langgraph_client):
    """POST /api/analyze drains the same generator and returns only the data."""
    response = langgraph_client.post(
        "/api/analyze", json={"description": DESCRIPTION, "claims": CLAIMS}
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "title", "date", "concepts", "patents",
        "top_score", "verdict", "query_count", "recommendations",
    }
    assert body["patents"], "no matches in the result payload"


def test_json_endpoint_surfaces_failure_as_502(langgraph_client):
    with patch("llm._llm_chat", side_effect=RuntimeError("LLM request failed")):
        response = langgraph_client.post(
            "/api/analyze", json={"description": DESCRIPTION, "claims": CLAIMS}
        )
    assert response.status_code == 502
