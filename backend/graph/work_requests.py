"""The executor side of the two human work requests (spec §10, cases T and U).

`POST /api/runs/{id}/candidates` and `POST /api/runs/{id}/cells/{id}/rerun` record
a **request** and return `202`. Nothing in the API fetches a publication, builds a
passage, or re-verifies a cell. These two nodes are where the worker carries those
requests out, under a lease, inside the graph.

## Manual candidates re-enter the pipeline — they do not skip it

`absorb_manual_candidates_node` does one thing: it puts the requested publication
numbers into `candidates` and routes the graph **back to `normalize_publications`**.
It performs no normalization, no date screening, no enrichment and no eligibility
decision of its own, because those nodes already exist and a second implementation
of them is a second set of rules to drift.

Spec case T is explicit: a manually added publication "passes normalization, date
screening, enrichment, and full-text eligibility **identically** to a discovered
candidate. If it fails any, it is recorded with that state and cannot be approved
into the set." Looping through the real nodes is the only way to mean that
literally. A manual candidate that fails screening lands in
`publication_exclusions` exactly like a discovered one, and the loop returns to
Workflow Gate B so a human approves — or does not approve — the widened set.

## A rerun supersedes; it never edits

`rerun_cells_node` re-verifies **only** the cells a reviewer named, with **only**
the context they asked for, and writes the result as a **new `result_version`**
carrying `source_result_version` back to the one it came from. The prior version
stays immutable and retrievable (§13.1, case U), every other cell's result is left
untouched, and the graph returns to Workflow Gate C so the reviewer sees the new
result rather than the run marching on without them.

**Both nodes are idempotent through the database, not through a flag in state.**
Each request row is claimed by stamping `processed_at` in the same transaction that
consumes it, under the run lease. A recovery that re-runs either node finds no
unprocessed rows and does nothing — which is what makes them safe on the
at-least-once execution the lease provides (§3.3.1) rather than only on the happy
path.
"""

import logging
from typing import Any, Optional

import db
from langchain_core.runnables import RunnableConfig

from graph import durable
from graph.events import emit_progress
from graph.state import GraphState
from models import PatentRecord
from workflow import store
from workflow.types import UnapprovedArtifactError

logger = logging.getLogger(__name__)

MANUAL_SOURCE = "Added by reviewer"
"""What a manually added candidate's `source` says.

Distinct from any provider name on purpose: a publication nobody's search
returned should never be mistaken in a report for one that was discovered."""


def _run_id(state: GraphState, what: str) -> str:
    run_id = (state.get("run_id") or "").strip()
    if not run_id:
        raise UnapprovedArtifactError(
            f"{what} belongs to a durable run and this graph execution has no "
            f"run_id. A search-only run must never reach this node."
        )
    return run_id


# --- Workflow Gate B: manually added candidates ----------------------------


def pending_manual_candidates(run_id: str, actor_email: str) -> list[dict[str, Any]]:
    """Requested publications this run has not yet put through the pipeline."""
    with db.connect() as conn:
        requested = store.list_manual_candidates(
            conn, run_id=run_id, actor_email=actor_email
        )
    return [row for row in requested if not row["processed"]]


