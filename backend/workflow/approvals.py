"""The **only** writer of approved artifacts, and the only reader that can hand
one to the verifier.

Everything else in this codebase produces *candidates*. An LLM decomposes a
description into elements and relationships; a ranking stage orders publications.
Neither is approved, and neither becomes approved by being good. A row in
`feature_decompositions` or `candidate_sets` exists because a **named human, at a
recorded time**, said it should — and this module is the only place that
statement can be made.

**Why one module rather than a rule everyone follows.** The approval columns are
three (`is_approved`, `approved_by`, `approved_at`) and the schema's CHECK keeps
them consistent with each other, but nothing in the schema can tell whether the
email in `approved_by` belongs to someone who actually approved anything. That
part is only enforceable by keeping the write in one place and asserting, by
source contract, that no other module writes those columns.
`tests/test_approval_boundary.py` greps for exactly that.

**Three callers, and no fourth.**

1. The Workflow Gate A approval endpoint — *built in Implementation Gate D*.
2. The Workflow Gate B approval endpoint — *built in Implementation Gate D*.
3. `tests/harness.py`, which approves fixtures **explicitly**, so the verifier can
   be integrated and tested before those endpoints exist.

The harness is not a loophole and is deliberately not a shortcut: it calls the
same functions, under the same lease, writing the same rows with a real
`approved_by`. What it does *not* do is decide on a human's behalf. A test that
wants an approved decomposition has to say who approved it, which is exactly the
friction that keeps auto-approval from appearing by accident.

**There is no `auto_approve`, no `approve_if_valid`, and no default approver.**
If you find yourself adding one, the thing you actually want is a fixture.

**Reading is as scoped as writing.** `load_approved_decompositions` and
`load_approved_candidate_set` filter on `is_approved` **in SQL**, so an
unapproved row cannot be fetched and then checked in Python — the same reasoning
`store.py` applies to ownership. A missed `WHERE` clause there is a wrong answer;
a missed `if` here would be an unapproved artifact reaching the verifier.
"""

import json as _json
from typing import Any, Iterable, Optional, Sequence

import psycopg

from verification.decomposition import ApprovedDecomposition
from workflow.types import (
    RunNotFoundError,
    UnapprovedArtifactError,
    raise_for_sqlstate,
)

__all__ = [
    "approve_decomposition",
    "approve_candidate_set",
    "approve_candidate_set_by_owner",
    "approve_decomposition_by_owner",
    "latest_approved_candidate_set_version",
    "latest_approved_decomposition_version",
    "load_approved_decompositions",
    "load_approved_candidate_set",
    "require_approved_decompositions",
    "require_approved_candidate_set",
    "approve_understanding_by_owner",
    "latest_approved_understanding_version",
    "load_approved_understanding",
    "require_approved_understanding",
    "approve_search_strategy_by_owner",
    "latest_approved_search_strategy_version",
    "load_approved_search_strategy",
    "require_approved_search_strategy",
]


def _require_lease(
    conn: psycopg.Connection, *, run_id: str, owner_id: str, fence_token: int
) -> None:
    """The same fence every other worker write takes.

    Approvals arrive from the API rather than the worker, but they still write to
    a run the worker may be executing, so they take the mandatory lock order
    (`verification_runs` then `run_leases`) and hold it to commit.
    """
    try:
        conn.execute(
            "SELECT _lock_run_and_require_lease(%s, %s, %s)",
            (run_id, owner_id, fence_token),
        )
    except psycopg.Error as error:
        raise_for_sqlstate(error)
        raise


def _require_approver(approved_by: str, approved_at: str) -> None:
    """Refuse an approval that cannot name who made it, or when.

    The schema's CHECK already refuses a half-stamped row. This refuses it one
    layer earlier and with a message that says what is missing, because
    `approved_by=''` would satisfy `IS NOT NULL` while naming nobody.
    """
    if not (approved_by or "").strip():
        raise UnapprovedArtifactError(
            "approved_by is required: an approval that cannot name its approver is "
            "not an approval"
        )
    if not (approved_at or "").strip():
        raise UnapprovedArtifactError(
            "approved_at is required: an approval with no time cannot be audited "
            "against the artifact version it approved"
        )


