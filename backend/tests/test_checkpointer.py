"""The LangGraph Postgres checkpointer, and the boundary between its tables and
ours.

**Two migration systems must not own the same tables.** yoyo owns
`backend/migrations/`; `PostgresSaver.setup()` owns the four checkpoint tables
and tracks its own schema version in `checkpoint_migrations`. Overlap would be a
guaranteed conflict the first time either side changed a column, so the source
contract below asserts no yoyo migration ever names one of them.

**There is no in-memory fallback.** A checkpointer that cannot be constructed
fails run admission with `CHECKPOINT_UNAVAILABLE`; it does not silently become an
`InMemorySaver` whose contents vanish with the process, which would present a run
as durable when it is not.
"""

import traceback
from dataclasses import replace
from pathlib import Path
from typing import TypedDict
from unittest.mock import patch

import pytest
from langgraph.graph import END, START, StateGraph

import checkpointer as checkpointer_module
from checkpointer import CHECKPOINT_TABLES, checkpointer, setup
from workflow.types import CheckpointUnavailable, FencedError, IllegalStateError

BACKEND = Path(__file__).resolve().parent.parent


class _State(TypedDict, total=False):
    steps: list[str]


@pytest.mark.postgres
def test_setup_is_idempotent_and_creates_its_tables(pg):
    """`setup()` creates or migrates; running it twice is a no-op."""
    setup()
    setup()
    present = {
        row[0]
        for row in pg.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename = ANY(%s)",
            (list(CHECKPOINT_TABLES),),
        )
    }
    assert present == set(CHECKPOINT_TABLES)


@pytest.mark.postgres
def test_a_checkpoint_round_trips_through_postgres(pg, make_run, created_runs):
    """State written by one saver instance is readable by another -- which is the
    entire basis of a worker in one process continuing a run another started."""
    run_id = make_run()
    config = {"configurable": {"thread_id": run_id}}

    def step(state: _State) -> dict:
        return {"steps": [*(state.get("steps") or []), "one"]}

    builder = StateGraph(_State)
    builder.add_node("step", step)
    builder.add_edge(START, "step")
    builder.add_edge("step", END)

    with checkpointer() as saver:
        builder.compile(checkpointer=saver).invoke({"steps": []}, config)

    with checkpointer() as second_saver:
        snapshot = builder.compile(checkpointer=second_saver).get_state(config)

    assert snapshot.values["steps"] == ["one"]
    assert snapshot.config["configurable"]["checkpoint_id"]


@pytest.mark.postgres
def test_a_checkpoint_survives_the_process_that_wrote_it(pg, make_run):
    """The rows are in PostgreSQL, not in memory. Asserted directly rather than
    inferred, because "durable" is the one property an `InMemorySaver` would fake
    convincingly inside a single process."""
    run_id = make_run()
    config = {"configurable": {"thread_id": run_id}}

    def step(state: _State) -> dict:
        return {"steps": ["persisted"]}

    builder = StateGraph(_State)
    builder.add_node("step", step)
    builder.add_edge(START, "step")
    builder.add_edge("step", END)
    with checkpointer() as saver:
        builder.compile(checkpointer=saver).invoke({"steps": []}, config)

    stored = pg.execute(
        "SELECT count(*) FROM checkpoints WHERE thread_id = %s", (run_id,)
    ).fetchone()[0]
    assert stored > 0


def test_an_unreachable_checkpointer_raises_rather_than_falling_back():
    """`CHECKPOINT_UNAVAILABLE`, not an `InMemorySaver`.

    A run that continued against an in-memory saver would report success while
    its state vanished with the process -- the exact unlabelled degraded result
    the zero-tolerance policy forbids.
    """
    # `Settings` is a frozen slots dataclass, so the whole object is replaced
    # rather than one attribute patched on it. The URL points at the real port
    # with credentials that do not exist: PostgreSQL rejects it in milliseconds,
    # whereas an unroutable port is silently dropped and would hang the suite for
    # the OS connect timeout.
    unreachable = replace(
        checkpointer_module.settings,
        database_url="postgresql://nobody:nobody@127.0.0.1:5432/does-not-exist",
    )
    with patch.object(checkpointer_module, "settings", unreachable):
        with pytest.raises(CheckpointUnavailable) as caught:
            with checkpointer():
                pass
    assert "unreachable" in str(caught.value)


def test_the_error_message_carries_no_password():
    """psycopg builds its connection errors from host, port and database name and
    does not echo the password -- asserted, because this message reaches an
    operator's log."""
    unreachable = replace(
        checkpointer_module.settings,
        database_url="postgresql://someuser:hunter2@127.0.0.1:5432/nope",
    )
    with patch.object(checkpointer_module, "settings", unreachable):
        with pytest.raises(CheckpointUnavailable) as caught:
            with checkpointer():
                pass
    assert "hunter2" not in str(caught.value)


