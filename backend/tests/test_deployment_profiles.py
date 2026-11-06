"""Deployment profiles: declared, never inferred, and fail-closed.

The profile decides which provider the application talks to and which capability
qualifications may be reused. Both are things a machine must not guess at, so the
tests here are mostly about **refusals**: what the configuration will not do.

**The rule that matters most:** the profile is never derived from an endpoint
URL, a hostname, an API key, or a model name. A URL that looks like OpenAI is not
evidence that this is the development machine, and a machine that infers its own
environment is a machine that can be wrong about it silently.
"""

import os
from unittest.mock import patch

import pytest

import deployment
from deployment import (
    ConfigError,
    LLMTarget,
    MissingCredential,
    OPENAI_DEFAULT_BASE_URL,
    OPENAI_DEFAULT_MODEL,
    PROFILE_PROVIDER,
    Profile,
    Provider,
    require_llm_credentials,
    resolve_target,
)


class FakeSettings:
    llm_api_url = "https://gateway.example/v1/chat/completions"
    llm_model = "minimaxm3"
    llm_api_key = "gateway-key"


# --- profile validation ----------------------------------------------------


def test_an_unset_profile_refuses_to_resolve():
    """No default, on purpose: a default would choose the provider and the
    reusable qualifications on the operator's behalf."""
    with pytest.raises(ConfigError) as caught:
        resolve_target(FakeSettings(), environ={})
    assert "DEPLOYMENT_PROFILE" in str(caught.value)
    assert "no default" in str(caught.value)


@pytest.mark.parametrize("value", ["", "   ", "prod", "production", "aramco-dev", "loc"])
def test_an_unknown_or_blank_profile_refuses_to_resolve(value):
    with pytest.raises(ConfigError):
        resolve_target(FakeSettings(), environ={"DEPLOYMENT_PROFILE": value})


@pytest.mark.parametrize("profile", list(Profile))
def test_every_declared_profile_resolves_to_exactly_one_provider(profile):
    """One knob, not two. A separate LLM_PROVIDER setting would be a second thing
    to drift out of step, and "profile says local, provider says gateway" is
    exactly the internal inconsistency this design removes rather than detects."""
    settings = FakeSettings()
    if profile is Profile.FUSE:
        settings.llm_api_url = "https://api.fuse.humainaic.com/v1/chat/completions"
        settings.llm_model = "deepseek-ai/DeepSeek-V4.1-Flash"
    if profile is Profile.HUGGINGFACE:
        settings.llm_api_url = 'https://router.huggingface.co/v1/chat/completions'
        settings.llm_model = 'Qwen/Qwen3-235B-A22B-Instruct-2507:deepinfra'
    target = resolve_target(settings, environ={"DEPLOYMENT_PROFILE": profile.value})
    assert target.provider is PROFILE_PROVIDER[profile]


def test_profile_names_are_case_insensitive_but_not_fuzzy():
    assert (
        resolve_target(FakeSettings(), environ={"DEPLOYMENT_PROFILE": "LOCAL"}).profile
        is Profile.LOCAL
    )
    with pytest.raises(ConfigError):
        resolve_target(FakeSettings(), environ={"DEPLOYMENT_PROFILE": "loc"})


# --- the profile is never inferred -----------------------------------------


def test_an_openai_looking_url_does_not_make_the_profile_local():
    """The core rule. A configuration that *looks* like development is not
    evidence that this is a development machine."""

    class OpenAILooking:
        llm_api_url = "https://api.openai.com/v1/chat/completions"
        llm_model = "gpt-4o-2024-08-06"
        llm_api_key = "sk-something"

    target = resolve_target(OpenAILooking(), environ={"DEPLOYMENT_PROFILE": "aramco"})
    assert target.profile is Profile.ARAMCO
    assert target.provider is Provider.OPENAI_COMPATIBLE


def test_an_openai_api_key_in_the_environment_does_not_change_the_profile():
    target = resolve_target(
        FakeSettings(),
        environ={"DEPLOYMENT_PROFILE": "aramco", "OPENAI_API_KEY": "sk-live"},
    )
    assert target.profile is Profile.ARAMCO
    assert target.model == "minimaxm3"