def approve_decomposition(
    conn: psycopg.Connection,
    *,
    run_id: str,
    owner_id: str,
    fence_token: int,
    version: int,
    features: Sequence[dict[str, Any]],
    approved_by: str,
    approved_at: str,
) -> int:
    """Record a human-approved decomposition for `run_id` at `version`.

    `features` is the spec §2 wire shape, one entry per feature: `feature_id`,
    `feature_version`, `feature_text`, `elements[]`, `relationships[]`. It is
    written **verbatim** — this function does not reorder, renumber, deduplicate
    or repair anything. A decomposition that needed repairing to be stored is one
    the human did not actually approve.

    Returns the number of features written.

    Raises:
        UnapprovedArtifactError: no approver, no time, or an empty feature set.
        FencedError: the caller does not hold the lease.
    """
    _require_approver(approved_by, approved_at)
    if not features:
        raise UnapprovedArtifactError(
            "an approved decomposition with no features is not an approval of "
            "anything; there would be no question to ask a publication"
        )

    _require_lease(conn, run_id=run_id, owner_id=owner_id, fence_token=fence_token)
    return _write_decomposition(
        conn,
        run_id=run_id,
        version=version,
        features=features,
        approved_by=approved_by,
        approved_at=approved_at,
    )


def approve_decomposition_by_owner(
    conn: psycopg.Connection,
    *,
    run_id: str,
    actor_email: str,
    version: int,
    features: Sequence[dict[str, Any]],
    approved_at: str,
) -> int:
    """The **API** authority for a Gate A approval: ownership, not a lease.

    A parked run has no lease holder -- the worker released it when it paused
    (spec 3.3.2) -- so requiring a fence token here would make approval impossible,
    and inventing one would be worse. The guard is the one section 8 assigns to the
    API path: `run_id + actor_email`, resolved **in the same query that fetches the
    run**, never fetch-then-compare in Python.

    The approver is the **authenticated caller**, not a parameter a request can
    set. That is the difference between recording who approved something and
    letting a caller claim it.

    Called inside the approval transaction alongside the audit row, the idempotency
    record, the `resumed` event and the WAITING_FOR_REVIEW -> RESUMING transition.
    All of them commit together or none does.
    """
    _require_approver(actor_email, approved_at)
    if not features:
        raise UnapprovedArtifactError(
            "an approved decomposition with no features is not an approval of "
            "anything; there would be no question to ask a publication"
        )
    _require_owner(conn, run_id=run_id, actor_email=actor_email)
    return _write_decomposition(
        conn,
        run_id=run_id,
        version=version,
        features=features,
        approved_by=actor_email,
        approved_at=approved_at,
    )


def _require_owner(conn: psycopg.Connection, *, run_id: str, actor_email: str) -> None:
    """Lock the run and confirm the caller owns it, in one query.

    `FOR UPDATE` because the approval that follows must not interleave with a
    worker claiming the run: the lock is held to commit, exactly as the
    lease-taking path holds its own.

    Another user's run raises `RunNotFoundError`, never a permission error -- ids
    must not be enumerable (spec 3.5).
    """
    row = conn.execute(
        "SELECT 1 FROM verification_runs WHERE id = %s AND created_by = %s FOR UPDATE",
        (run_id, actor_email),
    ).fetchone()
    if row is None:
        raise RunNotFoundError(f"run {run_id} was not found")


