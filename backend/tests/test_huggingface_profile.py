"""HF is explicit and never inherits GPT credentials or its inference settings."""
from types import SimpleNamespace
from unittest.mock import patch, Mock
import pytest
import requests
from deployment import Profile, Provider, ConfigError, MissingCredential, resolve_target, require_llm_credentials


def settings():
    return SimpleNamespace(llm_api_url='https://router.huggingface.co/v1/chat/completions',
        llm_model='Qwen/Qwen3-235B-A22B-Instruct-2507:deepinfra',hf_token='hf-test',
        llm_api_key='wrong-provider-key')


def test_hf_target_uses_only_hf_token(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','wrong-openai-key')
    s=settings(); target=resolve_target(s,{'DEPLOYMENT_PROFILE':'huggingface'})
    assert target.profile is Profile.HUGGINGFACE and target.provider is Provider.HUGGINGFACE
    assert require_llm_credentials(target,s)=='hf-test'
    s.hf_token=''
    with pytest.raises(MissingCredential): require_llm_credentials(target,s)


@pytest.mark.parametrize('url',['https://api.openai.com/v1/chat/completions',
    'https://other.example/v1/chat/completions','http://router.huggingface.co/v1/chat/completions'])
def test_hf_refuses_other_hosts(url):
    s=settings();s.llm_api_url=url
    with pytest.raises(ConfigError): resolve_target(s,{'DEPLOYMENT_PROFILE':'huggingface'})


def test_hf_ignores_openai_only_reasoning_setting(monkeypatch):
    import llm
    monkeypatch.setenv('OPENAI_REASONING_EFFORT','medium')
    payload=llm.build_chat_payload(resolve_target(settings(),{'DEPLOYMENT_PROFILE':'huggingface'}),
        [{'role':'user','content':'JSON please'}],max_tokens=100,temperature=.2)
    assert 'reasoning_effort' not in payload
    assert payload['max_tokens']==100


def test_hf_billing_error_does_not_retry_or_use_openai():
    import llm
    response=requests.Response();response.status_code=402
    with patch.object(llm.config,'llm_target',resolve_target(settings(),{'DEPLOYMENT_PROFILE':'huggingface'})),\
         patch.object(llm,'settings',settings()),patch.object(llm.requests,'post',return_value=response) as post,\
         patch.object(llm,'_openai_chat') as openai:
        with pytest.raises(RuntimeError,match='402'): llm._llm_chat('Return JSON',max_tokens=50)
    assert post.call_count==1
    assert post.call_args.kwargs['headers']['Authorization']=='Bearer hf-test'
    openai.assert_not_called()


def test_embedding_billing_error_does_not_retry():
    import embeddings
    response=requests.Response();response.status_code=402
    with patch.object(embeddings.requests,'post',return_value=response) as post:
        with pytest.raises(RuntimeError,match='402'): embeddings._embed_batch(['test'])
    assert post.call_count==1
