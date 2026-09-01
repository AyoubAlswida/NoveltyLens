"""Persistence for the evidence layer: dates, exclusions and passages.

Part of `workflow/`, not of `evidence/`, and that split is the point.
`evidence/` is pure functions over explicit inputs -- no database, no lease, no
ownership -- which is what makes them unit-testable without a graph. Everything
that touches a table lives here, behind the same boundaries Gate A established:

* **Ownership-scoped.** Every function takes an `actor_email` and resolves the
  run with it, so one user's evidence is never written into or read out of
  another's run. `store.get_run` does the resolving, so there is one ownership
  query rather than a copy per table.
* **Fenced.** Writes made by a worker carry `owner_id` and `fence_token` and are
  refused if the lease has moved on. A fenced enrichment response must never
  become a passage: the response arrived, but the authority to act on it did not
  survive.
* **Atomic.** A publication's dates, exclusion and passages commit together or
  not at all. A half-written publication would show as eligible with some of its
  passages, which is a coverage claim nobody made.
* **Replay-safe, strictly.** Recovery re-runs a node, so every write here is
  replayed sooner or later. Both exclusion and passage writes go through SQL
  functions (migration 0018) that compare the stored row under lock: an
  **identical** replay is a no-op, and a **different** payload under the same
  identity raises `FN003` rather than overwriting. Same contract as terminal
  events, same reason -- a replay and a conflicting reuse of an identity look
  alike from outside, and only the stored content tells them apart.

**A publication is excluded OR it has evidence, never both**, and the database
enforces it in both directions: `record_publication_exclusion` refuses a
publication that already has passages, and `record_evidence_passage` refuses one
that is already excluded. An exclusion means no comparison was attempted; citable
passages attached to one would be a contradiction a reviewer could only resolve
by guessing.

`(run_id, chunk_id)` is a passage's identity, and chunk ids are pure functions of
publication + section + locator -- so re-running the same enrichment addresses the
same rows, while genuinely changed evidence arrives under a **new** identity
rather than mutating an old one.
"""

import logging
from typing import Any, Iterable, Optional

import psycopg
from psycopg.types.json import Jsonb

from evidence.dates import PublicationDate
from evidence.eligibility import PublicationExclusion
from evidence.passages import BuiltPassage
from workflow import store
from workflow.types import raise_for_sqlstate

logger = logging.getLogger(__name__)


def _require_lease(
    conn: psycopg.Connection, *, run_id: str, owner_id: str, fence_token: int
) -> None:
    """Take the mandatory lock order and prove the caller still holds the lease.

    The same `_lock_run_and_require_lease` every other worker write goes through,
    so evidence writes are fenced on identical terms. Called inside the caller's
    transaction, so the lock is held to commit and a takeover cannot slip between
    the check and the write.
    """
    try:
        conn.execute(
            "SELECT _lock_run_and_require_lease(%s, %s, %s)",
            (run_id, owner_id, fence_token),
        )
    except psycopg.Error as error:
        raise_for_sqlstate(error)
        raise


def record_publication_date(
    conn: psycopg.Connection,
    *,
    run_id: str,
    owner_id: str,
    fence_token: int,
    publication_date: PublicationDate,
) -> None:
    """Store one publication's date evidence.

    `date_original` is the provider's string **verbatim**, so a reviewer
    challenging an exclusion sees what the provider said rather than what we made
    of it. `date_normalized` is a plain `DATE` with no timezone applied -- patent
    dates are date-only, and a shift could move a publication across the cutoff.
    """
    _require_lease(conn, run_id=run_id, owner_id=owner_id, fence_token=fence_token)
    conn.execute(
        """
        INSERT INTO publication_dates
            (run_id, publication_number, date_original, date_normalized, date_source,
             date_certainty, priority_date, screened_at_stage)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (run_id, publication_number) DO UPDATE SET
            date_original     = EXCLUDED.date_original,
            date_normalized   = EXCLUDED.date_normalized,
            date_source       = EXCLUDED.date_source,
            date_certainty    = EXCLUDED.date_certainty,
            priority_date     = EXCLUDED.priority_date,
            screened_at_stage = EXCLUDED.screened_at_stage
        """,
        (
            run_id,
            publication_date.publication_number,
            publication_date.date_original,
            publication_date.date_normalized,
            publication_date.date_source,
            str(publication_date.date_certainty),
            publication_date.priority_date,
            str(publication_date.screened_at_stage),
        ),
    )


