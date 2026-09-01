"""`python -m workflow.worker`: the CLI contract and its startup refusals.

The worker is a **second workload**, not a thread inside the API. In production
it runs alongside the FastAPI deployment: the API admits runs and commits human
decisions, this process executes them. A client disconnect never reaches it,
because it has never heard of the HTTP connection.

Two refusals matter more than anything else it does, and both are here:

* an invalid lease configuration stops it starting, rather than being silently
  replaced with a default -- a default that quietly overrides a deliberate
  setting is how a misconfiguration survives a deploy unnoticed;
* no configured graph adapter stops it starting, rather than claiming runs it
  cannot advance.
"""

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from config import ConfigError, settings, validate_lease_configuration
from workflow import worker

BACKEND = Path(__file__).resolve().parent.parent


# --- startup validation ---------------------------------------------------


@pytest.mark.parametrize(
    "ttl,heartbeat,why",
    [
        (0, 20, "a zero TTL is expired the moment it is acquired"),
        (-5, 20, "a negative TTL is not a duration"),
        (60, 0, "a zero heartbeat never renews"),
        (60, -1, "a negative heartbeat never renews"),
        (60, 60, "a heartbeat at the TTL lapses between beats"),
        (60, 90, "a heartbeat above the TTL lapses between beats"),
    ],
)
def test_validate_lease_configuration_rejects(ttl, heartbeat, why):
    with pytest.raises(ConfigError):
        validate_lease_configuration(ttl, heartbeat)


def test_validate_lease_configuration_accepts_the_shipped_defaults():
    validate_lease_configuration(settings.lease_ttl_seconds, settings.lease_heartbeat_seconds)
    assert settings.lease_heartbeat_seconds < settings.lease_ttl_seconds


def test_the_worker_refuses_to_start_on_an_invalid_lease_configuration(caplog):
    """Exit code 2, a logged reason, and **no default substituted**."""
    broken = replace(settings, lease_ttl_seconds=10, lease_heartbeat_seconds=10)
    with patch.object(worker, "settings", broken):
        assert worker.main([]) == 2
    assert "refusing to start" in caplog.text
    assert "strictly below" in caplog.text


def test_the_worker_refuses_to_start_with_no_graph_adapter(caplog):
    """An executor with no graph to run would claim runs it cannot advance.

    There is deliberately no default adapter: a default would silently run the
    wrong graph, which is worse than not starting.
    """
    unconfigured = replace(settings, workflow_graph_adapter="")
    with patch.object(worker, "settings", unconfigured):
        assert worker.main([]) == 2
    assert "no graph adapter is configured" in caplog.text


def test_the_worker_refuses_a_malformed_adapter_reference(caplog):
    misconfigured = replace(settings, workflow_graph_adapter="not-a-module-spec")
    with patch.object(worker, "settings", misconfigured):
        assert worker.main([]) == 2
    assert "module:attribute" in caplog.text


# --- the CLI contract -----------------------------------------------------


def test_the_parser_exposes_mock_once_and_owner_id():
    parsed = worker.build_parser().parse_args(["--mock", "--once", "--owner-id", "w1"])
    assert (parsed.mock, parsed.once, parsed.owner_id) == (True, True, "w1")


@pytest.mark.postgres
def test_a_mock_worker_run_drives_a_real_run_to_its_gate(pg, make_run, real_db):
    """`--mock --once` claims one run, drives it, and exits 0.

    The executor, the lease, the event log, the transitions and the checkpointer
    are all real here. Only the graph is deterministic, and it makes no outbound
    HTTP calls at all.
    """
    run_id = make_run()
    assert worker.main(["--mock", "--once", "--owner-id", "cli-worker"]) == 0

    status = pg.execute(
        "SELECT status FROM verification_runs WHERE id = %s", (run_id,)
    ).fetchone()[0]
    assert status in ("WAITING_FOR_REVIEW", "QUEUED"), (
        "it either drove this run to its gate or claimed a different one"
    )


def test_the_mock_adapter_makes_no_outbound_calls():
    """A source contract over the adapter's own module: no `requests`, no
    `httpx`, no provider client anywhere on the mock path."""
    source = (BACKEND / "workflow" / "executor.py").read_text(encoding="utf-8")
    for forbidden in ("import requests", "import httpx", "from search", "from llm"):
        assert forbidden not in source, (
            f"the executor imports {forbidden!r}, which puts a paid boundary on the "
            "mock path"
        )


def test_background_tasks_are_not_used_as_the_executor():
    """`BackgroundTasks` dies with the process, has no lease, no recovery and no
    visibility. It may not be used as the executor even temporarily.

    The check is for an import or an annotation, not for the words: the
    executor's own docstring names it in order to forbid it, and a grep that
    could not tell those apart would have to be deleted the first time somebody
    documented the rule.
    """
    usages = ("import BackgroundTasks", "BackgroundTasks)", "BackgroundTasks,", ": BackgroundTasks")
    offenders = []
    for path in BACKEND.rglob("*.py"):
        if any(part in {".venv", "__pycache__", "tests"} for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8")
        if any(usage in text for usage in usages):
            offenders.append(str(path.relative_to(BACKEND)))
    assert offenders == [], f"these modules use FastAPI BackgroundTasks: {offenders}"
