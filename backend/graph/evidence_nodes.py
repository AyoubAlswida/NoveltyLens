"""Gate B's evidence modules, wrapped as graph nodes.

**These are thin on purpose, and "thin" is a testable property.** Every decision
in this file already has a home: normalization in `evidence/normalize.py`, date
screening in `evidence/dates.py`, enrichment in `search/enrichment.py`, passage
construction in `evidence/passages.py`, eligibility in `evidence/eligibility.py`.
A node that grew logic of its own would have behaviour reachable **only** through
a graph run — and a graph run needs a compiled `StateGraph`, a stream writer and
a state dict, which is a great deal of machinery to stand between a rule and the
test that pins it. `tests/test_verification_integration.py` asserts each node's
output equals what the pure function returns for the same input.

**The order is §4's, and each step narrows the set:**

```
normalize_publications -> screen_dates -> enrich_epo -> build_passages
                       -> confirm_eligibility -> rank_and_reduce
```

**Exclusions accumulate; they never become cells.** `EXCLUDED_DATE`,
`EXCLUDED_NO_FULL_TEXT` and `EXCLUDED_INELIGIBLE` are properties of a
*publication*. Each node appends to `publication_exclusions` and drops that
publication from the working set. Nothing here creates a `verification_cells`
row, and there is no code path that could: a cell implies a comparison was
attempted, and an excluded publication is one no comparison was attempted for.

**A provider failure is never an exclusion.** `enrich_epo` lets
`EpoEnrichmentFailed` propagate, which fails the run. That is the Gate B rule
carried into the graph unchanged — see `docs/decisions/2026-08-28-evidence-layer-policies.md`
decision 3a.
"""

import logging
from typing import Any, Optional

from evidence.dates import (
    DateScreeningOutcome,
    ScreeningStage,
    build_publication_date,
    parse_patent_date,
    screen,
)
from evidence.eligibility import ExclusionState, PublicationExclusion, confirm_eligibility
from evidence.normalize import PublicationNumberError, normalize
from evidence.passages import build_passages
from langchain_core.runnables import RunnableConfig

from graph import durable
from graph.events import emit_progress
from graph.state import GraphState
from models import EnrichmentStatus
from search.enrichment import enrich_candidates, enrichment_targets

logger = logging.getLogger(__name__)

MAX_APPROVED_CANDIDATES = 15
"""The cap `rank_and_reduce` narrows to before Workflow Gate B.

A **reviewed constant** (spec §16 decision 5), not a tuning knob: 10-15 is what a
human can actually curate in one sitting, and it bounds the matrix that follows
at `features x publications` LLM calls. Raising it raises the number of cells a
reviewer must read at Workflow Gate C, which is the real limit -- not the quota.
"""


def _exclusion_payload(exclusion: PublicationExclusion) -> dict[str, Any]:
    """One exclusion, flattened for `GraphState` and for the Gate B panel.

    Every field a reviewer needs to challenge the exclusion is here, including
    `passages_built` -- an `EXCLUDED_INELIGIBLE` row with
    `sections_retrieved=['DESCRIPTION']` and `passages_built=0` says exactly why:
    text was there, a citable locator was not.
    """
    return {
        "requested_number": exclusion.requested_number,
        "normalized_number": exclusion.normalized_number,
        "served_number": exclusion.served_number,
        "state": str(exclusion.state),
        "reason": exclusion.reason,
        "failing_check": exclusion.failing_check,
        "language_attempted": exclusion.language_attempted,
        "sections_available": list(exclusion.sections_available),
        "sections_retrieved": list(exclusion.sections_retrieved),
        "passages_built": exclusion.passages_built,
        "provider_detail": exclusion.provider_detail,
    }


