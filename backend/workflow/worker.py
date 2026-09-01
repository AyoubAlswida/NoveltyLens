"""``python -m workflow.worker`` -- the executor's production host process.

**This is a second workload, not a thread in the API.** In production it runs
alongside the FastAPI deployment: the API admits runs and commits human
decisions, this process executes them. A client disconnect never reaches here,
because this process has never heard of the HTTP connection.

Usage::

    python -m workflow.worker                 # the configured graph adapter
    python -m workflow.worker --mock          # the deterministic, no-HTTP adapter
    python -m workflow.worker --once          # claim at most one run and exit

**It refuses to start on an invalid lease configuration**, and it refuses to
start with no graph adapter configured. Neither is silently defaulted: a default
that quietly overrides a deliberate setting is how a misconfiguration survives a
deploy unnoticed, and an executor with no graph would claim runs it cannot
advance.

SIGTERM and SIGINT stop the loop cleanly. A run in flight keeps its lease until
it expires, and the reclaim sweep hands it to a replacement, which continues from
the committed checkpoint -- that is the designed recovery path, not an error.
"""

import argparse
import logging
import signal
import sys
import threading

logger = logging.getLogger(__name__)

try:
    # Guarded, and at module level rather than inside main(), for one specific
    # reason: config.py validates the lease pair **at import**. That is the
    # stronger guarantee -- no process anywhere runs with a heartbeat at or above
    # its TTL, which would fence a healthy worker mid-run -- but it means a bad
    # value raises here, before main() has run, and an operator reading a crashed
    # worker's log deserves the one sentence naming the offending setting rather
    # than a traceback through a module header.
    #
    # The catch is broad because any failure to load configuration is a refusal
    # to start: there is nothing this process could usefully do with a partial
    # one, and `from config import ConfigError` cannot be used to narrow it --
    # when the import is what raises, that name is never bound.
    from config import ConfigError, settings, validate_lease_configuration
except Exception as error:  # noqa: BLE001 - see above
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    logger.error("worker refusing to start: %s", error)
    raise SystemExit(2)

from workflow.executor import MOCK_ADAPTER, Executor, load_adapter
from workflow.types import ExecutorUnavailable


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m workflow.worker",
        description="Execute durable verification runs from the database.",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help=(
            "drive the deterministic adapter, which makes no outbound HTTP calls. "
            "The executor, lease, event log and checkpointer are all real."
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="claim at most one run, drive it as far as it goes, and exit.",
    )
    parser.add_argument(
        "--owner-id",
        default=None,
        help="override the generated host:pid:uuid worker identity (tests use this).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = build_parser().parse_args(argv)

    try:
        # Checked again here, explicitly. The import-time check above covers the
        # environment; this covers a `settings` object handed in by a caller, and
        # it is what makes the refusal visibly the worker's own.
        validate_lease_configuration(
            settings.lease_ttl_seconds, settings.lease_heartbeat_seconds
        )
        adapter = (
            MOCK_ADAPTER if args.mock else load_adapter(settings.workflow_graph_adapter)
        )
    except (ConfigError, ExecutorUnavailable) as error:
        logger.error("worker refusing to start: %s", error)
        return 2

    executor = Executor(adapter=adapter, owner_id=args.owner_id)
    logger.info(
        "worker %s: ttl=%ss heartbeat=%ss adapter=%s",
        executor.owner_id,
        executor.ttl_seconds,
        executor.heartbeat_seconds,
        "mock" if args.mock else settings.workflow_graph_adapter,
    )

    if args.once:
        claimed = executor.claim_and_run_one()
        logger.info("worker %s: claimed %s", executor.owner_id, claimed or "nothing")
        return 0

    stop = threading.Event()

    def _stop(signum: int, _frame: object) -> None:
        logger.info("worker %s: signal %s, stopping", executor.owner_id, signum)
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _stop)

    executor.run_forever(stop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
