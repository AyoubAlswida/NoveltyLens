from types import SimpleNamespace

import pytest

from deployment import ConfigError, MissingCredential, Provider, require_llm_credentials, resolve_target


def settings():
    return SimpleNamespace(llm_api_url='https://api.fuse.humainaic.com/v1/chat/completions',
                           llm_model='deepseek-ai/DeepSeek-V4.1-Flash',
                           fuse_api_key='fuse-test', hf_token='wrong-hf', llm_api_key='wrong-key')


def test_fuse_uses_its_own_credential_and_payload(monkeypatch):
    import llm
    s = settings()
    target = resolve_target(s, {'DEPLOYMENT_PROFILE': 'fuse'})
    assert target.provider is Provider.FUSE
    assert require_llm_credentials(target, s) == 'fuse-test'
    monkeypatch.setenv('OPENAI_REASONING_EFFORT', 'medium')
    payload = llm.build_chat_payload(target, [{'role': 'user', 'content': 'JSON'}],
                                     max_tokens=100, temperature=0)
    assert 'reasoning_effort' not in payload
    assert payload['max_tokens'] == 32100
    assert target.chat_dialect.minimum_timeout_seconds == 300
    s.fuse_api_key = ''
    with pytest.raises(MissingCredential):
        require_llm_credentials(target, s)


@pytest.mark.parametrize('url', ['http://api.fuse.humainaic.com/v1/chat/completions',
    'https://other.example/v1/chat/completions',
    'https://api.fuse.humainaic.com/v1/chat/completions?key=secret'])
def test_fuse_refuses_wrong_endpoint(url):
    s = settings()
    s.llm_api_url = url
    with pytest.raises(ConfigError):
        resolve_target(s, {'DEPLOYMENT_PROFILE': 'fuse'})
