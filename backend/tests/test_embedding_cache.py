"""The embedding cache saves exact repeats without changing vector identity."""

from unittest.mock import patch

import embeddings


def test_embed_texts_embeds_only_unique_cache_misses_and_restores_order():
    cached_vector = [1.0, 0.0]
    fresh_vector = [0.0, 1.0]

    with patch("embeddings.get_cached_embeddings", return_value={0: cached_vector}), \
         patch("embeddings._embed_batch", return_value=[fresh_vector]) as provider, \
         patch("embeddings.put_cached_embeddings") as store:
        result = embeddings.embed_texts(["cached", "new", "new"])

    provider.assert_called_once_with(["new"])
    store.assert_called_once_with(["new"], [fresh_vector])
    assert result == [cached_vector, fresh_vector, fresh_vector]


def test_embed_texts_makes_no_provider_call_when_everything_is_cached():
    with patch(
        "embeddings.get_cached_embeddings",
        return_value={0: [1.0, 0.0], 1: [0.0, 1.0]},
    ), patch("embeddings._embed_batch") as provider, \
         patch("embeddings.put_cached_embeddings") as store:
        result = embeddings.embed_texts(["one", "two"])

    provider.assert_not_called()
    store.assert_not_called()
    assert result == [[1.0, 0.0], [0.0, 1.0]]
