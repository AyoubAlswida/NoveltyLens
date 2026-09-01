"""The two nodes that sit on either side of the approval boundary.

`decompose_features_node` produces **machine output**. `verify_features_node`
consumes **human-approved input**. They are in the same module so the asymmetry
between them is impossible to miss, and so is the fact that nothing connects
them directly: `candidate_decomposition` is written by the first and read by the
Workflow Gate A panel, never by the second.

**The rule this module exists to enforce.** `verify_features_node` reads
`approved_decomposition` and `approved_candidate_set` and *nothing else*. It does
not read `candidate_decomposition`. It does not read `ranked_candidates`. There
is no setting, environment variable, keyword argument or debug flag that makes it
read either, and adding one would defeat the purpose of every other file in this
gate. When an approved artifact is missing it raises
:class:`~workflow.types.UnapprovedArtifactError` **before any LLM call**, so a run
that reaches here unapproved spends no quota and produces no cell.

**Why "before any LLM call" is stated as a property rather than assumed.** The
expensive, irreversible thing here is not the exception — it is a cell asserting
something about a patent in answer to a question nobody approved. The approval
checks therefore run first, on state alone, and
`tests/test_approval_boundary.py` asserts the LLM was never called.

**The matrix is `approved features x approved publications`, exactly.** Not the
eligible publications, not the ranked ones, not the ones with passages — the
approved ones. A publication that lost its passages between approval and
verification produces a `PassageSetIneligible`, which is caught **here, at the
matrix level only**, recorded as a publication exclusion, and removes that
publication from the matrix. It creates no cells and does not fail the run: an
exclusion means no comparison was attempted, which is exactly what happened.
Every other verifier error propagates.
"""

import logging
from typing import Any, Optional

import llm
from langchain_core.runnables import RunnableConfig

import db
from graph import durable
from workflow import understanding as understanding_store
from evidence.eligibility import ExclusionState, PublicationExclusion
from graph.events import emit_progress
from graph.state import GraphState
from verification.decomposition import ApprovedDecomposition
from verification.states import PassageSetIneligible
from verification.verify import PublicationUnderEvaluation, verify_feature_disclosure
from workflow.types import UnapprovedArtifactError

logger = logging.getLogger(__name__)

MATRIX_CELL_CAP = 75
"""The hard ceiling on `features x publications` LLM calls in one run.

A **reviewed constant** (spec §16 decision 5). 5 core features x 15 approved
publications is 75 cells, which is the largest matrix the Gate B cap
(`evidence_nodes.MAX_APPROVED_CANDIDATES`) can produce. Exceeding it raises
rather than truncating: a silently shortened matrix would present partial
coverage as complete, which is the substitution the zero-tolerance policy
forbids. If a larger matrix is ever wanted, the cap moves deliberately and the
review burden at Workflow Gate C moves with it.
"""

MATRIX_CONCURRENCY = 1
"""Cells are verified **serially**, and that is a measurement, not a preference.

Spec §16 decision 5 requires gateway concurrency to be *measured, not assumed* --
Lens's trial tier already forced a global request lock in this codebase, so the
assumption has been wrong here before. No concurrency measurement has been taken
against the configured gateway, so this stays at 1: the value that cannot exceed
a limit nobody has measured. Raising it requires a recorded measurement, not an
argument that it is probably fine.
"""

RETRIEVAL_FLOOR: Optional[float] = None
"""**Deliberately `None`. Do not invent a threshold here.**

Spec §16 decision 6 blocks the `NOT_EVALUATED` retrieval floor on the Qdrant /
Qwen3-Embedding-4B recalibration CLAUDE.md flags as outstanding: the 0.6 constant
in `search/vector_store.py` was measured against bge-large and is stale for a
2560-dim model whose score distribution sits somewhere else entirely. A guessed
floor would drop passages from the prompt and report the result as
`NOT_EVALUATED` -- an evaluation that did not happen, attributed to a threshold
nobody established.

With `None`, every eligible passage is a selection candidate and the distinct
`NOT_EVALUATED` reason `NO_PASSAGE_OVER_FLOOR` (regression case M) is
unreachable, which is the honest state until the measurement exists.
"""