def test_no_yoyo_migration_owns_a_checkpoint_table():
    """The boundary, as a source contract.

    If a migration ever created one of these, yoyo and LangGraph would both
    believe they owned it and the first schema change on either side would
    conflict.
    """
    offenders = []
    for path in (BACKEND / "migrations").glob("*.sql"):
        text = path.read_text(encoding="utf-8").lower()
        for table in CHECKPOINT_TABLES:
            if f"table {table}" in text or f"table if not exists {table}" in text:
                offenders.append(f"{path.name} -> {table}")
    assert offenders == [], f"yoyo migrations must not own checkpoint tables: {offenders}"


def test_migrate_py_points_at_the_separate_checkpointer_setup():
    """A reader of `migrate.py` has to be told that applying it is not the whole
    schema story."""
    docstring = (BACKEND / "migrate.py").read_text(encoding="utf-8")
    assert "checkpointer.py setup" in docstring


# ===========================================================================
# The exception boundary
# ===========================================================================
#
# An earlier revision wrapped the `yield` in a broad `except Exception`, so every
# exception the *caller's* body raised came back through the generator at the
# yield point and was relabelled `CheckpointUnavailable`. A `FencedError` -- a
# worker correctly discovering it had lost its lease -- was reported as a database
# outage; so was a plain `ValueError` from a graph node.
#
# That is not a cosmetic mislabelling. `CheckpointUnavailable` is the one failure
# the executor treats as "persistence is gone, stop and retry later" rather than
# "this run failed", so a relabelled bug changed what the executor *did* about it,
# and pointed an operator at PostgreSQL for a defect in the code.


@pytest.mark.postgres
@pytest.mark.parametrize(
    "raised",
    [
        FencedError("run X was fenced mid-stream at token 3"),
        IllegalStateError("run X is QUEUED, not RUNNING"),
        ValueError("a programming error inside a graph node"),
        RuntimeError("a provider adapter blew up"),
        KeyError("a missing state key"),
    ],
    ids=["fenced", "illegal-state", "value-error", "runtime-error", "key-error"],
)
def test_an_exception_inside_the_context_keeps_its_own_type(raised):
    """The caller's failure propagates untouched -- same type, same message."""
    with pytest.raises(type(raised)) as caught:
        with checkpointer():
            raise raised
    assert not isinstance(caught.value, CheckpointUnavailable)
    assert str(caught.value) == str(raised)


@pytest.mark.postgres
def test_a_fenced_worker_is_not_reported_as_a_database_outage():
    """The specific misdiagnosis, named.

    `FencedError` means "somebody else owns this run now" and the executor
    abandons it. `CheckpointUnavailable` means "persistence is gone" and the
    executor waits and retries. Confusing them changes behaviour, not just wording.
    """
    with pytest.raises(FencedError):
        with checkpointer():
            raise FencedError("run X is not leased to worker-1 at token 1")


@pytest.mark.postgres
def test_the_original_traceback_is_preserved_for_server_side_diagnosis():
    """Safe diagnostics survive: the exception the caller raised is the exception
    an operator sees, with its own traceback, not one wrapped in a false cause."""
    try:
        with checkpointer():
            raise ValueError("the real defect")
    except ValueError as error:
        assert error.__cause__ is None, "nothing was chained onto it"
        assert traceback.format_exc().count("the real defect") >= 1


def test_an_unreachable_checkpointer_raises_checkpoint_unavailable():
    """Construction failure is the one thing that *should* be relabelled."""
    unreachable = replace(
        checkpointer_module.settings,
        database_url="postgresql://nobody:nobody@127.0.0.1:5432/does-not-exist",
    )
    with patch.object(checkpointer_module, "settings", unreachable):
        with pytest.raises(CheckpointUnavailable) as caught:
            with checkpointer():
                pass
    assert "unreachable" in str(caught.value)


@pytest.mark.parametrize(
    "dsn,secret",
    [
        ("postgresql://someuser:hunter2@127.0.0.1:5432/nope", "hunter2"),
        ("postgresql://u:p@127.0.0.1:5432/nope?password=s3cr3t", "s3cr3t"),
        ("postgresql://u:p@127.0.0.1:5432/nope?sslpassword=keypass", "keypass"),
    ],
)
def test_the_failure_message_carries_no_credential_and_no_dsn(dsn, secret):
    """A DSN carries a password in its userinfo and can carry more in its query
    string. This message reaches an operator's log and can reach a stored
    `failure_detail`, so it is built from the parsed host/port/database rather
    than echoed."""
    unreachable = replace(checkpointer_module.settings, database_url=dsn)
    with patch.object(checkpointer_module, "settings", unreachable):
        with pytest.raises(CheckpointUnavailable) as caught:
            with checkpointer():
                pass
    message = str(caught.value)
    assert secret not in message
    assert "postgresql://" not in message and "postgres://" not in message
    assert "127.0.0.1:5432/nope" in message, "a safe target is still named"


