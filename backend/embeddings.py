"""Semantic text embeddings via the Hugging Face Inference Router.

Reuses the same HF_TOKEN already configured for chat completions in llm.py.
"""

import logging
import time

import requests

from config import settings
from tracing import trace

logger = logging.getLogger(__name__)

EMBEDDING_URL_TEMPLATE = "https://router.huggingface.co/hf-inference/models/{model}/pipeline/feature-extraction"
BATCH_SIZE = 32
# Bounds outbound calls per patent: a 3000-word full-text document chunked at
# 180 words/chunk would be ~17 chunks; capping keeps cost/latency predictable
# for very long documents. Increase if you want full-document coverage on
# large patents at the cost of more embedding calls.
MAX_CHUNKS_PER_DOCUMENT = 12


def chunk_text(text: str, max_words: int = 180, overlap_words: int = 20, max_chunks: int = MAX_CHUNKS_PER_DOCUMENT) -> list[str]:
    """Split text into overlapping word-count windows.

    Overlap avoids losing a match that straddles a chunk boundary. Capped at
    max_chunks (from the start of the document) to bound embedding-call volume
    on very long full-text patents.
    """
    words = text.split()
    if not words:
        return []
    if len(words) <= max_words:
        trace("chunk_text.single_chunk", word_count=len(words))
        return [text.strip()]
    chunks: list[str] = []
    step = max(1, max_words - overlap_words)
    for start in range(0, len(words), step):
        if len(chunks) >= max_chunks:
            break
        chunk_words = words[start : start + max_words]
        if not chunk_words:
            break
        chunks.append(" ".join(chunk_words))
        if start + max_words >= len(words):
            break
    trace("chunk_text.split", word_count=len(words), chunk_count=len(chunks))
    return chunks


def _embed_batch(texts: list[str], timeout: int = 30, retries: int = 2) -> list[list[float]]:
    """Embed one batch. HF Inference Router can 503 while a model cold-starts,
    so this retries with backoff the same way llm.py's chat helper does.
    """
    if not settings.hf_token:
        raise RuntimeError("HF_TOKEN is not configured")
    url = EMBEDDING_URL_TEMPLATE.format(model=settings.embedding_model)
    trace("embeddings.batch.request", model=settings.embedding_model, batch_size=len(texts))
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = requests.post(
                url,
                headers={"Authorization": f"Bearer {settings.hf_token}"},
                json={"inputs": texts},
                timeout=timeout,
            )
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict) and "error" in data:
                raise RuntimeError(str(data["error"]))
            if not isinstance(data, list) or (data and not isinstance(data[0], list)):
                raise RuntimeError(f"Unexpected embedding response shape: {type(data)}")
            trace("embeddings.batch.response", attempt=attempt, vectors=len(data), dims=len(data[0]) if data else 0)
            return data
        except Exception as error:
            last_error = error
            trace("embeddings.batch.attempt_failed", attempt=attempt, error=str(error))
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("Embedding request failed") from last_error


def embed_texts(texts: list[str], batch_size: int = BATCH_SIZE) -> list[list[float]]:
    """Embed a list of texts, batching requests to keep payloads bounded.

    Returns vectors in the same order as ``texts``.
    """
    if not texts:
        return []
    trace("embeddings.embed_texts.start", total_texts=len(texts), batch_size=batch_size)
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        vectors.extend(_embed_batch(batch))
    trace("embeddings.embed_texts.done", total_vectors=len(vectors))
    return vectors