def _write_decomposition(
    conn: psycopg.Connection,
    *,
    run_id: str,
    version: int,
    features: Sequence[dict[str, Any]],
    approved_by: str,
    approved_at: str,
) -> int:
    """The rows themselves. One copy, two authorities above it.

    Written **verbatim**: nothing here reorders, renumbers, deduplicates or
    repairs. A decomposition that needed repairing to be stored is one the human
    did not actually approve.
    """
    for feature in features:
        row = conn.execute(
            """
            INSERT INTO feature_decompositions
                (run_id, version, feature_id, feature_version, feature_text,
                 is_approved, approved_by, approved_at)
            VALUES (%s, %s, %s, %s, %s, TRUE, %s, %s)
            RETURNING id
            """,
            (
                run_id,
                version,
                feature["feature_id"],
                feature["feature_version"],
                feature["feature_text"],
                approved_by,
                approved_at,
            ),
        ).fetchone()
        decomposition_id = row[0]

        for ordinal, element in enumerate(feature.get("elements", ()), start=1):
            conn.execute(
                """
                INSERT INTO decomposition_elements
                    (decomposition_id, element_id, text, core, ord)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    decomposition_id,
                    element["element_id"],
                    element["text"],
                    bool(element["core"]),
                    int(element.get("order", ordinal)),
                ),
            )
        for ordinal, relationship in enumerate(
            feature.get("relationships", ()), start=1
        ):
            conn.execute(
                """
                INSERT INTO decomposition_relationships
                    (decomposition_id, relationship_id, text, ord)
                VALUES (%s, %s, %s, %s)
                """,
                (
                    decomposition_id,
                    relationship["relationship_id"],
                    relationship["text"],
                    int(relationship.get("order", ordinal)),
                ),
            )
    return len(features)


def approve_candidate_set(
    conn: psycopg.Connection,
    *,
    run_id: str,
    owner_id: str,
    fence_token: int,
    version: int,
    publications: Sequence[tuple[str, str]],
    approved_by: str,
    approved_at: str,
    manually_added: Iterable[str] = (),
) -> int:
    """Record a human-approved candidate set for `run_id` at `version`.

    `publications` is `(publication_number, normalized_number)` in **approved
    order** — the order the human left the panel in, which is the order the matrix
    is built in. `manually_added` marks the ones the human typed in rather than
    accepted from the ranking, so a later reader can tell curation from selection.

    An **empty approved set is legal**: "none of these are worth verifying" is a
    real human decision, and refusing to record it would force an approver to keep
    a publication they rejected. It produces a matrix with no cells, which is a
    different thing from a matrix nobody approved.

    Returns the number of publications written.
    """
    _require_approver(approved_by, approved_at)
    _require_lease(conn, run_id=run_id, owner_id=owner_id, fence_token=fence_token)
    return _write_candidate_set(
        conn,
        run_id=run_id,
        version=version,
        publications=publications,
        approved_by=approved_by,
        approved_at=approved_at,
        manually_added=manually_added,
    )


def approve_candidate_set_by_owner(
    conn: psycopg.Connection,
    *,
    run_id: str,
    actor_email: str,
    version: int,
    publications: Sequence[tuple[str, str]],
    approved_at: str,
    manually_added: Iterable[str] = (),
) -> int:
    """The **API** authority for a Gate B approval.

    Ownership-scoped rather than lease-fenced, for the reason given on
    :func:`approve_decomposition_by_owner`.

    An approved-but-**empty** set is legal and is not a mistake: "none of these are
    worth verifying" is a real decision, and it produces a matrix with no cells --
    which is a different thing from a matrix nobody approved.
    """
    _require_approver(actor_email, approved_at)
    _require_owner(conn, run_id=run_id, actor_email=actor_email)
    return _write_candidate_set(
        conn,
        run_id=run_id,
        version=version,
        publications=publications,
        approved_by=actor_email,
        approved_at=approved_at,
        manually_added=manually_added,
    )


def _write_candidate_set(
    conn: psycopg.Connection,
    *,
    run_id: str,
    version: int,
    publications: Sequence[tuple[str, str]],
    approved_by: str,
    approved_at: str,
    manually_added: Iterable[str] = (),
) -> int:
    """The rows themselves. One copy, two authorities above it."""
    row = conn.execute(
        """
        INSERT INTO candidate_sets (run_id, version, is_approved, approved_by, approved_at)
        VALUES (%s, %s, TRUE, %s, %s)
        RETURNING id
        """,
        (run_id, version, approved_by, approved_at),
    ).fetchone()
    candidate_set_id = row[0]

    manual = {number for number in manually_added}
    for ordinal, (publication, normalized) in enumerate(publications):
        conn.execute(
            """
            INSERT INTO candidate_set_members
                (candidate_set_id, publication_number, normalized_number, ord, added_manually)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (candidate_set_id, publication, normalized, ordinal, publication in manual),
        )
    return len(publications)


