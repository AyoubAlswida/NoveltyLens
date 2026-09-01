"""Idempotency is a **transaction**, not a unique index.

A unique index prevents a duplicate row. It cannot make a multi-statement
mutation replay-safe, cannot return the original response body, and cannot tell a
retry from a conflicting reuse of the same key. This file asserts the three
things the index cannot give and the one thing it can.

The concurrency test uses two real connections, because the property under test
-- the loser **blocks** on `FOR SHARE` until the winner commits, rather than
racing -- exists only in the database.
"""

import threading

import pytest

from workflow.idempotency import (
    IdempotencyConflict,
    MissingIdempotencyKey,
    idempotent,
    request_digest,
    stored_response,
)

pytestmark = pytest.mark.postgres

SCOPE = "gate_a_approve"
ACTOR = "testnovelty@gmail.com"


@pytest.fixture
def resource(make_run) -> str:
    """A real run to key against -- `idempotency_records.actor_email` references
    `users(email)`, and the resource is a run id in every real caller."""
    return make_run()


def _approve(pg, resource: str, key: str, body: dict, *, mutations: list) -> tuple:
    """One mutating operation, wrapped in the protocol exactly as a real endpoint
    would wrap it -- the mutation inside the same transaction as the record."""
    with idempotent(
        pg,
        actor_email=ACTOR,
        scope=SCOPE,
        resource_id=resource,
        idempotency_key=key,
        request_body=body,
    ) as slot:
        if slot.replayed:
            return slot.status, slot.response, True
        mutations.append(body)
        pg.execute(
            "UPDATE verification_runs SET description = %s WHERE id = %s",
            (body["description"], resource),
        )
        slot.record(200, {"decomposition_version": len(mutations)})
        return slot.status, slot.response, False


def test_a_missing_key_is_refused(pg, resource):
    with pytest.raises(MissingIdempotencyKey):
        with idempotent(
            pg,
            actor_email=ACTOR,
            scope=SCOPE,
            resource_id=resource,
            idempotency_key="",
            request_body={},
        ):
            pass


def test_an_identical_replay_returns_the_stored_response(pg, resource):
    mutations: list = []
    body = {"description": "approved decomposition v1"}
    first = _approve(pg, resource, "key-1", body, mutations=mutations)
    second = _approve(pg, resource, "key-1", body, mutations=mutations)

    assert first == (200, {"decomposition_version": 1}, False)
    assert second == (200, {"decomposition_version": 1}, True)
    assert mutations == [body], "exactly one mutation"


def test_key_reordering_is_a_replay_not_a_conflict(pg, resource):
    """`request_digest` sorts keys, so a client that serialises its JSON in a
    different order is retrying, not asking something new."""
    assert request_digest({"a": 1, "b": 2}) == request_digest({"b": 2, "a": 1})


def test_the_same_key_with_a_different_payload_conflicts(pg, resource):
    mutations: list = []
    _approve(pg, resource, "key-1", {"description": "v1"}, mutations=mutations)
    with pytest.raises(IdempotencyConflict):
        _approve(pg, resource, "key-1", {"description": "v2 -- different"}, mutations=mutations)
    assert len(mutations) == 1


def test_a_rolled_back_transaction_leaves_no_record_and_frees_the_key(pg, resource):
    """The record and the mutation are in one transaction, so a rollback discards
    both -- and the key is reusable afterwards, which is the observable proof."""
    mutations: list = []
    try:
        with idempotent(
            pg,
            actor_email=ACTOR,
            scope=SCOPE,
            resource_id=resource,
            idempotency_key="key-rollback",
            request_body={"description": "v1"},
        ) as slot:
            assert not slot.replayed
            slot.record(200, {"ok": True})
            raise RuntimeError("the mutation failed after recording")
    except RuntimeError:
        pass

    assert (
        stored_response(
            pg,
            actor_email=ACTOR,
            scope=SCOPE,
            resource_id=resource,
            idempotency_key="key-rollback",
        )
        is None
    )
    # Scoped to this test's own (actor, scope, resource, key), not to every row
    # in the table that happens to share the literal key.
    #
    # `resource` is unique per test; the key string is not. An unscoped count
    # asserts something stronger than the test means -- "nobody anywhere has ever
    # used this key" -- and that is false the moment a second process runs the
    # same suite against the same database. The record this transaction rolled
    # back is the one under test, and it is the one counted.
    assert pg.execute(
        """
        SELECT count(*) FROM idempotency_records
         WHERE actor_email = %s AND scope = %s AND resource_id = %s
           AND idempotency_key = 'key-rollback'
        """,
        (ACTOR, SCOPE, resource),
    ).fetchone()[0] == 0

    status, response, replayed = _approve(
        pg, resource, "key-rollback", {"description": "v1"}, mutations=mutations
    )
    assert (status, replayed) == (200, False), "the key is reusable after a rollback"


def test_not_recording_a_response_rolls_the_whole_thing_back(pg, resource):
    """A record with no response would block every later replay on a winner that
    had already finished, so the protocol refuses to commit one."""
    with pytest.raises(RuntimeError) as caught:
        with idempotent(
            pg,
            actor_email=ACTOR,
            scope=SCOPE,
            resource_id=resource,
            idempotency_key="key-unrecorded",
            request_body={},
        ) as slot:
            assert not slot.replayed
    assert "slot.record" in str(caught.value)
    assert pg.execute(
        "SELECT count(*) FROM idempotency_records WHERE idempotency_key = 'key-unrecorded'"
    ).fetchone()[0] == 0


def test_the_key_is_scoped_to_the_authenticated_user(pg, resource, second_user):
    """One account's idempotency key can never surface another's stored response."""
    mutations: list = []
    _approve(pg, resource, "shared-key", {"description": "v1"}, mutations=mutations)

    assert (
        stored_response(
            pg,
            actor_email=second_user,
            scope=SCOPE,
            resource_id=resource,
            idempotency_key="shared-key",
        )
        is None
    )


def test_concurrent_identical_requests_produce_exactly_one_mutation(
    postgres_url, resource
):
    """Two simultaneous identical requests: one mutation, one stored response,
    and both callers get the same body.

    The loser **blocks** on `SELECT ... FOR SHARE` rather than racing, so it never
    returns before the winner has committed. Two real connections, because that
    blocking is a property of the database and of nothing in Python.
    """
    import psycopg

    mutations: list = []
    results: list = []
    barrier = threading.Barrier(2)
    lock = threading.Lock()

    def attempt() -> None:
        with psycopg.connect(postgres_url, autocommit=True) as conn:
            barrier.wait(timeout=10)
            with idempotent(
                conn,
                actor_email=ACTOR,
                scope=SCOPE,
                resource_id=resource,
                idempotency_key="race-key",
                request_body={"description": "concurrent"},
            ) as slot:
                if slot.replayed:
                    with lock:
                        results.append((slot.status, slot.response, True))
                    return
                conn.execute(
                    "UPDATE verification_runs SET description = 'concurrent' WHERE id = %s",
                    (resource,),
                )
                with lock:
                    mutations.append("mutated")
                slot.record(200, {"decomposition_version": 1})
                with lock:
                    results.append((slot.status, slot.response, False))

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), "a caller deadlocked instead of blocking cleanly"

    assert len(mutations) == 1, "exactly one mutation"
    assert len(results) == 2
    assert sorted(replayed for _, _, replayed in results) == [False, True]
    assert {str(response) for _, response, _ in results} == {
        "{'decomposition_version': 1}"
    }, "both callers get the same response body"
