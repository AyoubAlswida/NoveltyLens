"""Semantic vector storage using Qdrant."""

from uuid import uuid5, NAMESPACE_DNS


from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    PointStruct,
    VectorParams,
)

from config import settings
from embeddings import chunk_text, embed_texts
from models import PatentRecord

COLLECTION_NAME = "patents"

MAX_INDEX_WORDS = 600
"""Words of each document actually sent to the embedding API.

Sized for BAAI/bge-large-en-v1.5's 512-token limit, past which the endpoint
truncated silently, so everything beyond roughly the first 400 words never
reached the model. Qwen3-Embedding-4B accepts far more (32k tokens), so this cap
is now a deliberate cost/latency bound rather than a model limit - raising it
would change stored vectors and require re-indexing, so it is left alone.
This project's indexed patents average **273,000 characters**
(median 156,000; largest observed 3.2 MB), which meant a 20-patent batch shipped
~5.5 MB over HTTP to compute vectors that only ever reflected each document's
first ~2,000 characters - 99.3% of the payload was uploaded and discarded.
Measured cost: 21.8s of a 123s request, second only to ranking.

600 words is deliberately *above* the model's cap rather than at it: English
averages ~1.3 tokens/word and patent prose runs higher, so 600 words exceeds 512
tokens in practice and the model's own truncation still decides where the text
ends. The vectors this produces are therefore identical to the ones the
untruncated call produced - **no re-indexing is needed** - and the bandwidth
drops by ~98%. Lowering this below ~400 would start changing vectors.

The real fix for retrieval *quality* is chunk-and-average, so an indexed vector
represents the whole patent instead of its first page (``compare.py`` already
chunks properly for scoring). That is a behavior change with a corpus migration
attached, not a performance fix, so it is deliberately not done here."""

MIN_SIMILARITY_SCORE = 0.6
"""Floor on cosine similarity for a Qdrant hit to be returned at all.

Without this, ``query_points`` always returns its ``limit`` nearest neighbors
regardless of how far away they actually are - on a small or unrelated corpus
that means "the least-bad options available," not "genuinely similar," and
those would flow into validation and ranking looking like real candidates.

**This number is stale and has to be re-calibrated.** It was measured against
BAAI/bge-large-en-v1.5 on a live Qdrant holding ~80 water-bottle/UV-sterilization
patents: an unrelated query ("tractor gearbox") scored 0.51-0.56 and closely
related queries scored 0.68-0.76, so 0.6 sat in the gap between the two clusters.
The project now embeds with Qwen3-Embedding-4B, whose scores occupy a different
range entirely, and there is no exact-zero baseline for either model - any two
pieces of English text land above 0, so the threshold has to clear whatever that
model's floor turns out to be rather than sit at 0. Re-run the same probe (an
obviously unrelated query and a few obviously related ones against a populated
collection) and move this into the observed gap before trusting semantic search.
Semantic search is currently disabled in ``graph/pipeline.py``, which is the only
reason a stale threshold is not affecting live results today."""

QUERY_INSTRUCTION = (
    "Instruct: Given a description of an invention, retrieve prior-art patents "
    "describing similar technology\nQuery: "
)
"""Instruction prefix Qwen3-Embedding expects on the *query* side.

Qwen3-Embedding is asymmetric the same way bge was, but its trained format is an
``Instruct:``/``Query:`` pair naming the retrieval task, not a fixed sentence -
so the task description above is part of the contract, not decoration. Documents
are still embedded bare; ``store_patents`` deliberately does not prefix them, and
adding it there would pull every stored vector toward a shared region and flatten
the gap MIN_SIMILARITY_SCORE has to cut through.

**Not yet measured on this project's corpus.** The bge-era prefix it replaced was
backed by a live probe (relevant hits rose ~0.02-0.03, the unrelated tail stayed
flat or fell). Nothing equivalent has been run against Qwen3-Embedding-4B, and
the old numbers do not carry over - they were a different model in a different
space. Re-probe alongside MIN_SIMILARITY_SCORE once there is an indexed corpus
to probe against."""

client = QdrantClient(
    url=settings.qdrant_url,
    api_key=settings.qdrant_api_key or None,
)


