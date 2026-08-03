"""The shared state schema every graph node reads and writes.

This is the vocabulary the whole migration is built on, so two properties matter
more than convenience:

**Field names do not drift from ``backend/models.py``.**  The dataclasses there are
already the normalized shape the pipeline threads around; the state carries those
objects rather than re-flattening them into loose keys.

**There is no error field, and that is deliberate.**  Under the zero-tolerance
policy a node that cannot produce a genuine result raises, which fails the whole
request.  An ``errors: list[str]`` slot would invite the opposite: nodes recording
a problem and letting the graph continue to a degraded answer.  If you find
yourself wanting one, the node should be raising instead.

Phase 3 adds semantic search, fusion and validation.  Room is left for them here
(see "Reserved") so those tickets extend the schema additively.  ``total=False``
means every key is optional to the type checker, so adding a key is never a
breaking change for existing nodes.
"""

from typing import TypedDict

from models import ComparisonResult, InventionAnalysis, MatchAnalysis, PatentRecord


class GraphState(TypedDict, total=False):
    """State threaded through the novelty-search StateGraph.

    Grouped by the agent that owns each field.  A node writes only the fields in
    its own group; everything else it treats as read-only.
    """

    # --- Request input.  Set once by the caller, never written by a node. ---
    description: str
    claims: str | None

    # --- Query Understanding (P1.2) ---
    analysis: InventionAnalysis
    """Concepts, keywords and queries from the single understanding LLM call."""

    invention_text: str
    """Description + claims + concepts + keywords, concatenated.  This is the
    text the ranking stage scores patents against, kept in state so ranking does
    not need to know how it was assembled."""

    # --- Query Expansion (P1.2) ---
    queries: list[str]
    """The search queries actually dispatched.  Distinct from
    ``analysis.queries`` so a future expansion step can add to the set without
    rewriting what the understanding call returned."""

    # --- Keyword Search (P1.3) ---
    keyword_patents: list[PatentRecord]
    """Deduplicated results from the keyword providers in ``SOURCES``, enriched
    with full text. These are the patents this request actually reasons about."""

    index_only_patents: list[PatentRecord]
    """Extra provider results that are indexed but never shown.

    Providers return more per call than the pipeline enriches (see
    ``search.search.FETCH_PER_QUERY``), and the surplus used to be discarded.
    It is carried here instead so ``index_patents_node`` can grow the Qdrant
    corpus with it. Deliberately kept out of ``candidates``: these records were
    never enriched, so promoting them would mean ranking abstract-only patents
    against full-text ones and reporting the difference as similarity."""

    # --- Ranking (P1.4, replaced in P3.6) ---
    comparisons: list[ComparisonResult]
    """Scored candidates, highest similarity first."""

    # --- Explanation (P1.4) ---
    match_analysis: MatchAnalysis
    """Per-patent rationales plus the shared recommendations."""

    # --- The working candidate set ---
    candidates: list[PatentRecord]
    """The patents the ranking stage will score.

    Every retrieval or filtering stage *replaces* this list as the pipeline
    narrows: today keyword search writes it directly, and in Phase 3 fusion and
    then validation overwrite it in turn.  Ranking always reads ``candidates``
    and never a stage-specific field, which is what lets Phase 3 slot new stages
    in front of it without touching the ranking node.
    """

    # --- Semantic Search (P3.3) ---
    semantic_patents: list[PatentRecord]
    """Vector-similarity hits from Qdrant, written by ``semantic_search_node``
    (``graph/searching.py``).  Runs in parallel with keyword search, so it is a
    separate field rather than a shared accumulator - the two branches must not
    race on one list."""

    # --- Result Fusion (P3.4) ---
    fused_patents: list[PatentRecord]
    """Keyword and semantic results merged by Reciprocal Rank Fusion
    (``graph/fusion.py``).  Writing this also replaces ``candidates``."""

    # --- Validation (P3.5) ---
    validated_patents: list[PatentRecord]
    """The subset of ``fused_patents`` the LLM relevance check
    (``graph/validation.py``) kept.  Writing this also replaces ``candidates``."""
