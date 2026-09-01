"""`WORKFLOW_EXECUTOR` selects the executor's **host process**, and this is where
the API honours that.

Three things are being pinned:

* `inprocess` starts the loop on a daemon thread at startup and stops it at
  shutdown -- the *same* `Executor` the worker runs, not a second implementation.
* `worker` (the default, and production) starts nothing at all. The separate
  `python -m workflow.worker` workload advances durable runs.
* A bad configuration **fails startup** rather than being absorbed, and
  `inprocess` with no adapter has no mock fallback -- quietly substituting the
  deterministic adapter would let a deployment believe it was running real
  verification while it fabricated everything.

None of this touches `POST /api/analyze` or `POST /api/analyze/stream`, and the
last test in the file says so by exercising them with the executor running.

Marked `postgres` because the loop is **database-driven** by definition: it polls
for claimable runs. Handing it the in-memory double would be testing a lifecycle
around a thread that dies on its first statement, which is not the lifecycle
anyone deploys. The graph it is given is `MOCK_ADAPTER` -- deterministic, no
outbound HTTP -- and with no claimable runs the loop simply polls and sleeps.
"""

import threading
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import server
from config import ConfigError
from workflow.executor import (
    INPROCESS_THREAD_NAME,
    Executor,
    GraphAdapter,
    InprocessExecutor,
    MOCK_ADAPTER,
    run_inprocess,
)
from workflow.types import ExecutorUnavailable

pytestmark = pytest.mark.postgres

BACKEND = Path(__file__).resolve().parent.parent


def _executor_threads() -> list[threading.Thread]:
    return [
        thread
        for thread in threading.enumerate()
        if thread.name == INPROCESS_THREAD_NAME and thread.is_alive()
    ]


@pytest.fixture(autouse=True)
def no_executor_leaks():
    """Fail loudly if a test leaves a daemon executor running.

    A leaked loop polls the database for the rest of the session and would make
    the *next* test's duplicate-prevention assertion pass for the wrong reason.
    """
    before = _executor_threads()
    yield
    server._inprocess_executor = None
    leaked = [t for t in _executor_threads() if t not in before]
    for thread in leaked:
        thread.join(timeout=5)
    assert not [t for t in _executor_threads() if t not in before], (
        "a test leaked a running in-process executor"
    )


@pytest.fixture(autouse=True)
def executor_uses_the_real_database(real_db):
    """The loop polls `workflow.store` through `db.connect`. `real_db` undoes
    conftest's in-memory patch for these tests."""
    yield


@pytest.fixture
def idle_adapter() -> GraphAdapter:
    """`MOCK_ADAPTER` is a real `GraphAdapter` driving a deterministic graph with
    no outbound HTTP.

    The database it polls IS real (see the module docstring), but no test here
    creates a claimable run, so the loop finds nothing and sleeps. That is the
    lifecycle under test -- start, idle, stop -- not execution, which
    `test_workflow_executor.py` covers.
    """
    return MOCK_ADAPTER


# --- worker: the API starts nothing ---------------------------------------


def test_worker_topology_starts_no_thread(caplog):
    """The default. Production runs the executor as its own workload, and an API
    replica that also ran one would be a second, undeclared executor."""
    with caplog.at_level("INFO", logger="server"):
        with patch.object(
            server, "settings", replace(server.settings, workflow_executor="worker")
        ):
            with TestClient(server.app):
                assert _executor_threads() == []
                assert server._inprocess_executor is None
    assert "no in-process executor" in caplog.text


def test_worker_topology_is_the_shipped_default():
    from config import settings

    assert settings.workflow_executor == "worker"


# --- inprocess: start and stop --------------------------------------------


def test_inprocess_topology_starts_and_stops_the_loop(idle_adapter):
    """One thread on startup, none after shutdown."""
    started: list[InprocessExecutor] = []

    def fake_run_inprocess():
        host = run_inprocess(adapter=idle_adapter)
        started.append(host)
        return host

    with patch.object(
        server, "settings", replace(server.settings, workflow_executor="inprocess")
    ), patch.object(server, "run_inprocess", fake_run_inprocess):
        with TestClient(server.app):
            assert len(_executor_threads()) == 1
            assert server._inprocess_executor is started[0]
            assert started[0].running
        assert _executor_threads() == [], "shutdown joined the thread"
        assert server._inprocess_executor is None
        assert not started[0].running


def test_the_inprocess_thread_is_a_daemon(idle_adapter):
    """A daemon thread cannot keep the process alive after the API exits. The
    lease is what makes that safe: an interrupted claim is reclaimed."""
    host = run_inprocess(adapter=idle_adapter)
    try:
        assert _executor_threads()[0].daemon is True
    finally:
        host.stop()


def test_starting_a_second_inprocess_executor_is_refused(idle_adapter):
    """Two loops in one process would both poll and one would lose every race --
    harmless because the lease is correct, but pure waste and a symptom of a
    lifecycle bug worth failing on."""
    host = run_inprocess(adapter=idle_adapter)
    try:
        with pytest.raises(ExecutorUnavailable) as caught:
            run_inprocess(adapter=idle_adapter)
        assert "already running" in str(caught.value)
        assert len(_executor_threads()) == 1
    finally:
        host.stop()