def load_approved_decompositions(
    conn: psycopg.Connection, *, run_id: str, version: int
) -> list[ApprovedDecomposition]:
    """Every approved feature for `run_id` at `version`, in approved order.

    `is_approved` is filtered **in SQL**. An unapproved row is not fetched and
    then rejected — it is not fetched.
    """
    rows = conn.execute(
        """
        SELECT id, feature_id, feature_version, feature_text, approved_by, approved_at
          FROM feature_decompositions
         WHERE run_id = %s AND version = %s AND is_approved
         ORDER BY feature_id
        """,
        (run_id, version),
    ).fetchall()

    decompositions: list[ApprovedDecomposition] = []
    for row in rows:
        decomposition_id = row[0]
        elements = conn.execute(
            """
            SELECT element_id, text, core, ord FROM decomposition_elements
             WHERE decomposition_id = %s ORDER BY ord
            """,
            (decomposition_id,),
        ).fetchall()
        relationships = conn.execute(
            """
            SELECT relationship_id, text, ord FROM decomposition_relationships
             WHERE decomposition_id = %s ORDER BY ord
            """,
            (decomposition_id,),
        ).fetchall()
        decompositions.append(
            ApprovedDecomposition.from_json(
                {
                    "decomposition_id": str(decomposition_id),
                    "decomposition_version": str(version),
                    "approved_by": row[4],
                    "approved_at": row[5].isoformat(),
                    "feature_id": row[1],
                    "feature_version": row[2],
                    "feature_text": row[3],
                    "elements": [
                        {
                            "element_id": e[0],
                            "text": e[1],
                            "core": e[2],
                            "order": e[3],
                        }
                        for e in elements
                    ],
                    "relationships": [
                        {"relationship_id": r[0], "text": r[1], "order": r[2]}
                        for r in relationships
                    ],
                }
            )
        )
    return decompositions


def load_approved_candidate_set(
    conn: psycopg.Connection, *, run_id: str, version: int
) -> list[dict[str, Any]]:
    """The approved publications for `run_id` at `version`, in approved order."""
    rows = conn.execute(
        """
        SELECT m.publication_number, m.normalized_number, m.added_manually
          FROM candidate_set_members m
          JOIN candidate_sets s ON s.id = m.candidate_set_id
         WHERE s.run_id = %s AND s.version = %s AND s.is_approved
         ORDER BY m.ord
        """,
        (run_id, version),
    ).fetchall()
    return [
        {
            "publication_number": row[0],
            "normalized_number": row[1],
            "added_manually": row[2],
        }
        for row in rows
    ]


def latest_approved_decomposition_version(
    conn: psycopg.Connection, *, run_id: str
) -> Optional[int]:
    """The highest **approved** decomposition version for a run, or None.

    `is_approved` is filtered in SQL, so a draft or a rejected proposal cannot be
    returned as "the latest" and then rejected downstream -- it is not returned.
    """
    row = conn.execute(
        "SELECT max(version) FROM feature_decompositions "
        "WHERE run_id = %s AND is_approved",
        (run_id,),
    ).fetchone()
    return None if row is None or row[0] is None else int(row[0])


def latest_approved_candidate_set_version(
    conn: psycopg.Connection, *, run_id: str
) -> Optional[int]:
    """The highest **approved** candidate-set version for a run, or None."""
    row = conn.execute(
        "SELECT max(version) FROM candidate_sets WHERE run_id = %s AND is_approved",
        (run_id,),
    ).fetchone()
    return None if row is None or row[0] is None else int(row[0])


