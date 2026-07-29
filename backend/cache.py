"""Postgres-backed patent search cache (ticket P2.2).

Two TTLs, chosen so the cache trades staleness for speed/cost without
becoming a correctness problem -- an unbounded cache is explicitly called
out as one in the migration plan:

- PATENT_TTL: patent bibliographic data is essentially immutable once
  published, so a cached PatentRecord is trusted for 30 days before a live
  re-fetch is allowed to refresh it (e.g. picking up a rare source
  correction).
- QUERY_TTL: which patents a given query text matches DOES change over
  time, as new patents are published and indexed. A cached query result is
  only trusted for 7 days -- short enough that a novelty search won't
  silently miss recently-published prior art.

A cache MISS (row absent, or present but past its TTL) is normal; the
caller falls through to a live provider call. A database error (connection
failure, broken query) is NOT a cache miss -- every function here lets
psycopg exceptions propagate uncaught, so an unreachable database fails the
request instead of being silently treated as "nothing cached, try live."
"""

from datetime import timedelta

import psycopg

from models import PatentRecord

PATENT_TTL = timedelta(days=30)
QUERY_TTL = timedelta(days=7)


def _normalize(patent_id: str) -> str:
    """Match search/search.py's merge_patents() dedupe key exactly."""
    return patent_id.strip().upper()


def get_cached_query_result(conn: psycopg.Connection, query: str) -> list[PatentRecord] | None:
    """Return the cached patents for `query`, or None on a cache miss."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT patent_ids FROM query_cache WHERE query = %s AND fetched_at > now() - %s",
            (query, QUERY_TTL),
        )
        row = cur.fetchone()
        if row is None:
            return None
        patent_ids: list[str] = row[0]

        cur.execute(
            "SELECT id, title, abstract, inventors, assignee, publication_date, source, url, serpapi_id, full_text "
            "FROM patents WHERE id = ANY(%s)",
            (patent_ids,),
        )
        by_id = {
            r[0]: PatentRecord(
                id=r[0],
                title=r[1],
                abstract=r[2],
                inventors=list(r[3]),
                assignee=r[4],
                publication_date=r[5],
                source=r[6],
                url=r[7],
                serpapi_id=r[8],
                full_text=r[9],
            )
            for r in cur.fetchall()
        }

    missing = [pid for pid in patent_ids if pid not in by_id]
    if missing:
        # query_cache pointing at patent rows that don't exist is a bug in
        # this cache, not an ordinary miss -- surface it instead of quietly
        # returning a truncated, wrong result set.
        raise RuntimeError(f"query_cache references patent IDs missing from patents cache: {missing}")
    return [by_id[pid] for pid in patent_ids]


def _upsert_patents(cur: psycopg.Cursor, patents: list[PatentRecord]) -> None:
    """Insert-or-refresh the patent cache rows for `patents`.

    Shared by both write paths: search.py calls this (via
    store_query_result) BEFORE full-text enrichment runs, when every
    record's full_text is still empty; server.py calls it again (via
    store_enriched_patents) AFTER enrichment, once full_text is populated.
    The CASE WHEN preserves whichever full_text is already on record when a
    write carries an empty one, so the first write never clobbers what the
    second one later fills in, and neither write can erase a good value
    left by an earlier, unrelated query that happened to surface the same
    patent.
    """
    for patent in patents:
        cur.execute(
            """
            INSERT INTO patents (id, title, abstract, inventors, assignee, publication_date, source, url, serpapi_id, full_text, fetched_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (id) DO UPDATE SET
                title = EXCLUDED.title,
                abstract = EXCLUDED.abstract,
                inventors = EXCLUDED.inventors,
                assignee = EXCLUDED.assignee,
                publication_date = EXCLUDED.publication_date,
                source = EXCLUDED.source,
                url = EXCLUDED.url,
                serpapi_id = EXCLUDED.serpapi_id,
                full_text = CASE WHEN EXCLUDED.full_text <> '' THEN EXCLUDED.full_text ELSE patents.full_text END,
                fetched_at = now()
            """,
            (
                _normalize(patent.id),
                patent.title,
                patent.abstract,
                patent.inventors,
                patent.assignee,
                patent.publication_date,
                patent.source,
                patent.url,
                patent.serpapi_id,
                patent.full_text,
            ),
        )


def store_query_result(conn: psycopg.Connection, query: str, patents: list[PatentRecord]) -> None:
    """Upsert `patents` into the patent cache and record this query's result set."""
    with conn.cursor() as cur:
        _upsert_patents(cur, patents)

        patent_ids = [_normalize(p.id) for p in patents]
        cur.execute(
            """
            INSERT INTO query_cache (query, patent_ids, fetched_at)
            VALUES (%s, %s, now())
            ON CONFLICT (query) DO UPDATE SET
                patent_ids = EXCLUDED.patent_ids,
                fetched_at = now()
            """,
            (query, patent_ids),
        )


def store_enriched_patents(conn: psycopg.Connection, patents: list[PatentRecord]) -> None:
    """Refresh the patent cache with post-enrichment full_text.

    Called from server.py after enrich_with_full_text runs. Without this
    second write, a cached patent could only ever carry the empty full_text
    it had at search time (see _upsert_patents' docstring) -- a query-cache
    hit would then never skip re-enrichment, because there would be nothing
    enriched to skip it with.
    """
    with conn.cursor() as cur:
        _upsert_patents(cur, patents)