def decompose_features_node(state: GraphState) -> dict[str, Any]:
    """Propose a candidate understanding. Writes `candidate_decomposition` only.

    **Nothing downstream may treat this as approved**, and nothing can: the field
    it writes is not the field `verify_features_node` reads, and
    `workflow/approvals.py` is the only module that can produce the field that is.
    The proposal is also persisted to `understanding_proposals`, a table with no
    column in which an approval could be recorded.

    The inventor's optional Stage 1 context -- the problem they say the invention
    solves, and what they believe is new -- steers the proposal. Neither is ever
    treated as evidence: what an inventor believes is new is a claim this run
    exists to test.

    `understanding_feedback` carries what the inventor said the system
    misunderstood, when the executor is re-running this node to carry out a
    regeneration request. A regeneration produces a **new version**; it never
    edits the version it was asked to improve on.

    The component and its prompt are named explicitly in
    `llm.DECOMPOSITION_COMPONENT`, because spec section 16 decision 8 leaves the
    owner of this step open and an unnamed component cannot be assigned one.
    """
    from graph.limits import MAX_CAPABILITIES, MAX_ELEMENTS_PER_CAPABILITY

    invention = state.get("invention_text") or state["description"]
    feedback = str(state.get("understanding_feedback") or "")
    emit_progress(
        "decompose",
        "Re-analysing the invention…" if feedback else "Analysing the invention…",
    )

    proposal = llm.propose_understanding(
        invention,
        problem_solved=str(state.get("problem_solved") or ""),
        novelty_statement=str(state.get("inventor_novelty_statement") or ""),
        feedback=feedback,
        max_features=MAX_CAPABILITIES,
        max_elements_per_feature=MAX_ELEMENTS_PER_CAPABILITY,
    )

    run_id = str(state.get("run_id") or "").strip()
    version = int(state.get("understanding_version") or 0)
    if run_id:
        # Persisted so the screen can still show what the machine said after the
        # inventor has edited it, and so a regeneration can be told apart from a
        # first proposal. `_if_absent` because recovery re-runs this node: the
        # same version arriving twice is an ordinary replay, not a second
        # proposal, and a model is not deterministic so comparing bodies would
        # turn a recovery into a failure.
        with db.connect() as conn:
            if version < 1:
                version = understanding_store.next_version(conn, run_id=run_id)
            understanding_store.record_proposal_if_absent(
                conn,
                run_id=run_id,
                version=version,
                source=understanding_store.SOURCE_LLM,
                summary=proposal["summary"],
                features=proposal["features"],
                created_by=str(state.get("created_by") or ""),
                feedback=feedback,
            )

    return {
        "candidate_decomposition": {
            "component": llm.DECOMPOSITION_COMPONENT,
            "summary": proposal["summary"],
            "features": proposal["features"],
            "version": version,
        },
        "understanding_version": version,
        # Cleared so a later recovery of this run does not silently re-apply
        # feedback the inventor gave about a version that has since been replaced.
        "understanding_feedback": "",
    }