def ensure_collection(vector_size: int) -> None:
    """Create the patent collection if it doesn't already exist."""

    collections = client.get_collections().collections

    if any(c.name == COLLECTION_NAME for c in collections):
        return

    client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=VectorParams(
            size=vector_size,
            distance=Distance.COSINE,
        ),
    )


def _index_text(patent: PatentRecord) -> str:
    """The text actually embedded for one patent, capped at ``MAX_INDEX_WORDS``.

    ``chunk_text`` is reused only for its word-boundary splitting; taking the
    first chunk is a truncation, not a chunking strategy. See MAX_INDEX_WORDS
    for why cutting here does not change the resulting vector.
    """
    document = f"{patent.title}\n\n{patent.full_text or patent.abstract}"
    chunks = chunk_text(
        document,
        max_words=MAX_INDEX_WORDS,
        overlap_words=0,
        max_chunks=1,
    )
    return chunks[0] if chunks else document


def _point_id(patent_id: str) -> str:
    """Qdrant point id for a patent. Stable, so re-indexing overwrites."""
    return str(uuid5(NAMESPACE_DNS, patent_id))


def known_ids(patents: list[PatentRecord]) -> set[str]:
    """Return the ``PatentRecord.id`` values already present in the collection.

    Two reasons this exists, both about the ``index_only`` extras:

    - **Cost.** Re-embedding a patent Qdrant already holds spends an embedding
      call to write a vector identical to the one being overwritten.
    - **Quality.** ``index_only`` records are abstract-only; a candidate from an
      earlier request may already be stored with its enriched full text. Blindly
      upserting the extra would overwrite a richer vector with a poorer one, and
      the corpus would silently degrade the more it was used.

    A missing collection is a cold start, not an error: nothing is known yet.
    """
    if not patents:
        return set()

    collections = client.get_collections().collections
    if not any(c.name == COLLECTION_NAME for c in collections):
        return set()

    by_point_id = {_point_id(patent.id): patent.id for patent in patents}
    found = client.retrieve(
        collection_name=COLLECTION_NAME,
        ids=list(by_point_id),
        with_payload=False,
        with_vectors=False,
    )
    return {by_point_id[str(point.id)] for point in found if str(point.id) in by_point_id}


def store_patents(patents: list[PatentRecord]) -> None:
    """Embed patents and store them in Qdrant."""

    if not patents:
        return

    texts = [_index_text(patent) for patent in patents]

    vectors = embed_texts(texts)

    ensure_collection(len(vectors[0]))

    points = []

    for patent, vector in zip(patents, vectors, strict=True):
        points.append(
            PointStruct(
                id=_point_id(patent.id),
                vector=vector,
                payload={
                    "id": patent.id,
                    "title": patent.title,
                    "abstract": patent.abstract,
                    "inventors": patent.inventors,
                    "assignee": patent.assignee,
                    "publication_date": patent.publication_date,
                    "source": patent.source,
                    "url": patent.url,
                    "serpapi_id": patent.serpapi_id,
                    "full_text": patent.full_text,
                },
            )
        )

    client.upsert(
        collection_name=COLLECTION_NAME,
        points=points,
    )


def semantic_search(
    invention_text: str,
    limit: int = 20,
):
    """Return the most semantically similar patents.

    A collection that does not exist yet means nothing has been indexed by any
    earlier request - a legitimate empty state, not an outage - so this returns
    an empty list rather than letting the client's "not found" error surface as
    a failure. ``client.get_collections()`` still raises if Qdrant itself is
    unreachable, and that failure is not caught here.

    The query is embedded with ``QUERY_INSTRUCTION`` prepended; the documents in
    the collection are not. That asymmetry is what bge was trained for - see the
    constant.
    """

    collections = client.get_collections().collections
    if not any(c.name == COLLECTION_NAME for c in collections):
        return []

    vector = embed_texts([QUERY_INSTRUCTION + invention_text])[0]

    # QdrantClient.search() was removed in qdrant-client 1.10+; query_points()
    # is the replacement and wraps the same ScoredPoint list in `.points`.
    return client.query_points(
        collection_name=COLLECTION_NAME,
        query=vector,
        limit=limit,
        with_payload=True,
        score_threshold=MIN_SIMILARITY_SCORE,
    ).points