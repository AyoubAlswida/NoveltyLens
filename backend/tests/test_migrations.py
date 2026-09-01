"""Migrations apply and roll back cleanly on real PostgreSQL 17.

The rollback half is the part that is easy to skip and expensive to get wrong: a
migration whose rollback is missing or partial cannot be undone on a deployment
that has already applied it, and the failure surfaces during an incident rather
than during review.

**This test rolls the new migrations back and re-applies them against the
configured database.** It is destructive to workflow data by design -- rolling
0006 back drops `verification_runs` and everything that cascades from it -- so it
refuses to run unless the database looks like a local development one, and it
restores the schema before it returns whatever happens.
"""

from pathlib import Path

import pytest
from yoyo import get_backend, read_migrations

from config import settings
from migrate import _yoyo_database_uri

pytestmark = pytest.mark.postgres

BACKEND = Path(__file__).resolve().parent.parent
MIGRATIONS = BACKEND / "migrations"

from tests.conftest import foreign_workflow_backends, local_backend_is_serving
from tests.workflow_schema import (
    FIRST_WORKFLOW_MIGRATION,
    PRE_EXISTING_TABLES,
    WORKFLOW_ENUMS,
    WORKFLOW_FUNCTIONS,
    WORKFLOW_TABLES,
)
from tests.workflow_schema import enums as _enums
from tests.workflow_schema import functions as _functions
from tests.workflow_schema import tables as _tables

# The declared schema and the three introspection helpers moved to
# `tests/workflow_schema.py` unchanged. `tests/conftest.py` needs the same list
# to check, once per session, that the schema this test drops and re-applies is
# actually whole before any `postgres` test runs -- and a second hand-maintained
# copy of it would go stale in exactly the direction that hides a broken
# database. The assertions below are untouched and still compare against the
# full set.


def test_every_migration_has_a_rollback():
    """A source contract, so a new migration without one fails at review time
    rather than during an incident."""
    missing = [
        path.name
        for path in sorted(MIGRATIONS.glob("*.sql"))
        if ".rollback" not in path.name
        and not (MIGRATIONS / path.name.replace(".sql", ".rollback.sql")).exists()
    ]
    assert missing == [], f"migrations with no rollback: {missing}"


def test_migrations_declare_their_dependency_chain():
    """yoyo orders by the `-- depends:` header, not by filename. A missing header
    is an unordered migration, which applies in whatever order yoyo happens to
    choose."""
    for path in sorted(MIGRATIONS.glob("*.sql")):
        if ".rollback" in path.name:
            continue
        if path.name.startswith("0001"):
            continue
        assert path.read_text(encoding="utf-8").lstrip().startswith("-- depends:"), (
            f"{path.name} declares no dependency"
        )


def test_the_workflow_schema_is_fully_applied(pg):
    """Everything migrations 0006-0015 create is present."""
    assert WORKFLOW_TABLES == tuple(t for t in WORKFLOW_TABLES if t in _tables(pg))
    assert set(WORKFLOW_FUNCTIONS) <= _functions(pg)
    assert set(WORKFLOW_ENUMS) <= _enums(pg)
    assert pg.execute(
        "SELECT count(*) FROM pg_extension WHERE extname = 'pgcrypto'"
    ).fetchone()[0] == 1


def test_the_rollbacks_reverse_the_workflow_schema_and_re_apply(
    exclusive_schema, postgres_url
):
    """Roll 0015 back to 0006, assert the database is exactly as it was before
    this work, then re-apply.

    Destructive to workflow rows by design, so it refuses to run against anything
    that does not look like a local development database, and it re-applies in a
    `finally` so a failure mid-rollback does not leave the developer's schema
    half-dropped.

    ## Why this takes `exclusive_schema` and not `pg`

    Dropping the workflow schema while any other session does ordinary workflow
    work deadlocks, and the two lock orders are genuinely opposed: the rollback
    holds `AccessExclusiveLock` on the table it is dropping and waits for the
    next, while a concurrent `create_run` holds a workflow-table lock and waits
    for `RowShareLock` on `users` -- the foreign key behind
    `verification_runs.created_by`. PostgreSQL kills one side, and *which* side
    it kills is why this surfaced as unrelated assertion failures scattered
    across the workflow suite rather than as one legible error.

    `exclusive_schema` holds `conftest.SCHEMA_LOCK_KEY` exclusively; every `pg`
    holder takes it shared. The two cannot overlap, in this process or in any
    other sharing the database. It is used **instead of** `pg`, not alongside it:
    holding the shared lock on one connection while requesting the exclusive lock
    on another would be waiting for itself.
    """
    if "127.0.0.1" not in postgres_url and "localhost" not in postgres_url:
        pytest.skip(
            "the rollback test drops the workflow schema and will only run against a "
            "local development database"
        )

    # The advisory lock serializes every session that cooperates with it. A psql
    # window, a hand-started API or a worker does not cooperate, and dropping the
    # schema underneath one reproduces the original deadlock with none of the
    # explanation -- so say so plainly rather than deadlocking.
    serving = local_backend_is_serving()
    if serving:
        pytest.skip(
            f"an application is serving on {serving}, and this test drops the "
            "workflow schema. It opens a database connection per request, so it "
            "is invisible to a connection check while idle -- and dropping the "
            "schema under it kills the executor thread and every run in flight. "
            "Stop it and re-run."
        )

    intruders = foreign_workflow_backends(exclusive_schema)
    if intruders:
        pytest.skip(
            "another session is using this database and does not take the schema "
            f"advisory lock, so dropping the schema would deadlock it: {intruders}. "
            "Stop it (a psql window, `python server.py`, or `python -m "
            "workflow.worker`) and re-run."
        )

    pg = exclusive_schema

    backend = get_backend(_yoyo_database_uri(settings.database_url))
    migrations = read_migrations(str(MIGRATIONS))

    before_tables = _tables(pg)
    assert set(PRE_EXISTING_TABLES) <= before_tables

    try:
        with backend.lock():
            backend.rollback_migrations(
                backend.to_rollback(migrations).filter(
                    lambda migration: migration.id >= FIRST_WORKFLOW_MIGRATION
                )
            )

        after = _tables(pg)
        assert set(WORKFLOW_TABLES) & after == set(), (
            f"these workflow tables survived the rollback: {set(WORKFLOW_TABLES) & after}"
        )
        assert set(WORKFLOW_FUNCTIONS) & _functions(pg) == set()
        assert set(WORKFLOW_ENUMS) & _enums(pg) == set()
        assert set(PRE_EXISTING_TABLES) <= after, (
            "the rollback must not touch the pre-existing search schema"
        )
    finally:
        with backend.lock():
            backend.apply_migrations(backend.to_apply(migrations))

    restored = _tables(pg)
    assert set(WORKFLOW_TABLES) <= restored
    assert set(WORKFLOW_FUNCTIONS) <= _functions(pg)
    assert restored == before_tables, "re-applying restores exactly what was rolled back"