def test_a_gateway_url_does_not_make_the_profile_aramco():
    target = resolve_target(FakeSettings(), environ={"DEPLOYMENT_PROFILE": "local"})
    assert target.profile is Profile.LOCAL
    assert target.base_url == OPENAI_DEFAULT_BASE_URL
    assert "aramco" not in target.base_url


def test_no_module_derives_the_profile_from_a_url_or_a_key():
    """Source contract. An inference added later would pass every behavioural
    test above by only firing on inputs those tests do not use."""
    import ast
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "deployment.py").read_text(
        encoding="utf-8"
    )
    read = set()
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"get", "getenv"}
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            read.add(node.args[0].value)
    # The profile comes from exactly one variable. Everything else read here is
    # provider configuration selected *after* the profile is known.
    assert "DEPLOYMENT_PROFILE" in read
    assert not {"LLM_API_URL", "SERPAPI_KEY", "HF_TOKEN"} & read


# --- provider/model configuration validation -------------------------------


def test_local_defaults_to_the_official_openai_endpoint_and_a_pinned_model():
    target = resolve_target(FakeSettings(), environ={"DEPLOYMENT_PROFILE": "local"})
    assert target.base_url == OPENAI_DEFAULT_BASE_URL
    assert target.model == OPENAI_DEFAULT_MODEL
    assert target.model_is_pinned, (
        "the default names a snapshot, not a floating alias: an alias can be "
        "moved under a capability record without any configuration changing"
    )


def test_the_local_model_is_configurable():
    target = resolve_target(
        FakeSettings(),
        environ={"DEPLOYMENT_PROFILE": "local", "OPENAI_MODEL": "gpt-4.1-2025-04-14"},
    )
    assert target.model == "gpt-4.1-2025-04-14"


def test_an_empty_local_model_is_refused_rather_than_defaulted():
    with pytest.raises(ConfigError):
        resolve_target(
            FakeSettings(),
            environ={"DEPLOYMENT_PROFILE": "local", "OPENAI_MODEL": "   "},
        )


@pytest.mark.parametrize("missing", ["llm_api_url", "llm_model"])
def test_the_aramco_profile_refuses_incomplete_gateway_settings(missing):
    """Internally inconsistent: the profile selects the provider, but it cannot
    supply the provider's address."""

    class Incomplete(FakeSettings):
        pass

    setattr(Incomplete, missing, "")
    with pytest.raises(ConfigError) as caught:
        resolve_target(Incomplete(), environ={"DEPLOYMENT_PROFILE": "aramco"})
    assert missing.upper() in str(caught.value)


@pytest.mark.parametrize(
    "model,pinned",
    [
        ("gpt-4o-2024-08-06", True),
        ("gpt-4.1-2025-04-14", True),
        ("meta-llama/Llama-3.1-8B-Instruct:novita", True),
        ("gpt-4o", False),
        ("minimaxm3", False),
    ],
)
def test_pinned_models_are_recognised_as_pinned(model, pinned):
    target = LLMTarget(Profile.LOCAL, Provider.OPENAI, OPENAI_DEFAULT_BASE_URL, model)
    assert target.model_is_pinned is pinned


# --- no credential leaks ---------------------------------------------------


def test_the_target_object_cannot_carry_a_credential():
    """Structural. This object is rendered into capability artifacts, log lines
    and error messages, so it must not have a field a key could land in."""
    import dataclasses

    fields = {field.name for field in dataclasses.fields(LLMTarget)}
    assert fields == {"profile", "provider", "base_url", "model"}
    assert not any("key" in name or "token" in name or "secret" in name for name in fields)


def test_the_endpoint_is_parsed_so_userinfo_and_query_keys_cannot_survive():
    hostile = "https://user:sup3rsecret@gateway.example/v1/chat?api_key=SECRET123"
    target = LLMTarget(Profile.ARAMCO, Provider.OPENAI_COMPATIBLE, hostile, "m")
    import json

    rendered = json.dumps(target.endpoint)
    assert "sup3rsecret" not in rendered
    assert "SECRET123" not in rendered
    assert target.endpoint["host"] == "gateway.example"


