"""The lease as a permanent fencing counter: creation, monotonicity, strict
release, the heartbeat that cannot revive, TTL validation, the expiry equality
instant, and the cascade that is the only way a lease row disappears.

All against the real Compose PostgreSQL, because every property here is a
property of the SQL. The one exception is the source contract at the bottom,
which reads the repository rather than the database and therefore always runs.

**The defect this file exists to prevent.** An earlier design deleted the lease
row on release; the next acquisition inserted a fresh one at `fence_token = 1`,
and monotonicity was gone. A recycled token is indistinguishable from a live one:
an attempt fenced at token 3, whose `owner_id` happened to be reused -- a
restarted pod on the same host and pid, a deterministic worker id, a container
rescheduled onto the same slot -- would match a *later* lease that had cycled back
through 3 and could commit under authority it never held.
"""

from datetime import timedelta
from pathlib import Path

import psycopg
import pytest

from workflow import lease
from workflow.types import FencedError, LeaseLifecycleError

BACKEND = Path(__file__).resolve().parent.parent

TTL = 60.0


def _expire(pg, run_id: str, *, seconds_ago: float = 5.0) -> None:
    """Age a held lease into the past, keeping the row legal.

    ``run_leases_held_consistently`` requires ``expires_at > acquired_at`` while a
    lease is held, so an expired lease is one whose *whole holding* is in the
    past -- acquired ten minutes ago, expired nine. Setting only ``expires_at``
    backwards would produce a row that could never exist in production, and the
    CHECK correctly refuses it.
    """
    pg.execute(
        """
        UPDATE run_leases
           SET acquired_at  = now() - make_interval(secs => %s),
               heartbeat_at = now() - make_interval(secs => %s),
               expires_at   = now() - make_interval(secs => %s)
         WHERE run_id = %s
        """,
        (seconds_ago * 2, seconds_ago * 2, seconds_ago, run_id),
    )


def _snapshot(pg, run_id: str) -> tuple:
    """Every column of the lease row, for a before/after comparison.

    A failed call must leave the row *byte-identical*; comparing whole tuples is
    what catches a call that "failed" but still moved `heartbeat_at`.
    """
    return pg.execute(
        """
        SELECT run_id, owner_id, fence_token, acquired_at, heartbeat_at, expires_at,
               released_at
          FROM run_leases WHERE run_id = %s
        """,
        (run_id,),
    ).fetchone()


# --- creation and monotonicity -------------------------------------------


@pytest.mark.postgres
def test_create_run_makes_the_lease_row_unheld_at_token_zero(pg, make_run):
    """Token 0 is the never-yet-acquired state and is never a valid holder token:
    every acquisition increments first."""
    run_id = make_run()
    row = lease.lease_row(pg, run_id=run_id)
    assert row["fence_token"] == 0
    assert row["owner_id"] is None
    assert row["acquired_at"] is None
    assert row["expires_at"] is None
    assert row["released_at"] is None


@pytest.mark.postgres
def test_first_acquisition_returns_token_one_not_zero(pg, make_run):
    run_id = make_run()
    assert lease.acquire(pg, run_id=run_id, owner_id="w1", ttl_seconds=TTL) == 1


@pytest.mark.postgres
def test_acquire_release_acquire_returns_three(pg, make_run):
    """The counter never resets. Three acquisitions produce 1, 2, 3 -- not 1, 1, 1
    and not 1, 2, 1."""
    run_id = make_run()
    tokens = []
    for owner in ("w1", "w2", "w3"):
        token = lease.acquire(pg, run_id=run_id, owner_id=owner, ttl_seconds=TTL)
        tokens.append(token)
        lease.release(pg, run_id=run_id, owner_id=owner, fence_token=token)
    assert tokens == [1, 2, 3]


