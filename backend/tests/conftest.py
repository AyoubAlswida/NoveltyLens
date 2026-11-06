"""Autouse fixtures shared by every test module.

``compare.py``'s embedding-based ranking, the semantic-search path, and
``graph/pipeline.py``'s ``persist_run`` are all unconditionally on the
pipeline's happy path, so embeddings, Qdrant and Postgres are stubbed here
rather than per-test - the suite's "no credentials, no network calls, no
running services" property (see ``test_pipeline_contract.py``'s module
docstring) would otherwise silently depend on every future test remembering to
opt in.

Patched at the *import* site in each consuming module (``compare.embed_texts``,
``search.vector_store.embed_texts``, ``graph.pipeline.save_report``), not at the
origin module (``embeddings.embed_texts``, ``reports.save_report``): consuming
modules do ``from embeddings import embed_texts``, which binds a local name that
a patch on the origin module would not reach.

``db.connect`` is the one exception - ``graph/pipeline.py`` and ``server.py``
both do ``import db`` and call ``db.connect()`` through the module, so patching
the attribute on ``db`` itself is what reaches them.
"""

import warnings
from pathlib import Path
from unittest.mock import patch

import psycopg
import pytest

import db

from tests.fakes import (
    FakeQdrantClient,
    FakeReportStore,
    fake_embed_texts,
    fake_epo_enrich_with_full_text,
    fake_google_enrich_with_full_text,
)


@pytest.fixture(autouse=True)
def stub_semantic_layer():
    with patch("compare.embed_texts", side_effect=fake_embed_texts), \
         patch("search.vector_store.embed_texts", side_effect=fake_embed_texts), \
         patch("search.vector_store.client", FakeQdrantClient()), \
         patch("graph.searching.enrich_epo_full_text", side_effect=fake_epo_enrich_with_full_text), \
         patch("graph.searching.enrich_with_full_text", side_effect=fake_google_enrich_with_full_text):
        yield


@pytest.fixture(autouse=True)
def report_store():
    """One in-memory report store per test, standing in for Postgres.

    Yielded so a test can read back what the pipeline stored (see
    ``test_history.py``) without reaching for the patch objects.
    """
    store = FakeReportStore()
    with patch("db.connect", store.connect), \
         patch("graph.pipeline.save_report", store.save_report), \
         patch("server.list_history", store.list_history), \
         patch("server.clear_history", store.clear_history):
        yield store


# --- Durable workflow fixtures --------------------------------------------
#
# Everything above stubs a boundary so the suite needs no credentials, no
# network and no running services. The fixtures below do the opposite on
# purpose: they hand a test the **real Compose PostgreSQL**, because the
# properties Implementation Gate A has to prove -- a lock-ordering bug, a lost
# update, a partial unique index, a trigger, a lease race, an expiry boundary --
# are properties of the SQL, and `FakeWorkflowStore` cannot exhibit any of them.
# Asserting them against the double would be asserting nothing.
#
# They skip with a clear message when no database is reachable. A green run of a
# skipped suite is a skipped suite, not a passing one, and the Gate A report says
# which it was.

_REAL_DB_CONNECT = db.connect
"""Captured at import, before ``report_store`` patches ``db.connect`` with the
in-memory double, so ``real_db`` can hand it back to a test that needs the real
thing."""