def record_exclusion(
    conn: psycopg.Connection,
    *,
    run_id: str,
    owner_id: str,
    fence_token: int,
    exclusion: PublicationExclusion,
) -> str:
    """Store one publication-scoped exclusion, **replay-safe**.

    An earlier version was a plain INSERT into a table with no natural-key
    constraint, so a replay after recovery wrote a *second* exclusion row for the
    same publication -- two rows, possibly with different reasons, both claiming
    to be why it was excluded. Migration 0018 added the key and moved the logic
    into `record_publication_exclusion`, which compares every content column
    under lock:

    * identical replay -> returns the existing id, writes nothing;
    * different state, reason, identity, sections or provider outcome -> `FN003`.

    **This creates no verification cell**, and there is deliberately no code path
    here that could: an exclusion means no comparison was attempted, and a cell
    would imply one was attempted and answered negatively. The function also
    refuses a publication that already holds evidence passages -- excluded or
    cited, never both, enforced in the database rather than by a hopeful `if`.

    `provider_detail` is expected to arrive already redacted; it can reach an
    operator, and EPO authenticates with OAuth client credentials.

    Returns:
        The exclusion's id, whether it was inserted or replayed.

    Raises:
        FencedError: `FN001` -- the caller does not hold the lease.
        ReplayConflictError: `FN003` -- a conflicting replay, or the publication
            already carries evidence.
    """
    try:
        row = conn.execute(
            """
            SELECT record_publication_exclusion(
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                run_id, owner_id, fence_token,
                exclusion.requested_number, exclusion.normalized_number,
                exclusion.served_number or "", str(exclusion.state), exclusion.reason,
                exclusion.failing_check, exclusion.language_attempted,
                list(exclusion.sections_available), list(exclusion.sections_retrieved),
                exclusion.passages_built, exclusion.provider_detail,
            ),
        ).fetchone()
    except psycopg.Error as error:
        raise_for_sqlstate(error)
        raise
    return str(row[0])


def record_passages(
    conn: psycopg.Connection,
    *,
    run_id: str,
    owner_id: str,
    fence_token: int,
    passages: Iterable[BuiltPassage],
) -> int:
    """Store a publication's passages under a **strict replay contract**.

    Returns how many rows were newly inserted; a replayed passage counts zero,
    because nothing was written.

    An earlier version upserted with a SET list covering only the text and its
    hashes, so a replay carrying genuinely different evidence overwrote the text
    while keeping the ORIGINAL text-version date, locator, section, language and
    provenance. That is immutable machine evidence mutated in place, and a stored
    verdict citing it would silently come to point at text it was never validated
    against.

    `record_evidence_passage` (migration 0018) compares every content and
    provenance column under lock: identical is a no-op, anything different is
    `FN003`. Changed evidence therefore arrives as a **new passage identity** -- a
    `SOURCE_RANGE` chunk id moves when the offsets move, and a changed claim
    number is a different claim -- or, where the identity genuinely is the same,
    as an explicit conflict a human has to look at.

    `canonical_text` is the passage of record: post-canonicalization, the string
    every `exact_quote` must be a plain substring of. `section_text_sha256` is
    over the **pre**-canonicalization provider text, because that is what
    `char_start`/`char_end` index into.
    """
    _require_lease(conn, run_id=run_id, owner_id=owner_id, fence_token=fence_token)
    inserted = 0
    for built in passages:
        passage = built.passage
        try:
            row = conn.execute(
                """
                SELECT record_evidence_passage(
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    run_id, owner_id, fence_token,
                    passage.chunk_id, passage.publication_number,
                    passage.source_document_id, passage.source, passage.section,
                    passage.locator, str(built.locator_kind), built.locator_source,
                    built.char_start, built.char_end, built.section_text_sha256,
                    built.segmenter_version, passage.language,
                    built.text_version_original, passage.text_version_date,
                    str(built.text_version_source), built.retrieved_at or None,
                    passage.text, passage.translation, built.text_sha256,
                ),
            ).fetchone()
        except psycopg.Error as error:
            raise_for_sqlstate(error)
            raise
        if row[0]:
            inserted += 1
    return inserted