def _require_approved(state: GraphState) -> tuple[list[ApprovedDecomposition], list[dict]]:
    """Read the approved artifacts, or raise before anything is spent.

    Four separate refusals, because they fail for four different reasons and a
    single "not approved" message would hide which:

    1. no approved decomposition at all -- Workflow Gate A never completed;
    2. an approved decomposition with no approver or no approval time -- a row
       that claims approval without recording one;
    3. no approved candidate set -- Workflow Gate B never completed;
    4. a version mismatch between the two -- each was approved, but not together,
       so the cells built from them would carry a pair of versions that never
       coexisted at any point a human looked at.

    An approved-but-**empty** candidate set is not a refusal. "None of these are
    worth verifying" is a real decision, and it produces a matrix with no cells --
    which is a different thing from a matrix nobody approved.
    """
    approved = state.get("approved_decomposition")
    if not approved:
        raise UnapprovedArtifactError(
            "verify_features requires an approved decomposition. Workflow Gate A "
            "has not been completed for this run, and candidate_decomposition is "
            "machine output that is never read as a substitute."
        )

    features = approved.get("features") or []
    if not features:
        raise UnapprovedArtifactError(
            "the approved decomposition contains no features; there is no question "
            "to ask any publication"
        )
    if not (approved.get("approved_by") or "").strip():
        raise UnapprovedArtifactError(
            "the approved decomposition does not name an approver; an artifact that "
            "cannot say who approved it has not been approved"
        )
    if not (approved.get("approved_at") or "").strip():
        raise UnapprovedArtifactError(
            "the approved decomposition records no approval time; it cannot be "
            "audited against the version it approved"
        )

    candidate_set = state.get("approved_candidate_set")
    if candidate_set is None:
        raise UnapprovedArtifactError(
            "verify_features requires an approved candidate set. Workflow Gate B "
            "has not been completed for this run, and a ranked candidate list is "
            "machine output that is never read as a substitute."
        )

    decomposition_version = state.get("decomposition_version")
    if decomposition_version is None:
        raise UnapprovedArtifactError(
            "the approved decomposition carries no version; a cell records the "
            "versions it was built from and cannot record a missing one"
        )
    if state.get("candidate_set_version") is None:
        raise UnapprovedArtifactError(
            "the approved candidate set carries no version; a cell records the "
            "versions it was built from and cannot record a missing one"
        )
    if str(approved.get("version")) != str(decomposition_version):
        raise UnapprovedArtifactError(
            f"approved decomposition is version {approved.get('version')!r} but the "
            f"run records decomposition_version {decomposition_version!r}; the cells "
            "would name a pairing no human ever approved"
        )

    decompositions = [
        ApprovedDecomposition.from_json(
            {
                **feature,
                "decomposition_id": approved.get("decomposition_id", ""),
                "decomposition_version": str(decomposition_version),
                "approved_by": approved["approved_by"],
                "approved_at": approved["approved_at"],
            }
        )
        for feature in features
    ]
    return decompositions, list(candidate_set)


def _cell_id(run_id: str, feature_id: str, publication: str) -> str:
    """Spec §13.2's `run:feature:publication`.

    Deterministic, so a re-run of the same node addresses the same cells and a
    review overlay bound to one of them still binds after recovery.
    """
    return f"{run_id}:{feature_id}:{publication}"


def _hash_by_chunk(built: list[Any]) -> dict[str, str]:
    """chunk id -> the SHA-256 of the canonical passage text of record."""
    return {
        entry.passage.chunk_id: entry.text_sha256
        for entry in built
        if getattr(entry, "text_sha256", "")
    }


