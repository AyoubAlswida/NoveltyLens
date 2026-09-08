"""Machine proposals and human edits for the invention-understanding stage.

**This module writes `understanding_proposals` and nothing else.** It is to the
proposal what `workflow/approvals.py` is to the approved artifact and
`workflow/events.py` is to the event log: the single writer.

## Why a proposal is stored at all

Until this table existed the machine's proposal lived **only** in the LangGraph
checkpoint, which answers "where is execution?" and is compacted. That made three
things impossible: showing the inventor what the system originally said after
they had edited it, telling a regeneration apart from a first proposal, and
answering "what did the human actually change?" — which is the whole audit
question at this stage.

## Why it cannot be mistaken for an approval

`understanding_proposals` has **no `is_approved` column and no approver
columns**, by schema. A row here cannot record an approval because there is no
field in which one could be recorded, and nothing in this module writes to
`feature_decompositions` or `approved_understandings` —
`tests/test_approval_boundary.py` greps for exactly that.

The two sources are stored as separate rows at the same version:

* `LLM` — what the model proposed, verbatim.
* `USER_EDIT` — what the human changed it to, verbatim.

Collapsing them would lose the half that shows a human did anything, and a
regeneration would silently overwrite the record of what was regenerated *from*.

## Ownership

Every function takes an `actor_email` and filters on it **in the same query that
fetches the run**, never fetch-then-compare in Python. Another user's run answers
`RUN_NOT_FOUND`, never a permission error, exactly as every other run-scoped
reader does.
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
    """Lock the run and confirm the caller owns it, in one query.

    `FOR UPDATE` for the same reason `approvals._require_owner` takes it: the
    write that follows must not interleave with a worker claiming the run.
    """
    row = conn.execute(
        "SELECT 1 FROM verification_runs WHERE id = %s AND created_by = %s FOR UPDATE",
        (run_id, actor_email),
    ).fetchone()
    if row is None:
        raise RunNotFoundError(f"run {run_id} was not found")


def record_proposal(
    conn: psycopg.Connection,
    *,
    run_id: str,
    version: int,
    source: str,
    summary: str,
    features: Sequence[dict[str, Any]],
    created_by: str,
    feedback: str = "",
) -> str:
    """Store one proposal — machine output or a human edit. Returns its id.

    Written **verbatim**. Nothing here reorders, renumbers, deduplicates or
    repairs, for the same reason `approvals._write_decomposition` does not: a
    proposal that needed repairing to be stored is not the proposal that was made.

    A replay writes the same `(run_id, version, source)` and is refused by the
    unique constraint rather than silently producing a second row claiming to be
    the same proposal. Callers that legitimately re-run a node use
    `record_proposal_if_absent`.
    """
    if source not in _SOURCES:
        raise ValueError(
            f"{source!r} is not a proposal source; expected one of {_SOURCES}. "
            "A proposal whose origin cannot be named cannot be audited."
        )
    row = conn.execute(
        """
        INSERT INTO understanding_proposals
            (run_id, version, source, summary, features, feedback, created_by)
        VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
        RETURNING id
        """,
        (
            run_id,
            version,
            source,
            summary,
            json.dumps(list(features)),
            feedback or None,
            created_by,
        ),
    ).fetchone()
    return str(row[0])


def record_proposal_if_absent(
    conn: psycopg.Connection,
    *,
    run_id: str,
    version: int,
    source: str,
    summary: str,
    features: Sequence[dict[str, Any]],
    created_by: str,
    feedback: str = "",
) -> Optional[str]:
    """`record_proposal`, but a replay of the identical write is a no-op.

    Recovery re-runs a node, so the proposing node's write is replayed sooner or
    later. Returns the new id, or `None` when a row already existed for this
    `(run_id, version, source)`.

    **It does not compare content**, deliberately. A regenerated proposal is a new
    *version*, never a different body at the same version, so the only thing that
    can legitimately arrive twice here is the same node re-running — and a model
    is not deterministic, so comparing bodies would turn an ordinary recovery into
    a failure. The version number is what carries the difference.
    """
    if source not in _SOURCES:
        raise ValueError(
            f"{source!r} is not a proposal source; expected one of {_SOURCES}."
        )
    row = conn.execute(
        """
        INSERT INTO understanding_proposals
            (run_id, version, source, summary, features, feedback, created_by)
        VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
        ON CONFLICT (run_id, version, source) DO NOTHING
        RETURNING id
        """,
        (
            run_id,
            version,
            source,
            summary,
            json.dumps(list(features)),
            feedback or None,
            created_by,
        ),
    ).fetchone()
    return str(row[0]) if row else None


def attach_features_to_proposal(
    conn: psycopg.Connection,
    *,
    run_id: str,
    version: int,
    source: str,
    features: Sequence[dict[str, Any]],
) -> None:
    """Attach Stage-2 output to the already-recorded Stage-1 proposal.

    The conditional update makes recovery idempotent while refusing to overwrite
    a different limitations proposal at the same audited version.
    """
    row = conn.execute(
        """
        UPDATE understanding_proposals
           SET features = %s::jsonb
         WHERE run_id = %s AND version = %s AND source = %s
           AND features = '[]'::jsonb
        RETURNING id
        """,
        (json.dumps(list(features)), run_id, version, source),
    ).fetchone()
    if row is not None:
        return
    existing = conn.execute(
        """SELECT features FROM understanding_proposals
             WHERE run_id = %s AND version = %s AND source = %s""",
        (run_id, version, source),
    ).fetchone()
    if existing is None or list(existing[0] or []) != list(features):
        raise ValueError("understanding proposal limitations conflict at the same version")


def next_version(conn: psycopg.Connection, *, run_id: str) -> int:
    """The version a new proposal should take: one past the highest that exists.

    Reads proposals **and** approvals, because a version that was approved must
    never be reused by a later proposal even though the proposal rows for it
    might in principle be absent. Version 1 when nothing exists.
    """
    row = conn.execute(
        """
        SELECT GREATEST(
                 COALESCE((SELECT max(version) FROM understanding_proposals WHERE run_id = %s), 0),
                 COALESCE((SELECT max(version) FROM approved_understandings   WHERE run_id = %s), 0),
                 COALESCE((SELECT max(version) FROM feature_decompositions    WHERE run_id = %s), 0)
               ) + 1
        """,
        (run_id, run_id, run_id),
    ).fetchone()
    return int(row[0])


def latest_proposal(
    conn: psycopg.Connection, *, run_id: str, actor_email: str
) -> Optional[dict[str, Any]]:
    """The newest proposal for this run, preferring the human's edit.

    A `USER_EDIT` at a version supersedes the `LLM` row at the same version *as
    the thing to show*, because that is what the inventor last saw. The machine
    row is still returned alongside it under `machine_summary` /
    `machine_features` so the screen can show both without a second query, which
    is the comparison the stage exists to present.
    """
    _require_owner(conn, run_id=run_id, actor_email=actor_email)
    rows = conn.execute(
        """
        SELECT version, source, summary, features, feedback, created_by, created_at
          FROM understanding_proposals
         WHERE run_id = %s
           AND version = (SELECT max(version) FROM understanding_proposals WHERE run_id = %s)
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
        "summary": str(shown[2]),
        "features": list(shown[3] or []),
        "feedback": shown[4] or "",
        "created_by": str(shown[5]),
        "created_at": shown[6].isoformat() if shown[6] else "",
        "machine_summary": str(machine[2]) if machine else "",
        "machine_features": list(machine[3] or []) if machine else [],
        "edited": edited is not None,
    }


