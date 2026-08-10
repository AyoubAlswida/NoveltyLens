"""Autouse fixtures shared by every test module.

``compare.py``'s embedding-based ranking, the semantic-search path, and
``graph/pipeline.py``'s ``persist_run`` are all unconditionally on the
pipeline's happy path, so embeddings, Qdrant and Postgres are stubbed here
rather than per-test - the suite's "no credentials, no network calls, no
running services" property (see ``test_pipeline_contract.py``'s module
docstring) would otherwise silently depend on every future test remembering to
opt in.

Patched at the *import* site in each consuming module (``compare.embed_texts``,
``search.vector_store.embed_texts``, ``graph.pipeline.save_report``), not at the
origin module (``embeddings.embed_texts``, ``reports.save_report``): consuming
modules do ``from embeddings import embed_texts``, which binds a local name that
a patch on the origin module would not reach.

``db.connect`` is the one exception - ``graph/pipeline.py`` and ``server.py``
both do ``import db`` and call ``db.connect()`` through the module, so patching
the attribute on ``db`` itself is what reaches them.
"""

from unittest.mock import patch

import pytest

from tests.fakes import (
    FakeQdrantClient,
    FakeReportStore,
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


@pytest.fixture(autouse=True)
def report_store():
    """One in-memory report store per test, standing in for Postgres.

    Yielded so a test can read back what the pipeline stored (see
    ``test_history.py``) without reaching for the patch objects.
    """
    store = FakeReportStore()
    with patch("db.connect", store.connect), \
         patch("graph.pipeline.save_report", store.save_report), \
         patch("server.list_history", store.list_history), \
         patch("server.clear_history", store.clear_history):
        yield store
