"""Provider-neutral enrichment routing: **the §17.2 fix**.

**The defect.** `search/epo.py`'s `enrich_with_full_text` selects its targets
with ``patent.source == "European Patent Office"``. Under the approved workflow
every candidate arrives from the *discovery* provider -- Lens.org today, Google
Patents before it -- so that target list is **empty**. Enrichment would silently
no-op, every candidate would come back with no text, and every one would fall to
`EXCLUDED_NO_FULL_TEXT`: a total failure that is indistinguishable from a
legitimate coverage result, because both look like "EPO had nothing".

The guard assumed each provider enriches only what it found. That is exactly
backwards for this workflow: **a discovery source and an enrichment source are
different roles.** Whatever `SOURCES` enables does discovery; EPO supplies
trustworthy full text for the candidates it found, regardless of who found them.

So there is no source guard here at all. Every approved candidate is routed to
the enrichment provider, and `PatentRecord.source` is treated as what it is --
*provenance metadata about where the record came from*, not permission to enrich
it.

**This module changes no discovery behaviour.** `SOURCES` is untouched,
`search/epo.py` is untouched, and nothing here is reachable from
`POST /api/analyze`.
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Optional

from evidence.normalize import PublicationNumberError, normalize
from models import EnrichmentResult, EnrichmentStatus, PatentRecord, TextVersionSource
from search.epo_fulltext import EpoEnrichmentFailed, enrich_publication

logger = logging.getLogger(__name__)

MAX_ENRICHMENT_WORKERS = 4
"""Concurrent EPO calls.

Lower than the search fan-out's 8: EPO OPS is rate-limited, this project's
credentials have been throttled before, and each publication costs three calls
(claims, description, biblio). Four is a deliberate ceiling, not a tuned number.
"""

ENRICHMENT_PROVIDER = "EPO OPS"
"""The single enrichment source for this gate. Named as data rather than assumed
in a comparison, so adding a second one is a routing change here and not a
condition scattered through the pipeline."""


@dataclass(frozen=True, slots=True)
class EnrichmentOutcome:
    """One candidate's enrichment, or the failure that stopped it.

    Failures are carried rather than raised *per candidate* so the caller can
    decide -- and `enrich_candidates` does raise, because a provider outage fails
    the run. Keeping the failure attached to its publication is what lets that
    message name which one.
    """

    record: PatentRecord
    result: Optional[EnrichmentResult] = None
    failure: Optional[Exception] = None

    @property
    def failed(self) -> bool:
        return self.failure is not None


def enrichment_targets(candidates: list[PatentRecord]) -> list[PatentRecord]:
    """Every candidate with a usable publication number. **No source guard.**

    The only thing filtered out is a record whose id cannot be parsed as a
    publication number at all -- and that is not a provider judgement, it is the
    absence of the identifier the lookup needs.

    A candidate from the discovery provider **is** enriched. That sentence is the
    whole fix, and `tests/test_epo_enrichment.py` pins it specifically.
    """
    targets: list[PatentRecord] = []
    for candidate in candidates:
        if not candidate.id or not candidate.id.strip():
            continue
        try:
            normalize(candidate.id)
        except PublicationNumberError:
            logger.warning(
                "candidate %r has no parseable publication number; it cannot be "
                "enriched and will be excluded rather than guessed at",
                candidate.id,
            )
            continue
        targets.append(candidate)
    return targets


def enrich_candidate(
    candidate: PatentRecord,
    *,
    enricher: Optional[Callable[..., EnrichmentResult]] = None,
    **kwargs: object,
) -> EnrichmentOutcome:
    """Enrich one candidate, keeping any failure attached to it.

    `enricher` defaults to `None` and is resolved to the module-level
    `enrich_publication` **at call time**, not bound as a default at import.

    That distinction is the difference between a seam and a decoration. A default
    argument is evaluated once when the function is defined, so patching
    `search.enrichment.enrich_publication` afterwards changed nothing -- the old
    function was already captured. Mock mode looked correctly configured and the
    run still tried to reach EPO, failing with an authentication error that named
    a provider nobody meant to call.
    """
    call = enricher or enrich_publication
    try:
        return EnrichmentOutcome(record=candidate, result=call(candidate.id, **kwargs))
    except Exception as error:  # noqa: BLE001 - re-raised by the caller
        return EnrichmentOutcome(record=candidate, failure=error)


def enrich_candidates(
    candidates: list[PatentRecord],
    *,
    enricher: Optional[Callable[..., EnrichmentResult]] = None,
    max_workers: int = MAX_ENRICHMENT_WORKERS,
    **kwargs: object,
) -> list[EnrichmentOutcome]:
    """Enrich every approved candidate, in parallel, failing loudly.

    **A failure fails the run.** One unreachable provider means the candidate set
    was never fully assessed, and proceeding would present a partial matrix as a
    complete one -- the same substitution the zero-tolerance policy forbids,
    moved into coverage.

    `NO_TEXT` is not a failure: the provider answered, and had none. That is a
    real answer about the document and becomes `EXCLUDED_NO_FULL_TEXT`.

    Raises:
        EpoEnrichmentFailed: naming the publication that failed. Its message is
            already redacted at the provider boundary.
    """
    targets = enrichment_targets(candidates)
    if not targets:
        return []

    outcomes: list[EnrichmentOutcome] = []
    with ThreadPoolExecutor(max_workers=min(len(targets), max_workers)) as executor:
        futures = [
            executor.submit(enrich_candidate, candidate, enricher=enricher, **kwargs)
            for candidate in targets
        ]
        for future in futures:
            outcomes.append(future.result())

    failures = [outcome for outcome in outcomes if outcome.failed]
    if failures:
        first = failures[0]
        raise EpoEnrichmentFailed(
            f"enrichment failed for {len(failures)} of {len(targets)} candidates; "
            f"first: {first.record.id}: {first.failure}"
        )

    counts: dict[str, int] = {}
    for outcome in outcomes:
        counts[str(outcome.result.status)] = counts.get(str(outcome.result.status), 0) + 1
    logger.info(
        "enrichment via %s: %d candidate(s), %s",
        ENRICHMENT_PROVIDER,
        len(targets),
        ", ".join(f"{status}={count}" for status, count in sorted(counts.items())),
    )
    return outcomes