def absorb_manual_candidates_node(state: GraphState) -> dict[str, Any]:
    """Add requested publications to `candidates` and mark the requests claimed.

    Returns the widened candidate list. The graph then routes back to
    `normalize_publications`, so every one of them is normalized, date-screened,
    enriched, segmented into passages and eligibility-checked by the same nodes a
    discovered candidate goes through.

    **Claimed inside the same transaction that reads them**, under the run's
    lease, so a recovery that re-runs this node does not add a publication twice.
    """
    run_id = _run_id(state, "a manual candidate")
    owner = str(state.get("created_by") or "")
    pending = pending_manual_candidates(run_id, owner)

    emit_progress(
        "normalize",
        f"Adding {len(pending)} manually requested publication(s) to the pipeline…",
        count=len(pending),
    )

    if not pending:
        return {"pending_manual_candidates": 0}

    candidates = list(state.get("candidates") or [])
    known = {candidate.id.strip().upper() for candidate in candidates}

    added = []
    for row in pending:
        number = str(row["publication_number"]).strip()
        if number.upper() in known:
            # Already in the set from discovery. The request is still claimed --
            # it was carried out, and the answer is "it is already here".
            added.append(number)
            continue
        candidates.append(
            PatentRecord(
                id=number,
                title="",
                abstract="",
                url="",
                # Never a provider name. A publication nobody's search returned
                # must not read in a report as though one did.
                source=MANUAL_SOURCE,
            )
        )
        added.append(number)

    with db.connect() as conn:
        store.claim_manual_candidates(
            conn, run_id=run_id, actor_email=owner, publication_numbers=added
        )

    return {
        "candidates": candidates,
        "pending_manual_candidates": 0,
        "manual_candidates_absorbed": added,
    }


def route_after_gate_b(state: GraphState) -> str:
    """Back through the pipeline if a human added a publication; on if not.

    The loop is the point. A widened candidate set has to be normalized,
    screened, enriched and eligibility-checked before anybody can approve it, and
    then it has to be **approved again** -- which is why the pipeline's tail
    leads back to Gate B rather than past it.
    """
    run_id = (state.get("run_id") or "").strip()
    if not run_id:
        return "verify_features"
    owner = str(state.get("created_by") or "")
    try:
        pending = pending_manual_candidates(run_id, owner)
    except Exception:
        # A read failure here must not silently skip a human's request. Raising
        # from a router is awkward, so the honest fallback is to take the branch
        # that does the work: `absorb` re-reads and will fail loudly there.
        return "absorb_manual_candidates"
    return "absorb_manual_candidates" if pending else "verify_features"


# --- Workflow Gate C: cell reruns ------------------------------------------


def pending_cell_reruns(run_id: str, actor_email: str) -> list[dict[str, Any]]:
    with db.connect() as conn:
        requested = store.list_cell_rerun_requests(
            conn, run_id=run_id, actor_email=actor_email
        )
    return [row for row in requested if not row["processed"]]


def rerun_cells_node(
    state: GraphState, config: Optional[RunnableConfig] = None
) -> dict[str, Any]:
    """Re-verify only the named cells, as a new result version.

    Three properties, and each is a rule rather than an implementation detail:

    * **Only the named cells.** Every other cell's stored result is untouched, so
      a rerun cannot quietly change a verdict a reviewer already accepted.
    * **Only the named context.** A rerun is not a licence to widen the passage
      set; the reviewer asked for specific surrounding material.
    * **A new version, never an edit.** The result is written at
      `result_version + 1` with `source_result_version` pointing back. The prior
      version stays immutable and retrievable, and the reviewer's earlier
      decision stays attached to what they actually saw (§13.4 check 16).
    """
    run_id = _run_id(state, "a cell rerun")
    owner = str(state.get("created_by") or "")
    pending = pending_cell_reruns(run_id, owner)

    emit_progress(
        "verify",
        f"Re-running {len(pending)} cell(s) with the requested context…",
        count=len(pending),
    )

    if not pending:
        return {"pending_cell_reruns": 0}

    from graph.verification_nodes import rerun_single_cell

    reran: list[dict[str, Any]] = []
    for row in pending:
        result = rerun_single_cell(
            state,
            cell_id=str(row["cell_id"]),
            context_requests=list(row["context_requests"] or []),
            source_result_version=int(row["source_result_version"]),
        )
        reran.append(result)

    with db.connect() as conn:
        store.claim_cell_reruns(
            conn,
            run_id=run_id,
            actor_email=owner,
            keys=[(str(row["cell_id"]), int(row["source_result_version"])) for row in pending],
        )

    # The reruns replace their own cells in state and leave every other cell as
    # it was, so the matrix the reviewer sees next is the same one plus the new
    # versions.
    cells = list(state.get("cells") or [])
    by_id = {str(cell.get("cell_id")): index for index, cell in enumerate(cells)}
    for result in reran:
        index = by_id.get(str(result.get("cell_id")))
        if index is None:
            cells.append(result)
        else:
            cells[index] = result

    # Only the reran cells are written, at their NEW version. The prior version
    # is not touched: `save_cell_result` inserts, and a review overlay bound to
    # the old `(cell_id, result_version)` stays attached to exactly what its
    # reviewer saw (§13.4 check 16).
    durable.persist_cells(run_id, config, reran)

    return {"cells": cells, "pending_cell_reruns": 0, "cells_reran": len(reran)}


