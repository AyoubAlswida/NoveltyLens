from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import embeddings
from config import ConfigError, validate_embedding_configuration


def _settings(api_format: str) -> SimpleNamespace:
    return SimpleNamespace(
        embedding_api_format=api_format,
        embedding_api_url="https://embeddings.example.test",
        embedding_api_key="secret",
        embedding_model="BAAI/bge-large-en-v1.5",
    )


def test_huggingface_protocol_uses_inputs_and_reads_bare_vectors(monkeypatch):
    monkeypatch.setattr(embeddings, "settings", _settings("huggingface"))
    response = Mock()
    response.json.return_value = [[1.0, 2.0], [3.0, 4.0]]
    monkeypatch.setattr(embeddings.requests, "post", Mock(return_value=response))

    assert embeddings._embed_batch(["one", "two"], retries=0) == [
        [1.0, 2.0],
        [3.0, 4.0],
    ]
    assert embeddings.requests.post.call_args.kwargs["json"] == {
        "inputs": ["one", "two"],
        "normalize": True,
        "truncate": True,
    }


def test_huggingface_protocol_accepts_a_flat_single_vector(monkeypatch):
    monkeypatch.setattr(embeddings, "settings", _settings("huggingface"))
    response = Mock()
    response.json.return_value = [1.0, 2.0]
    monkeypatch.setattr(embeddings.requests, "post", Mock(return_value=response))

    assert embeddings._embed_batch(["one"], retries=0) == [[1.0, 2.0]]


def test_huggingface_protocol_rejects_token_level_or_other_nested_output(monkeypatch):
    monkeypatch.setattr(embeddings, "settings", _settings("huggingface"))
    response = Mock()
    response.json.return_value = [[[1.0, 2.0], [3.0, 4.0]]]
    monkeypatch.setattr(embeddings.requests, "post", Mock(return_value=response))

    with pytest.raises(RuntimeError, match="Embedding request failed"):
        embeddings._embed_batch(["one"], retries=0)


def test_openai_protocol_keeps_index_ordering(monkeypatch):
    monkeypatch.setattr(embeddings, "settings", _settings("openai"))
    response = Mock()
    response.json.return_value = {
        "data": [
            {"index": 1, "embedding": [3.0, 4.0]},
            {"index": 0, "embedding": [1.0, 2.0]},
        ]
    }
    monkeypatch.setattr(embeddings.requests, "post", Mock(return_value=response))

    assert embeddings._embed_batch(["one", "two"], retries=0) == [
        [1.0, 2.0],
        [3.0, 4.0],
    ]
    assert embeddings.requests.post.call_args.kwargs["json"] == {
        "model": "BAAI/bge-large-en-v1.5",
        "input": ["one", "two"],
    }


def test_embedding_api_format_must_be_explicitly_supported():
    with pytest.raises(ConfigError, match="EMBEDDING_API_FORMAT"):
        validate_embedding_configuration("automatic")
