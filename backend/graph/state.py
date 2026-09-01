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

    # =====================================================================
    # Verification workflow (Implementation Gate C)
    # =====================================================================
    #
    # The split below is the whole point of this gate, so it is written as two
    # separate groups with a hard line between them rather than as neighbouring
    # fields that happen to be named differently.

    created_by: str
    """The account that owns this run.

    Carried in state so the **worker** can make ownership-scoped reads while it
    acts on the run: `workflow/store.py`'s run-scoped functions all take an
    `actor_email` and filter on it in the same query that fetches the run, and
    the worker is acting on *this* owner's behalf. Without it the reads ran with
    an empty actor and answered `RUN_NOT_FOUND` -- correctly, since nobody owns
    "".

    Empty on a search-only run, which has no `verification_runs` row and never
    reaches a node that reads one."""

    run_id: str
    """The durable run this graph execution belongs to. Empty for a search-only
    run, which has no `verification_runs` row."""

    verification_requested: bool
    """Whether this run goes through the verification branch at all.

    A search-only run must traverse exactly the nodes it does today and emit
    exactly today's phases -- `tests/test_verification_integration.py` asserts
    both, because the existing SSE contract is live production behaviour and this
    gate must not perturb it."""

    problem_solved: str
    """Optional Stage 1 input, as the inventor typed it. Context for the
    proposal, never evidence."""

    inventor_novelty_statement: str
    """Optional Stage 1 input: what the inventor believes is new. A claim this
    run exists to test, and never treated as a finding."""

    cutoff_date: str | None
    date_policy: str
    """Copied from the run row at admission and never re-read from settings, so a
    finished run's exclusions stay explicable by the rules it actually ran under."""

    text_version_policy: str
    """The second recorded policy, and it has to be declared here to survive.

    `graph/adapter.py` passed it from the run row all along, but a key absent
    from this `TypedDict` is not a channel, and **LangGraph drops it** -- so it
    never reached a node, and the reviewed report rendered its text-version
    policy as a blank. Same failure mode as `created_by`, and silent in exactly
    the same way: nothing raises, a field is simply never there.

    Copied from the run row for the same reason as `date_policy` above: a
    finished report must state the rules it actually ran under, not whatever
    settings say today."""

    # --- MACHINE OUTPUT. Never sufficient to verify against. ---------------
    #
    # Written by graph nodes. Read by the Workflow Gate A and B review panels.
    # NEVER read by verify_features_node -- see graph/verification_nodes.py.

    candidate_decomposition: dict | None
    """An LLM's proposal, from `decompose_features_node`: a `summary`, the
    `features`, and the `version` they were proposed at. A human edits and
    approves it at the understanding stage; until then it is a suggestion."""

    understanding_version: int | None
    """The version the current proposal was made at. A regeneration takes the
    NEXT version -- an approved version is never reused and never overwritten."""

    understanding_feedback: str
    """What the inventor said the system misunderstood, carried into a
    regeneration and cleared by the node that consumes it. Left set, it would
    silently re-apply to a version it was never about."""

    candidate_search_strategy: dict | None
    """The queries an LLM proposed for the **approved** understanding, from
    `propose_strategy_node`: `queries`, each with the element ids it claims to
    cover, plus the `version` and the `understanding_version` they were proposed
    against. A reviewer edits, enables, disables and approves them; until then
    nothing here is dispatched."""

    strategy_version: int | None
    """The version the current strategy proposal was made at. A regeneration takes
    the NEXT version -- an approved version is never reused and never overwritten,
    exactly as for the understanding."""

    strategy_feedback: str
    """What the reviewer said was wrong with a rejected strategy, carried into a
    regeneration and cleared by the node that consumes it. Left set, it would
    silently re-apply to a version it was never about."""

    ranked_candidates: list[str]
    """Eligible publications in similarity order, from `rank_and_reduce_node`.
    A ranked list is not an approved set."""

    # --- HUMAN-APPROVED. Written ONLY by workflow/approvals.py. ------------
    #
    # `tests/test_approval_boundary.py` asserts by source contract that no other
    # module writes these. A missing one raises UnapprovedArtifactError at the
    # verifier boundary, before any LLM call.

    approved_decomposition: dict | None
    """Spec §2 wire shape plus `approved_by` and `approved_at`. The presence of a
    real approver is checked, not assumed: `approved_by=''` names nobody."""

    decomposition_version: int | None
    approved_summary: str
    """The technical summary the human approved, read back from
    `approved_understandings`. Never the machine's proposal, and never the
    inventor's original description -- both of those are stored elsewhere and
    neither is this."""

    approved_candidate_set: list[dict] | None
    """Approved publications in approved order. An approved-but-EMPTY set is
    legal -- "none of these are worth verifying" is a real decision -- and is a
    different thing from a set nobody approved, which is `None`."""

    candidate_set_version: int | None

    approved_search_strategy: dict | None
    """The strategy a human approved: every query as approved, enabled or not,
    with the `version` and the `understanding_version` it was approved against.

    **`queries` is what the search dispatches, and it is written from the enabled
    subset of this.** A proposed query is never dispatched, and a disabled one is
    kept here rather than dropped: "we deliberately did not search for this" is
    part of what was approved and part of what the report has to be able to
    state."""

    strategy_approved_version: int | None

    # --- Evidence layer (Gate B modules, wired in Gate C) ------------------

    normalized_publications: dict[str, str]
    """Discovery id -> epodoc form. A number that could not be parsed is absent
    here and present in `publication_exclusions`; it is never guessed at."""

    publication_dates: dict[str, dict]
    enrichment: dict[str, dict]
    passages_by_publication: dict[str, list]

    publication_exclusions: list[dict]
    """`EXCLUDED_*` records. **These are properties of a publication and never
    become cells.** A cell would imply a comparison was attempted and answered
    negatively; an exclusion means no comparison was attempted at all."""

    cells: list[dict]
    """One entry per approved feature x approved publication actually verified.
    `NOT_EVALUATED` and `VALIDATION_FAILURE` are cell states and always appear
    here -- they are never folded into an exclusion, and never rendered blank."""

