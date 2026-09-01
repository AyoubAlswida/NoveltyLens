"""The LangGraph Postgres checkpointer: construction, setup, and the boundary
between LangGraph's tables and ours.

**The checkpoint is not the event log, and never becomes one.** A checkpoint
answers "where is execution?". A reconnecting client asks "what did I miss?" --
a different question the checkpoint structurally cannot answer, because it holds
state, not a history of emissions. That second question is `workflow_events`'
job (`backend/workflow/events.py`).

**LangGraph owns its own tables and yoyo must not touch them.**
``PostgresSaver.setup()`` creates and migrates ``checkpoints``,
``checkpoint_blobs``, ``checkpoint_writes`` and ``checkpoint_migrations`` itself,
tracking its own schema version in that last one. Adding a yoyo migration that
also created them would give two migration systems the same tables and a
guaranteed conflict the first time either side changed a column. So: yoyo owns
``backend/migrations/``, LangGraph owns the four checkpoint tables, and the two
never overlap.

Run it explicitly, once, after ``python migrate.py``::

    python checkpointer.py setup

**There is no in-memory fallback.** A checkpointer that cannot be constructed
fails run admission with ``CHECKPOINT_UNAVAILABLE``; it does not silently become
an ``InMemorySaver`` whose contents vanish with the process, which would present
a run as durable when it is not.
"""

import logging
import re
import sys
from contextlib import contextmanager
from typing import Iterator
from urllib.parse import urlsplit

from langgraph.checkpoint.postgres import PostgresSaver

from config import settings
from workflow import redaction
from workflow.types import CheckpointUnavailable

logger = logging.getLogger(__name__)


def _safe_target() -> str:
    """``host:port/dbname`` and nothing else.

    Built from the parsed DSN rather than echoed from the driver, because a
    connection string carries a password in its userinfo and can carry more in its
    query string (``?password=``, ``?sslpassword=``). This string reaches an
    operator's log and, through `CheckpointUnavailable`, can reach a stored
    `failure_detail`; a DSN must never travel with it.

    Falls back to a constant rather than to the raw URL when parsing fails --
    "unparseable" is a safe thing to say, and echoing an unparseable DSN is
    exactly how a credential leaks.
    """
    try:
        parsed = urlsplit(settings.database_url)
        host = parsed.hostname or "unknown-host"
        port = f":{parsed.port}" if parsed.port else ""
        name = parsed.path.lstrip("/") or "unknown-database"
        return f"{host}{port}/{name}"
    except Exception:  # pragma: no cover - a malformed DSN
        return "the configured database"


def _safe_reason(error: Exception) -> str:
    """The driver failure as allow-listed facts -- exception type and SQLSTATE.

    **It no longer carries the driver's own prose.** psycopg builds its connection
    errors from host, port and database name and does not echo the password -- but
    "does not today" is a weak guarantee for a string that can be persisted, and
    the 2026-08-29 amendment replaced "redact the message" with "rebuild from
    fields" everywhere, after an incomplete pattern let an account identifier
    reach stdout.

    Nothing actionable is lost. `CheckpointUnavailable` already names the parsed
    host, port and database, and SQLSTATE is a sharper diagnostic than the
    sentence it replaces: `08006` is a connection failure and `28P01` is a bad
    password, and an operator reading either knows precisely where to go.

    The DSN substitution below is kept as a belt-and-braces pass over a string
    that no longer contains one.
    """
    return _DSN_PATTERN.sub("<redacted-dsn>", redaction.safe_log(error))


_DSN_PATTERN = re.compile(r"postgres(?:ql)?(?:\+\w+)?://\S*", re.IGNORECASE)
"""Anything shaped like a connection string, credentials and all."""


CHECKPOINT_TABLES: tuple[str, ...] = (
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
    "checkpoint_migrations",
)
"""The tables ``PostgresSaver.setup()`` owns. Listed here so a reader of
``backend/migrations/`` can see what is deliberately missing from it, and so
``tests/test_checkpointer.py`` can assert yoyo never creates one of them."""


@contextmanager
def checkpointer() -> Iterator[PostgresSaver]:
    """Yield a `PostgresSaver` bound to ``settings.database_url``.

    A context manager because the saver owns a connection pool that has to be
    closed; the executor holds one open for the life of a claimed run.

    **Only checkpointer failures become `CheckpointUnavailable`.** An earlier
    revision wrapped the `yield` itself in a broad `except Exception`, which meant
    every exception the *caller's* `with` body raised came back through the
    generator at the yield point and was relabelled. A `FencedError` -- a worker
    correctly discovering it had lost its lease -- was reported as a database
    outage, and so was a plain `ValueError` from a graph node. Both diagnoses were
    wrong, and the second is worse than useless: it points an operator at
    PostgreSQL for a bug in the code.

    So the construction and the teardown are guarded and the body is not. Anything
    raised inside the `with` propagates with its original type and message intact.

    Raises:
        CheckpointUnavailable: when the saver cannot be **constructed** or cleanly
            **closed**. The message carries a redacted DSN -- host, port and
            database only -- built by this module rather than echoed from the
            driver, so no password, query-string credential or userinfo can travel
            with it.
    """
    manager = PostgresSaver.from_conn_string(settings.database_url)
    try:
        saver = manager.__enter__()
    except Exception as error:
        raise CheckpointUnavailable(
            f"the LangGraph checkpointer at {_safe_target()} is unreachable: "
            f"{_safe_reason(error)}"
        ) from error

    try:
        yield saver
    except BaseException:
        # The caller's own failure. Close the saver, discard any secondary error
        # from that close (the primary failure is the one worth reporting), and
        # let the original propagate untouched -- same type, same message.
        try:
            manager.__exit__(*sys.exc_info())
        except Exception:  # pragma: no cover - a close error during unwind
            logger.warning("the checkpointer failed to close while unwinding")
        raise
    else:
        try:
            manager.__exit__(None, None, None)
        except Exception as error:
            raise CheckpointUnavailable(
                f"the LangGraph checkpointer at {_safe_target()} could not be closed: "
                f"{_safe_reason(error)}"
            ) from error


def setup() -> None:
    """Create or migrate LangGraph's checkpoint tables. Idempotent.

    Deliberately separate from ``python migrate.py``: see the module docstring
    for why two migration systems must not own the same tables.
    """
    with checkpointer() as saver:
        saver.setup()


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] != "setup":
        print("usage: python checkpointer.py setup", file=sys.stderr)
        raise SystemExit(2)
    setup()
    print("LangGraph checkpoint tables ready: " + ", ".join(CHECKPOINT_TABLES))