@pytest.mark.postgres
def test_an_update_lowering_a_fence_token_raises_fn006(pg, make_run):
    """Monotonicity is a database property, not a convention: even a hand-run
    UPDATE in psql cannot walk the counter backwards."""
    run_id = make_run()
    lease.acquire(pg, run_id=run_id, owner_id="w1", ttl_seconds=TTL)
    with pytest.raises(psycopg.Error) as caught:
        pg.execute("UPDATE run_leases SET fence_token = 0 WHERE run_id = %s", (run_id,))
    assert caught.value.sqlstate == "FN006"


@pytest.mark.postgres
def test_a_superseded_token_cannot_write_even_with_a_reused_owner_id(pg, make_run):
    """The scenario the permanent counter exists for.

    The same `owner_id` is deliberately reused across several cycles. Under the
    old delete-on-release design the token would have cycled back and this write
    would have succeeded under authority it never held.
    """
    run_id = make_run()
    owner = "deterministic-worker-id"
    first = lease.acquire(pg, run_id=run_id, owner_id=owner, ttl_seconds=TTL)
    lease.release(pg, run_id=run_id, owner_id=owner, fence_token=first)
    for _ in range(3):
        token = lease.acquire(pg, run_id=run_id, owner_id=owner, ttl_seconds=TTL)
        lease.release(pg, run_id=run_id, owner_id=owner, fence_token=token)

    current = lease.lease_row(pg, run_id=run_id)["fence_token"]
    assert current > first
    lease.acquire(pg, run_id=run_id, owner_id=owner, ttl_seconds=TTL)
    with pytest.raises(psycopg.Error) as caught:
        pg.execute(
            "SELECT _lock_run_and_require_lease(%s, %s, %s)", (run_id, owner, first)
        )
    assert caught.value.sqlstate == "FN001"


@pytest.mark.postgres
def test_a_run_with_no_lease_row_raises_rather_than_inserting_one(pg, created_runs):
    """A run not created by `create_run` has no lease row. Acquisition fails
    loudly instead of inserting one, which would restart the fencing counter at a
    value already used."""
    run_id = pg.execute(
        """
        INSERT INTO verification_runs
            (description, date_policy, text_version_policy, thread_id, created_by)
        VALUES ('hand-written', 'p', 't', 'hand-written-thread', 'testnovelty@gmail.com')
        RETURNING id
        """
    ).fetchone()[0]
    created_runs.append(str(run_id))
    with pytest.raises(LeaseLifecycleError):
        lease.acquire(pg, run_id=str(run_id), owner_id="w1", ttl_seconds=TTL)
    assert lease.lease_row(pg, run_id=str(run_id)) is None


# --- release is strict ----------------------------------------------------


@pytest.mark.postgres
def test_release_clears_the_holder_and_keeps_the_row_and_the_token(pg, make_run):
    run_id = make_run()
    token = lease.acquire(pg, run_id=run_id, owner_id="w1", ttl_seconds=TTL)
    lease.release(pg, run_id=run_id, owner_id="w1", fence_token=token)

    row = lease.lease_row(pg, run_id=run_id)
    assert row is not None, "release must never delete the row"
    assert row["owner_id"] is None
    assert row["acquired_at"] is None
    assert row["expires_at"] is None
    assert row["released_at"] is not None
    assert row["fence_token"] == token, "release must not touch the counter"


@pytest.mark.postgres
@pytest.mark.parametrize("wrong", ["owner", "token", "expired", "already-released"])
def test_release_raises_fn001_when_the_caller_is_not_the_active_holder(pg, make_run, wrong):
    """Zero rows is a fencing failure, not a no-op.

    A worker releasing a lease it no longer holds has been operating on a false
    belief about its own authority and needs to find that out here rather than
    carry on as though the handover were clean.
    """
    run_id = make_run()
    token = lease.acquire(pg, run_id=run_id, owner_id="w1", ttl_seconds=TTL)
    owner, used_token = "w1", token
    if wrong == "owner":
        owner = "someone-else"
    elif wrong == "token":
        used_token = token + 7
    elif wrong == "expired":
        _expire(pg, run_id)
    elif wrong == "already-released":
        lease.release(pg, run_id=run_id, owner_id="w1", fence_token=token)

    with pytest.raises(FencedError):
        lease.release(pg, run_id=run_id, owner_id=owner, fence_token=used_token)


