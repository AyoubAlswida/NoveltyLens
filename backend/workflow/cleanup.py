"""``python -m workflow.cleanup`` -- event-log retention and the replay watermark.

Compacts finished runs: for `COMPLETED` / `FAILED` runs older than
`WORKFLOW_EVENT_RETENTION_DAYS` it deletes **only** `progress` and
`verification_progress` rows, raises `verification_runs.events_compacted_below`
to the highest deleted sequence, and **keeps every lifecycle event** --
`review_required`, `paused`, `resumed`, and the terminal -- for as long as the run
row exists.

**The watermark is a column, not an implicit consequence of deletion.** A client
reconnecting with `last_event_id` below it cannot be given a correct replay --
the rows it missed are gone -- so it must be given a `snapshot` instead. Deriving
that from "the lowest surviving sequence" would be wrong the moment a run's
oldest surviving event is a lifecycle event that was never deleted.

**It never touches a live run**, and it never deletes a terminal event: a
completed run must still be able to answer "what was the result?" years later.
"""

import argparse
import logging
import sys
from typing import Optional

import psycopg

import db
from config import settings

logger = logging.getLogger(__name__)

COMPACTABLE_EVENT_TYPES = ("progress", "verification_progress")
"""The only two types compaction removes. Both are high-volume telemetry a
finished run no longer needs frame by frame; everything else is a lifecycle fact
somebody may have to audit."""


def compact_run(conn: psycopg.Connection, *, run_id: str) -> int:
    """Compact one finished run. Returns how many rows were deleted.

    One transaction: the delete and the watermark move together, so a crash
    between them cannot leave a run whose events are gone and whose watermark
    still says they are replayable.
    """
    with conn.transaction():
        deleted = conn.execute(
            """
            DELETE FROM workflow_events
             WHERE run_id = %s
               AND event_type = ANY(%s)
               AND NOT is_terminal
            RETURNING sequence
            """,
            (run_id, list(COMPACTABLE_EVENT_TYPES)),
        ).fetchall()
        if not deleted:
            return 0
        watermark = max(int(row[0]) for row in deleted)
        conn.execute(
            """
            UPDATE verification_runs
               SET events_compacted_below = GREATEST(events_compacted_below, %s)
             WHERE id = %s
            """,
            (watermark, run_id),
        )
    return len(deleted)


def compactable_run_ids(
    conn: psycopg.Connection, *, retention_days: int, limit: int = 100
) -> list[str]:
    """Finished runs old enough to compact. Live runs are never selected."""
    rows = conn.execute(
        """
        SELECT id FROM verification_runs
         WHERE status IN ('COMPLETED', 'FAILED')
           AND updated_at < now() - make_interval(days => %s)
         ORDER BY updated_at
         LIMIT %s
        """,
        (retention_days, limit),
    ).fetchall()
    return [str(row[0]) for row in rows]


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        prog="python -m workflow.cleanup",
        description="Compact finished runs' progress events and raise the replay watermark.",
    )
    parser.add_argument(
        "--retention-days",
        type=int,
        default=settings.workflow_event_retention_days,
        help="how old a finished run must be before its progress events are removed.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list the runs that would be compacted and delete nothing.",
    )
    args = parser.parse_args(argv)

    with db.connect() as conn:
        run_ids = compactable_run_ids(conn, retention_days=args.retention_days)
        if args.dry_run:
            for run_id in run_ids:
                print(run_id)
            logger.info("%d run(s) would be compacted", len(run_ids))
            return 0
        total = sum(compact_run(conn, run_id=run_id) for run_id in run_ids)
    logger.info("compacted %d run(s), %d event row(s) removed", len(run_ids), total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
