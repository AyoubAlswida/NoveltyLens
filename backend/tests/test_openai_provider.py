"""The OpenAI provider path inside `_llm_chat`.

**The contract does not change by provider.** Spec §14.1 is categorical that
validation strictness does not vary, so the truncation rule, the retry rule, the
`response_format` pass-through and the failure behaviour asserted here are the
same ones `tests/test_llm_json_robustness.py` asserts on the OpenAI-compatible
path. A provider-specific relaxation would be the unlabelled degradation the
zero-tolerance policy forbids.

**No outbound call is made.** The SDK client is stubbed at
`llm._openai_client`, which is the module's only network boundary.
"""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import config
import llm
from deployment import LLMTarget, MissingCredential, Profile, Provider
from llm import LLMResponseError, LLMTruncatedError

OPENAI = LLMTarget(
    Profile.LOCAL, Provider.OPENAI, "https://api.openai.com/v1", "gpt-4o-2024-08-06"
)


def _completion(content: str, *, finish_reason: str = "stop", refusal=None, model="gpt-4o-2024-08-06"):
    return SimpleNamespace(
        model=model,
        system_fingerprint="fp_test",
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(content=content, refusal=refusal),
            )
        ],
    )


class FakeClient:
    """A stand-in for `openai.OpenAI`, recording what it was asked for."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **payload):
        self.calls.append(payload)
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def openai_profile():
    """Run these tests against the `local`/OpenAI target with a stub credential."""
    with patch.object(config, "llm_target", OPENAI), \
         patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test-not-real"}, clear=False):
        yield


def _run(responses, **kwargs):
    client = FakeClient(responses)
    with patch.object(llm, "_openai_client", return_value=client):
        result = llm._llm_chat("prompt", **kwargs)
    return result, client


# --- the request the application actually sends ----------------------------


def test_the_configured_model_is_the_one_requested(openai_profile):
    _, client = _run([_completion("{}")])
    assert client.calls[0]["model"] == "gpt-4o-2024-08-06"


def test_a_strict_json_schema_response_format_is_passed_through_unchanged(openai_profile):
    """The §10 schema is the contract. Nothing here rewrites, relaxes or
    reinterprets it for the provider."""
    from verification.schema import provider_response_format

    fmt = provider_response_format()
    _, client = _run([_completion("{}")], response_format=fmt)
    sent = client.calls[0]["response_format"]
    assert sent == fmt
    assert sent["json_schema"]["strict"] is True
    assert sent["json_schema"]["schema"]["additionalProperties"] is False


def test_the_system_user_split_is_preserved(openai_profile):
    """The injection defence: untrusted passages must sit in a different message
    from the rules governing them."""
    _, client = _run([_completion("{}")], system="RULES", json_object=False)
    messages = client.calls[0]["messages"]
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[0]["content"] == "RULES"


def test_json_object_is_requested_when_no_explicit_format_is_given(openai_profile):
    _, client = _run([_completion("{}")])
    assert client.calls[0]["response_format"] == {"type": "json_object"}


# --- truncation and refusal ------------------------------------------------


def test_truncation_raises_and_is_never_retried(openai_profile):
    """Same rule as every other provider: the same prompt at the same ceiling
    truncates again, so a retry spends tokens to reach the identical failure."""
    client = FakeClient([_completion('{"a": ', finish_reason="length")])
    with patch.object(llm, "_openai_client", return_value=client):
        with pytest.raises(LLMTruncatedError, match="cut off"):
            llm._llm_chat("prompt", max_tokens=50)
    assert len(client.calls) == 1


def test_a_refusal_is_not_parsed_as_content(openai_profile):
    """A refusal is a real answer. Treating it as a malformed response would
    retry it into one."""
    client = FakeClient([_completion("", refusal="I cannot help with that")])
    with patch.object(llm, "_openai_client", return_value=client):
        with pytest.raises(LLMResponseError, match="refused"):
            llm._llm_chat("prompt")


# --- failure behaviour: no fallback ----------------------------------------


def test_a_deterministic_4xx_is_not_retried(openai_profile):
    """A bad request, an unsupported `response_format` or an unknown model does
    not become valid on the second attempt, and for 401/403 retrying hammers an
    auth endpoint."""
    from openai import APIStatusError

    error = APIStatusError(
        "Bad Request",
        response=SimpleNamespace(status_code=400, headers={}, request=None),
        body=None,
    )
    client = FakeClient([error])
    with patch.object(llm, "_openai_client", return_value=client):
        with pytest.raises(RuntimeError) as caught:
            llm._llm_chat("prompt")
    assert len(client.calls) == 1
    assert "400" in str(caught.value)


def test_a_transient_failure_is_retried_and_then_fails(openai_profile):
    from openai import APIConnectionError

    errors = [APIConnectionError(request=None) for _ in range(3)]
    client = FakeClient(errors)
    with patch.object(llm, "_openai_client", return_value=client), \
         patch("llm.time.sleep"):
        with pytest.raises(RuntimeError, match="after 3 attempts"):
            llm._llm_chat("prompt")
    assert len(client.calls) == 3


def test_a_transient_failure_that_recovers_succeeds(openai_profile):
    from openai import APIConnectionError

    client = FakeClient([APIConnectionError(request=None), _completion('{"ok": true}')])
    with patch.object(llm, "_openai_client", return_value=client), \
         patch("llm.time.sleep"):
        assert json.loads(llm._llm_chat("prompt"))["ok"] is True


def test_a_failing_model_is_never_silently_swapped_for_another(openai_profile):
    """**No automatic fallback**, to another model or another provider. Every
    attempt names the configured model."""
    from openai import APIConnectionError

    client = FakeClient([APIConnectionError(request=None) for _ in range(3)])
    with patch.object(llm, "_openai_client", return_value=client), \
         patch("llm.time.sleep"):
        with pytest.raises(RuntimeError):
            llm._llm_chat("prompt")
    assert {call["model"] for call in client.calls} == {"gpt-4o-2024-08-06"}


def test_no_module_lists_fallback_models():
    """Source contract: a fallback chain is a list of models somewhere."""
    import ast
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "llm.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.List, ast.Tuple)):
            values = [
                element.value
                for element in node.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            ]
            model_like = [v for v in values if "gpt-" in v or "llama" in v.lower()]
            assert len(model_like) < 2, f"a model fallback list exists: {model_like}"


# --- credentials never leak ------------------------------------------------


def test_the_key_never_appears_in_a_failure_message(openai_profile):
    from openai import APIStatusError

    error = APIStatusError(
        "401 Unauthorized for https://api.openai.com/v1/chat/completions?api_key=sk-live-SECRET",
        response=SimpleNamespace(status_code=401, headers={}, request=None),
        body=None,
    )
    client = FakeClient([error])
    with patch.object(llm, "_openai_client", return_value=client):
        with pytest.raises(RuntimeError) as caught:
            llm._llm_chat("prompt")
    message = str(caught.value)
    assert "sk-live-SECRET" not in message
    assert "401" in message, "the status survives; it is the actionable fact"


def test_no_credential_is_hard_coded_in_the_llm_module():
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "llm.py").read_text(encoding="utf-8")
    for marker in ("sk-", "hf_", "org-"):
        assert marker not in source, f"a literal credential shape appears: {marker!r}"


def test_the_local_profile_refuses_without_a_key():
    with patch.object(config, "llm_target", OPENAI), \
         patch.dict("os.environ", {"OPENAI_API_KEY": ""}, clear=False):
        with pytest.raises(MissingCredential, match="OPENAI_API_KEY"):
            llm._llm_chat("prompt")


# --- the abstraction is not bypassed ---------------------------------------


def test_no_workflow_or_graph_module_imports_the_openai_sdk():
    """Every caller goes through `llm._llm_chat`. A node reaching for the SDK
    directly would be a second, unvalidated request path."""
    from pathlib import Path

    backend = Path(__file__).resolve().parent.parent
    offenders = []
    for path in list((backend / "graph").rglob("*.py")) + list(
        (backend / "workflow").rglob("*.py")
    ) + list((backend / "evidence").rglob("*.py")):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith(("import openai", "from openai")):
                offenders.append(f"{path.relative_to(backend)}: {stripped}")
    assert offenders == [], f"the LLM abstraction is bypassed: {offenders}"


def test_the_verifier_still_goes_through_the_shared_abstraction():
    import verification.verify as verify

    assert verify._llm_chat is llm._llm_chat


# --- search-only behaviour is unchanged ------------------------------------


def test_the_search_pipeline_is_untouched_by_the_provider_change():
    """The amendment changed which provider development talks to. It did not
    change what a search does, and admission's capability check is not on the
    search path."""
    import ast
    from pathlib import Path

    backend = Path(__file__).resolve().parent.parent
    pipeline = (backend / "graph" / "pipeline.py").read_text(encoding="utf-8")
    called = set()
    for node in ast.walk(ast.parse(pipeline)):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Name):
                called.add(target.id)
            elif isinstance(target, ast.Attribute):
                called.add(target.attr)
    assert "require_capability" not in called
    assert "validate_admission_policies" not in called
