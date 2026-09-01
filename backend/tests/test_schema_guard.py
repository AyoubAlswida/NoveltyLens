"""The session-start workflow-schema guard, and the defect it exists to catch.

`tests/test_migrations.py` proves the migrations roll back and re-apply. This
module proves the *guard* in `tests/conftest.py` notices when they have not --
which is a different claim, and the one that decides whether a run reports a
broken database clearly or fails sixteen tests confusingly and then repairs the
evidence halfway through.

**The failure this pins was observed, not imagined.**
`test_the_rollbacks_reverse_the_workflow_schema_and_re_apply` drops the whole
workflow schema and restores it in a `finally`. A `finally` does not survive the
process being killed, so a run interrupted inside that window leaves the schema
dropped. The next run then failed every `postgres` test sorting before
`test_migrations.py` -- 16 of them, across `test_evidence_persistence`,
`test_lease_races` and `test_migrations` -- reached `test_migrations.py`, and was
**silently healed** by that test's own `finally`, so all ten `postgres` files
after it passed and the following run was green with no intervention.

The old guard could not see any of that: it probed
`to_regclass('verification_runs')`, one table out of forty-nine declared objects.
"""

import pytest

from tests import workflow_schema
from tests.workflow_schema import (
    WORKFLOW_ENUMS,
    WORKFLOW_FUNCTIONS,
    WORKFLOW_TABLES,
    describe_missing,
    missing_workflow_objects,
)


def _present(monkeypatch, *, tables=(), functions=(), enums=()) -> None:
    """Pretend the database holds exactly these objects."""
    monkeypatch.setattr(workflow_schema, "tables", lambda conn: set(tables))
    monkeypatch.setattr(workflow_schema, "functions", lambda conn: set(functions))
    monkeypatch.setattr(workflow_schema, "enums", lambda conn: set(enums))


def test_a_whole_schema_reports_nothing_missing(monkeypatch):
    _present(
        monkeypatch,
        tables=WORKFLOW_TABLES,
        functions=WORKFLOW_FUNCTIONS,
        enums=WORKFLOW_ENUMS,
    )
    assert missing_workflow_objects(object()) == {}


def test_the_single_table_probe_the_old_guard_used_is_not_enough(monkeypatch):
    """The exact half-applied state that made the old guard pass.

    `verification_runs` is present -- so `to_regclass('verification_runs')` is
    satisfied and the old guard returned "migrated" -- while every function and
    enum from the later migrations is gone. That is what a process killed partway
    through re-applying leaves behind, and it is what made the suite fail with
    `UndefinedFunction` instead of skipping with a reason.
    """
    _present(monkeypatch, tables=("verification_runs",))

    missing = missing_workflow_objects(object())

    assert "verification_runs" not in missing.get("tables", ())
    assert set(missing["functions"]) == set(WORKFLOW_FUNCTIONS)
    assert set(missing["enums"]) == set(WORKFLOW_ENUMS)
    assert "create_run" in missing["functions"], (
        "create_run is the function whose absence produced the observed "
        "UndefinedFunction failures; the guard must name it"
    )


def test_a_missing_function_alone_is_still_incomplete(monkeypatch):
    """One absent object is a broken schema. There is no partial-credit state:
    a run that proceeds against it fails somewhere unrelated and confusingly."""
    _present(
        monkeypatch,
        tables=WORKFLOW_TABLES,
        functions=[f for f in WORKFLOW_FUNCTIONS if f != "create_run"],
        enums=WORKFLOW_ENUMS,
    )
    assert missing_workflow_objects(object()) == {"functions": ("create_run",)}


def test_the_reason_names_every_missing_object(monkeypatch):
    """A skip reason or warning that does not say what is absent sends the reader
    back to the database to find out, which is the work the guard just did."""
    _present(monkeypatch, tables=WORKFLOW_TABLES, enums=WORKFLOW_ENUMS)

    described = describe_missing(missing_workflow_objects(object()))

    assert described.startswith("functions: ")
    for name in WORKFLOW_FUNCTIONS:
        assert name in described


def test_the_declared_schema_is_not_empty():
    """A guard whose list silently became empty would pass against any database
    at all, including one with no workflow schema whatsoever."""
    assert len(WORKFLOW_TABLES) >= 21
    assert len(WORKFLOW_FUNCTIONS) >= 18
    assert len(WORKFLOW_ENUMS) >= 10


def test_test_migrations_and_the_guard_read_one_list():
    """Not two lists that agree today. `tests/test_migrations.py` imports these
    objects rather than declaring its own, so the set the rollback test asserts
    against and the set the guard checks cannot drift apart."""
    from tests import test_migrations

    assert test_migrations.WORKFLOW_TABLES is WORKFLOW_TABLES
    assert test_migrations.WORKFLOW_FUNCTIONS is WORKFLOW_FUNCTIONS
    assert test_migrations.WORKFLOW_ENUMS is WORKFLOW_ENUMS


@pytest.mark.postgres
def test_the_real_database_is_whole_once_the_guard_has_run(postgres_url):
    """The end-to-end statement: after `postgres_url` has done its work, the
    schema every other `postgres` test depends on is actually there. If the guard
    repaired it, this passes because the repair worked; if it could not, the
    session skipped before reaching here."""
    import psycopg

    with psycopg.connect(postgres_url, autocommit=True) as conn:
        assert missing_workflow_objects(conn) == {}