@pytest.mark.postgres
def test_a_repeated_release_raises_rather_than_silently_succeeding(pg, make_run):
    """The second call is evidence of exactly the confusion the first one should
    have surfaced."""
    run_id = make_run()
    token = lease.acquire(pg, run_id=run_id, owner_id="w1", ttl_seconds=TTL)
    lease.release(pg, run_id=run_id, owner_id="w1", fence_token=token)
    with pytest.raises(FencedError):
        lease.release(pg, run_id=run_id, owner_id="w1", fence_token=token)


@pytest.mark.postgres
def test_acquisition_after_release_updates_the_same_row_and_increments(pg, make_run):
    run_id = make_run()
    first = lease.acquire(pg, run_id=run_id, owner_id="w1", ttl_seconds=TTL)
    lease.release(pg, run_id=run_id, owner_id="w1", fence_token=first)
    rows_before = pg.execute("SELECT count(*) FROM run_leases").fetchone()[0]
    second = lease.acquire(pg, run_id=run_id, owner_id="w2", ttl_seconds=TTL)
    rows_after = pg.execute("SELECT count(*) FROM run_leases").fetchone()[0]

    assert second == first + 1
    assert rows_after == rows_before, "acquisition updates; it never inserts"


@pytest.mark.postgres
def test_pause_releases_without_deleting_the_row(pg, make_run):
    """`pause_run_for_review` releases the same way `release_run_lease` does --
    by clearing the holder. Nothing anywhere deletes a lease row."""
    from workflow import events

    run_id = make_run()
    token = lease.acquire(pg, run_id=run_id, owner_id="w1", ttl_seconds=TTL)
    pg.execute("UPDATE verification_runs SET status = 'RUNNING' WHERE id = %s", (run_id,))
    events.pause_run_for_review(
        pg,
        run_id=run_id,
        owner_id="w1",
        fence_token=token,
        gate="A",
        review_payload={"type": "review_required"},
        pause_payload={"type": "paused"},
        checkpoint_id="cp-1",
    )
    row = lease.lease_row(pg, run_id=run_id)
    assert row is not None
    assert row["owner_id"] is None
    assert row["fence_token"] == token


# --- the heartbeat cannot revive -----------------------------------------


@pytest.mark.postgres
def test_a_heartbeat_before_expiry_succeeds_and_extends(pg, make_run):
    run_id = make_run()
    token = lease.acquire(pg, run_id=run_id, owner_id="w1", ttl_seconds=5)
    before = _snapshot(pg, run_id)
    assert lease.heartbeat(
        pg, run_id=run_id, owner_id="w1", fence_token=token, ttl_seconds=TTL
    )
    after = _snapshot(pg, run_id)
    assert after[5] > before[5], "expires_at must move forward"


@pytest.mark.postgres
def test_the_expiry_equality_instant(pg, make_run):
    """`expires_at = transaction_timestamp()`: heartbeat fails **and** acquisition
    succeeds, in the same instant.

    This is the boundary `expires_at < now()` got wrong. Under that comparison
    the lease was inactive *and* unclaimable at the same moment, stranding the run
    for a tick with nobody entitled to advance it. Active is `> now()`, claimable
    is `<= now()`, and those are exact negations.

    One explicit transaction so `now()` is identical in all three statements --
    which is the only way to land exactly on the boundary rather than near it.
    """
    run_id = make_run()
    first = lease.acquire(pg, run_id=run_id, owner_id="w1", ttl_seconds=TTL)

    with pg.transaction():
        pg.execute(
            "UPDATE run_leases SET expires_at = transaction_timestamp() WHERE run_id = %s",
            (run_id,),
        )
        beat = pg.execute(
            "SELECT heartbeat_run_lease(%s, %s, %s, %s)",
            (run_id, "w1", first, timedelta(seconds=TTL)),
        ).fetchone()[0]
        claimed = pg.execute(
            "SELECT acquire_run_lease(%s, %s, %s)",
            (run_id, "w2", timedelta(seconds=TTL)),
        ).fetchone()[0]

    assert beat is False, "at expires_at = now() the lease is no longer active"
    assert claimed == first + 1, "at expires_at = now() the lease is already claimable"


