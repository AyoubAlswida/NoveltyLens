"""Machine proposals and human edits for the SEARCH STRATEGY stage.

**This module writes `search_strategy_proposals` and nothing else.** It is to the
proposed strategy what `workflow/understanding.py` is to the proposed
understanding, `workflow/approvals.py` is to every approved artifact, and
`workflow/events.py` is to the event log: the single writer.

## Why a proposal is stored at all

The same three reasons the understanding stage stores one. Without a row the
proposal lives only in the LangGraph checkpoint, which answers "where is
execution?" and is compacted -- so the screen could not show what the machine
originally proposed after a reviewer had edited it, a regeneration could not be
told from a first proposal, and "what did the human actually change about the
search?" would have no answer at all. That last question is the one this stage
exists to make answerable: the queries are what the run went looking for, and a
report that cannot say whether a person widened or narrowed them is a report that
cannot explain its own coverage.

## Why it cannot be mistaken for an approval

`search_strategy_proposals` has **no `is_approved` column and no approver
columns**, by schema. A row here cannot record an approval because there is no
field in which one could be recorded, and nothing in this module writes to
`approved_search_strategies` or `approved_strategy_queries` --
`tests/test_approval_boundary.py` greps for exactly that.

The two sources are stored as separate rows at the same version:

* `LLM` -- what the model proposed, verbatim.
* `USER_EDIT` -- what the human changed it to, verbatim.

## Ownership

Every function takes an `actor_email` and filters on it **in the same query that
fetches the run**, never fetch-then-compare in Python. Another user's run answers
`RUN_NOT_FOUND`, never a permission error.
"""

import json
import logging
from typing import Any, Optional, Sequence

import psycopg

from workflow.types import RunNotFoundError

logger = logging.getLogger(__name__)

SOURCE_LLM = "LLM"
"""A machine proposal, verbatim. Never edited in place."""

SOURCE_USER_EDIT = "USER_EDIT"
"""What the human changed the machine proposal into, verbatim."""

_SOURCES = (SOURCE_LLM, SOURCE_USER_EDIT)


def _require_owner(conn: psycopg.Connection, *, run_id: str, actor_email: str) -> None:
    """Lock the run and confirm the caller owns it, in one query."""
    row = conn.execute(
        "SELECT 1 FROM verification_runs WHERE id = %s AND created_by = %s FOR UPDATE",
        (run_id, actor_email),
    ).fetchone()
    if row is None:
        raise RunNotFoundError(f"run {run_id} was not found")


def record_proposal_if_absent(
    conn: psycopg.Connection,
    *,
    run_id: str,
    version: int,
    understanding_version: int,
    source: str,
    queries: Sequence[dict[str, Any]],
    created_by: str,
    feedback: str = "",
) -> Optional[str]:
    """Store one strategy proposal, or no-op if that version already has one.

    Returns the new id, or `None` when a row already existed for this
    `(run_id, version, source)`.

    Written **verbatim**: nothing here reorders, renumbers, deduplicates or
    repairs a coverage claim. A proposal that needed repairing to be stored is not
    the proposal that was made, and an unrecognised element id is refused at the
    gate rather than quietly removed here.

    **It does not compare content**, deliberately, for the same reason
    `understanding.record_proposal_if_absent` does not: recovery re-runs the
    proposing node, a model is not deterministic, and a regenerated strategy is a
    new *version* rather than a different body at the same one. Comparing bodies
    would turn an ordinary replay into a failure.
    """
    if source not in _SOURCES:
        raise ValueError(
            f"{source!r} is not a strategy proposal source; expected one of "
            f"{_SOURCES}. A proposal whose origin cannot be named cannot be audited."
        )
    row = conn.execute(
        """
        INSERT INTO search_strategy_proposals
            (run_id, version, understanding_version, source, queries, feedback, created_by)
        VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
        ON CONFLICT (run_id, version, source) DO NOTHING
        RETURNING id
        """,
        (
            run_id,
            version,
            understanding_version,
            source,
            json.dumps(list(queries)),
            feedback or None,
            created_by,
        ),
    ).fetchone()
    return str(row[0]) if row else None


def next_version(conn: psycopg.Connection, *, run_id: str) -> int:
    """The version a new strategy proposal should take: one past the highest.

    Reads proposals **and** approvals, so a version that was approved is never
    reused by a later proposal even if its proposal rows were never written.
    Version 1 when nothing exists.
    """
    row = conn.execute(
        """
        SELECT GREATEST(
                 COALESCE((SELECT max(version) FROM search_strategy_proposals   WHERE run_id = %s), 0),
                 COALESCE((SELECT max(version) FROM approved_search_strategies  WHERE run_id = %s), 0)
               ) + 1
        """,
        (run_id, run_id),
    ).fetchone()
    return int(row[0])


def latest_proposal(
    conn: psycopg.Connection, *, run_id: str, actor_email: str
) -> Optional[dict[str, Any]]:
    """The newest strategy proposal for this run, preferring the human's edit.

    A `USER_EDIT` at a version supersedes the `LLM` row at the same version *as
    the thing to show*, because that is what the reviewer last saw. The machine
    row travels alongside it under `machine_queries` so the screen can show what
    was proposed next to what it was changed into, without a second query.
    """
    _require_owner(conn, run_id=run_id, actor_email=actor_email)
    rows = conn.execute(
        """
        SELECT version, source, queries, feedback, understanding_version,
               created_by, created_at
          FROM search_strategy_proposals
         WHERE run_id = %s
           AND version = (SELECT max(version) FROM search_strategy_proposals WHERE run_id = %s)
        """,
        (run_id, run_id),
    ).fetchall()
    if not rows:
        return None

    by_source = {str(row[1]): row for row in rows}
    machine = by_source.get(SOURCE_LLM)
    edited = by_source.get(SOURCE_USER_EDIT)
    shown = edited or machine
    return {
        "version": int(shown[0]),
        "source": str(shown[1]),
        "queries": list(shown[2] or []),
        "feedback": shown[3] or "",
        "understanding_version": int(shown[4]),
        "created_by": str(shown[5]),
        "created_at": shown[6].isoformat() if shown[6] else "",
        "machine_queries": list(machine[2] or []) if machine else [],
        "edited": edited is not None,
    }


def proposal_history(
    conn: psycopg.Connection, *, run_id: str, actor_email: str
) -> list[dict[str, Any]]:
    """Every strategy ever proposed for this run, newest version first.

    Nothing is deleted or rewritten when a new version is proposed, so this is the
    whole record: what the machine proposed, what the human changed, and what
    feedback prompted each regeneration.
    """
    _require_owner(conn, run_id=run_id, actor_email=actor_email)
    rows = conn.execute(
        """
        SELECT version, source, queries, feedback, understanding_version,
               created_by, created_at
          FROM search_strategy_proposals
         WHERE run_id = %s
         ORDER BY version DESC, source
        """,
        (run_id,),
    ).fetchall()
    return [
        {
            "version": int(row[0]),
            "source": str(row[1]),
            "queries": list(row[2] or []),
            "feedback": row[3] or "",
            "understanding_version": int(row[4]),
            "created_by": str(row[5]),
            "created_at": row[6].isoformat() if row[6] else "",
        }
        for row in rows
    ]


__all__ = [
    "SOURCE_LLM",
    "SOURCE_USER_EDIT",
    "latest_proposal",
    "next_version",
    "proposal_history",
    "record_proposal_if_absent",
]
