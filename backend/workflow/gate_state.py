"""What a reviewer needs to see at a gate, read from the run's own checkpoint.

## Why the checkpoint and not the event log

The event log answers "what has the client been told?" and is append-only; the
checkpoint answers "where is execution, and with what state?" (§3.4). A gate
payload is the second question: the reviewer is looking at the decomposition the
machine just proposed, or the candidate set it just built, and that lives in graph
state.

Copying it into `workflow_events` instead would put mutable state into an
append-only store, where the copy starts going stale the moment a rerun or a
manual candidate changes it -- which is exactly why `review_required` carries
**counts and a URL** rather than the artifact, and why this module exists to
serve that URL.

## This is a read, and only a read

Nothing here writes a checkpoint, an approval, an event or a status. It opens the
saver, asks for the latest state of one thread, and returns plain data. A caller
that cannot reach the checkpointer gets `None` and the endpoint reports that
honestly rather than rendering an empty gate -- an empty decomposition and an
unreachable database must not look the same.

**Ownership is the caller's job.** This module takes a `run_id` and no actor, so
every endpoint resolves the run with `actor_email` *first*; that ordering is
asserted in `tests/test_run_ownership.py` rather than trusted here.
"""

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def read_state(run_id: str) -> Optional[dict[str, Any]]:
    """The latest checkpointed graph state for `run_id`, or `None`.

    `None` means the state could not be read -- no checkpoint yet, or the
    checkpointer is unreachable. It never means "empty": a caller must be able to
    tell those apart, because rendering an unreachable database as an empty gate
    is the substitution the zero-tolerance policy forbids.
    """
    try:
        from checkpointer import checkpointer
        from graph.pipeline import build_graph_with_checkpointer

        with checkpointer() as saver:
            graph = build_graph_with_checkpointer(saver)
            snapshot = graph.get_state({"configurable": {"thread_id": run_id}})
            values = getattr(snapshot, "values", None)
            return dict(values) if isinstance(values, dict) else None
    except Exception as error:  # noqa: BLE001 - reported, never swallowed into {}
        logger.warning(
            "could not read checkpointed state for run %s: %s", run_id, type(error).__name__
        )
        return None


def gate_a_payload(state: dict[str, Any]) -> dict[str, Any]:
    """What the Invention Understanding stage shows.

    The field is called `candidate_decomposition` and not `decomposition`, here
    and in the graph, because the name is the guard: `verify_features_node` reads
    `approved_decomposition` and has no path to this one.

    The inventor's own submission travels beside the proposal so the screen can
    show them together -- which is the comparison the stage exists to present.
    `original_description` is read straight from state and is **never** the
    summary: the summary is the machine's interpretation, the description is what
    the inventor wrote, and the whole point of this screen is that they are two
    different things a person can compare.
    """
    return {
        "original_description": state.get("description") or "",
        "problem_solved": state.get("problem_solved") or "",
        "inventor_novelty_statement": state.get("inventor_novelty_statement") or "",
        "candidate_decomposition": state.get("candidate_decomposition"),
    }


def gate_strategy_payload(state: dict[str, Any]) -> dict[str, Any]:
    """What the SEARCH STRATEGY stage shows.

    The field is called `candidate_search_strategy` and not `search_strategy`,
    here and in the graph, because the name is the guard: the search reads
    `queries`, which is written from the **approved** strategy by
    `gate_strategy_node` and by nothing else.

    The approved summary travels beside it so the reviewer can judge the queries
    against the understanding they approved, rather than against their own memory
    of it. It is shown read-only: this stage approves queries, and changing the
    understanding here would edit an artifact that was already approved at its own
    version.
    """
    strategy = state.get("candidate_search_strategy") or None
    return {
        "candidate_search_strategy": strategy,
        "approved_summary": state.get("approved_summary") or "",
        "understanding_version": state.get("decomposition_version"),
        # The approved elements, so the screen can render coverage against real
        # ids rather than asking the reviewer to hold them in their head. Read
        # from the approved decomposition in state -- which `gate_a_node` filled
        # from the approved-only readers -- never from the candidate one.
        "elements": _approved_elements(state),
    }


def _approved_elements(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Every approved element, feature-qualified, with whether it is core.

    Qualified (`F1.E2`) because a requirement id is unique within one feature and
    not across a run: two features may each have an `E1`, and a coverage claim
    that resolved to both would report a core element as covered when nobody
    searched for it.
    """
    approved = state.get("approved_decomposition") or {}
    elements: list[dict[str, Any]] = []
    for feature in approved.get("features") or ():
        feature_id = str(feature.get("feature_id") or "")
        for element in feature.get("elements") or ():
            elements.append(
                {
                    "id": f"{feature_id}.{element.get('element_id')}",
                    "feature_id": feature_id,
                    "feature_text": str(feature.get("feature_text") or ""),
                    "text": str(element.get("text") or ""),
                    "core": bool(element.get("core")),
                }
            )
    return elements


def gate_b_payload(state: dict[str, Any]) -> dict[str, Any]:
    """What Workflow Gate B shows: eligible candidates, and exclusions separately.

    **Eligibility is derived from passages actually built** (§3.7), not asserted:
    a publication appears as a candidate only if it has at least one passage. That
    is the same rule `confirm_eligibility` applies, read the same way, so the
    screen cannot offer a candidate the verifier would refuse.

    Exclusions are returned in their **own** list. They are never candidates and
    never matrix cells (case AH): an exclusion means no comparison was attempted.
    """
    passages = state.get("passages_by_publication") or {}
    enrichment = state.get("enrichment") or {}
    exclusions = list(state.get("publication_exclusions") or ())
    excluded_numbers = {
        str(item.get("normalized_number") or item.get("requested_number"))
        for item in exclusions
    }

    candidates = []
    for publication, built in passages.items():
        if not built or publication in excluded_numbers:
            continue
        entry = enrichment.get(publication) or {}
        result = entry.get("result")
        candidates.append({
            "publication_number": publication,
            "normalized_number": publication,
            "passages_built": len(built),
            "sections_retrieved": list(getattr(result, "sections_retrieved", ()) or ()),
            "publication_date": getattr(result, "text_version_normalized", "") or "",
            "source": _source_for(state, publication),
        })

    return {
        "ranked_candidates": sorted(candidates, key=lambda item: item["publication_number"]),
        "excluded": exclusions,
    }


def _source_for(state: dict[str, Any], publication: str) -> str:
    """`discovered` or the manual marker, so a report never presents a
    publication nobody's search returned as though one did."""
    for candidate in state.get("candidates") or ():
        identifier = getattr(candidate, "id", "") or ""
        if identifier and publication.upper().startswith(identifier[:7].upper()):
            return getattr(candidate, "source", "") or "discovered"
    return "discovered"


__all__ = ["gate_a_payload", "gate_b_payload", "gate_strategy_payload", "read_state"]
