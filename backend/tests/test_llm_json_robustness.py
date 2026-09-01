"""Truncated and malformed LLM responses must fail loudly, never partially.

Both JSON failures seen in production traced to one cause: the response hit
``max_tokens`` and was cut off. The old code could not tell that apart from a
model that simply writes bad JSON, and in one shape it could not tell it apart
from success at all.
"""

import json
from unittest.mock import patch

import pytest

import config
import llm
from deployment import LLMTarget, Profile, Provider
from llm import LLMResponseError, LLMTruncatedError
from models import PatentRecord


@pytest.fixture(autouse=True)
def openai_compatible_target():
    """Declare which provider path these tests exercise.

    They patch `llm.requests.post`, so they have always been about the
    **OpenAI-compatible** transport specifically -- that was implicit while there
    was only one provider and has to be stated now there are two. Under the
    suite's own `test` profile there is no provider at all, and
    `require_llm_credentials` correctly refuses: a test reaching a real call is a
    test about to spend money.

    This declares the target rather than relaxing the refusal. The truncation,
    retry and `response_format` contracts asserted below are unchanged, and
    `tests/test_openai_provider.py` asserts the same contracts on the OpenAI path.
    """
    target = LLMTarget(
        profile=Profile.ARAMCO,
        provider=Provider.OPENAI_COMPATIBLE,
        base_url="https://gateway.invalid/v1/chat/completions",
        model="test-model",
    )
    with patch.object(config, "llm_target", target):
        yield

GOOD_UNDERSTANDING = json.dumps({
    "concepts": ["c1"], "keywords": ["k1"],
    "queries": ["q1", "q2", "q3", "q4", "q5"],
})


def _response(content: str, finish_reason: str = "stop") -> object:
    """A stand-in for the provider's HTTP response object."""
    class FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"choices": [{
                "message": {"content": content},
                "finish_reason": finish_reason,
            }]}

    return FakeResponse()


# --------------------------------------------------------------------------
# Truncation
# --------------------------------------------------------------------------

def test_truncated_response_raises_instead_of_being_parsed():
    """finish_reason='length' means the answer is incomplete, whatever it parses to."""
    with patch("llm.requests.post", return_value=_response('{"concepts": ["a"', "length")):
        with pytest.raises(LLMTruncatedError, match="cut off"):
            llm._llm_chat("prompt", max_tokens=50)


def test_truncation_is_not_retried():
    """The same prompt at the same max_tokens truncates again - retrying only
    spends time and tokens to arrive at the identical failure."""
    calls = 0

    def count_calls(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return _response('{"concepts": [', "length")

    with patch("llm.requests.post", side_effect=count_calls):
        with pytest.raises(LLMTruncatedError):
            llm._llm_chat("prompt", max_tokens=50, retries=2)

    assert calls == 1, f"truncation was retried {calls} times"


def test_truncated_but_syntactically_parseable_response_still_fails():
    """The dangerous case, and the reason `finish_reason` is checked before
    parsing rather than after.

    A response cut off right after a complete element parses cleanly on its own.
    The old greedy `\\{.*\\}` regex did exactly this - verified against the live
    model at max_tokens=120 - and `understand_invention` would have returned two
    search queries instead of five and reported success.
    """
    truncated_but_valid = json.dumps({"concepts": ["c1"], "keywords": ["k1"], "queries": ["q1", "q2"]})
    json.loads(truncated_but_valid)  # it really is valid JSON

    with patch("llm.requests.post", return_value=_response(truncated_but_valid, "length")):
        with pytest.raises(LLMTruncatedError):
            llm.understand_invention("an invention")


def test_analyze_scales_max_tokens_with_patent_count():
    """A flat ceiling is what truncated a real 14-match run."""
    seen: list[int] = []

    def capture(prompt: str, **kwargs: object) -> str:
        seen.append(int(kwargs["max_tokens"]))
        return json.dumps({
            "rationales": [
                {"index": i, "text": f"r{i}", "key_overlap": ["tag a", "tag b"]}
                for i in range(1, PATENT_COUNT + 1)
            ],
            "recommendations": ["rec"],
        })

    PATENT_COUNT = 12
    patents = [PatentRecord(id=f"US{i}", title=f"t{i}") for i in range(PATENT_COUNT)]

    with patch("llm._llm_chat", side_effect=capture):
        llm.analyze_all_matches("an invention", patents)

    assert seen[0] > 3200, f"12 patents should ask for more than the old flat 3200, got {seen[0]}"


# --------------------------------------------------------------------------
# Malformed (but complete) responses
# --------------------------------------------------------------------------

def test_malformed_json_is_retried_then_fails():
    """Re-asking is legitimate; repairing would not be. If every attempt is
    unparseable the request fails rather than guessing at the content."""
    attempts = 0

    def always_broken(*args: object, **kwargs: object) -> str:
        nonlocal attempts
        attempts += 1
        return '{"concepts": ["a" "b"]}'  # missing comma

    with patch("llm._llm_chat", side_effect=always_broken):
        with pytest.raises(LLMResponseError, match="malformed JSON"):
            llm._llm_json("prompt", max_tokens=100, temperature=0.0, timeout=10)

    assert attempts == llm.JSON_ATTEMPTS


def test_malformed_first_attempt_recovers_on_retry():
    responses = ['{"queries": ["q1" ', GOOD_UNDERSTANDING]

    with patch("llm._llm_chat", side_effect=lambda *a, **k: responses.pop(0)):
        analysis = llm.understand_invention("an invention")

    assert analysis.queries == ["q1", "q2", "q3", "q4", "q5"]


def test_retry_uses_a_nonzero_temperature():
    """Re-asking at temperature 0.0 resamples the identical broken output, which
    wastes the attempt. Only the retry is nudged; the first call is unchanged."""
    temperatures: list[float] = []
    responses = ['not json at all', GOOD_UNDERSTANDING]

    def capture(prompt: str, **kwargs: object) -> str:
        temperatures.append(float(kwargs["temperature"]))
        return responses.pop(0)

    with patch("llm._llm_chat", side_effect=capture):
        llm.understand_invention("an invention")

    assert temperatures[0] == 0.0, "the first attempt must honor the caller's temperature"
    assert temperatures[1] >= llm.RETRY_TEMPERATURE


def test_json_object_response_format_is_requested():
    """Constrained decoding is what removes the unescaped-quote and stray-prose
    failure modes at the source, rather than asking the prompt nicely."""
    captured: dict = {}

    def capture(url: str, **kwargs: object) -> object:
        captured.update(kwargs.get("json", {}))
        return _response(GOOD_UNDERSTANDING)

    with patch("llm.requests.post", side_effect=capture):
        llm._llm_chat("prompt")

    assert captured.get("response_format") == {"type": "json_object"}


def test_markdown_fences_are_still_tolerated():
    fenced = f"```json\n{GOOD_UNDERSTANDING}\n```"
    with patch("llm._llm_chat", return_value=fenced):
        assert llm.understand_invention("an invention").queries[0] == "q1"