@pytest.mark.postgres
@pytest.mark.parametrize("when", ["at-expiry", "after-expiry", "after-release"])
def test_a_heartbeat_cannot_revive_a_lease(pg, make_run, when):
    """Reactivation is `acquire_run_lease`'s job alone, and it always increments
    the token. A beat that could revive would produce two workers each believing
    they hold the run."""
    run_id = make_run()
    token = lease.acquire(pg, run_id=run_id, owner_id="w1", ttl_seconds=TTL)
    if when == "at-expiry":
        # expires_at exactly now, and still after acquired_at: the equality
        # instant, which is inactive and claimable at the same moment.
        pg.execute(
            """
            UPDATE run_leases
               SET acquired_at = now() - interval '1 second', expires_at = now()
             WHERE run_id = %s
            """,
            (run_id,),
        )
    elif when == "after-expiry":
        _expire(pg, run_id)
    else:
        lease.release(pg, run_id=run_id, owner_id="w1", fence_token=token)

    before = _snapshot(pg, run_id)
    assert (
        lease.heartbeat(
            pg, run_id=run_id, owner_id="w1", fence_token=token, ttl_seconds=TTL
        )
        is False
    )
    assert _snapshot(pg, run_id) == before, "a failed beat must change nothing at all"


@pytest.mark.postgres
def test_a_failed_beat_returns_false_rather_than_raising(pg, make_run):
    """The documented asymmetry: every *other* lease-sensitive path raises
    `FN001`, but the heartbeat runs on a thread whose job is to answer "do we
    still hold this?"."""
    run_id = make_run()
    lease.acquire(pg, run_id=run_id, owner_id="w1", ttl_seconds=TTL)
    assert (
        lease.heartbeat(
            pg, run_id=run_id, owner_id="w1", fence_token=999, ttl_seconds=TTL
        )
        is False
    )


@pytest.mark.postgres
def test_only_acquisition_reactivates_and_it_increments(pg, make_run):
    run_id = make_run()
    first = lease.acquire(pg, run_id=run_id, owner_id="w1", ttl_seconds=TTL)
    _expire(pg, run_id)
    assert (
        lease.heartbeat(
            pg, run_id=run_id, owner_id="w1", fence_token=first, ttl_seconds=TTL
        )
        is False
    )
    assert lease.acquire(pg, run_id=run_id, owner_id="w2", ttl_seconds=TTL) == first + 1


# --- TTL validation -------------------------------------------------------


@pytest.mark.postgres
@pytest.mark.parametrize("ttl", [None, timedelta(seconds=0), timedelta(seconds=-5)])
@pytest.mark.parametrize("function", ["acquire_run_lease", "heartbeat_run_lease"])
def test_a_non_positive_ttl_is_rejected_and_the_row_is_unchanged(
    pg, make_run, ttl, function
):
    """The SQL functions validate the TTL at their own boundary, because a
    function is not entitled to trust its caller -- config's startup check is the
    other half, not a substitute."""
    run_id = make_run()
    token = lease.acquire(pg, run_id=run_id, owner_id="w1", ttl_seconds=TTL)
    before = _snapshot(pg, run_id)

    with pytest.raises(psycopg.errors.InvalidParameterValue):
        if function == "acquire_run_lease":
            pg.execute("SELECT acquire_run_lease(%s, %s, %s)", (run_id, "w2", ttl))
        else:
            pg.execute(
                "SELECT heartbeat_run_lease(%s, %s, %s, %s)", (run_id, "w1", token, ttl)
            )

    assert _snapshot(pg, run_id) == before


@pytest.mark.postgres
def test_a_positive_ttl_is_accepted(pg, make_run):
    run_id = make_run()
    assert lease.acquire(pg, run_id=run_id, owner_id="w1", ttl_seconds=1) == 1