def require_approved_decompositions(
    conn: psycopg.Connection, *, run_id: str, version: Optional[int]
) -> list[ApprovedDecomposition]:
    """Load approved features, or raise.

    The verifier calls this rather than `load_*` so the "nothing approved" case is
    an error at the boundary instead of an empty matrix downstream — an empty
    matrix would be reported as "we compared everything and found nothing", which
    is a claim about patents rather than about a missing approval.
    """
    if version is None:
        raise UnapprovedArtifactError(
            "no approved decomposition version on this run: Workflow Gate A has "
            "not been completed, and a candidate decomposition is not a substitute"
        )
    decompositions = load_approved_decompositions(conn, run_id=run_id, version=version)
    if not decompositions:
        raise UnapprovedArtifactError(
            f"no approved features at decomposition version {version}; the verifier "
            "reads only approved artifacts and never falls back to candidates"
        )
    return decompositions


def require_approved_candidate_set(
    conn: psycopg.Connection, *, run_id: str, version: Optional[int]
) -> list[dict[str, Any]]:
    """Load the approved candidate set, or raise if the *set* was never approved.

    An approved-but-empty set returns `[]` and is not an error — see
    `approve_candidate_set`. The error is for a set that does not exist.
    """
    if version is None:
        raise UnapprovedArtifactError(
            "no approved candidate set version on this run: Workflow Gate B has "
            "not been completed, and a ranked list is not an approved set"
        )
    exists = conn.execute(
        """
        SELECT 1 FROM candidate_sets
         WHERE run_id = %s AND version = %s AND is_approved
        """,
        (run_id, version),
    ).fetchone()
    if exists is None:
        raise UnapprovedArtifactError(
            f"no approved candidate set at version {version}; a ranked candidate "
            "list is machine output and is never read as a substitute"
        )
    return load_approved_candidate_set(conn, run_id=run_id, version=version)


# --- The invention understanding: summary and decomposition, one act ---------


def approve_understanding_by_owner(
    conn: psycopg.Connection,
    *,
    run_id: str,
    actor_email: str,
    version: int,
    summary: str,
    features: Sequence[dict[str, Any]],
    approved_at: str,
) -> int:
    """Approve the technical summary **and** the decomposition, together.

    One human act, one version, one transaction. There is deliberately no way to
    approve one without the other: a summary approved beside a decomposition
    nobody approved would be a statement of what the run is about that no
    requirement was checked against, and an approved decomposition with no
    approved summary would leave the report unable to say what was searched for
    in the inventor's own reviewed words.

    `summary` is the **approved** summary — the machine's proposal as the human
    left it. The inventor's original description is never touched: it stays in
    `verification_runs.description`, and this is stored beside it rather than
    over it.

    Ownership, not a lease, for the reason `approve_decomposition_by_owner` gives:
    a parked run has no lease holder, and inventing a fence token would be worse
    than not requiring one.

    Returns the number of features written.

    Raises:
        UnapprovedArtifactError: no approver, no time, no features, or an empty
            summary.
    """
    _require_approver(actor_email, approved_at)
    if not (summary or "").strip():
        raise UnapprovedArtifactError(
            "an approved understanding with an empty summary is not an approval of "
            "anything: the summary is what the inventor confirmed the system "
            "understood, and nothing downstream can reconstruct it"
        )
    if not features:
        raise UnapprovedArtifactError(
            "an approved decomposition with no features is not an approval of "
            "anything; there would be no question to ask a publication"
        )
    _require_owner(conn, run_id=run_id, actor_email=actor_email)

    conn.execute(
        """
        INSERT INTO approved_understandings
            (run_id, version, summary, is_approved, approved_by, approved_at)
        VALUES (%s, %s, %s, TRUE, %s, %s)
        """,
        (run_id, version, summary.strip(), actor_email, approved_at),
    )
    return _write_decomposition(
        conn,
        run_id=run_id,
        version=version,
        features=features,
        approved_by=actor_email,
        approved_at=approved_at,
    )