def _postgres_reachable() -> str:
    from config import settings

    try:
        with psycopg.connect(settings.database_url, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
    except Exception as error:  # pragma: no cover - environment dependent
        return f"the Compose PostgreSQL at {settings.database_url.rsplit('@', 1)[-1]} is not reachable: {error}"
    return ""


@pytest.fixture(scope="session")
def postgres_url() -> str:
    """The Compose PostgreSQL URL, or skip the test with why it was skipped.

    Also asserts the workflow schema is present **in full**: a database that is up
    but un-migrated would otherwise fail every test here with a confusing
    "relation does not exist" instead of the one sentence that actually helps.

    ## Why this checks the whole schema and repairs it, rather than probing one table

    `tests/test_migrations.py::test_the_rollbacks_reverse_the_workflow_schema_and_re_apply`
    **drops this entire schema and re-applies it**, and restores it in a `finally`
    so an assertion failure inside that window cannot leave a developer's database
    broken. A `finally` cannot survive the process being killed, and killing a run
    inside that window -- Ctrl-C, an editor's stop button, a CI step timeout --
    leaves the schema dropped for the *next* run.

    What that looked like before this guard was thorough, observed rather than
    theorised: every `postgres` test sorting alphabetically before
    `test_migrations.py` failed with `UndefinedFunction`, `test_migrations.py`
    then **silently healed the database** through its own `finally`, and every one
    of the ten `postgres` files after it passed. The run read as a flaky subset of
    16 tests across three files; the following run was green with no
    intervention. The cause was invisible because the evidence repaired itself
    halfway through the run that exposed it.

    Probing a single table cannot catch that, and that is what this used to do.
    The damage is not all-or-nothing: yoyo rolls back and re-applies one migration
    at a time, so a process killed partway leaves tables present while functions
    and enums from later migrations are gone --
    `to_regclass('verification_runs')` is satisfied by exactly that state.

    **Repair is applying the migrations the developer would run by hand**, which
    is what the destructive test's own `finally` does; it is not a fallback and it
    invents nothing. It is announced through a warning rather than done quietly,
    because a database that needed repairing is a fact about the previous run.
    If the repair does not produce a whole schema the session **skips and names
    what is still missing**, and a skipped run stays a skipped run.
    """
    from config import settings

    from tests.workflow_schema import describe_missing, missing_workflow_objects

    reason = _postgres_reachable()
    if reason:
        pytest.skip(reason)

    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        missing = missing_workflow_objects(conn)
        if not missing:
            return settings.database_url

        warnings.warn(
            "the workflow schema was incomplete at session start and is being "
            f"re-applied ({describe_missing(missing)}). The usual cause is a "
            "previous run killed inside test_migrations.py's rollback window, "
            "which drops the schema and restores it in a finally that a killed "
            "process never reaches.",
            stacklevel=1,
        )
        try:
            from yoyo import get_backend, read_migrations

            from migrate import _yoyo_database_uri

            backend = get_backend(_yoyo_database_uri(settings.database_url))
            migrations = read_migrations(str(Path(__file__).resolve().parent.parent / "migrations"))
            with backend.lock():
                backend.apply_migrations(backend.to_apply(migrations))
        except Exception as error:  # pragma: no cover - environment dependent
            pytest.skip(
                "the workflow schema is incomplete and could not be re-applied "
                f"({describe_missing(missing)}): {error}. Run `python migrate.py` "
                "from backend/."
            )

        still_missing = missing_workflow_objects(conn)
    if still_missing:
        pytest.skip(
            "the workflow schema is still incomplete after re-applying the "
            f"migrations ({describe_missing(still_missing)}): run `python "
            "migrate.py` from backend/."
        )
    return settings.database_url


SCHEMA_LOCK_KEY = 0x4E4C_5343  # "NLSC" - Novelty Lens SChema
"""The advisory lock that gives ONE process at a time the database.

**Held exclusively, for the whole session, by any process that runs a
`postgres` test.** A second process reaching its first `postgres` test waits
until the first has finished all of them. Non-`postgres` tests never take it, so
the fast majority of the suite is unaffected.

## Why the whole session and not per test

Two failure modes were observed against one shared Compose PostgreSQL, and they
have the same cause -- two processes, one database -- but they break different
things, so a fix aimed at one leaves the other:

**1. Schema DDL against live data.**
`test_migrations.py::test_the_rollbacks_reverse_the_workflow_schema_and_re_apply`
drops the entire workflow schema and re-applies it. A concurrent `create_run`
deadlocks it: the rollback holds `AccessExclusiveLock` on the table it is
dropping and waits for the next, while `create_run` holds a workflow-table lock
and waits for `RowShareLock` on `users` -- the foreign key behind
`verification_runs.created_by`. PostgreSQL kills one side, and *which* side is
why this surfaced as unrelated assertion failures scattered across the suite.

**2. Tests that assert on globally-shared state.** Rows keyed by a fixed seeded
account, a fixed `idempotency_key`, or "the newest run" are shared by every
process running the same suite. A concurrent copy of a test breaks its
positional or whole-table assertions while doing nothing wrong. Observed on
`test_list_runs_returns_only_the_callers_runs`,
`test_a_rolled_back_transaction_leaves_no_record_and_frees_the_key`,
`test_acquisition_after_release_updates_the_same_row_and_increments` and
`test_a_completed_run_is_not_claimed_again` -- a *different pair* on each
concurrent run, which is the signature of a class rather than of four bugs.

Scoping each assertion to the rows its own test created fixes them one at a time
and is worth doing where the assertion was genuinely imprecise (two of the four
were, and are now fixed). It does not fix the class: any test that reads a table
rather than its own rows is a candidate, and finding them by running the suite
against itself until something breaks is not a method.

**Serializing the processes removes the class outright**, and it does so without
weakening a single assertion. The cost is that two concurrent suites take turns
over their `postgres` portions, which is the correct trade: a test that shares a
database with an unrelated writer is not measuring what it claims to.

Session-scoped rather than per-test because the guarantee has to span the whole
`postgres` phase -- a lock released between tests lets the next process interleave
at exactly the granularity that breaks these. Held on its own connection, so
closing it releases the lock and a killed process cannot strand it.

**This is not a substitute for test isolation.** The properly isolated design is
a database per process, which the Compose setup does not provide today; this
makes the shared one behave correctly and says so.
"""


@pytest.fixture(scope="session")
def _database_ownership(postgres_url: str):
    """Take :data:`SCHEMA_LOCK_KEY` exclusively for this whole test session.

    Blocks until any other process running `postgres` tests has finished. It does
    not time out on purpose: a bounded wait would turn contention back into a
    flaky failure, and the thing being waited for is a test session that ends.
    """
    conn = psycopg.connect(postgres_url, autocommit=True)
    try:
        conn.execute("SELECT pg_advisory_lock(%s)", (SCHEMA_LOCK_KEY,))
        yield conn
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def database_ownership(request):
    """Give every ``postgres``-marked test the session-wide database lock.

    Autouse and marker-gated: a test that never touches the database neither
    waits for the lock nor holds it, so the non-`postgres` majority of the suite
    runs at full speed and in any order.
    """
    if request.node.get_closest_marker("postgres") is None:
        yield
        return
    request.getfixturevalue("_database_ownership")
    yield


@pytest.fixture
def exclusive_schema(postgres_url: str, database_ownership):
    """A connection for destructive DDL, on a database this process already owns.

    The exclusivity comes from :func:`_database_ownership`, which this process
    holds for the whole session; within a process pytest is sequential, so no
    other test can be mid-query while this one drops the schema. This fixture
    therefore only supplies the connection -- it takes no second lock, because a
    process waiting for a lock it already holds on another connection would wait
    for itself.
    """
    with psycopg.connect(postgres_url, autocommit=True) as conn:
        yield conn


def local_backend_is_serving() -> str:
    """The address of a locally-served API, or `""`.

    **A connection check cannot see an idle application, and that is the case
    that bit.** `db.py` opens a connection per request and closes it, so a
    running `python server.py` or `python run_mock.py --workflow` holds *no*
    database connection between requests -- `pg_stat_activity` shows nothing at
    all. The destructive schema test therefore saw an empty database, dropped
    the workflow schema, and killed a live executor mid-session
    (`UndefinedTable: relation "run_leases" does not exist`), taking every run
    in flight with it.

    So this asks the question that has an answer while the app is idle: is
    anything serving the port the application documents? A listening socket is
    present whether or not the app is currently talking to Postgres.

    Together with :func:`foreign_workflow_backends` this covers both shapes --
    a process holding a connection, and a process that will open one the moment
    it is asked to.
    """
    import socket

    for port in (8001, 8000):
        probe = socket.socket()
        probe.settimeout(0.2)
        try:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return f"127.0.0.1:{port}"
        finally:
            probe.close()
    return ""


def foreign_workflow_backends(conn) -> list[tuple]:
    """Other sessions connected to this database that do **not** cooperate.

    The advisory lock serializes every process that takes it. A `psql` window, a
    running API, or a worker started by hand does not, and dropping the schema
    underneath one produces the original deadlock with none of the explanation.
    This lets the destructive test say so in a sentence instead.

    **Cooperation is the test, not activity.** This used to filter on
    `state <> 'idle'`, which sees only a session executing a query at that exact
    instant -- and a pooled connection belonging to a live worker is `idle`
    almost all of the time. So a running `run_mock.py --workflow` was invisible
    here, the destructive test did not skip, and it dropped the workflow schema
    out from under a live executor mid-session: the worker thread died with
    `UndefinedTable: relation "run_leases" does not exist`, and every run in
    flight went with the tables. An idle connection is not a safe one; it is a
    connection that is about to be used.

    So the question asked is the right one directly: which backends on this
    database are **not** holding `SCHEMA_LOCK_KEY`? A cooperating test session
    holds it (shared or exclusive) for its whole session, and anything else --
    idle or not -- is something that will be broken by the drop.
    """
    return conn.execute(
        r"""
        SELECT activity.pid, activity.state,
               left(regexp_replace(coalesce(activity.query, ''), E'\s+', ' ', 'g'), 60)
          FROM pg_stat_activity AS activity
         WHERE activity.datname = current_database()
           AND activity.pid <> pg_backend_pid()
           AND activity.backend_type = 'client backend'
           AND NOT EXISTS (
                 SELECT 1 FROM pg_locks AS locks
                  WHERE locks.pid = activity.pid
                    AND locks.locktype = 'advisory'
                    AND locks.classid = 0
                    AND locks.objid = %s
                    AND locks.granted
           )
        """,
        (SCHEMA_LOCK_KEY,),
    ).fetchall()


@pytest.fixture
def created_runs() -> list[str]:
    """Run ids this test made, so ``pg`` can remove them on the way out."""
    return []


@pytest.fixture
def pg(postgres_url: str, created_runs: list[str]):
    """An autocommit connection to the real database, with its runs cleaned up.

    The database-wide lock is held for the whole session by
    :func:`_database_ownership`, not here, so it covers every connection this
    test opens -- including the raw ones `test_lease_races` opens inside worker
    threads.

    Teardown deletes every run the test registered in ``created_runs``, which
    also exercises the cascade that removes the lease row, its events and its
    attempts -- the only way a lease row ever disappears. LangGraph's checkpoint
    tables are cleaned separately because yoyo does not own them and they carry
    no foreign key to ours.
    """
    with psycopg.connect(postgres_url, autocommit=True) as conn:
        try:
            yield conn
        finally:
            for run_id in created_runs:
                for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                    conn.execute(f"DELETE FROM {table} WHERE thread_id = %s", (run_id,))
                conn.execute("DELETE FROM verification_runs WHERE id = %s", (run_id,))
                # `idempotency_records` keys its resource as a plain string, so
                # it carries no foreign key and the run's cascade does not reach
                # it. Without this the table grew by a dozen rows on every suite
                # run and outlived the runs the rows referred to -- harmless
                # while every test mints a fresh UUID key, and exactly the kind
                # of residue that makes "clean teardown" untrue.
                conn.execute(
                    "DELETE FROM idempotency_records WHERE resource_id = %s"
                    " OR resource_id LIKE %s",
                    (run_id, run_id + ":%"),
                )


@pytest.fixture
def make_run(pg, created_runs: list[str]):
    """Admit a run through ``create_run`` and register it for cleanup.

    Every workflow test starts here rather than with a hand-written INSERT,
    because ``create_run`` is what also makes the permanent lease row and the
    seq-1 queued event -- a run inserted any other way is not a run this system
    can execute, and testing against one would test a shape production never
    produces.
    """
    from workflow import store

    def _make(
        *,
        description: str = "A closed-loop exhaust heat recovery system",
        claims: str | None = None,
        cutoff_date: str | None = None,
        date_policy: str = "publication_lt_cutoff",
        text_version_policy: str = "provider_publication_date",
        created_by: str = "testnovelty@gmail.com",
    ) -> str:
        run_id = store.create_run(
            pg,
            description=description,
            claims=claims,
            cutoff_date=cutoff_date,
            date_policy=date_policy,
            text_version_policy=text_version_policy,
            created_by=created_by,
        )
        created_runs.append(run_id)
        return run_id

    return _make


@pytest.fixture
def second_user(pg) -> str:
    """A second account, so ownership scoping can be tested for real.

    The repository seeds exactly one shared demo login, so a two-user test has to
    create its own. Removed on the way out, and any run it owns goes first --
    ``verification_runs.created_by`` references ``users(email)``.
    """
    email = "second-user@example.test"
    pg.execute(
        "INSERT INTO users (email, password_hash) VALUES (%s, %s) "
        "ON CONFLICT (email) DO NOTHING",
        (email, "pbkdf2_sha256$1$unused$unused"),
    )
    try:
        yield email
    finally:
        pg.execute("DELETE FROM verification_runs WHERE created_by = %s", (email,))
        pg.execute("DELETE FROM idempotency_records WHERE actor_email = %s", (email,))
        pg.execute("DELETE FROM users WHERE email = %s", (email,))


@pytest.fixture
def real_db(postgres_url: str, report_store):
    """Undo the in-memory ``db.connect`` patch for this test.

    Declared to depend on ``report_store`` so it is set up *after* that autouse
    fixture and its patch therefore wins. A test driving the executor needs this:
    the executor opens its own connections through ``db.connect`` and would
    otherwise be handed the dict double.
    """
    with patch("db.connect", _REAL_DB_CONNECT):
        yield


@pytest.fixture
def capability_fixture_dir(tmp_path):
    """An eligible capability artifact for the suite's own `test` profile.

    Explicitly a **test-only fixture**, which is what spec-amendment §6 permits
    for the `test` profile and nothing else. It is impossible to use under
    `local` or `aramco` for a structural reason rather than a conventional one:
    `deployment_profile` is one of the seven identity fields
    `verification/capability.py` compares, so this artifact's
    `deployment_profile: test` can never match a `local` or `aramco` runtime.
    `tests/test_capability_admission.py` asserts exactly that.

    Written to `tmp_path` and passed through `capability_directory=`, so it never
    lands in `docs/measurements/` and cannot be mistaken for a real
    qualification.
    """
    import json

    import config
    from verification.capability import RuntimeIdentity, artifact_filename

    identity = RuntimeIdentity.for_target(config.llm_target)
    record = {
        "gate": "spec §14.2.1 structured-output capability",
        "run_at": "2026-08-28T00:00:00+00:00",
        "runtime_identity": identity.as_dict(),
        "probes": [],
        "eligible": True,
        "note": "TEST FIXTURE. Qualifies the `test` profile only.",
    }
    path = tmp_path / artifact_filename(identity, "2026-08-28")
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return tmp_path


@pytest.fixture(autouse=True)
def abstract_discovery_boundary():
    """Keep old source fixtures useful at the new OPS transport boundary."""
    from search import search as sources
    from search.epo_abstracts import Page
    from search.search import PatentSearchError
    def page(query, start, deadline):
        records=[]
        for name, provider in sources.SOURCES:
            try:
                records.extend(provider(query, 50))
            except Exception as error:
                raise PatentSearchError(f'{name}: {error}') from error
        return Page(records, len(records), len(records))
    with patch('graph.abstract_search.search_page', side_effect=page), \
         patch('graph.abstract_search.family_publications', return_value=[]):
        yield