def route_after_gate_c(state: GraphState) -> str:
    """Back to the reviewer if they asked for a rerun; on to the draft if not.

    A rerun returns to Gate C rather than continuing, because the reviewer asked
    a question and the run marching past the answer would defeat the point of
    asking.
    """
    run_id = (state.get("run_id") or "").strip()
    if not run_id:
        return "draft_report"
    owner = str(state.get("created_by") or "")
    try:
        pending = pending_cell_reruns(run_id, owner)
    except Exception:
        return "rerun_cells"
    return "rerun_cells" if pending else "draft_report"


__all__ = [
    "MANUAL_SOURCE",
    "absorb_manual_candidates_node",
    "pending_cell_reruns",
    "pending_manual_candidates",
    "rerun_cells_node",
    "route_after_gate_a",
    "route_after_gate_b",
    "route_after_gate_c",
    "pending_understanding_regeneration",
    "regenerate_understanding_node",
    "pending_strategy_regeneration",
    "regenerate_strategy_node",
    "route_after_gate_strategy",
]


# --- The understanding stage: regeneration ---------------------------------


def pending_understanding_regeneration(run_id: str) -> Optional[dict[str, Any]]:
    """The oldest unprocessed regeneration request for this run, or `None`.

    Reads through the `_system_*` reader because no human is behind this call:
    the executor is deciding whether to route the graph back for a re-analysis,
    under the run's lease, and there is no actor to scope to.
    """
    with db.connect() as conn:
        return store._system_pending_understanding_regeneration(conn, run_id=run_id)


def route_after_gate_a(state: GraphState) -> str:
    """Back to the analysis if the inventor rejected it; on to the SEARCH STRATEGY
    stage if not.

    The loop is the point, and it is **not** the Gate B loop with a different
    name. A regeneration is the inventor saying the system misunderstood them, so
    the graph goes back to the node that proposed, which produces a **new
    version** and pauses again for review. Nothing about the rejected version is
    edited, and no search runs on an understanding nobody approved.

    An approved understanding goes to `propose_strategy`, **not** to the search.
    The search is reached only through the strategy stage own gate, so the
    approval this router guards is now the first of two in front of any provider
    call rather than the only one.
    """
    run_id = (state.get("run_id") or "").strip()
    if not run_id:
        return "propose_strategy"
    try:
        pending = pending_understanding_regeneration(run_id)
    except Exception:
        # A read failure must not silently skip a human's request. Raising from a
        # router is awkward, so the honest fallback is the branch that does the
        # work: `regenerate_understanding` re-reads and fails loudly there.
        return "regenerate_understanding"
    return "regenerate_understanding" if pending else "propose_strategy"


