"""The SEARCH STRATEGY stage: proposing the queries a person will approve.

This node sits between the **approved** invention understanding and the search
that uses it. Everything about its placement is the point:

* It reads `approved_summary` and `approved_decomposition` -- the artifacts a
  human approved at the understanding stage -- and never `candidate_decomposition`.
  Proposing queries from an unapproved reading of the invention would put the
  search back in front of the approval it was moved behind.
* It writes `candidate_search_strategy`, which is **machine output**. The field
  name is the guard, exactly as `candidate_decomposition` is: the search reads
  `queries`, and `queries` is written from the **approved** strategy by
  `gate_strategy_node` and by nothing else.
* **No provider is contacted anywhere in this module.** The stage exists so that
  no provider is contacted until a person has approved both what the invention is
  and what will be searched for.

## Coverage is a claim, not a guarantee

Each proposed query names the approved element ids it is aimed at. That claim is
what the gate's `UNCOVERED_CORE_ELEMENT` rule is computed from, and it is checked
for *existence* -- the ids must be real -- and never for truth. Whether a query
would actually retrieve prior art for the element it names is a judgement, which
is why a person approves this stage rather than a rule doing it.

Ids are feature-qualified (`F1.E2`) because a requirement id is unique within one
feature and not across a run.
"""

import logging
from typing import Any

import db
import llm
from graph.events import emit_progress
from graph.state import GraphState
from workflow import strategy as strategy_store
from workflow.types import UnapprovedArtifactError

logger = logging.getLogger(__name__)


def _approved_understanding(state: GraphState) -> tuple[str, list[dict], int]:
    """The approved summary, features and version, or refuse.

    Read from state, which `gate_a_node` filled from the approved-only readers --
    the ones that filter `is_approved` in SQL. There is deliberately no fallback
    to `candidate_decomposition`: a strategy proposed from a reading of the
    invention nobody approved would be queries built on machine output, which is
    the thing the whole stage ordering exists to prevent.
    """
    approved = state.get("approved_decomposition")
    if not approved:
        raise UnapprovedArtifactError(
            "the search strategy stage requires an APPROVED understanding. The "
            "understanding stage has not completed for this run, and "
            "candidate_decomposition is machine output that is never read as a "
            "substitute for it."
        )
    features = list(approved.get("features") or ())
    if not features:
        raise UnapprovedArtifactError(
            "the approved understanding carries no features, so there is nothing "
            "for a search strategy to cover"
        )
    version = int(state.get("decomposition_version") or approved.get("version") or 0)
    if version < 1:
        raise UnapprovedArtifactError(
            "the approved understanding does not name its version, so a strategy "
            "proposed against it could not state what it covers"
        )
    return str(state.get("approved_summary") or ""), features, version


def propose_strategy_node(state: GraphState) -> dict[str, Any]:
    """Propose candidate search queries. Writes `candidate_search_strategy` only.

    **Nothing downstream may treat this as approved**, and nothing can: the field
    it writes is not the field the search reads, and `workflow/approvals.py` is
    the only module that can produce the field that is. The proposal is also
    persisted to `search_strategy_proposals`, a table with no column in which an
    approval could be recorded.

    `strategy_feedback` carries what the reviewer said was wrong with a rejected
    strategy, when the executor is re-running this node to carry out a
    regeneration request. A regeneration produces a **new version**; it never
    edits the version it was asked to improve on.
    """
    from graph.limits import MAX_STRATEGY_QUERIES

    summary, features, understanding_version = _approved_understanding(state)
    feedback = str(state.get("strategy_feedback") or "")
    emit_progress(
        "strategy",
        "Proposing fresh search queries…" if feedback else "Planning the search…",
    )

    queries = llm.propose_search_strategy(
        summary,
        features,
        existing_queries=list(state.get("queries") or ()),
        feedback=feedback,
        max_queries=MAX_STRATEGY_QUERIES,
    )
    # Every proposed query arrives enabled. Disabling one is a reviewer's
    # decision, and a proposal that pre-disabled queries would be the machine
    # making that decision quietly.
    proposed = [
        {
            "query_id": str(query.get("query_id") or f"Q{index}"),
            "query_text": str(query.get("query_text") or ""),
            "rationale": str(query.get("rationale") or ""),
            "covered_element_ids": [
                str(item) for item in (query.get("covered_element_ids") or ())
            ],
            "enabled": True,
            "order": index,
        }
        for index, query in enumerate(queries, start=1)
    ]

    run_id = str(state.get("run_id") or "").strip()
    version = int(state.get("strategy_version") or 0)
    if run_id:
        # Persisted so the screen can still show what the machine proposed after
        # the reviewer has edited it, and so a regeneration can be told apart
        # from a first proposal. `_if_absent` because recovery re-runs this node.
        with db.connect() as conn:
            if version < 1:
                version = strategy_store.next_version(conn, run_id=run_id)
            strategy_store.record_proposal_if_absent(
                conn,
                run_id=run_id,
                version=version,
                understanding_version=understanding_version,
                source=strategy_store.SOURCE_LLM,
                queries=proposed,
                created_by=str(state.get("created_by") or ""),
                feedback=feedback,
            )

    emit_progress(
        "strategy",
        f"Proposed {len(proposed)} search queries for your review",
        count=len(proposed),
    )
    return {
        "candidate_search_strategy": {
            "component": llm.STRATEGY_COMPONENT,
            "queries": proposed,
            "version": version,
            "understanding_version": understanding_version,
        },
        "strategy_version": version,
        # Cleared so a later recovery of this run does not silently re-apply
        # feedback the reviewer gave about a version that has since been replaced.
        "strategy_feedback": "",
    }


__all__ = ["propose_strategy_node"]
