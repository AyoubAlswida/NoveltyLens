"""Idempotency as a **transaction**, not as a unique index.

A unique index prevents a duplicate row. It does not make a multi-statement
mutation replay-safe, it cannot return the original response body, and it cannot
tell a retry from a conflicting reuse of the same key. All three are things a
mutating endpoint actually needs, so the mechanism is the protocol below and the
`idempotency_records` table is only its bookkeeping::

    BEGIN
      INSERT ... ON CONFLICT DO NOTHING RETURNING 1
      IF inserted:
          perform the mutation IN THIS SAME TRANSACTION
          UPDATE ... SET http_status, response_body, completed_at
          COMMIT  -> return the fresh response
      ELSE:
          SELECT ... FOR SHARE      -- BLOCKS until the in-flight winner settles
          row gone            -> the winner rolled back; retry from the INSERT
          request hash differs -> ROLLBACK; 409 IDEMPOTENCY_CONFLICT
          http_status present -> COMMIT; return the STORED status and body
    COMMIT

Four consequences, all of them load-bearing:

* Same scope, key, user, resource and payload returns the stored response byte
  for byte.
* The same key with a **different** payload is `409 IDEMPOTENCY_CONFLICT`, never
  a silently returned unrelated result.
* **Concurrent identical requests produce exactly one mutation.** The loser
  blocks on `FOR SHARE` rather than racing, so it never returns before the winner
  has committed.
* **A failed transaction leaves no successful idempotency record.** The record
  and the mutation are in one transaction, so a rollback discards both and the
  key is reusable.

The key is scoped to the **authenticated user** as well as the resource, so one
account's idempotency key can never surface another's stored response.
"""

import hashlib
import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

import psycopg
from psycopg.types.json import Jsonb

from workflow.types import WorkflowError

logger = logging.getLogger(__name__)

MAX_WINNER_ROLLBACK_RETRIES = 3
"""How many times to restart the protocol when the in-flight winner rolled back.

Bounded rather than unbounded: a caller that keeps losing to callers that keep
failing is looking at a broken mutation, and looping forever would turn that into
a hung request instead of a reported failure.
"""


class IdempotencyConflict(WorkflowError):
    """Same scope, key, user and resource; **different** payload.

    Answering with the stored response would answer a different question than the
    caller asked.
    """

    code = "IDEMPOTENCY_CONFLICT"


class MissingIdempotencyKey(WorkflowError):
    """A mutating request arrived without one."""

    code = "MISSING_IDEMPOTENCY_KEY"


def request_digest(payload: Any) -> str:
    """The canonical hash of a request body.

    `sort_keys` so a client reordering JSON keys is a replay rather than a
    conflict; separators pinned so whitespace is not part of the identity.
    """
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


@dataclass
class IdempotentSlot:
    """The state of one idempotent attempt, handed to the caller's `with` body.

    `replayed` is the whole decision: when it is true the mutation must **not**
    run again and `status`/`response` carry what the original call returned.
    """

    replayed: bool
    status: Optional[int] = None
    response: Optional[dict[str, Any]] = None
    _recorded: bool = field(default=False, repr=False)

    def record(self, status: int, response: dict[str, Any]) -> None:
        """Store the response this mutation produced, inside the same transaction.

        Called by the protocol's `with` body after the mutation succeeds. Not
        calling it is a programming error the context manager reports, because an
        unrecorded record would leave `http_status IS NULL` forever and every
        later replay would block on a winner that already committed.
        """
        self.status = status
        self.response = response
        self._recorded = True


