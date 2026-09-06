"""Semantic text embeddings via an explicitly selected hosted API protocol.

``EMBEDDING_API_FORMAT=openai`` uses ``{"model", "input"}`` and the indexed
OpenAI response shape. ``huggingface`` uses Inference Providers' feature-
extraction contract: ``{"inputs", "normalize", "truncate"}`` and a bare array
of vectors. Neither protocol is guessed from the URL.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from config import settings
from tracing import trace

logger = logging.getLogger(__name__)

BATCH_SIZE = 8
MAX_BATCH_WORKERS = 8
"""Batch size and concurrency for embedding round trips.

Batches are independent HTTP calls against a hosted endpoint, and issuing them
one at a time made latency scale linearly with candidate count: ranking 13
candidates chunks to ~156 texts, which cost 55.6s serially - 45% of a measured
123s request, nearly all of it spent waiting on a socket.

These numbers are measured, not guessed. Benchmarking 96 chunks of ~180 words
against the live endpoint:

    batch=32 workers=1   41.3s     <- the original serial configuration
    batch=32 workers=4   22.4s
    batch=16 workers=1   46.2s
    batch=16 workers=6   19.7s
    batch= 8 workers=8   16.6s     <- current

So the endpoint is **latency-bound, not throughput-bound**: concurrency genuinely
helps, and smaller requests help on top of it.

The first attempt at this used batch=32/workers=4 and measured *slower end to end
than serial*, because a 32-text request under concurrency ran long enough to
exceed the 30s read timeout in ``_embed_batch`` - and every timeout burned the
full 30s before its retry even began. Small batches keep each request
comfortably inside the timeout, which is what makes the concurrency pay off
instead of backfire. **If you raise BATCH_SIZE, raise that timeout with it**, or
you will reintroduce the same failure - and it will present as "parallelism made
things slower" rather than as a timeout."""
# Bounds outbound calls per patent: a 3000-word full-text document chunked at
# 180 words/chunk would be ~17 chunks; capping keeps cost/latency predictable
# for very long documents. Increase if you want full-document coverage on
# large patents at the cost of more embedding calls.
MAX_CHUNKS_PER_DOCUMENT = 12


def _request_body(texts: list[str]) -> dict[str, object]:
    if settings.embedding_api_format == "huggingface":
        return {"inputs": texts, "normalize": True, "truncate": True}
    return {"model": settings.embedding_model, "input": texts}


def _response_vectors(payload: object, expected: int) -> list[list[float]]:
    if settings.embedding_api_format == "huggingface":
        if not isinstance(payload, list):
            raise RuntimeError("Hugging Face embedding response is not an array")
        # The feature-extraction API may flatten a single input to one vector.
        if expected == 1 and payload and all(isinstance(value, (int, float)) for value in payload):
            payload = [payload]
        data = payload
    else:
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise RuntimeError("OpenAI embedding response has no data array")
        # The provider may return a batch out of order; `index` restores the
        # input order before callers zip vectors against patents.
        rows = sorted(payload["data"], key=lambda row: row["index"])
        data = [row["embedding"] for row in rows]

    if len(data) != expected:
        raise RuntimeError(f"Embedding count mismatch: sent {expected}, got {len(data)}")
    if any(not isinstance(vector, list) or not vector for vector in data):
        raise RuntimeError("Embedding response contains an empty or invalid vector")
    if any(
        not isinstance(value, (int, float))
        for vector in data
        for value in vector
    ):
        raise RuntimeError("Embedding response contains a non-numeric vector")
    width = len(data[0])
    if any(len(vector) != width for vector in data):
        raise RuntimeError("Embedding response contains inconsistent vector dimensions")
    return data


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


def _embed_batch(texts: list[str], timeout: int = 60, retries: int = 2) -> list[list[float]]:
    """Embed one batch. A hosted endpoint can 503 while a model cold-starts, so
    this retries with backoff the same way llm.py's chat helper does.

    The timeout is 60s rather than 30s because a retry here is expensive in a way
    a retry usually isn't: the request that times out has already burned the full
    timeout doing work the server may well have completed. Waiting a little
    longer for a slow batch beats paying 30s to throw one away. See BATCH_SIZE.
    """
    if not settings.embedding_api_url:
        raise RuntimeError("EMBEDDING_API_URL is not configured")
    if not settings.embedding_api_key:
        raise RuntimeError("EMBEDDING_API_KEY (or LLM_API_KEY) is not configured")
    trace("embeddings.batch.request", model=settings.embedding_model, batch_size=len(texts))
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = requests.post(
                settings.embedding_api_url,
                headers={"Authorization": f"Bearer {settings.embedding_api_key}"},
                json=_request_body(texts),
                timeout=timeout,
            )
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict) and "error" in payload:
                raise RuntimeError(str(payload["error"]))
            data = _response_vectors(payload, len(texts))
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

    Batches are issued concurrently (``MAX_BATCH_WORKERS``) because each one is
    an independent network round trip. Returns vectors in the same order as
    ``texts`` regardless of which batch finished first - callers zip the result
    against their input (``compare.py`` maps vectors back to owning patents by
    position), so any reordering here would silently misattribute scores.

    A failing batch still raises: ``Executor.map`` re-raises on iteration, so
    one bad batch fails the whole call rather than yielding a short or
    partially-embedded list.
    """
    if not texts:
        return []
    trace("embeddings.embed_texts.start", total_texts=len(texts), batch_size=batch_size)
    batches = [texts[start : start + batch_size] for start in range(0, len(texts), batch_size)]

    if len(batches) == 1:
        vectors = _embed_batch(batches[0])
    else:
        with ThreadPoolExecutor(max_workers=min(len(batches), MAX_BATCH_WORKERS)) as executor:
            # map() preserves input order; list() forces the first failure to surface here.
            vectors = [vector for batch in executor.map(_embed_batch, batches) for vector in batch]

    trace("embeddings.embed_texts.done", total_vectors=len(vectors))
    return vectors