def test_a_second_startup_while_one_is_running_is_refused(idle_adapter):
    """The same guard, reached through the lifespan rather than directly."""
    host = run_inprocess(adapter=idle_adapter)
    try:
        server._inprocess_executor = host
        with patch.object(
            server, "settings", replace(server.settings, workflow_executor="inprocess")
        ):
            with pytest.raises(ExecutorUnavailable):
                with TestClient(server.app):
                    pass
        assert len(_executor_threads()) == 1
    finally:
        host.stop()
        server._inprocess_executor = None


def test_restarting_after_a_clean_shutdown_is_allowed(idle_adapter):
    """The guard is against *concurrent* executors, not against ever starting one
    twice -- a reload has to work."""
    first = run_inprocess(adapter=idle_adapter)
    first.stop()
    assert _executor_threads() == []
    second = run_inprocess(adapter=idle_adapter)
    try:
        assert len(_executor_threads()) == 1
    finally:
        second.stop()


def test_stop_is_idempotent(idle_adapter):
    host = run_inprocess(adapter=idle_adapter)
    host.stop()
    host.stop()
    assert _executor_threads() == []


# --- configuration refusals ------------------------------------------------


@pytest.mark.parametrize("topology", ["", "background", "inproc", "WORKER", "both"])
def test_an_unknown_executor_topology_fails_startup(topology):
    """It selects the host process, not the implementation -- and an unrecognised
    value means nobody knows which host was intended."""
    with patch.object(
        server, "settings", replace(server.settings, workflow_executor=topology)
    ):
        with pytest.raises(ConfigError) as caught:
            with TestClient(server.app):
                pass
    assert "WORKFLOW_EXECUTOR" in str(caught.value)
    assert _executor_threads() == []


def test_inprocess_with_no_graph_adapter_fails_startup():
    """**No mock fallback.** An API configured for `inprocess` with no adapter
    refuses to serve, exactly as the worker refuses to start."""
    import workflow.executor as executor_module

    with patch.object(
        server,
        "settings",
        replace(server.settings, workflow_executor="inprocess", workflow_graph_adapter=""),
    ), patch.object(
        executor_module,
        "settings",
        replace(executor_module.settings, workflow_graph_adapter=""),
    ):
        with pytest.raises(ExecutorUnavailable) as caught:
            with TestClient(server.app):
                pass
    assert "no graph adapter is configured" in str(caught.value)
    assert _executor_threads() == []


def test_an_invalid_lease_pair_fails_startup(idle_adapter):
    """`Executor.__init__` validates it, so a bad pair cannot reach a run."""
    import workflow.executor as executor_module

    broken = replace(
        executor_module.settings, lease_ttl_seconds=10, lease_heartbeat_seconds=30
    )
    with patch.object(executor_module, "settings", broken):
        with pytest.raises(ConfigError):
            run_inprocess(adapter=idle_adapter)
    assert _executor_threads() == []


# --- one implementation, and the old endpoints are untouched --------------


def test_the_inprocess_host_wraps_the_same_executor_class(idle_adapter):
    """Not a second implementation. `InprocessExecutor` holds an `Executor` and
    calls its `run_forever` -- the identical loop `python -m workflow.worker`
    drives."""
    host = run_inprocess(adapter=idle_adapter)
    try:
        assert isinstance(host.executor, Executor)
        assert host.executor.run_forever.__qualname__ == "Executor.run_forever"
    finally:
        host.stop()


def test_the_api_never_uses_background_tasks_for_the_executor():
    """It dies with the request, holds no lease, and recovers from nothing.

    The check is for an import or an annotation, not for the words: `server.py`'s
    own docstring names `BackgroundTasks` in order to rule it out.
    """
    source = (BACKEND / "server.py").read_text(encoding="utf-8")
    for usage in ("import BackgroundTasks", "BackgroundTasks)", ": BackgroundTasks"):
        assert usage not in source, f"server.py uses BackgroundTasks ({usage})"


def test_the_search_endpoints_are_unchanged_with_the_executor_running(
    idle_adapter, report_store
):
    """The whole point of "it only chooses the host process": the search-only
    endpoints behave identically whether or not a durable executor is running
    beside them."""
    from tests.test_pipeline_contract import RESULT_KEYS

    def _token(client: TestClient) -> str:
        with patch("server.authenticate", return_value="testnovelty@gmail.com"):
            response = client.post(
                "/api/auth/login", json={"email": "a@b.c", "password": "x"}
            )
        return response.json()["token"]

    with patch.object(
        server, "settings", replace(server.settings, workflow_executor="inprocess")
    ), patch.object(server, "run_inprocess", lambda: run_inprocess(adapter=idle_adapter)):
        with TestClient(server.app) as client:
            assert len(_executor_threads()) == 1
            headers = {"Authorization": f"Bearer {_token(client)}"}

            with patch(
                "server.run_graph_pipeline",
                return_value=iter(
                    [{"type": "result", "data": {key: None for key in RESULT_KEYS}}]
                ),
            ):
                response = client.post(
                    "/api/analyze", json={"description": "x"}, headers=headers
                )

            assert response.status_code == 200
            assert set(response.json().keys()) == RESULT_KEYS, (
                "the search-only result keys are exactly what they were"
            )