def persist_publication(
    conn: psycopg.Connection,
    *,
    run_id: str,
    owner_id: str,
    fence_token: int,
    publication_date: Optional[PublicationDate] = None,
    exclusion: Optional[PublicationExclusion] = None,
    passages: Iterable[BuiltPassage] = (),
) -> None:
    """One publication's whole evidence outcome, in **one transaction**.

    Atomic on purpose. A crash between the passages and the exclusion would leave
    a publication that is neither eligible nor excluded -- invisible to both
    surfaces, which is worse than either. And a publication that is *both*
    excluded and carrying passages would be an exclusion with citable evidence
    attached, which is a contradiction a reviewer would have to resolve by
    guessing.
    """
    if exclusion is not None and list(passages):
        raise ValueError(
            f"{exclusion.requested_number} cannot be both excluded and carry "
            "passages: an exclusion means no comparison was attempted"
        )
    with conn.transaction():
        if publication_date is not None:
            record_publication_date(
                conn,
                run_id=run_id,
                owner_id=owner_id,
                fence_token=fence_token,
                publication_date=publication_date,
            )
        if exclusion is not None:
            record_exclusion(
                conn,
                run_id=run_id,
                owner_id=owner_id,
                fence_token=fence_token,
                exclusion=exclusion,
            )
        record_passages(
            conn,
            run_id=run_id,
            owner_id=owner_id,
            fence_token=fence_token,
            passages=passages,
        )


# --- ownership-scoped reads ------------------------------------------------


def list_exclusions(
    conn: psycopg.Connection, *, run_id: str, actor_email: str
) -> list[dict[str, Any]]:
    """A run's exclusions, for the Gate B panel. Ownership-scoped."""
    store.get_run(conn, run_id=run_id, actor_email=actor_email)
    rows = conn.execute(
        """
        SELECT requested_number, normalized_number, served_number, state, reason,
               failing_check, sections_available, sections_retrieved, passages_built,
               excluded_at
          FROM publication_exclusions
         WHERE run_id = %s
         ORDER BY excluded_at, requested_number
        """,
        (run_id,),
    ).fetchall()
    return [
        {
            "requested_number": row[0],
            "normalized_number": row[1],
            "served_number": row[2],
            "state": row[3],
            "reason": row[4],
            "failing_check": row[5],
            "sections_available": list(row[6]),
            "sections_retrieved": list(row[7]),
            "passages_built": row[8],
            "excluded_at": row[9],
        }
        for row in rows
    ]


def list_passages(
    conn: psycopg.Connection,
    *,
    run_id: str,
    actor_email: str,
    publication_number: Optional[str] = None,
) -> list[dict[str, Any]]:
    """A run's passages, optionally for one publication. Ownership-scoped."""
    store.get_run(conn, run_id=run_id, actor_email=actor_email)
    if publication_number is None:
        rows = conn.execute(
            """
            SELECT chunk_id, publication_number, section, locator, locator_kind,
                   locator_source, language, text_version_date, canonical_text,
                   text_sha256, char_start, char_end, segmenter_version
              FROM evidence_passages WHERE run_id = %s ORDER BY publication_number, chunk_id
            """,
            (run_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT chunk_id, publication_number, section, locator, locator_kind,
                   locator_source, language, text_version_date, canonical_text,
                   text_sha256, char_start, char_end, segmenter_version
              FROM evidence_passages WHERE run_id = %s AND publication_number = %s
             ORDER BY chunk_id
            """,
            (run_id, publication_number),
        ).fetchall()
    return [
        {
            "chunk_id": row[0],
            "publication_number": row[1],
            "section": row[2],
            "locator": row[3],
            "locator_kind": row[4],
            "locator_source": row[5],
            "language": row[6],
            "text_version_date": row[7],
            "canonical_text": row[8],
            "text_sha256": row[9],
            "char_start": row[10],
            "char_end": row[11],
            "segmenter_version": row[12],
        }
        for row in rows
    ]


def stale_passage_citations(
    conn: psycopg.Connection, *, run_id: str
) -> list[tuple[str, int]]:
    """Results citing passage text that no longer matches what is stored.

    A changed source text changes `text_sha256`, which no longer matches a
    result's `passage_text_hashes` -- and that result is stale. Stale results are
    retained and inspectable, never displayed as current and never in a report,
    so this identifies them rather than deleting anything.
    """
    rows = conn.execute(
        """
        SELECT r.cell_id, r.result_version
          FROM verification_results r
          JOIN verification_cells c ON c.cell_id = r.cell_id
         WHERE c.run_id = %s
           AND EXISTS (
               SELECT 1 FROM unnest(r.passage_chunk_ids, r.passage_text_hashes)
                            AS cited(chunk_id, text_hash)
                LEFT JOIN evidence_passages p
                       ON p.run_id = c.run_id AND p.chunk_id = cited.chunk_id
                WHERE p.chunk_id IS NULL OR p.text_sha256 <> cited.text_hash
           )
        """,
        (run_id,),
    ).fetchall()
    return [(str(row[0]), int(row[1])) for row in rows]