def load_approved_understanding(
    conn: psycopg.Connection, *, run_id: str, version: int
) -> Optional[dict[str, Any]]:
    """The approved summary at `version`, or `None`.

    `is_approved` is filtered **in SQL**, like every other approved-artifact
    reader here: an unapproved row is not fetched and then rejected, it is not
    fetched. A row that could not be fetched and a row that was refused after
    being fetched are the same outcome only when nobody makes a mistake.
    """
    row = conn.execute(
        """
        SELECT summary, approved_by, approved_at
          FROM approved_understandings
         WHERE run_id = %s AND version = %s AND is_approved
        """,
        (run_id, version),
    ).fetchone()
    if row is None:
        return None
    return {
        "summary": str(row[0]),
        "approved_by": str(row[1]),
        "approved_at": row[2].isoformat() if row[2] else "",
        "version": version,
    }


def latest_approved_understanding_version(
    conn: psycopg.Connection, *, run_id: str
) -> Optional[int]:
    """The newest approved understanding version, or `None` if none is approved."""
    row = conn.execute(
        "SELECT max(version) FROM approved_understandings WHERE run_id = %s AND is_approved",
        (run_id,),
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def require_approved_understanding(
    conn: psycopg.Connection, *, run_id: str, version: Optional[int]
) -> dict[str, Any]:
    """The approved understanding, or refuse before anything is spent.

    Raises:
        UnapprovedArtifactError: no version, or no approved row at that version.
    """
    if version is None:
        raise UnapprovedArtifactError(
            f"run {run_id} has no approved understanding; the understanding stage "
            "never completed, and nothing downstream may proceed on a proposal"
        )
    approved = load_approved_understanding(conn, run_id=run_id, version=version)
    if approved is None:
        raise UnapprovedArtifactError(
            f"run {run_id} has no APPROVED understanding at version {version}; a "
            "proposal at that version is not an approval of it"
        )
    return approved


# --- The search strategy: what will be searched for, before anything is ------


def approve_search_strategy_by_owner(
    conn: psycopg.Connection,
    *,
    run_id: str,
    actor_email: str,
    version: int,
    understanding_version: int,
    queries: Sequence[dict[str, Any]],
    approved_at: str,
) -> int:
    """Record a human-approved search strategy for `run_id` at `version`.

    `queries` is the strategy as the human left it, in approved order: one entry
    per query with `query_id`, `query_text`, `rationale`, `enabled` and
    `covered_element_ids`. It is written **verbatim** -- nothing here reorders,
    renumbers, deduplicates or repairs a coverage claim, for the same reason
    `_write_decomposition` does not.

    **Disabled queries are written too.** A reviewer switching a query off is a
    decision about the search, not the absence of one, and only the enabled subset
    is ever dispatched. Dropping the disabled rows would leave the report unable to
    say what was deliberately not searched for.

    `understanding_version` is the approved understanding this strategy covers.
    Recording it is what makes a strategy detectably **stale** if the
    understanding is later regenerated and re-approved: coverage was checked
    against that version's element ids and against no other.

    Ownership, not a lease, for the reason `approve_decomposition_by_owner` gives:
    a parked run has no lease holder, and inventing a fence token would be worse
    than not requiring one.

    Returns the number of queries written.

    Raises:
        UnapprovedArtifactError: no approver, no time, or no enabled query.
    """
    _require_approver(actor_email, approved_at)
    if not queries:
        raise UnapprovedArtifactError(
            "an approved search strategy with no queries is not an approval of "
            "anything; there would be nothing to search for"
        )
    if not any(bool(query.get("enabled")) for query in queries):
        raise UnapprovedArtifactError(
            "an approved search strategy with no ENABLED query would dispatch "
            "nothing and then report that nothing was found, which is a statement "
            "about prior art rather than about a search that never ran"
        )
    _require_owner(conn, run_id=run_id, actor_email=actor_email)

    conn.execute(
        """
        INSERT INTO approved_search_strategies
            (run_id, version, understanding_version, is_approved, approved_by, approved_at)
        VALUES (%s, %s, %s, TRUE, %s, %s)
        """,
        (run_id, version, understanding_version, actor_email, approved_at),
    )
    for ordinal, query in enumerate(queries):
        conn.execute(
            """
            INSERT INTO approved_strategy_queries
                (run_id, version, query_id, query_text, rationale, enabled, ord,
                 covered_element_ids)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                run_id,
                version,
                query["query_id"],
                query["query_text"],
                query.get("rationale") or "",
                bool(query.get("enabled")),
                int(query.get("order", ordinal)),
                _json.dumps([str(item) for item in query.get("covered_element_ids") or []]),
            ),
        )
    return len(queries)


def load_approved_search_strategy(
    conn: psycopg.Connection, *, run_id: str, version: int
) -> Optional[dict[str, Any]]:
    """The approved strategy at `version`, or `None`.

    `is_approved` is filtered **in SQL**, like every other approved-artifact
    reader here: an unapproved row is not fetched and then rejected, it is not
    fetched.

    Returns every approved query, enabled or not, in approved order, plus
    `enabled_queries` -- the query text the search actually dispatches. The two
    are returned together on purpose: a caller that needs the text must not have
    to filter for itself and risk dispatching a query the reviewer switched off.
    """
    header = conn.execute(
        """
        SELECT understanding_version, approved_by, approved_at
          FROM approved_search_strategies
         WHERE run_id = %s AND version = %s AND is_approved
        """,
        (run_id, version),
    ).fetchone()
    if header is None:
        return None

    rows = conn.execute(
        """
        SELECT query_id, query_text, rationale, enabled, ord, covered_element_ids
          FROM approved_strategy_queries
         WHERE run_id = %s AND version = %s
         ORDER BY ord
        """,
        (run_id, version),
    ).fetchall()
    queries = [
        {
            "query_id": str(row[0]),
            "query_text": str(row[1]),
            "rationale": str(row[2] or ""),
            "enabled": bool(row[3]),
            "order": int(row[4]),
            "covered_element_ids": [str(item) for item in (row[5] or [])],
        }
        for row in rows
    ]
    return {
        "version": version,
        "understanding_version": int(header[0]),
        "approved_by": str(header[1]),
        "approved_at": header[2].isoformat() if header[2] else "",
        "queries": queries,
        "enabled_queries": [q["query_text"] for q in queries if q["enabled"]],
    }


def latest_approved_search_strategy_version(
    conn: psycopg.Connection, *, run_id: str
) -> Optional[int]:
    """The newest approved strategy version, or `None` if none is approved."""
    row = conn.execute(
        "SELECT max(version) FROM approved_search_strategies "
        "WHERE run_id = %s AND is_approved",
        (run_id,),
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def require_approved_search_strategy(
    conn: psycopg.Connection, *, run_id: str, version: Optional[int]
) -> dict[str, Any]:
    """The approved strategy, or refuse before any provider is contacted.

    The search node calls this rather than `load_*` so "nothing approved" is an
    error at the boundary instead of an empty query list downstream. An empty
    query list would search for nothing and then report that nothing was found --
    a statement about prior art rather than about a search that never ran.

    Raises:
        UnapprovedArtifactError: no version, no approved row at that version, or
            an approved strategy with no enabled query.
    """
    if version is None:
        raise UnapprovedArtifactError(
            f"run {run_id} has no approved search strategy; the search strategy "
            "stage never completed, and a proposed query is never dispatched"
        )
    approved = load_approved_search_strategy(conn, run_id=run_id, version=version)
    if approved is None:
        raise UnapprovedArtifactError(
            f"run {run_id} has no APPROVED search strategy at version {version}; a "
            "proposal at that version is not an approval of it"
        )
    if not approved["enabled_queries"]:
        raise UnapprovedArtifactError(
            f"the approved search strategy at version {version} has no enabled "
            "query, so there is nothing to dispatch and nothing this run could "
            "honestly report about prior art"
        )
    return approved
