"""Generate search queries from the approved invention understanding.

This node sits between the **approved** invention understanding and the search
that uses it. Everything about its placement is the point:

* It reads `approved_summary` and `approved_decomposition` -- the artifacts a
  human approved at the understanding stage -- and never `candidate_decomposition`.
  Proposing queries from an unapproved reading of the invention would put the
  search back in front of the approval it was moved behind.
* It writes `candidate_search_strategy`, which is **machine output**. Flow
  version 2 validates and selects that output in `select_generated_queries_node`;
  legacy flow version 1 still sends it through `gate_strategy_node`.
* **No patent provider is contacted anywhere in this module.** Both routes stay
  behind the two approved invention artifacts.

## Coverage metadata is not a human approval

Each proposed query names the approved element ids it is aimed at. That claim is
retained with the machine proposal for audit. Flow version 2 does not present or
approve that claim; it validates the generated query set structurally and runs
it. Legacy flow version 1 still permits a person to edit and approve the text.

Ids are feature-qualified (`F1.E2`) because a requirement id is unique within one
feature and not across a run.
"""

import logging
from typing import Any

import db
import llm
from graph.events import emit_progress
from graph.understanding import build_retrieval_queries
from graph.state import GraphState
from workflow import strategy as strategy_store
from workflow.types import UnapprovedArtifactError

logger = logging.getLogger(__name__)


MAX_STRATEGY_PRIMARY_QUERIES = 5
"""Model-planned Advanced Search angles before deterministic recall anchors.

The final plan is still bounded by ``MAX_STRATEGY_QUERIES``. Reserving three
slots means the lexical coverage of the approved requirements cannot disappear
when the model spends every available slot on an added subsystem.
"""


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

    The proposal is not called or stored as approved. Flow version 2 has an
    explicit validation node that copies its query text into the execution field;
    legacy flow version 1 can populate that field only through human approval.
    The proposal is persisted to `search_strategy_proposals`, a table with no
    column in which an approval could be recorded.

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
        max_queries=MAX_STRATEGY_PRIMARY_QUERIES,
    )
    primary_texts = [str(query.get("query_text") or "") for query in queries]
    approved_requirement_texts = [
        str(element.get("text") or "").strip()
        for feature in features
        for element in (feature.get("elements") or ())
        if str(element.get("text") or "").strip()
    ]
    if not approved_requirement_texts:
        raise UnapprovedArtifactError(
            "approved invention requirements contain no executable technical text"
        )
    retrieval_texts = build_retrieval_queries(
        str(state.get("description") or summary),
        primary_texts,
        approved_requirement_texts,
        [],
        max_queries=MAX_STRATEGY_QUERIES,
        prefer_trailing_phrase=True,
    )
    # Every generated query is executable. Legacy review may edit the list;
    # version 2 validates this invariant before dispatching the whole set.
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
    for query_text in retrieval_texts[len(primary_texts):]:
        proposed.append(
            {
                "query_id": f"Q{len(proposed) + 1}",
                "query_text": query_text,
                "rationale": (
                    "Deterministic lexical anchor for an approved invention "
                    "requirement."
                ),
                # This anchor is executable retrieval text, not an LLM coverage
                # claim. Leaving the metadata empty avoids inventing an audit
                # assertion about exactly which requirement it covers.
                "covered_element_ids": [],
                "enabled": True,
                "order": len(proposed) + 1,
            }
        )

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

    needs_review = int(state.get("advanced_search_flow_version") or 1) < 2
    emit_progress(
        "strategy",
        (
            f"Proposed {len(proposed)} search queries for your review"
            if needs_review
            else f"Generated {len(proposed)} search queries"
        ),
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


def select_generated_queries_node(state: GraphState) -> dict[str, Any]:
    """Validate and release version-2 machine queries into the search.

    This is deliberately separate from proposal generation: a persisted proposal
    remains machine output and is never inserted into the human-approval tables.
    The node refuses malformed, empty, or stale output as a whole instead of
    silently searching with a degraded subset.
    """
    candidate = state.get("candidate_search_strategy")
    if not isinstance(candidate, dict):
        raise ValueError(
            "Advanced Search query generation produced no strategy to execute"
        )

    approved_version = int(state.get("decomposition_version") or 0)
    candidate_understanding_version = int(
        candidate.get("understanding_version") or 0
    )
    if approved_version < 1 or candidate_understanding_version != approved_version:
        raise ValueError(
            "Advanced Search query generation does not match the approved "
            "invention requirements version"
        )

    raw_queries = candidate.get("queries")
    if not isinstance(raw_queries, list) or not raw_queries:
        raise ValueError("Advanced Search query generation returned no queries")

    queries: list[str] = []
    for index, query in enumerate(raw_queries, start=1):
        if not isinstance(query, dict):
            raise ValueError(
                f"Advanced Search query {index} has an invalid generated shape"
            )
        query_text = str(query.get("query_text") or "").strip()
        if not query_text:
            raise ValueError(f"Advanced Search query {index} is empty")
        if query.get("enabled") is not True:
            raise ValueError(
                f"Advanced Search query {index} was not enabled by generation"
            )
        queries.append(query_text)

    emit_progress(
        "strategy",
        f"Generated {len(queries)} search queries; starting the search",
        count=len(queries),
    )
    return {"queries": queries}


__all__ = ["propose_strategy_node", "select_generated_queries_node"]
