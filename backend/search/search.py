"""Provider-agnostic patent search orchestration."""

import logging
from collections.abc import Callable

from models import PatentRecord
from tracing import trace
from .epo import search_epo
from .google import enrich_with_full_text, search_google

logger = logging.getLogger(__name__)
Provider = Callable[[str, int], list[PatentRecord]]
SOURCES: tuple[tuple[str, Provider], ...] = (
    ("Google Patents", search_google),
    ("European Patent Office", search_epo),
)


class PatentSearchError(RuntimeError):
    """A provider call failed.

    ``search_patents`` (legacy) raises this only when every provider failed.
    ``search_all_providers_strict`` (LangGraph) raises it when any single one
    does.
    """


__all__ = ["PatentSearchError", "search_patents", "enrich_with_full_text"]


def merge_patents(patents: list[PatentRecord]) -> list[PatentRecord]:
    """Deduplicate records by normalized publication identifier.

    This deliberately avoids fuzzy title/family matching until stable source
    metadata for those fields is available.
    """
    seen: set[str] = set()
    merged: list[PatentRecord] = []
    dropped = 0
    for patent in patents:
        patent_id = patent.id.strip().upper()
        if not patent_id or patent_id in seen:
            dropped += 1
            continue
        seen.add(patent_id)
        merged.append(patent)
    trace("search.merge", raw_count=len(patents), unique_count=len(merged), dropped=dropped)
    return merged


def search_patents(
    queries: list[str], results_per_query: int = 10
) -> list[PatentRecord]:
    """Search every provider for every query and return normalized records.

    A failure from an individual source is isolated.  The pipeline fails only
    when no provider call succeeded at all.
    """
    all_patents: list[PatentRecord] = []
    successful_calls = 0
    for query in filter(None, (query.strip() for query in queries)):
        for provider_name, provider in SOURCES:
            try:
                results = provider(query, results_per_query)
                successful_calls += 1
                all_patents.extend(results)
                logger.info("%s returned %d patents for %r", provider_name, len(results), query)
                trace("search.provider.ok", provider=provider_name, query=query, count=len(results))
            except Exception as error:
                logger.exception("%s search failed for %r", provider_name, query)
                trace("search.provider.failed", provider=provider_name, query=query, error=str(error))

    if not successful_calls:
        raise PatentSearchError("All patent search providers failed")

    merged = merge_patents(all_patents)
    logger.info("Merged %d search records into %d unique patents", len(all_patents), len(merged))
    return merged


def search_all_providers_strict(
    queries: list[str], results_per_query: int = 10
) -> list[PatentRecord]:
    """Search every provider for every query, failing if any single call fails.

    This is :func:`search_patents` without the partial-success behavior, for the
    LangGraph pipeline.  The two exist side by side until ticket P1.5 retires
    the legacy path.

    The difference is the whole point.  ``search_patents`` treats a run as
    successful when at least one provider worked, which means an EPO outage
    silently halves the corpus and the user is shown a novelty verdict computed
    against prior art the system never actually retrieved.  The verdict looks
    identical to a healthy one.  Here, a provider that fails fails the request.

    Raises:
        PatentSearchError: if any provider call raises, wrapping the original
            error so the real cause survives.
    """
    all_patents: list[PatentRecord] = []
    for query in filter(None, (query.strip() for query in queries)):
        for provider_name, provider in SOURCES:
            try:
                results = provider(query, results_per_query)
            except Exception as error:
                raise PatentSearchError(
                    f"{provider_name} failed for query {query!r}: {error}"
                ) from error
            all_patents.extend(results)
            logger.info("%s returned %d patents for %r", provider_name, len(results), query)

    merged = merge_patents(all_patents)
    logger.info("Merged %d search records into %d unique patents", len(all_patents), len(merged))
    return merged