def regenerate_understanding_node(state: GraphState) -> dict[str, Any]:
    """Claim a regeneration request and carry its feedback into a new proposal.

    Three properties, each a rule rather than an implementation detail:

    * **A new version, never an edit.** `understanding_version` is cleared so the
      proposing node takes the next version. The rejected proposal stays exactly
      as it was, and `understanding_proposals` keeps it.
    * **Claimed under the lease, in the database.** Stamping `processed_at` is
      what stops the at-least-once execution the lease provides from producing
      two re-analyses for one request. A recovery that re-runs this node finds
      nothing to claim.
    * **The feedback is instruction, never content.** It is passed to the model
      as what the inventor said was wrong; nothing copies it into a summary or an
      element, because the inventor describing a misunderstanding is not the same
      act as the inventor writing a requirement.
    """
    run_id = _run_id(state, "an understanding regeneration")
    pending = pending_understanding_regeneration(run_id)
    if pending is None:
        # Nothing to do: another attempt already claimed it. Returning cleanly
        # rather than raising is right here -- the request WAS carried out, and a
        # recovery re-running this node is ordinary, not an error.
        return {"understanding_feedback": ""}

    with db.connect() as conn:
        claimed = store.claim_understanding_regeneration(
            conn, run_id=run_id, from_version=int(pending["from_version"])
        )
    if not claimed:
        return {"understanding_feedback": ""}

    logger.info(
        "run %s: regenerating the invention understanding from version %s",
        run_id,
        pending["from_version"],
    )
    return {
        "understanding_feedback": str(pending.get("feedback") or ""),
        # Cleared so `decompose_features_node` allocates the NEXT version rather
        # than writing a second proposal at the version being rejected.
        "understanding_version": 0,
    }


# --- The search strategy: regeneration -------------------------------------


def pending_strategy_regeneration(run_id: str) -> Optional[dict[str, Any]]:
    """The oldest unprocessed strategy regeneration request for this run, or `None`.

    Reads through the `_system_*` reader because no human is behind this call: the
    executor is deciding whether to route the graph back for fresh queries, under
    the run lease, and there is no actor to scope to.
    """
    with db.connect() as conn:
        return store._system_pending_strategy_regeneration(conn, run_id=run_id)


def route_after_gate_strategy(state: GraphState) -> str:
    """Back to the planning if the reviewer rejected the queries; on to the search
    if not.

    The loop is the point and it is the same shape as the understanding stage's:
    a regeneration is the reviewer saying these are not the right queries, so the
    graph goes back to the node that proposed, which produces a **new version**
    and pauses again. Nothing about the rejected version is edited, and **no
    provider is contacted on a strategy nobody approved**.
    """
    run_id = (state.get("run_id") or "").strip()
    if not run_id:
        return "search_orchestrator"
    try:
        pending = pending_strategy_regeneration(run_id)
    except Exception:
        # A read failure must not silently skip a human's request. Raising from a
        # router is awkward, so the honest fallback is the branch that does the
        # work: `regenerate_strategy` re-reads and fails loudly there.
        return "regenerate_strategy"
    return "regenerate_strategy" if pending else "search_orchestrator"


def regenerate_strategy_node(state: GraphState) -> dict[str, Any]:
    """Claim a strategy regeneration request and carry its feedback into a new
    proposal.

    The same three properties as the understanding stage's regeneration, for the
    same reasons:

    * **A new version, never an edit.** `strategy_version` is cleared so the
      proposing node takes the next version. The rejected proposal stays exactly
      as it was, and `search_strategy_proposals` keeps it.
    * **Claimed under the lease, in the database.** Stamping `processed_at` is
      what stops the at-least-once execution the lease provides from producing two
      strategies for one request. A recovery that re-runs this node finds nothing
      to claim.
    * **The feedback is instruction, never content.** It is passed to the model as
      what the reviewer said was wrong; nothing copies it into a query.
    """
    run_id = _run_id(state, "a search strategy regeneration")
    pending = pending_strategy_regeneration(run_id)
    if pending is None:
        # Nothing to do: another attempt already claimed it. Returning cleanly
        # rather than raising is right here -- the request WAS carried out, and a
        # recovery re-running this node is ordinary, not an error.
        return {"strategy_feedback": ""}

    with db.connect() as conn:
        claimed = store.claim_strategy_regeneration(
            conn, run_id=run_id, from_version=int(pending["from_version"])
        )
    if not claimed:
        return {"strategy_feedback": ""}

    logger.info(
        "run %s: regenerating the search strategy from version %s",
        run_id,
        pending["from_version"],
    )
    return {
        "strategy_feedback": str(pending.get("feedback") or ""),
        # Cleared so `propose_strategy_node` allocates the NEXT version rather
        # than writing a second proposal at the version being rejected.
        "strategy_version": 0,
    }