def verify_features_node(
    state: GraphState, config: Optional[RunnableConfig] = None
) -> dict[str, Any]:
    """Run the matrix: one verifier call per approved feature x approved publication.

    Emits one `verification_progress` event per completed cell, so a reviewer
    watching a long matrix sees real progress rather than a stalled bar.
    """
    decompositions, candidate_set = _require_approved(state)

    publications = [entry["normalized_number"] for entry in candidate_set]
    total = len(decompositions) * len(publications)
    if total > MATRIX_CELL_CAP:
        raise ValueError(
            f"the approved matrix is {len(decompositions)} features x "
            f"{len(publications)} publications = {total} cells, over the reviewed cap "
            f"of {MATRIX_CELL_CAP}. It is not truncated: a shortened matrix would "
            "present partial coverage as complete."
        )

    passages_by_publication = state.get("passages_by_publication") or {}
    enrichment = state.get("enrichment") or {}
    run_id = state.get("run_id") or ""

    emit_progress(
        "verify",
        f"Verifying {len(decompositions)} features against {len(publications)} publications…",
        count=total,
    )

    cells: list[dict[str, Any]] = []
    exclusions = list(state.get("publication_exclusions") or [])
    excluded_now: set[str] = set()
    completed = 0

    for publication in publications:
        if publication in excluded_now:
            continue
        built = passages_by_publication.get(publication, [])
        entry = enrichment.get(publication)
        document_ids = (
            frozenset({entry["result"].source_document_id}) if entry else frozenset()
        )
        under_evaluation = PublicationUnderEvaluation(
            publication_number=publication,
            title=(entry["result"].requested_publication if entry else publication),
            jurisdiction=publication[:2],
            document_ids=document_ids,
        )

        for decomposition in decompositions:
            cell_id = _cell_id(run_id, decomposition.feature_id, publication)
            try:
                result = verify_feature_disclosure(
                    decomposition=decomposition,
                    publication=under_evaluation,
                    passages=[entry.passage for entry in built],
                    cell_id=cell_id,
                    # Spec §16 decision 6: blocked on the Qdrant/Qwen3
                    # recalibration. Never a guessed number.
                    retrieval_floor=RETRIEVAL_FLOOR,
                )
            except PassageSetIneligible as ineligible:
                # Caught at the matrix level ONLY. The publication is excluded
                # and leaves the matrix; the run continues, and no cell is
                # created for it -- a cell would imply a comparison was
                # attempted and answered negatively.
                exclusions.append(
                    {
                        "requested_number": publication,
                        "normalized_number": publication,
                        "served_number": None,
                        "state": str(ExclusionState.EXCLUDED_INELIGIBLE),
                        "reason": str(ineligible),
                        "failing_check": getattr(ineligible, "check", ""),
                        "language_attempted": "",
                        "sections_available": [],
                        "sections_retrieved": [],
                        "passages_built": len(built),
                        "provider_detail": "",
                    }
                )
                excluded_now.add(publication)
                cells = [cell for cell in cells if cell["publication_number"] != publication]
                break

            completed += 1
            cells.append(
                {
                    "cell_id": result.cell_id,
                    "run_id": run_id,
                    "feature_id": decomposition.feature_id,
                    "feature_version": decomposition.feature_version,
                    "publication_number": publication,
                    "decomposition_version": state["decomposition_version"],
                    "candidate_set_version": state["candidate_set_version"],
                    "state": str(result.state),
                    "outcome": (
                        str(result.machine_result.outcome) if result.machine_result else ""
                    ),
                    "failed_check": result.failed_check,
                    "reason": result.reason,
                    "result_version": result.result_version,
                    "raw_response": result.raw_response,
                    "prompt_version": result.prompt_version,
                    "model": result.model,
                    "attempts": result.attempts,
                    "passage_chunk_ids": list(result.passage_chunk_ids),
                    # Positionally aligned with the ids above, taken from the
                    # passages this call was actually given. Recording the ids
                    # without the hashes made a citation checkable only for
                    # existence, never for having changed.
                    "passage_text_hashes": [
                        _hash_by_chunk(built).get(chunk_id, "")
                        for chunk_id in result.passage_chunk_ids
                    ],
                    # Dumped to plain JSON, not carried as a model: the state is
                    # checkpointed, and a validated result must survive that round
                    # trip byte-identical. `mode="json"` is the faithful
                    # representation -- it renames nothing and drops nothing.
                    "machine_result": (
                        result.machine_result.model_dump(mode="json")
                        if result.machine_result
                        else None
                    ),
                }
            )
            emit_progress(
                "verify",
                f"Verified {completed} of {total} cells…",
                count=completed,
                total=total,
            )

    # The matrix outlives the checkpoint: `verification_cells` /
    # `verification_results` are what a review overlay binds to, what a rerun
    # versions against, and what the report is assembled from later. Written
    # under the fence the executor is holding right now -- `graph/durable.py`
    # explains why the lease comes from `config` and never from checkpointed
    # state.
    durable.persist_cells(run_id, config, cells)

    return {"cells": cells, "publication_exclusions": exclusions}