def normalize_publications_node(state: GraphState) -> dict[str, Any]:
    """Publication ids to epodoc form, with the kind code stripped.

    An unparseable number is **excluded, not guessed at**. A number quietly
    corrected into a different real document would attach that document's text to
    this candidate, which is the worst failure available here: a confident
    citation to the wrong patent.
    """
    candidates = state.get("candidates") or []
    emit_progress(
        "normalize", f"Normalizing {len(candidates)} publication numbers…",
        count=len(candidates),
    )

    normalized: dict[str, str] = {}
    kept = []
    exclusions = list(state.get("publication_exclusions") or [])

    for candidate in candidates:
        try:
            number = normalize(candidate.id)
        except PublicationNumberError as error:
            exclusions.append(
                _exclusion_payload(
                    PublicationExclusion(
                        requested_number=candidate.id or "",
                        normalized_number="",
                        state=ExclusionState.EXCLUDED_INELIGIBLE,
                        reason=(
                            f"{candidate.id!r} is not a parseable publication number, "
                            "so no document can be retrieved for it"
                        ),
                        failing_check=type(error).__name__,
                    )
                )
            )
            continue
        normalized[candidate.id] = number.normalized
        kept.append(candidate)

    return {
        "candidates": kept,
        "normalized_publications": normalized,
        "publication_exclusions": exclusions,
    }


def screen_dates_node(state: GraphState) -> dict[str, Any]:
    """Screen every candidate against the run's cutoff, under the approved policy.

    The policy and the cutoff are read from the **run**, not from settings: a
    finished run's exclusions must always be explicable by the rules it was
    actually executed under, and settings can change afterwards.

    A run with no cutoff screens nothing -- there is no comparison to be uncertain
    *about*, so an unparseable date is not held against a publication in a run
    that was never date-limited.
    """
    candidates = state.get("candidates") or []
    cutoff = parse_patent_date(state.get("cutoff_date") or "")
    policy = state.get("date_policy") or ""

    emit_progress(
        "screen_dates",
        f"Screening {len(candidates)} publications against the cutoff…"
        if cutoff
        else f"No cutoff on this run; {len(candidates)} publications pass unscreened.",
        count=len(candidates),
    )

    kept = []
    exclusions = list(state.get("publication_exclusions") or [])
    dates: dict[str, dict[str, Any]] = dict(state.get("publication_dates") or {})

    for candidate in candidates:
        publication_date = build_publication_date(
            publication_number=state["normalized_publications"][candidate.id],
            # `publication_date`, which is what `PatentRecord` actually calls
            # this field. It read `candidate.date` and raised `AttributeError`
            # the first time the evidence branch ran end to end against real
            # discovery records -- the branch had only ever been exercised with
            # constructed state, so nothing caught it.
            date_original=candidate.publication_date or "",
            date_source=f"{candidate.source}.date",
            stage=ScreeningStage.DISCOVERY,
        )
        result = screen(publication_date, cutoff=cutoff, policy=policy)
        dates[publication_date.publication_number] = {
            "publication_number": publication_date.publication_number,
            "date_original": publication_date.date_original,
            "date_normalized": (
                publication_date.date_normalized.isoformat()
                if publication_date.date_normalized
                else None
            ),
            "date_source": publication_date.date_source,
            "date_certainty": str(publication_date.date_certainty),
            "screened_at_stage": str(publication_date.screened_at_stage),
        }

        if result.outcome is DateScreeningOutcome.EXCLUDED_DATE:
            exclusions.append(
                _exclusion_payload(
                    PublicationExclusion(
                        requested_number=candidate.id,
                        normalized_number=publication_date.publication_number,
                        state=ExclusionState.EXCLUDED_DATE,
                        reason=result.reason,
                    )
                )
            )
            continue
        kept.append(candidate)

    return {
        "candidates": kept,
        "publication_dates": dates,
        "publication_exclusions": exclusions,
    }


def enrich_epo_node(state: GraphState) -> dict[str, Any]:
    """Fetch full text from EPO for every remaining candidate.

    **`FAILED` is not handled here, and that is the point.** `enrich_candidates`
    raises on any provider failure and the exception propagates to the executor,
    which fails the run. Catching it to record an exclusion would report a
    provider outage as a fact about a patent and would understate the product's
    own coverage using the provider's downtime.

    `NO_TEXT` **is** handled: the provider answered and had none, which is a real
    answer about the document and becomes `EXCLUDED_NO_FULL_TEXT`.
    """
    candidates = state.get("candidates") or []
    targets = enrichment_targets(candidates)
    emit_progress(
        "enrich_epo", f"Retrieving full text for {len(targets)} publications…",
        count=len(targets),
    )

    outcomes = enrich_candidates(targets)

    enrichment: dict[str, dict[str, Any]] = {}
    kept = []
    exclusions = list(state.get("publication_exclusions") or [])

    for outcome in outcomes:
        result = outcome.result
        assert result is not None, "enrich_candidates raises rather than returning a failure"
        if result.status is EnrichmentStatus.NO_TEXT:
            exclusions.append(
                _exclusion_payload(
                    PublicationExclusion(
                        requested_number=result.requested_publication,
                        normalized_number=result.normalized_publication,
                        state=ExclusionState.EXCLUDED_NO_FULL_TEXT,
                        reason=(
                            "EPO answered and holds no eligible full text for this "
                            "publication; it was not evaluated for feature disclosure"
                        ),
                        language_attempted=result.language_attempted,
                        sections_available=result.sections_available,
                    )
                )
            )
            continue
        enrichment[result.normalized_publication] = {"result": result}
        kept.append(outcome.record)

    return {
        "candidates": kept,
        "enrichment": enrichment,
        "publication_exclusions": exclusions,
    }


