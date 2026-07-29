"""Persist a completed pipeline run (ticket P2.2).

Called from server.py's _run_pipeline BEFORE the "result" SSE event is
yielded. That ordering is deliberate: if persistence fails, the caller
raises instead of yielding, so the stream ends with a clean "error" event
rather than telling the user the search succeeded and then silently
dropping the report. See CLAUDE.md's zero-tolerance failure policy.
"""

from datetime import timedelta

import psycopg

REPORT_TTL = timedelta(hours=24)
"""How long an identical resubmission reuses a stored report instead of
re-running the pipeline (ticket: description-level caching).

Longer than this and a fresh run is forced -- the LLM's query generation
and rationale-writing are not fully deterministic even at low temperature,
and re-running periodically also picks up ranking/prompt improvements
teammates ship (e.g. Phase 3's embedding-based ranking) instead of serving
an increasingly stale report forever."""


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
) -> str:
    """Store the run and its ranked matches; returns the new run id.

    `matches` is the same list that becomes the "result" event's patents[]
    array (backend/server.py's `formatted`) -- each entry already carries
    num/title/pct/risk/rationale/source.
    """
    with conn.transaction(), conn.cursor() as cur:
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
                    str(match["num"]).strip().upper(),
                    match["pct"],
                    match["risk"],
                    match["rationale"],
                    match["source"],
                ),
            )
    return str(run_id)


def list_history(conn: psycopg.Connection, limit: int = 50) -> list[dict[str, object]]:
    """Return the most recent completed runs with their ranked matches, most
    recent first (ticket P2.3 -- backs GET /api/history).

    Shaped like the "result" SSE event's data object, but `id`/`created_at`
    replace the SSE payload's placeholder `title`/`date`: the frontend
    already derives a display title/date from a raw description rather than
    trusting the backend's values verbatim (buildReport() in App.tsx), so
    history entries go through the same client-side logic instead of a
    second, divergent formatting path.
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


def find_recent_report(conn: psycopg.Connection, description: str, claims: str | None) -> dict[str, object] | None:
    """Return the most recent stored report for this exact description+claims
    within REPORT_TTL, or None if there isn't one.

    An exact resubmission of the same invention should return the same
    result -- not because the pipeline is deterministic (it isn't: query
    generation and rationale-writing both go through an LLM), but because a
    user re-running the identical search reasonably expects the identical
    answer, not a different one every time. `_run_pipeline` checks this
    before doing any work at all, so a hit costs zero LLM/provider calls.

    `claims IS NOT DISTINCT FROM %s` rather than `claims = %s`: Postgres
    NULL never equals NULL under `=`, so a plain equality would never match
    two requests that both omitted claims.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, concepts, query_count, top_score, verdict, recommendations
            FROM search_runs
            WHERE description = %s AND claims IS NOT DISTINCT FROM %s AND created_at > now() - %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (description, claims, REPORT_TTL),
        )
        row = cur.fetchone()
        if row is None:
            return None
        run_id, concepts, query_count, top_score, verdict, recommendations = row

        cur.execute(
            """
            SELECT p.title, srm.pct, srm.risk, srm.rationale, srm.source, srm.patent_id
            FROM search_run_matches srm
            JOIN patents p ON p.id = srm.patent_id
            WHERE srm.run_id = %s
            ORDER BY srm.rank
            """,
            (run_id,),
        )
        patents = [
            {"num": patent_id, "title": title, "pct": pct, "risk": risk, "rationale": rationale, "source": source}
            for title, pct, risk, rationale, source, patent_id in cur.fetchall()
        ]

    return {
        "concepts": concepts,
        "query_count": query_count,
        "top_score": top_score,
        "verdict": verdict,
        "recommendations": recommendations,
        "patents": patents,
    }
