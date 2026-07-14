"""Provider-agnostic patent search orchestration."""

import logging
from collections.abc import Callable

from models import PatentRecord
from .epo import search_epo
from .google import search_google

logger = logging.getLogger(__name__)
Provider = Callable[[str, int], list[PatentRecord]]
SOURCES: tuple[tuple[str, Provider], ...] = (
    ("Google Patents", search_google),
    ("European Patent Office", search_epo),
)


class PatentSearchError(RuntimeError):
    """Raised only when every configured provider failed."""


def merge_patents(patents: list[PatentRecord]) -> list[PatentRecord]:
    """Deduplicate records by normalized publication identifier.

    This deliberately avoids fuzzy title/family matching until stable source
    metadata for those fields is available.
    """
    seen: set[str] = set()
    merged: list[PatentRecord] = []
    for patent in patents:
        patent_id = patent.id.strip().upper()
        if not patent_id or patent_id in seen:
            continue
        seen.add(patent_id)
        merged.append(patent)
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
            except Exception:
                logger.exception("%s search failed for %r", provider_name, query)

    if not successful_calls:
        raise PatentSearchError("All patent search providers failed")

    merged = merge_patents(all_patents)
    logger.info("Merged %d search records into %d unique patents", len(all_patents), len(merged))
    return merged