def test_a_targets_repr_carries_no_credential():
    target = LLMTarget(
        Profile.ARAMCO,
        Provider.OPENAI_COMPATIBLE,
        "https://user:sup3rsecret@gateway.example/v1",
        "m",
    )
    assert "sup3rsecret" not in repr(target)


# --- credentials are required where they are used --------------------------


def test_local_without_an_openai_key_refuses_and_names_the_setting():
    target = LLMTarget(Profile.LOCAL, Provider.OPENAI, OPENAI_DEFAULT_BASE_URL, "gpt-4o")
    with patch.dict("os.environ", {"OPENAI_API_KEY": ""}, clear=False):
        with pytest.raises(MissingCredential) as caught:
            require_llm_credentials(target, FakeSettings())
    assert "OPENAI_API_KEY" in str(caught.value)


def test_local_never_falls_back_to_another_providers_credential():
    """**No fallback.** A request authenticated with the gateway's key against
    OpenAI is a request to the wrong place, and the error it produces is far
    harder to read than the refusal."""
    target = LLMTarget(Profile.LOCAL, Provider.OPENAI, OPENAI_DEFAULT_BASE_URL, "gpt-4o")
    with patch.dict("os.environ", {"OPENAI_API_KEY": ""}, clear=False):
        with pytest.raises(MissingCredential):
            require_llm_credentials(target, FakeSettings())  # llm_api_key is set


def test_the_stub_provider_has_no_credential_and_says_why():
    target = LLMTarget(Profile.TEST, Provider.STUB, "", "stub")
    with pytest.raises(MissingCredential) as caught:
        require_llm_credentials(target, FakeSettings())
    assert "about to make a real call" in str(caught.value)


def test_openai_account_scoping_comes_only_from_the_environment():
    with patch.dict(
        "os.environ",
        {"OPENAI_ORG_ID": "org-abc", "OPENAI_PROJECT_ID": "proj-xyz"},
        clear=False,
    ):
        assert deployment.openai_client_options() == {
            "organization": "org-abc",
            "project": "proj-xyz",
        }
    with patch.dict("os.environ", {"OPENAI_ORG_ID": "", "OPENAI_PROJECT_ID": ""}, clear=False):
        assert deployment.openai_client_options() == {}


def test_no_credential_is_hard_coded_in_the_deployment_module():
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "deployment.py").read_text(
        encoding="utf-8"
    )
    for marker in ("sk-", "Bearer "):
        assert marker not in source, f"a literal credential shape appears: {marker!r}"
    import re
    assert not re.search(r'hf_[A-Za-z0-9]{20,}', source), 'a literal HF credential appears'


# --- the suite's own profile -----------------------------------------------


def test_the_test_suite_runs_under_the_test_profile():
    """`backend/conftest.py` pins it. Under `test` there is no provider at all,
    so a boundary somebody forgot to stub raises instead of reaching a paid API."""
    import config

    assert config.llm_target.profile is Profile.TEST
    assert config.llm_target.provider is Provider.STUB


def test_an_explicit_profile_is_not_overridden_by_dotenv():
    """`config.load_dotenv(override=True)` is deliberate for API keys and URLs,
    and DEPLOYMENT_PROFILE is deliberately exempt from it.

    The profile is a process-level declaration of *which environment this is*,
    not a config value, and what it gates is whether a capability qualification
    from elsewhere may be reused. A `.env` on a developer's disk must not be able
    to move a run into another profile behind an explicit
    `DEPLOYMENT_PROFILE=... python ...`.

    Found the moment it mattered: adding `DEPLOYMENT_PROFILE=local` to a real
    `.env` silently moved the whole test suite out of the `test` profile and into
    `local`, where an unstubbed boundary would reach a paid API.
    """
    import subprocess
    import sys
    from pathlib import Path

    backend = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, "-c", "import config; print(config.llm_target.profile.value)"],
        cwd=backend,
        capture_output=True,
        text=True,
        env={**os.environ, "DEPLOYMENT_PROFILE": "aramco"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "aramco"
