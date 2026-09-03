"""Persist completed pipeline runs and read them back for the History screen.

``save_report`` is called from ``graph/pipeline.py`` BEFORE the ``result`` SSE
event is yielded. That ordering is deliberate: if persistence fails, the caller
raises instead of yielding, so the stream ends with a clean ``error`` event
rather than telling the user the search succeeded and then silently dropping the
report. See CLAUDE.md's zero-tolerance failure policy.

Nothing here catches a psycopg exception. A down database fails the request.
"""

import psycopg

from models import PatentRecord


def _normalize(patent_id: str) -> str:
    """Match search/search.py's merge_patents() dedupe key exactly."""
    return patent_id.strip().upper()


def _upsert_patents(cur: psycopg.Cursor, patents: list[PatentRecord]) -> None:
    """Insert-or-refresh the ``patents`` rows a run's matches will reference.

    The CASE WHEN preserves whichever ``full_text`` is already on record when a
    write carries an empty one, so a later run that surfaced the same patent
    without enriching it cannot erase the full text an earlier run stored.
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


def save_report(
    conn: psycopg.Connection,
    *,
    description: str,
    claims: str | None,
    concepts: list[str],
    query_count: int,
    recommendations: list[str],
    matches: list[dict[str, object]],
    patents: list[PatentRecord],
) -> str:
    """Store the run and its ranked matches; returns the new run id.

    ``matches`` is the same list that becomes the ``result`` event's ``patents[]``
    array (``graph/ranking.py``'s ``format_matches``) -- each entry already
    carries num/title/risk/rationale/key_overlap/source. ``patents`` is the
    ``PatentRecord`` behind each of those entries, upserted first so
    ``search_run_matches``' foreign key resolves.

    No similarity number is stored. The ``rank`` column below is written from
    the caller's ordering and is now the **only** record of it, since the score
    that ordering was derived from is discarded before this function is
    reached; ``list_history`` therefore orders by ``rank`` and never re-sorts.

    The whole thing runs in one transaction despite the autocommit connection:
    a run whose matches half-inserted would render as a truncated report in
    History, indistinguishable from a genuine short result.

    Called both by ``graph/pipeline.py``'s inline ``persist_run`` and, for a
    durable Advanced Search run, by the executor's ``persist_result`` callback.
    In the latter case this function's transaction block is a savepoint inside
    the executor's outer transaction, not a separate commit.
    """
    with conn.transaction(), conn.cursor() as cur:
        _upsert_patents(cur, patents)

        cur.execute(
            """
            INSERT INTO search_runs (description, claims, concepts, query_count, recommendations)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (description, claims, concepts, query_count, recommendations),
        )
        row = cur.fetchone()
        assert row is not None
        run_id = row[0]

        for rank, match in enumerate(matches):
            cur.execute(
                """
                INSERT INTO search_run_matches (run_id, rank, patent_id, risk, rationale, key_overlap, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    run_id,
                    rank,
                    _normalize(str(match["num"])),
                    match["risk"],
                    match["rationale"],
                    # .get, not [], so a caller built before key_overlap existed
                    # still stores a run rather than failing on a KeyError.
                    list(match.get("key_overlap") or []),
                    match["source"],
                ),
            )
    return str(run_id)


def clear_history(conn: psycopg.Connection) -> int:
    """Delete every stored run and return how many were removed.

    Two statements, in this order and in one transaction:

    1. ``search_runs`` — ``search_run_matches`` goes with it through
       ``ON DELETE CASCADE``.
    2. ``patents`` rows no longer referenced by any match. Since there is no
       query cache any more, that table exists *only* to give a stored match its
       title and url; once no run references a patent, the row is dead weight
       and keeping it would mean "clear all" quietly left the descriptions of
       everything you ever searched for sitting in the database.

    The ``NOT IN`` is written against the surviving matches rather than being
    an unconditional ``DELETE FROM patents``, so this stays correct if a
    selective delete is ever added alongside it.

    Not a soft delete and not recoverable. The caller is responsible for asking
    first; the frontend does that with a two-step confirm.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM search_runs")
        deleted = cur.rowcount
        cur.execute(
            "DELETE FROM patents WHERE id NOT IN (SELECT patent_id FROM search_run_matches)"
        )
    return deleted


def list_history(conn: psycopg.Connection, limit: int = 50) -> list[dict[str, object]]:
    """Return the most recent completed runs with their ranked matches, most
    recent first. Backs ``GET /api/history``.

    Shaped like the ``result`` SSE event's data object, but ``id``/``created_at``
    replace the SSE payload's placeholder ``title``/``date``: those two are
    derived display values rather than stored state ("Today" is hardcoded), so
    a history entry ships the real description and the real timestamp and lets
    the frontend format them the same way it formats a live run.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, description, concepts, query_count, recommendations, created_at
            FROM search_runs
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        runs = cur.fetchall()
        if not runs:
            return []

        run_ids = [row[0] for row in runs]
        # Every field the frontend's PatentMatch declares, so a report reopened
        # from History renders through the same component as a live one with
        # nothing missing. `url` comes from the patents row rather than being
        # snapshotted per run (unlike `source`): it is a link to a page, so the
        # freshest one is the useful one, and a stale link is worse than a
        # changed one.
        cur.execute(
            """
            SELECT srm.run_id, srm.patent_id, p.title, srm.risk, srm.rationale,
                   srm.key_overlap, srm.source, p.url
            FROM search_run_matches srm
            JOIN patents p ON p.id = srm.patent_id
            WHERE srm.run_id = ANY(%s)
            ORDER BY srm.run_id, srm.rank
            """,
            (run_ids,),
        )
        matches_by_run: dict[str, list[dict[str, object]]] = {str(rid): [] for rid in run_ids}
        for run_id, patent_id, title, risk, rationale, key_overlap, source, url in cur.fetchall():
            matches_by_run[str(run_id)].append({
                "num": patent_id,
                "title": title,
                "risk": risk,
                "rationale": rationale,
                "key_overlap": list(key_overlap),
                "source": source,
                "url": url,
            })

    return [
        {
            "id": str(run_id),
            "description": description,
            "created_at": created_at.isoformat(),
            "concepts": concepts,
            "query_count": query_count,
            "recommendations": recommendations,
            "patents": matches_by_run[str(run_id)],
        }
        for run_id, description, concepts, query_count, recommendations, created_at in runs
    ]