def test_a_dsn_shaped_driver_message_never_reaches_the_reason():
    """`_safe_reason` no longer passes the driver's prose through at all.

    It used to redact it, and the 2026-08-29 amendment replaced "redact the
    message" with "rebuild from allow-listed fields" everywhere -- after an
    incomplete redaction pattern let an OpenAI account identifier reach stdout.
    A DSN cannot survive a rebuild that never reads the message.
    """
    leaky = RuntimeError(
        "could not connect using postgresql://user:hunter2@db:5432/x — refused"
    )
    reason = checkpointer_module._safe_reason(leaky)
    assert "hunter2" not in reason
    assert "postgresql://" not in reason
    assert "could not connect" not in reason, "no driver prose, redacted or not"
    assert "RuntimeError" in reason, "the type still travels"


def test_the_reason_carries_the_sqlstate_because_that_is_the_diagnostic():
    """Losing the prose loses nothing actionable: SQLSTATE is sharper than the
    sentence it replaces. `08006` is a connection failure and `28P01` is a bad
    password, and `CheckpointUnavailable` already names the parsed target."""

    class OperationalError(RuntimeError):
        sqlstate = "28P01"

    reason = checkpointer_module._safe_reason(OperationalError("password authentication failed"))
    assert "sqlstate=28P01" in reason
    assert "password authentication failed" not in reason


def test_a_multiline_driver_message_is_reduced_to_one_line():
    """The rest of a driver message is a stack of context -- a traceback by
    another name -- and never travels."""
    noisy = RuntimeError("connection failed\n  CONTEXT: PL/pgSQL function ...\n  at line 4")
    assert "\n" not in checkpointer_module._safe_reason(noisy)


def test_a_provider_error_carrying_a_key_never_becomes_a_checkpoint_error():
    """A SerpAPI-shaped failure -- the one that already leaked a live key into a
    browser console once -- raised inside the context is neither relabelled nor
    reworded here. It stays the caller's problem to sanitize at its own boundary,
    and this module adds nothing to it."""
    leaky = RuntimeError(
        "429 Client Error for url: https://serpapi.com/search?api_key=SECRET123&q=x"
    )
    with pytest.raises(RuntimeError) as caught:
        with checkpointer():
            raise leaky
    assert caught.value is leaky, "not wrapped, not copied, not relabelled"


@pytest.mark.postgres
def test_no_in_memory_saver_is_ever_selected(monkeypatch):
    """After any checkpointer failure the answer is an exception, never a
    substitute saver whose contents vanish with the process.

    Asserted two ways: the module never imports one, and a failed construction
    yields nothing at all rather than something that quacks like a saver.
    """
    # The check is for an import, not for the words: the module docstring names
    # `InMemorySaver` in order to rule it out, and a grep that could not tell
    # those apart would have to be deleted the first time somebody documented the
    # rule.
    for module in ("checkpointer.py", "workflow/executor.py", "workflow/worker.py"):
        source = (BACKEND / module).read_text(encoding="utf-8")
        assert "import InMemorySaver" not in source, f"{module} imports one"
        assert "import MemorySaver" not in source, f"{module} imports one"
        assert "InMemorySaver(" not in source, f"{module} constructs one"

    unreachable = replace(
        checkpointer_module.settings,
        database_url="postgresql://nobody:nobody@127.0.0.1:5432/does-not-exist",
    )
    yielded = []
    with patch.object(checkpointer_module, "settings", unreachable):
        with pytest.raises(CheckpointUnavailable):
            with checkpointer() as saver:
                yielded.append(saver)
    assert yielded == [], "nothing was yielded, so nothing could stand in"


def test_the_executor_treats_only_persistence_failures_as_persistence_failures():
    """Source contract on the other side of the boundary.

    `run_forever` retries on `CheckpointUnavailable` and on a psycopg
    `OperationalError`. It must not catch bare `Exception` there, or a graph bug
    would once again become "the database is down" -- the same defect, moved one
    module along.
    """
    source = (BACKEND / "workflow" / "executor.py").read_text(encoding="utf-8")
    loop = source[source.index("def run_forever") : source.index("def claim_and_run_one")]
    assert "except (CheckpointUnavailable, psycopg.OperationalError)" in loop
    assert "except Exception" not in loop
