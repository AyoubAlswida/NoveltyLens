"""Autouse fixtures shared by every test module.

``compare.py``'s embedding-based ranking and the semantic-search path are
unconditionally on the pipeline's happy path, so embeddings and Qdrant are
stubbed here rather than per-test - the suite's "no credentials, no network
calls" property (see ``test_pipeline_contract.py``'s module docstring) would
otherwise silently depend on every future test remembering to opt in.

Patched at the *import* site in each consuming module (``compare.embed_texts``,
``search.vector_store.embed_texts``), not at the origin module
(``embeddings.embed_texts``): consuming modules do
``from embeddings import embed_texts``, which binds a local name that a patch on
the origin module would not reach.
"""

from unittest.mock import patch

import pytest

from tests.fakes import (
    FakeQdrantClient,
    fake_embed_texts,
    fake_epo_enrich_with_full_text,
    fake_google_enrich_with_full_text,
)


@pytest.fixture(autouse=True)
def stub_semantic_layer():
    with patch("compare.embed_texts", side_effect=fake_embed_texts), \
         patch("search.vector_store.embed_texts", side_effect=fake_embed_texts), \
         patch("search.vector_store.client", FakeQdrantClient()), \
         patch("graph.searching.enrich_epo_full_text", side_effect=fake_epo_enrich_with_full_text), \
         patch("graph.searching.enrich_with_full_text", side_effect=fake_google_enrich_with_full_text):
        yield