# --- the cascade is the only way a lease row disappears ------------------


@pytest.mark.postgres
def test_deleting_the_parent_run_removes_its_lease_row(pg, make_run):
    """The approved retention path. Nothing blocks the cascade -- deliberately
    there is no BEFORE DELETE trigger on `run_leases`, because a row-level one
    fires for cascaded rows too and would have made deleting a run impossible."""
    run_id = make_run()
    lease.acquire(pg, run_id=run_id, owner_id="w1", ttl_seconds=TTL)
    assert lease.lease_row(pg, run_id=run_id) is not None

    pg.execute("DELETE FROM verification_runs WHERE id = %s", (run_id,))
    assert lease.lease_row(pg, run_id=run_id) is None


@pytest.mark.postgres
def test_run_leases_has_no_delete_trigger(pg):
    """Asserted directly, because the *absence* of this trigger is a deliberate
    design decision that a well-meaning future change would reverse."""
    triggers = pg.execute(
        """
        SELECT t.tgname FROM pg_trigger t
          JOIN pg_class c ON c.oid = t.tgrelid
         WHERE c.relname = 'run_leases' AND NOT t.tgisinternal
           AND (t.tgtype & 8) <> 0
        """
    ).fetchall()
    assert triggers == [], "a DELETE trigger here would block the run's cascade"


@pytest.mark.postgres
def test_lease_privileges_where_the_deployment_has_roles(pg):
    """Skipped, loudly, on a single-role local database rather than passing
    vacuously.

    The Compose Postgres runs everything as one superuser, so there is nothing
    here to revoke and no separate worker role to grant EXECUTE to. The source
    contract below is what carries the weight locally.
    """
    roles = pg.execute(
        "SELECT rolname FROM pg_roles WHERE rolname IN ('novelty_api', 'novelty_worker')"
    ).fetchall()
    if not roles:
        pytest.skip(
            "single-role local database: no novelty_api/novelty_worker roles exist, so "
            "the DELETE revocation and the EXECUTE grant have nothing to apply to. "
            "test_no_module_deletes_a_lease_row is the local guarantee."
        )
    for (role,) in roles:  # pragma: no cover - only on a role-based deployment
        assert not pg.execute(
            "SELECT has_table_privilege(%s, 'run_leases', 'DELETE')", (role,)
        ).fetchone()[0]
    assert pg.execute(
        "SELECT has_function_privilege('novelty_worker', "
        "'acquire_run_lease(uuid,text,interval)', 'EXECUTE')"
    ).fetchone()[0]


# --- the source contract (always runs; reads the repository, not the DB) --


def test_no_module_deletes_a_lease_row():
    """No code path under `backend/` issues a DELETE against `run_leases`.

    This is the guarantee that replaces the delete-blocking trigger. The trigger
    is gone on purpose -- a row-level BEFORE DELETE fires for cascaded rows too
    and would have turned a retention operation into a permanent failure -- so the
    application's discipline is enforced here and by privilege revocation where
    the deployment has roles.
    """
    offenders = []
    for path in BACKEND.rglob("*.py"):
        if any(part in {".venv", "__pycache__", "tests"} for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8").lower()
        if "delete from run_leases" in text:
            offenders.append(str(path.relative_to(BACKEND)))
    assert offenders == [], f"these modules delete lease rows: {offenders}"


def test_no_module_resets_or_lowers_a_fence_token():
    """No code path sets `fence_token` at all. It is written in exactly one place
    -- `acquire_run_lease`'s `fence_token = fence_token + 1` -- and migration SQL
    is where that lives."""
    offenders = []
    for path in BACKEND.rglob("*.py"):
        if any(part in {".venv", "__pycache__", "tests"} for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8").lower()
        if "set fence_token" in text or "fence_token =" in text.replace("fence_token ==", ""):
            if "update run_leases" in text:
                offenders.append(str(path.relative_to(BACKEND)))
    assert offenders == [], f"these modules write fence_token directly: {offenders}"
