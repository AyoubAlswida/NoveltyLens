"""Autouse fixtures shared by every test module.

``compare.py``'s embedding-based ranking and the semantic-search path are now
unconditionally on the happy path for both pipelines, so embeddings and Qdrant
are stubbed here rather than per-test - the suite's "no credentials, no network
calls" property (see ``test_pipeline_parity.py``'s module docstring) would
otherwise silently depend on every future test remembering to opt in.

Patched at the *import* site in each consuming module (``compare.embed_texts``,
``search.vector_store.embed_texts``), not at ``embeddings.embed_texts`` itself -
both modules do ``from embeddings import embed_texts``, which binds a local name
that a patch on the origin module would not reach. Same reasoning for
``graph.searching.enrich_epo_full_text`` - EPO's real enrichment keys off
``source``/``id`` alone (unlike Google's, which test fixtures naturally skip by
never setting ``serpapi_id``), so without this stub it makes real calls to
EPO's auth and full-text endpoints during the suite.

``graph.searching.enrich_with_full_text`` (Google's) is stubbed too, even
though the real one is already a test-safe no-op - ``keyword_search_node`` now
drops any candidate that comes out of enrichment with no full text at all, and
without this stub every Google-sourced synthetic test fixture would get
dropped for real (never having had a ``serpapi_id`` to enrich from in the
first place), gutting corpora that were never meant to model that gate.
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