def rerun_single_cell(
    state: GraphState,
    *,
    cell_id: str,
    context_requests: list[Any],
    source_result_version: int,
) -> dict[str, Any]:
    """Re-verify ONE cell as a new result version (spec case U).

    ## What "only that cell" means here

    The cell is located by its own id in the approved matrix, and the feature and
    publication are read back from it. Nothing else in the matrix is touched, no
    other publication is enriched, and no other verifier call is made -- so every
    other cell's stored result stays byte-identical, which is the property case U
    names.

    ## What "only the named context" means here

    `context_requests` carries the model's own `requests_for_more_context`
    entries that the reviewer chose to fulfil. It is recorded on the new result
    for the audit trail and it does **not** widen the passage set: the passages
    are the ones already built and stored for this publication. A rerun that
    quietly fetched more would make the second answer incomparable to the first,
    which defeats the purpose of asking for one.

    ## A new version, never an edit

    The returned cell carries `result_version = source_result_version + 1` and
    `source_result_version` pointing back. `workflow/store.py::save_cell_result`
    writes it as a new row; the prior version stays immutable and retrievable,
    and any review attached to it stays attached to what that reviewer actually
    saw (§13.4 check 16).

    Raises:
        UnapprovedArtifactError: the cell is not in this run's approved matrix.
            A rerun cannot invent a cell, and a cell id that does not resolve is
            a request about something that was never verified.
    """
    decompositions, candidate_set = _require_approved(state)

    existing = {str(cell.get("cell_id")): cell for cell in (state.get("cells") or [])}
    target = existing.get(cell_id)
    if target is None:
        raise UnapprovedArtifactError(
            f"cell {cell_id} is not in this run's matrix, so there is nothing to "
            f"re-run. A rerun supersedes a result; it never creates one."
        )

    publication = str(target["publication_number"])
    feature_id = str(target["feature_id"])
    decomposition = next(
        (item for item in decompositions if item.feature_id == feature_id), None
    )
    if decomposition is None:
        raise UnapprovedArtifactError(
            f"feature {feature_id} is not in the approved decomposition, so cell "
            f"{cell_id} cannot be re-run against it."
        )

    passages_by_publication = state.get("passages_by_publication") or {}
    enrichment = state.get("enrichment") or {}
    built = passages_by_publication.get(publication, [])
    entry = enrichment.get(publication)
    document_ids = (
        frozenset({entry["result"].source_document_id}) if entry else frozenset()
    )

    emit_progress(
        "verify",
        f"Re-running {feature_id} against {publication} at the reviewer's request…",
        count=1,
    )

    result = verify_feature_disclosure(
        decomposition=decomposition,
        publication=PublicationUnderEvaluation(
            publication_number=publication,
            title=(entry["result"].requested_publication if entry else publication),
            jurisdiction=publication[:2],
            document_ids=document_ids,
        ),
        passages=[item.passage for item in built],
        cell_id=cell_id,
        # Unchanged. Section 16 decision 6 is still open and a rerun is not the
        # place to invent a threshold.
        retrieval_floor=RETRIEVAL_FLOOR,
        result_version=str(source_result_version + 1),
    )

    return {
        "cell_id": result.cell_id,
        "run_id": state.get("run_id") or "",
        "feature_id": decomposition.feature_id,
        "feature_version": decomposition.feature_version,
        "publication_number": publication,
        "decomposition_version": state["decomposition_version"],
        "candidate_set_version": state["candidate_set_version"],
        "state": str(result.state),
        "outcome": (str(result.machine_result.outcome) if result.machine_result else ""),
        "failed_check": result.failed_check,
        "reason": result.reason,
        "result_version": source_result_version + 1,
        # The lineage. A result that cannot say which version it superseded
        # cannot be reconciled with the review attached to that version.
        "source_result_version": source_result_version,
        "context_requests_fulfilled": list(context_requests),
        "raw_response": result.raw_response,
        "prompt_version": result.prompt_version,
        "model": result.model,
        "attempts": result.attempts,
        "passage_chunk_ids": list(result.passage_chunk_ids),
        "machine_result": (
            result.machine_result.model_dump(mode="json") if result.machine_result else None
        ),
    }


__all__ = [
    "MATRIX_CELL_CAP",
    "MATRIX_CONCURRENCY",
    "RETRIEVAL_FLOOR",
    "decompose_features_node",
    "rerun_single_cell",
    "verify_features_node",
]
