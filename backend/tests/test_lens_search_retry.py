"""Bounded runtime retries for transient Lens search failures."""

from unittest.mock import patch
from types import SimpleNamespace

import pytest
import requests

from search import lens


class Response:
    def __init__(self, status_code: int, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload or {"data": []}
        self.headers = headers or {}

    def json(self):
        return self._payload


@pytest.fixture
def configured_lens():
    with patch.object(lens, "settings", SimpleNamespace(lens_api_key="test-key")):
        yield


def test_retryable_status_is_retried_once_then_succeeds(configured_lens):
    with patch.object(
        lens.requests, "post", side_effect=[Response(503), Response(200)]
    ) as post, patch.object(lens.time, "sleep") as sleep:
        assert lens.search_lens("folding bicycle", 5) == []

    assert post.call_count == 2
    sleep.assert_called_once_with(lens.RETRY_BACKOFF_SECONDS)


@pytest.mark.parametrize(
    "failure",
    [
        requests.exceptions.ConnectionError("reset"),
        requests.exceptions.ChunkedEncodingError("response ended prematurely"),
    ],
)
def test_transport_failure_is_retried_once_then_succeeds(configured_lens, failure):
    with patch.object(
        lens.requests,
        "post",
        side_effect=[failure, Response(200)],
    ) as post, patch.object(lens.time, "sleep"):
        assert lens.search_lens("folding bicycle", 5) == []

    assert post.call_count == 2


def test_deterministic_client_error_is_not_retried(configured_lens):
    with patch.object(lens.requests, "post", return_value=Response(401)) as post:
        with pytest.raises(RuntimeError, match="HTTP 401"):
            lens.search_lens("folding bicycle", 5)

    assert post.call_count == 1


def test_retry_exhaustion_still_fails(configured_lens):
    with patch.object(lens.requests, "post", return_value=Response(429)), patch.object(
        lens.time, "sleep"
    ):
        with pytest.raises(RuntimeError, match="HTTP 429"):
            lens.search_lens("folding bicycle", 5)