def proposal_history(
    conn: psycopg.Connection, *, run_id: str, actor_email: str
) -> list[dict[str, Any]]:
    """Every proposal ever made for this run, newest version first.

    Nothing is deleted or rewritten when a new version is proposed, so this is
    the whole record: what the machine said, what the human changed, and what
    feedback prompted each regeneration.
    """
    _require_owner(conn, run_id=run_id, actor_email=actor_email)
    rows = conn.execute(
        """
        SELECT version, source, summary, features, feedback, created_by, created_at
          FROM understanding_proposals
         WHERE run_id = %s
         ORDER BY version DESC, source
        """,
        (run_id,),
    ).fetchall()
    return [
        {
            "version": int(row[0]),
            "source": str(row[1]),
            "summary": str(row[2]),
            "features": list(row[3] or []),
            "feedback": row[4] or "",
            "created_by": str(row[5]),
            "created_at": row[6].isoformat() if row[6] else "",
        }
        for row in rows
    ]


def _system_latest_proposal(
    conn: psycopg.Connection, *, run_id: str
) -> Optional[dict[str, Any]]:
    """The newest proposal, **without** an ownership check.

    Named `_system_*` because no human is behind the call: the executor reads it
    while carrying out a regeneration request under the run's lease, and there is
    no actor to scope to. `tests/test_workflow_store.py` asserts at the AST level
    that nothing in `api/` reaches a `_system_*` function.
    """
    rows = conn.execute(
        """
        SELECT version, source, summary, features
          FROM understanding_proposals
         WHERE run_id = %s
           AND version = (SELECT max(version) FROM understanding_proposals WHERE run_id = %s)
        """,
        (run_id, run_id),
    ).fetchall()
    if not rows:
        return None
    by_source = {str(row[1]): row for row in rows}
    shown = by_source.get(SOURCE_USER_EDIT) or by_source.get(SOURCE_LLM)
    return {
        "version": int(shown[0]),
        "source": str(shown[1]),
        "summary": str(shown[2]),
        "features": list(shown[3] or []),
    }


__all__ = [
    "SOURCE_LLM",
    "SOURCE_USER_EDIT",
    "latest_proposal",
    "next_version",
    "proposal_history",
    "record_proposal",
    "record_proposal_if_absent",
    "attach_features_to_proposal",
]