@contextmanager
def idempotent(
    conn: psycopg.Connection,
    *,
    actor_email: str,
    scope: str,
    resource_id: str,
    idempotency_key: str,
    request_body: Any,
) -> Iterator[IdempotentSlot]:
    """Commit a **database** mutation once per (user, scope, resource, key).

    That scope is exact and deliberately narrow: the record and the mutation are
    in one transaction in one database, so the *commit* happens once. It says
    nothing about anything outside the database. An external call made inside the
    `with` body is still at-least-once across a retry, for the same reason a lease
    cannot make one exactly-once -- a request already sent cannot be unsent. Do
    not put a provider call in here and describe the result as exactly-once.

    The caller's mutation goes inside the `with` body and therefore inside the
    same transaction as the idempotency record::

        with idempotent(conn, actor_email=..., scope="gate_a_approve",
                        resource_id=run_id, idempotency_key=key,
                        request_body=body) as slot:
            if slot.replayed:
                return slot.status, slot.response
            ... the mutation ...
            slot.record(200, {"decomposition_version": 1})

    Raises:
        MissingIdempotencyKey: when `idempotency_key` is empty.
        IdempotencyConflict: when the key was used with a different payload.
    """
    if not idempotency_key:
        raise MissingIdempotencyKey(
            "a mutating request must carry an idempotency_key"
        )

    digest = request_digest(request_body)
    key = (actor_email, scope, resource_id, idempotency_key)

    for attempt in range(MAX_WINNER_ROLLBACK_RETRIES):
        with conn.transaction():
            inserted = conn.execute(
                """
                INSERT INTO idempotency_records
                    (actor_email, scope, resource_id, idempotency_key, request_sha256)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING 1
                """,
                (*key, digest),
            ).fetchone()

            if inserted is not None:
                slot = IdempotentSlot(replayed=False)
                yield slot
                if not slot._recorded:
                    # Raising rolls the whole transaction back, which discards the
                    # record along with whatever the body wrote. That is the right
                    # outcome: a record with no response would block every later
                    # replay on a winner that had already finished.
                    raise RuntimeError(
                        "an idempotent mutation must call slot.record(status, body) "
                        "before its transaction commits"
                    )
                conn.execute(
                    """
                    UPDATE idempotency_records
                       SET http_status = %s, response_body = %s, completed_at = now()
                     WHERE (actor_email, scope, resource_id, idempotency_key) = (%s, %s, %s, %s)
                    """,
                    (slot.status, Jsonb(slot.response), *key),
                )
                return

            # We lost the race, or this is a replay. FOR SHARE blocks until the
            # in-flight winner commits or rolls back -- it does not poll, and it
            # does not return before the winner has settled.
            row = conn.execute(
                """
                SELECT request_sha256, http_status, response_body
                  FROM idempotency_records
                 WHERE (actor_email, scope, resource_id, idempotency_key) = (%s, %s, %s, %s)
                 FOR SHARE
                """,
                key,
            ).fetchone()

            if row is None:
                # The winner rolled back and its record went with it. The key is
                # reusable, so start the protocol again.
                logger.info(
                    "idempotency key for %s/%s was released by a rolled-back winner; retrying",
                    scope,
                    resource_id,
                )
                continue

            stored_digest, status, response = row
            if stored_digest != digest:
                raise IdempotencyConflict(
                    f"idempotency key already used for {scope} on {resource_id} with a "
                    "different request body"
                )
            if status is None:
                # Reachable only if the winner committed the INSERT and then
                # somehow committed without recording -- which the RuntimeError
                # above prevents. Fail loudly rather than returning a response
                # nobody produced.
                raise WorkflowError(
                    f"idempotency record for {scope} on {resource_id} committed without "
                    "a response; refusing to invent one"
                )
            yield IdempotentSlot(replayed=True, status=int(status), response=response)
            return

    raise WorkflowError(
        f"idempotent mutation for {scope} on {resource_id} could not proceed: the "
        f"in-flight request rolled back {MAX_WINNER_ROLLBACK_RETRIES} times"
    )


def stored_response(
    conn: psycopg.Connection,
    *,
    actor_email: str,
    scope: str,
    resource_id: str,
    idempotency_key: str,
) -> Optional[tuple[int, dict[str, Any]]]:
    """A completed idempotency record's stored status and body, if any.

    Read-only and scoped by the authenticated user, so one account can never read
    another's stored response.
    """
    row = conn.execute(
        """
        SELECT http_status, response_body
          FROM idempotency_records
         WHERE (actor_email, scope, resource_id, idempotency_key) = (%s, %s, %s, %s)
           AND http_status IS NOT NULL
        """,
        (actor_email, scope, resource_id, idempotency_key),
    ).fetchone()
    return None if row is None else (int(row[0]), row[1])
