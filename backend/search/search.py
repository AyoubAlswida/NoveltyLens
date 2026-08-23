"""Provider-agnostic patent search orchestration."""

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import NamedTuple

from models import PatentRecord
from tracing import trace
from .epo import search_epo
from .google import enrich_with_full_text, search_google
from .lens import search_lens

logger = logging.getLogger(__name__)
Provider = Callable[[str, int], list[PatentRecord]]
SOURCES: tuple[tuple[str, Provider], ...] = (
    # ("Google Patents", search_google),  # TEMP: disabled - swapped out for
    # Lens.org while trialling it as a replacement source. Not removed - see
    # search/google.py, which is untouched and fully functional.
    # ("European Patent Office", search_epo),  # TEMP: disabled - EPO's search casts a
    # wide net across every national office, but its full-text service only covers
    # EP/WO-family documents, so most EPO finds get dropped by keyword_search_node's
    # "no full text = drop" rule anyway, wasting filter slots. Not removed - see
    # search/epo.py, which is untouched and fully functional.
    ("Lens.org", search_lens),  # TEMP: LENS_API_KEY is a 14-day non-commercial
    # trial token (see search/lens.py's module docstring) - every call here
    # spends against that trial.
)

MAX_SEARCH_WORKERS = 8
"""How many (query, provider) calls are in flight at once.

Every pair is an independent outbound HTTP request, so running them one at a
time made search latency scale linearly with query count for no reason:
5 queries measured 13.4s serially, 11% of a 123s request, essentially all of it
spent waiting. The cap bounds outbound concurrency against providers that meter
per call - it is not a throughput knob to raise casually.
"""

FETCH_PER_QUERY = 10
"""How many results each (query, provider) call asks for.

SerpAPI bills per *call*, not per result, and its Google Patents engine has
always returned 10 for the price of one - ``search_google`` was requesting 10
and discarding all but ``results_per_query`` of them. Across 5 queries that
threw away ~30 patents per search that had already been paid for.

The discards now become ``index_only``: not enriched, not ranked, just indexed
into Qdrant so the semantic corpus grows ~2.5x faster per search **at zero extra
provider cost**. Enrichment is what actually costs a credit per patent (a
separate Details call), which is why ``results_per_query`` stays small and
separate from this.
"""


class KeywordSearchResults(NamedTuple):
    """What one keyword-search pass produced, split by what it is worth spending on.

    Two lists rather than one because the two have different economics:
    ``candidates`` cost a Details API credit each to enrich and LLM tokens to
    validate and explain, while ``index_only`` costs nothing beyond an embedding
    and exists purely to grow the corpus for later requests.
    """

    candidates: list[PatentRecord]
    """Top ``results_per_query`` per query. Enriched, fused, validated, ranked."""

    index_only: list[PatentRecord]
    """The rest of what the providers returned. Never enriched and never shown -
    indexed into Qdrant so a *future* search can find them semantically."""


class PatentSearchError(RuntimeError):
    """A provider call failed.

    Raised when any single provider/query call fails. There is no
    partial-success mode: a provider outage that silently narrowed the corpus
    would still produce a confident novelty verdict, computed against prior art
    that was never retrieved and indistinguishable from a healthy run.
    """


__all__ = [
    "PatentSearchError",
    "search_all_providers_strict",
    "enrich_with_full_text",
    "merge_patents",
]


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


def search_all_providers_strict(
    queries: list[str],
    results_per_query: int = 10,
    fetch_per_query: int = FETCH_PER_QUERY,
) -> KeywordSearchResults:
    """Search every provider for every query, failing if any single call fails.

    Asks each provider for ``fetch_per_query`` results but promotes only the top
    ``results_per_query`` of each call to ``candidates``; the remainder comes
    back as ``index_only``. Taking the top N *per call* rather than the first N
    of the flattened list is deliberate - the latter would fill the candidate
    set from the first query or two and silently discard the perspectives the
    other generated queries were written to cover.

    Calls are dispatched concurrently but their **results are collected in the
    original query-major order**, which matters: ``merge_patents`` keeps the
    first record it sees for a given id, so collecting in completion order would
    make which copy of a duplicate survives depend on network timing, and the
    same search could return subtly different records run to run.

    Raises:
        PatentSearchError: if any provider call raises, wrapping the original
            error so the real cause survives. Reported in query order too, so
            the same set of failures always names the same one.
    """
    tasks = [
        (provider_name, provider, query)
        for query in filter(None, (query.strip() for query in queries))
        for provider_name, provider in SOURCES
    ]
    if not tasks:
        return KeywordSearchResults(candidates=[], index_only=[])

    top: list[PatentRecord] = []
    rest: list[PatentRecord] = []
    with ThreadPoolExecutor(max_workers=min(len(tasks), MAX_SEARCH_WORKERS)) as executor:
        pending = [
            (provider_name, query, executor.submit(provider, query, fetch_per_query))
            for provider_name, provider, query in tasks
        ]
        for provider_name, query, future in pending:
            try:
                results = future.result()
            except Exception as error:
                raise PatentSearchError(
                    f"{provider_name} failed for query {query!r}: {error}"
                ) from error
            top.extend(results[:results_per_query])
            rest.extend(results[results_per_query:])
            logger.info("%s returned %d patents for %r", provider_name, len(results), query)

    candidates = merge_patents(top)
    # Dedupe the remainder against itself *and* against the candidate set, so a
    # patent promoted by one query is not also indexed as an extra by another.
    promoted = {patent.id.strip().upper() for patent in candidates}
    index_only = [
        patent for patent in merge_patents(rest)
        if patent.id.strip().upper() not in promoted
    ]

    logger.info(
        "Keyword search: %d candidates to enrich, %d extra patents for the index",
        len(candidates), len(index_only),
    )
    return KeywordSearchResults(candidates=candidates, index_only=index_only)
