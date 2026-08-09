"""The SerpAPI key must never reach a user-visible error message.

SerpAPI authenticates by query parameter, and ``requests``' ``HTTPError``
message embeds the full request URL. Nothing in this project treats provider
errors as internal: ``search_all_providers_strict`` interpolates them into
``PatentSearchError``, and ``server.py``'s wrapper sends ``str(error)`` to the
browser as the SSE ``error`` detail. So the default ``raise_for_status()``
would print a live credential into the user's devtools console.

This is not hypothetical - it happened, on an ordinary exhausted-quota 429.
"""

import json
from unittest.mock import patch

import requests

from search.google import _raise_for_status

SECRET = "sk-live-DO-NOT-LEAK-abc123"
SERPAPI_ERROR_URL = (
    f"https://serpapi.com/search?engine=google_patents&q=widget&api_key={SECRET}"
)


def _response(status_code: int) -> requests.Response:
    response = requests.Response()
    response.status_code = status_code
    response.url = SERPAPI_ERROR_URL
    return response


def test_http_error_message_excludes_the_api_key():
    try:
        _raise_for_status(_response(429), "search for 'widget'")
    except RuntimeError as error:
        assert SECRET not in str(error)
        assert "429" in str(error), "the status code is what makes this actionable"
    else:
        raise AssertionError("a 4xx response must raise")


def test_requests_own_raise_for_status_would_have_leaked_it():
    """Pins *why* the helper exists. If this ever stops leaking, the helper can
    be reconsidered - until then it is load-bearing, not defensive clutter."""
    response = _response(429)
    response.reason = "Too Many Requests"
    try:
        response.raise_for_status()
    except requests.HTTPError as error:
        assert SECRET in str(error), (
            "requests no longer embeds the URL; re-evaluate search.google._raise_for_status"
        )


def test_success_status_does_not_raise():
    _raise_for_status(_response(200), "search for 'widget'")


def test_the_key_does_not_reach_the_sse_error_frame():
    """End to end through the real transport: a failing provider must produce an
    error frame the user can act on and nothing they shouldn't see."""
    from fastapi.testclient import TestClient

    import server

    def broken(query: str, size: int):
        _raise_for_status(_response(429), f"search for {query!r}")

    analysis = json.dumps({
        "concepts": ["c"], "keywords": ["k"], "queries": ["q1"],
    })

    with patch("llm._llm_chat", return_value=analysis), \
         patch("search.search.SOURCES", (("Google Patents", broken),)):
        response = TestClient(server.app).post(
            "/api/analyze/stream", json={"description": "a widget", "claims": None}
        )

    body = response.text
    assert "error" in body, "expected the provider failure to surface"
    assert SECRET not in body
