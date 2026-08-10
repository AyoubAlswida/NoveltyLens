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
    top_score: int,
    verdict: str,
    recommendations: list[str],
    matches: list[dict[str, object]],
    patents: list[PatentRecord],
) -> str:
    """Store the run and its ranked matches; returns the new run id.

    ``matches`` is the same list that becomes the ``result`` event's ``patents[]``
    array (``graph/ranking.py``'s ``format_matches``) -- each entry already
    carries num/title/pct/risk/rationale/source. ``patents`` is the
    ``PatentRecord`` behind each of those entries, upserted first so
    ``search_run_matches``' foreign key resolves.

    The whole thing runs in one transaction despite the autocommit connection:
    a run whose matches half-inserted would render as a truncated report in
    History, indistinguishable from a genuine short result.
    """
    with conn.transaction(), conn.cursor() as cur:
        _upsert_patents(cur, patents)

        cur.execute(
            """
            INSERT INTO search_runs (description, claims, concepts, query_count, top_score, verdict, recommendations)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (description, claims, concepts, query_count, top_score, verdict, recommendations),
        )
        row = cur.fetchone()
        assert row is not None
        run_id = row[0]

        for rank, match in enumerate(matches):
            cur.execute(
                """
                INSERT INTO search_run_matches (run_id, rank, patent_id, pct, risk, rationale, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    run_id,
                    rank,
                    _normalize(str(match["num"])),
                    match["pct"],
                    match["risk"],
                    match["rationale"],
                    match["source"],
                ),
            )
    return str(run_id)


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
            SELECT id, description, concepts, query_count, top_score, verdict, recommendations, created_at
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
        cur.execute(
            """
            SELECT srm.run_id, srm.patent_id, p.title, srm.pct, srm.risk, srm.rationale, srm.source
            FROM search_run_matches srm
            JOIN patents p ON p.id = srm.patent_id
            WHERE srm.run_id = ANY(%s)
            ORDER BY srm.run_id, srm.rank
            """,
            (run_ids,),
        )
        matches_by_run: dict[str, list[dict[str, object]]] = {str(rid): [] for rid in run_ids}
        for run_id, patent_id, title, pct, risk, rationale, source in cur.fetchall():
            matches_by_run[str(run_id)].append(
                {"num": patent_id, "title": title, "pct": pct, "risk": risk, "rationale": rationale, "source": source}
            )

    return [
        {
            "id": str(run_id),
            "description": description,
            "created_at": created_at.isoformat(),
            "concepts": concepts,
            "query_count": query_count,
            "top_score": top_score,
            "verdict": verdict,
            "recommendations": recommendations,
            "patents": matches_by_run[str(run_id)],
        }
        for run_id, description, concepts, query_count, top_score, verdict, recommendations, created_at in runs
    ]