def build_passages_node(
    state: GraphState, config: Optional[RunnableConfig] = None
) -> dict[str, Any]:
    """Turn retrieved sections into citable passages.

    Claim evidence comes from EPO's own numbered claims and description evidence
    from provider paragraph ids or reproducible character ranges. Discovery
    metadata is **not** an input here -- see `evidence/passages.py`, whose
    `build_passages` takes no `PatentRecord` at all.
    """
    enrichment = state.get("enrichment") or {}
    emit_progress(
        "build_passages", f"Building citable passages for {len(enrichment)} publications…",
        count=len(enrichment),
    )

    by_publication: dict[str, list[Any]] = {}
    for publication, entry in enrichment.items():
        by_publication[publication] = build_passages(entry["result"])

    # The passage of record, written under the fence the executor holds now. A
    # locator in an approved report resolves against these rows, not against a
    # checkpoint that compaction is free to drop.
    durable.persist_passages(
        str(state.get("run_id") or ""),
        config,
        [built for passages in by_publication.values() for built in passages],
    )

    return {"passages_by_publication": by_publication}


def confirm_eligibility_node(state: GraphState) -> dict[str, Any]:
    """Eligibility, **derived** from the passages that were actually built.

    A publication is eligible exactly when at least one valid passage exists for
    it. There is no separate assertion to disagree with the evidence: "eligible
    with nothing to cite" is not a state this can produce.
    """
    enrichment = state.get("enrichment") or {}
    passages = state.get("passages_by_publication") or {}
    emit_progress("confirm_eligibility", f"Confirming eligibility for {len(enrichment)} publications…")

    kept = []
    eligible_passages: dict[str, list[Any]] = {}
    exclusions = list(state.get("publication_exclusions") or [])

    for candidate in state.get("candidates") or []:
        publication = state["normalized_publications"][candidate.id]
        entry = enrichment.get(publication)
        if entry is None:
            continue
        result = confirm_eligibility(entry["result"], passages.get(publication, []))
        if not result.eligible:
            exclusions.append(_exclusion_payload(result.exclusion))
            continue
        eligible_passages[publication] = list(result.passages)
        kept.append(candidate)

    return {
        "candidates": kept,
        "passages_by_publication": eligible_passages,
        "publication_exclusions": exclusions,
    }


def rank_and_reduce_node(state: GraphState) -> dict[str, Any]:
    """Order the eligible publications and cap the set a human will curate.

    **This produces `ranked_candidates`, which is machine output and is never an
    approved set.** Workflow Gate B is where a human turns this list into
    `approved_candidate_set`; nothing here writes that field, and
    `workflow/approvals.py` is the only module that can.

    Ordering is by the similarity the search pipeline already measured, so this
    node introduces no scoring of its own -- one more thing that cannot silently
    disagree with what the report shows.
    """
    candidates = state.get("candidates") or []
    order = {
        comparison.patent.id: index
        for index, comparison in enumerate(state.get("comparisons") or [])
    }
    ranked = sorted(candidates, key=lambda candidate: order.get(candidate.id, len(order)))
    reduced = ranked[:MAX_APPROVED_CANDIDATES]

    emit_progress(
        "rank_candidates",
        f"{len(reduced)} publications ready for review…",
        count=len(reduced),
    )
    return {
        "candidates": reduced,
        "ranked_candidates": [
            state["normalized_publications"][candidate.id] for candidate in reduced
        ],
    }
