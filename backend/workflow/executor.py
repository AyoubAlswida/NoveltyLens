"""The durable executor: poll, acquire, **reconcile**, execute, transition,
release.

**A LangGraph Postgres checkpoint stores state. It does not continue execution.**
Nothing in LangGraph restarts a run after the process driving `graph.stream()`
goes away. In the search-only pipeline that driver is the HTTP request itself, so
a closed browser tab ends the run -- acceptable for a 60-second search, fatal for
a run that pauses for human review measured in minutes to days. This module is
the driver that outlives the request.

**One executor, deployed two ways.** `WORKFLOW_EXECUTOR=worker` runs this loop as
a second workload (`python -m workflow.worker`); `inprocess` runs the identical
loop on a daemon thread inside the API for local development, still
lease-protected, still database-driven, still recovering after a restart. This is
*not* the deleted `PIPELINE` flag pattern: that flag chose between two parallel
implementations, this chooses where one implementation runs.

**FastAPI `BackgroundTasks` is forbidden as the executor**, even temporarily. It
dies with the process, has no lease, no recovery and no visibility.

**Three stores, three jobs, and reconcile is where they are reconciled:**

* the **checkpoint** is the source of truth for *execution position*;
* the **event log** is the source of truth for *what the client has been told*;
* the **run row** is a derived index over both, for cheap querying.

LangGraph commits its checkpoint in its own transaction, which this work does not
join. So on every acquisition the executor reconciles **before doing anything
else**: read the checkpoint, derive the true position, append any missing
lifecycle events (idempotent through checkpoint-derived dedupe keys), then move
the status to match. The event log is repaired **forward only** -- never
rewritten, never deleted, never renumbered, because a client that already
received event 17 must never be told that 17 now means something else.

**No in-memory fallback anywhere.** If the lease store, the checkpointer or the
event log is unreachable the worker stops. It does not continue in memory and it
does not present success.
"""

import importlib
import logging
import traceback
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, TypedDict

import psycopg
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

import db
from config import settings, validate_lease_configuration
from workflow import attempts, lease
from workflow import events as event_log
from workflow import redaction
from workflow import store
from workflow.types import (
    CheckpointUnavailable,
    ExecutorUnavailable,
    FencedError,
    RunStatus,
    WorkflowEventType,
    WorkflowGate,
    WorkflowError,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GraphAdapter:
    """Everything the executor needs to know about the graph it drives.

    Kept as data rather than as a subclass hierarchy so a test can supply a
    two-node graph and production can supply the real one without either knowing
    about the other. The executor contains no graph-specific logic at all.

    Attributes:
        build: ``(checkpointer) -> compiled graph``. Called once per claimed run.
        initial_input: ``(run) -> dict`` -- the graph input for a QUEUED run.
        result_payload: ``(run, final_state) -> dict`` -- the terminal `result`
            event's `data` object.
        gate_of: ``(interrupt_value) -> WorkflowGate`` -- which workflow gate an
            `interrupt()` payload represents.
        review_payload: ``(run, gate, interrupt_value) -> dict`` -- the
            `review_required` event body.
        resume_value: ``(run, resumed_event) -> Any`` -- what to pass to
            ``Command(resume=...)``, derived from the human decision the API
            already committed.
    """

    build: Callable[[Any], Any]
    initial_input: Callable[[dict[str, Any]], dict[str, Any]]
    result_payload: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
    gate_of: Callable[[Any], WorkflowGate]
    review_payload: Callable[[dict[str, Any], WorkflowGate, Any], dict[str, Any]]
    resume_value: Callable[[dict[str, Any], Optional[dict[str, Any]]], Any]


def load_adapter(spec: str) -> GraphAdapter:
    """Resolve a ``module:attribute`` adapter reference.

    Raises:
        ExecutorUnavailable: when the spec is empty, malformed, or does not
            resolve to a `GraphAdapter`. The executor refuses to start rather
            than admitting runs it cannot advance -- there is no default adapter,
            because a default would silently run the wrong graph.
    """
    if not spec:
        raise ExecutorUnavailable(
            "no graph adapter is configured. Set WORKFLOW_GRAPH_ADAPTER to "
            "'module:attribute', or run `python -m workflow.worker --mock` for the "
            "deterministic no-HTTP adapter. There is deliberately no default."
        )
    if ":" not in spec:
        raise ExecutorUnavailable(
            f"WORKFLOW_GRAPH_ADAPTER must be 'module:attribute', got {spec!r}"
        )
    module_name, _, attribute = spec.partition(":")
    try:
        module = importlib.import_module(module_name)
        adapter = getattr(module, attribute)
    except Exception as error:
        raise ExecutorUnavailable(
            f"graph adapter {spec!r} could not be loaded: {error}"
        ) from error
    if not isinstance(adapter, GraphAdapter):
        raise ExecutorUnavailable(
            f"graph adapter {spec!r} is a {type(adapter).__name__}, not a GraphAdapter"
        )
    return adapter


class Executor:
    """One executor. Claims runs, reconciles them, drives their graph.

    Constructed with a :class:`GraphAdapter` and a checkpointer factory so the
    same class serves the worker process, the in-process development thread and
    the tests. It holds no per-run state between claims: everything it needs is in
    the database and in the checkpoint, which is what lets a *different* process
    pick a run up mid-flight.
    """

    def __init__(
        self,
        *,
        adapter: GraphAdapter,
        checkpointer_factory: Optional[Callable[[], Any]] = None,
        owner_id: Optional[str] = None,
        ttl_seconds: Optional[float] = None,
        heartbeat_seconds: Optional[float] = None,
        poll_interval_seconds: Optional[float] = None,
    ) -> None:
        self.adapter = adapter
        self.owner_id = owner_id or lease.new_owner_id()
        self.ttl_seconds = (
            settings.lease_ttl_seconds if ttl_seconds is None else ttl_seconds
        )
        self.heartbeat_seconds = (
            settings.lease_heartbeat_seconds
            if heartbeat_seconds is None
            else heartbeat_seconds
        )
        self.poll_interval_seconds = (
            settings.workflow_poll_interval_seconds
            if poll_interval_seconds is None
            else poll_interval_seconds
        )
        # Validated here as well as at import: an executor constructed with
        # explicit arguments has not been through config's check, and a heartbeat
        # at or above the TTL would fence a healthy worker mid-run.
        validate_lease_configuration(self.ttl_seconds, self.heartbeat_seconds)

        if checkpointer_factory is None:
            from checkpointer import checkpointer as _checkpointer

            checkpointer_factory = _checkpointer
        self._checkpointer_factory = checkpointer_factory

    # --- the loop ---------------------------------------------------------

    def run_forever(self, stop: Optional[threading.Event] = None) -> None:
        """Poll and execute until stopped. The worker's main loop.

        A claim that finds nothing sleeps for the poll interval; a claim that ran
        something loops straight back, because a busy queue should not wait.
        """
        stop = stop or threading.Event()
        logger.info("executor %s started", self.owner_id)
        while not stop.is_set():
            try:
                claimed = self.claim_and_run_one()
            except (CheckpointUnavailable, psycopg.OperationalError) as error:
                # The database or the checkpointer is gone. **There is no error
                # event**, and saying otherwise would be a claim this system
                # cannot honour: an `error` event cannot be persisted while the
                # event log is unreachable.
                #
                # So the worker stops advancing, waits, and retries. It does not
                # continue in memory, does not finish the graph, and above all
                # does not present success. The run keeps whatever state was last
                # committed; when the database recovers, reconciliation either
                # resumes it from the committed checkpoint or -- if it cannot be
                # advanced -- records a sanitized infrastructure failure at that
                # point, when there is finally somewhere to write it.
                # Redacted: a psycopg OperationalError embeds the DSN it tried,
                # and a log file is a place credentials get archived and shipped
                # to an aggregator.
                logger.error(
                    "executor cannot reach persistence: %s", redaction.safe_log(error)
                )
                claimed = None
            except psycopg.errors.ForeignKeyViolation as error:
                # **The run this worker claimed no longer exists.**
                #
                # Every lease-sensitive SQL function starts with
                # `_lock_run_and_require_lease`, which raises `unknown run <id>`
                # as a foreign-key violation when the row is gone. A run can
                # legitimately disappear mid-flight -- retention removing it, an
                # operator deleting it, a test fixture cleaning up against a
                # shared development database -- and the disappearance is not a
                # failure OF the run: there is no run left to fail, no status to
                # transition and nothing to write an `error` event to.
                #
                # It used to propagate, and it killed the worker thread outright:
                # one deleted run stopped every future run on that worker, in
                # silence, with the API still serving happily. So it is logged
                # and abandoned, and the loop continues.
                #
                # Deliberately narrow. This is the only integrity error treated
                # this way, and it is not a general `except IntegrityError`: a
                # violation that is NOT about a missing run is a real defect and
                # must still stop the worker rather than being polled past.
                if "unknown run" not in str(error):
                    raise
                logger.warning(
                    "the claimed run no longer exists; abandoning it and continuing: %s",
                    redaction.safe_log(error),
                )
                claimed = None
            if claimed is None:
                stop.wait(self.poll_interval_seconds)
        logger.info("executor %s stopped", self.owner_id)

    def claim_and_run_one(self) -> Optional[str]:
        """Try to claim one run and drive it as far as it goes.

        Returns:
            The run id it worked on, or ``None`` when nothing was claimable --
            which includes losing an acquisition race, and that is not an error.
        """
        with db.connect() as conn:
            candidates = store._system_claimable_run_ids(conn, limit=10)

        for run_id in candidates:
            with db.connect() as conn:
                fence_token = lease.acquire(
                    conn,
                    run_id=run_id,
                    owner_id=self.owner_id,
                    ttl_seconds=self.ttl_seconds,
                )
            if fence_token is None:
                # A live lease is held elsewhere, or the run stopped being
                # claimable between the sweep and now. Return to the poll -- do
                # not retry in a loop, and do not assume the lease is stale.
                continue
            self._execute_claimed(run_id, fence_token)
            return run_id
        return None

    # --- one claimed run --------------------------------------------------

    def _execute_claimed(self, run_id: str, fence_token: int) -> None:
        """Drive one leased run. Every write inside carries `fence_token`."""
        beat = lease.Heartbeat(
            run_id=run_id,
            owner_id=self.owner_id,
            fence_token=fence_token,
            ttl_seconds=self.ttl_seconds,
            interval_seconds=self.heartbeat_seconds,
        ).start()
        try:
            with self._checkpointer_factory() as checkpointer:
                graph = self.adapter.build(checkpointer)
                self._reconcile_and_run(graph, run_id, fence_token, beat)
        except FencedError:
            # Lost the lease. Somebody else owns this run now; everything we
            # might write would be rejected anyway, and writing it would be
            # claiming authority we no longer hold.
            logger.warning("run %s: fenced at token %s; abandoning", run_id, fence_token)
        except CheckpointUnavailable:
            raise
        finally:
            beat.stop()

    def _reconcile_and_run(
        self, graph: Any, run_id: str, fence_token: int, beat: lease.Heartbeat
    ) -> None:
        # The lease identity rides in `configurable`, NOT in graph state.
        #
        # A node that persists machine evidence (a matrix cell, a passage, an
        # exclusion) has to write under the fence, and `workflow/store.py` takes
        # `owner_id` and `fence_token` for exactly that. Putting them in
        # `GraphState` would checkpoint them -- and a checkpointed fence token is
        # stale the moment the run is resumed by a different worker, so the first
        # write after any recovery would fence itself against a token it had
        # merely remembered.
        #
        # `config` is rebuilt on every acquisition, so a node always reads the
        # authority this process actually holds right now.
        config = {
            "configurable": {
                "thread_id": run_id,
                "owner_id": self.owner_id,
                "fence_token": fence_token,
            }
        }

        with db.connect() as conn:
            run = store._system_get_run(conn, run_id=run_id)
            # Close attempts a superseded holder left open. Done here, by the new
            # holder under its own valid lease -- a fenced worker must never write
            # telemetry using the authority it just lost.
            attempts.close_superseded_attempts(
                conn, run_id=run_id, owner_id=self.owner_id, fence_token=fence_token
            )

        snapshot = graph.get_state(config)
        if self._reconcile_pause(graph, run, fence_token, snapshot, config):
            return  # the run was already parked at a gate; the repair parked it

        try:
            self._advance(graph, run, fence_token, config, beat)
        except FencedError:
            raise
        except Exception as error:
            # **The traceback, scrubbed -- not the traceback raw.**
            #
            # This used to be `logger.exception(...)`, on the reasoning that the
            # stored failure reaches a browser while the local log is the
            # operator's. That reasoning is wrong, and the sibling comment in
            # `run_forever` above already says why: a log file is a place
            # credentials get archived and shipped to an aggregator. A raw
            # traceback carries the exception's own text, and an ordinary
            # SerpAPI quota failure spells its api_key into the request URL --
            # so `logger.exception` published the key to every log sink.
            #
            # The operator still gets what costs the hour: the exception type,
            # the sanitized facts (provider, HTTP status, workflow step) and the
            # full call stack. Only the credential-bearing substrings are gone,
            # through the same redactor the persisted failure uses, so there is
            # one definition of what a secret looks like rather than two.
            logger.error(
                "run %s failed while executing: %s\n%s",
                run_id,
                redaction.safe_log(error),
                # `format_tb`, NOT `format_exception`: the frames are code
                # locations -- file, line, function, source -- and carry no
                # provider text. `format_exception` appends the exception's own
                # message, which is where the credential-bearing request URL
                # lives, and masking its *value* is not enough: an archived
                # `serpapi.com/search?api_key=...` line is a credential URL
                # whatever sits after the `=`. Redacted as well, because a
                # frame's source line is still text this process did not write.
                redaction.redact(
                    "".join(traceback.format_tb(error.__traceback__))
                ),
            )
            self._fail(run_id, fence_token, error, beat)
            return

    def _reconcile_pause(
        self,
        graph: Any,
        run: dict[str, Any],
        fence_token: int,
        snapshot: Any,
        config: dict[str, Any],
    ) -> bool:
        """Repair a crash between LangGraph's checkpoint commit and our pause
        transaction.

        Three crash boundaries exist and one call repairs all three, because the
        dedupe keys are functions of the **checkpoint**, not of the attempt:

        ==========================================  ======================================
        On disk                                     Repair
        ==========================================  ======================================
        interrupt checkpointed, no pause txn        this call writes both events, the
                                                    transition and the release
        `review_required` without `paused`          the first key replays as a no-op, the
                                                    second is appended once
        both events, no transition                  both keys replay, the guarded
                                                    `RUNNING -> WAITING_FOR_REVIEW` applies
        ==========================================  ======================================

        The last two are unreachable while the pause is one SQL function, and are
        handled anyway: a future refactor that split it would otherwise
        reintroduce them silently, and a database restored from a partial backup
        can present either shape.

        Returns:
            True when the run was parked (by this repair), so the caller stops.
        """
        pending = _pending_interrupt(snapshot)
        if pending is None:
            return False
        if run["status"] is not RunStatus.RUNNING:
            # A parked run is not claimable, so RUNNING is the only status that
            # can reach here with an interrupt on disk.
            return False

        logger.info(
            "run %s: reconciling an interrupt committed without its pause transaction",
            run["run_id"],
        )
        self._pause(run, fence_token, pending, snapshot, config)
        return True

    def _advance(
        self,
        graph: Any,
        run: dict[str, Any],
        fence_token: int,
        config: dict[str, Any],
        beat: lease.Heartbeat,
    ) -> None:
        """Move the run to RUNNING and stream the graph until it pauses or ends."""
        run_id = run["run_id"]
        status = run["status"]

        if status is RunStatus.QUEUED:
            graph_input: Any = self.adapter.initial_input(run)
        elif status is RunStatus.RESUMING:
            with db.connect() as conn:
                resumed = _latest_event(
                    conn, run_id=run_id, event_type=WorkflowEventType.RESUMED
                )
            graph_input = Command(resume=self.adapter.resume_value(run, resumed))
        elif status is RunStatus.RUNNING:
            # Recovered mid-run: LangGraph replays the pending task from the last
            # committed checkpoint. A node whose checkpoint already committed is
            # never repeated -- the checkpoint is the record of what finished.
            #
            # Unless there is no checkpoint at all. A previous holder can commit
            # the QUEUED -> RUNNING transition and then die before the graph
            # produces its first checkpoint, which leaves a RUNNING run whose
            # thread LangGraph has never seen. Resuming that with `None` raises
            # `EmptyInputError`; the honest recovery is to start it, because
            # nothing has run yet. This is recovery rule 3 -- a node interrupted
            # before its checkpoint committed may run again -- at the boundary
            # where the node in question is the first one.
            graph_input = None if _has_checkpoint(graph, config) else (
                self.adapter.initial_input(run)
            )
        else:  # pragma: no cover - acquire_run_lease refuses every other status
            raise WorkflowError(f"run {run_id} is {status}; it cannot be advanced")

        if status is not RunStatus.RUNNING:
            with db.connect() as conn:
                store.transition_status(
                    conn,
                    run_id=run_id,
                    owner_id=self.owner_id,
                    fence_token=fence_token,
                    expected_from=(status,),
                    to_status=RunStatus.RUNNING,
                )
            run = {**run, "status": RunStatus.RUNNING}

        final_state, pending = self._stream(graph, run, fence_token, graph_input, config, beat)

        if pending is not None:
            self._pause(run, fence_token, pending, graph.get_state(config), config)
            return

        self._succeed(run, fence_token, final_state, beat)

    def _stream(
        self,
        graph: Any,
        run: dict[str, Any],
        fence_token: int,
        graph_input: Any,
        config: dict[str, Any],
        beat: lease.Heartbeat,
    ) -> tuple[dict[str, Any], Optional[Any]]:
        """Stream the graph, persisting progress and attempt telemetry as it goes.

        `stream_mode="tasks"` is what makes per-node attempt telemetry possible:
        it emits a start chunk (`input`, no `result` key) and an end chunk
        (`result`, `error`, `interrupts`) for every task, carrying the **stable
        checkpoint task id** that :func:`attempts.execution_key_for_node` uses as
        the execution key. Verified against LangGraph 1.2.7: that id is identical
        when the task re-runs after a crash or after a resumed interrupt.
        """
        final_state: dict[str, Any] = {}
        pending_interrupt: Optional[Any] = None
        open_attempts: dict[str, str] = {}  # task id -> attempt id

        for mode, chunk in graph.stream(
            graph_input, config, stream_mode=["custom", "values", "tasks"]
        ):
            if beat.fenced:
                raise FencedError(
                    f"run {run['run_id']} was fenced mid-stream at token {fence_token}"
                )

            if mode == "values":
                final_state = chunk
            elif mode == "custom":
                self._persist_progress(run, fence_token, chunk, open_attempts)
            elif mode == "tasks":
                pending_interrupt = self._persist_task(
                    run, fence_token, chunk, open_attempts
                ) or pending_interrupt

        return final_state, pending_interrupt

    def _persist_task(
        self,
        run: dict[str, Any],
        fence_token: int,
        chunk: dict[str, Any],
        open_attempts: dict[str, str],
    ) -> Optional[Any]:
        """Open an attempt on a task start; close it on a task end.

        A task that ends **with interrupts** is deliberately left `RUNNING`. It
        did not fail and it did not commit -- it paused, and the pause releases
        the lease. The next holder's reconcile closes it as `FENCED` under its own
        valid lease, which is the one place a superseded attempt is allowed to be
        marked at all.
        """
        task_id = chunk.get("id")
        node = chunk.get("name", "")
        if task_id is None or not node:
            return None

        started = "result" not in chunk and "error" not in chunk
        if started:
            with db.connect() as conn:
                open_attempts[task_id] = attempts.begin_attempt(
                    conn,
                    run_id=run["run_id"],
                    node=node,
                    execution_key=attempts.execution_key_for_node(str(task_id)),
                    fence_token=fence_token,
                    owner_id=self.owner_id,
                )
            return None

        attempt_id = open_attempts.pop(task_id, None)
        interrupts = chunk.get("interrupts") or []
        if interrupts:
            return interrupts[0]

        if attempt_id is None:
            return None
        with db.connect() as conn:
            if chunk.get("error") is not None:
                # Fenced, like every other attempt mutation. If this worker has
                # been superseded the call raises FN001 and the attempt is left
                # RUNNING -- which is correct: the new holder's reconcile will
                # close it as FENCED, and FENCED is the true fact about an
                # attempt whose authority was taken away mid-flight.
                attempts.fail_attempt(
                    conn,
                    run_id=run["run_id"],
                    owner_id=self.owner_id,
                    fence_token=fence_token,
                    attempt_id=attempt_id,
                    detail=f"node {node} raised",
                )
            else:
                attempts.commit_attempt(
                    conn,
                    run_id=run["run_id"],
                    owner_id=self.owner_id,
                    fence_token=fence_token,
                    attempt_id=attempt_id,
                )
        return None

    def _persist_progress(
        self,
        run: dict[str, Any],
        fence_token: int,
        chunk: dict[str, Any],
        open_attempts: dict[str, str],
    ) -> None:
        """Persist one custom-stream event as a worker event.

        Dedupe keys are **content-addressed** unless the emitter supplied one:
        ``progress:{phase}:{sha256(payload)}``. That is deterministic, so a node
        re-executed during a recovery re-emitting the same message produces the
        same key and the replay is a no-op -- two attempts, one event. The cost is
        that two genuinely distinct events with an identical payload in one run
        collapse into one; an emitter that needs them distinguished says so by
        putting a `dedupe_key` in the event, which is why the field exists.
        """
        event_type = WorkflowEventType(chunk.get("type", "progress"))
        phase = chunk.get("phase")
        payload = {k: v for k, v in chunk.items() if k != "dedupe_key"}
        payload.setdefault("run_id", run["run_id"])
        dedupe_key = chunk.get("dedupe_key") or (
            f"{event_type}:{phase}:{event_log.payload_digest(payload)[:32]}"
        )
        with db.connect() as conn:
            store.append_worker_event(
                conn,
                run_id=run["run_id"],
                owner_id=self.owner_id,
                fence_token=fence_token,
                event_type=event_type,
                phase=phase,
                payload=payload,
                dedupe_key=dedupe_key,
            )

    # --- the three ways a claim ends -------------------------------------

    def _pause(
        self,
        run: dict[str, Any],
        fence_token: int,
        pending: Any,
        snapshot: Any,
        config: dict[str, Any],
    ) -> None:
        """Park the run at a gate: one transaction, five effects, then stop.

        Ordering matters and is written out rather than assumed. LangGraph has
        already committed its checkpoint in **its own** transaction, which this
        work does not join. Then one transaction of ours -- persist the gate,
        append `review_required` and `paused`, transition out of RUNNING, release
        the lease -- through the single `pause_run_for_review` call, precisely so
        those cannot partially apply.
        """
        value = getattr(pending, "value", pending)
        gate = self.adapter.gate_of(value)
        checkpoint_id = str(
            snapshot.config.get("configurable", {}).get("checkpoint_id", "")
        )
        review = self.adapter.review_payload(run, gate, value)
        review.setdefault("type", str(WorkflowEventType.REVIEW_REQUIRED))
        review.setdefault("run_id", run["run_id"])
        review.setdefault("gate", str(gate))
        review.setdefault("checkpoint_id", checkpoint_id)

        pause_payload = {
            "type": str(WorkflowEventType.PAUSED),
            "run_id": run["run_id"],
            "gate": str(gate),
            "status": str(RunStatus.WAITING_FOR_REVIEW),
        }
        with db.connect() as conn:
            store.pause_run_for_review(
                conn,
                run_id=run["run_id"],
                owner_id=self.owner_id,
                fence_token=fence_token,
                gate=gate,
                review_payload=review,
                pause_payload=pause_payload,
                checkpoint_id=checkpoint_id,
            )
        logger.info("run %s paused at workflow gate %s", run["run_id"], gate)

    def _succeed(
        self,
        run: dict[str, Any],
        fence_token: int,
        final_state: dict[str, Any],
        beat: lease.Heartbeat,
    ) -> None:
        """Write the single terminal `result` and move to COMPLETED, atomically,
        then release the lease."""
        if beat.fenced:
            raise FencedError(f"run {run['run_id']} was fenced before finalization")
        payload = {
            "type": str(WorkflowEventType.RESULT),
            "run_id": run["run_id"],
            "data": self.adapter.result_payload(run, final_state),
        }
        with db.connect() as conn:
            store.finalize_run_success(
                conn,
                run_id=run["run_id"],
                owner_id=self.owner_id,
                fence_token=fence_token,
                payload=payload,
            )
            lease.release(
                conn,
                run_id=run["run_id"],
                owner_id=self.owner_id,
                fence_token=fence_token,
            )
        logger.info("run %s completed", run["run_id"])

    def _fail(
        self,
        run_id: str,
        fence_token: int,
        error: Exception,
        beat: lease.Heartbeat,
    ) -> None:
        """Write the single terminal `error` and move to FAILED, atomically.

        The detail is `type: message` and nothing else. A traceback, a provider
        body or a URL carrying a key must never reach here -- SerpAPI
        authenticates by query parameter and `requests`' HTTPError embeds the full
        URL, which has already leaked a live key into a browser console once.
        """
        if beat.fenced:
            raise FencedError(f"run {run_id} was fenced before its failure was recorded")
        code = getattr(error, "code", None) or type(error).__name__
        payload = {
            "type": str(WorkflowEventType.ERROR),
            "run_id": run_id,
            "code": code,
            "detail": _safe_detail(error),
        }
        with db.connect() as conn:
            store.finalize_run_failure(
                conn,
                run_id=run_id,
                owner_id=self.owner_id,
                fence_token=fence_token,
                payload=payload,
                failure_code=code,
            )
            lease.release(
                conn, run_id=run_id, owner_id=self.owner_id, fence_token=fence_token
            )
        logger.warning("run %s failed: %s", run_id, code)


def _safe_detail(error: Exception, *, step: Optional[str] = None) -> str:
    """One sanitized sentence, safe to persist and to serve to a browser.

    Delegates to `workflow/redaction.py`, which treats the exception text as
    hostile. An earlier version returned the first line unchanged, and that was a
    live credential leak: an ordinary quota failure carries the request URL, and
    SerpAPI authenticates by query parameter, so

        429 Client Error for url: https://serpapi.com/search?api_key=SECRET&q=x

    went straight into the terminal `error` event and into
    `verification_runs.failure_detail`, both of which the API reads back and
    renders. CLAUDE.md records that this exact shape reached a devtools console
    once already, through the SSE path.

    `WorkflowError` and its subclasses are **trusted**: their messages are built
    by this codebase and by the SQL functions, which interpolate run ids,
    statuses and tokens and nothing else. Everything else is untrusted and is
    reduced to allow-listed facts -- the provider, the HTTP status, the step --
    because redaction is pattern-matching and patterns are never complete.
    """
    return redaction.safe_detail(
        error, step=step, trusted=isinstance(error, WorkflowError)
    )


def _has_checkpoint(graph: Any, config: dict[str, Any]) -> bool:
    """Whether LangGraph has ever committed a checkpoint for this thread.

    An unstarted thread's snapshot carries no `checkpoint_id` at all -- that is
    the discriminator, rather than an empty `values`, which a graph whose first
    node returns nothing would also produce.
    """
    snapshot = graph.get_state(config)
    return bool(snapshot.config.get("configurable", {}).get("checkpoint_id"))


def _pending_interrupt(snapshot: Any) -> Optional[Any]:
    """The interrupt sitting in a committed checkpoint, if any."""
    for task in getattr(snapshot, "tasks", ()) or ():
        for item in getattr(task, "interrupts", ()) or ():
            return item
    return None


def _latest_event(
    conn: Any, *, run_id: str, event_type: WorkflowEventType
) -> Optional[dict[str, Any]]:
    """The newest persisted event of one type, used to recover the human decision
    an approval transaction committed."""
    for event in reversed(event_log.read_events(conn, run_id=run_id, limit=10_000)):
        if event["event_type"] == str(event_type):
            return event
    return None


# --- the mock adapter ------------------------------------------------------
#
# A deterministic graph with one interrupt, driving the same state machine with
# **no outbound HTTP at all**. It is what `python -m workflow.worker --mock` runs
# and what `run_mock.py` will drive: the executor, the lease, the event log, the
# transitions and the checkpointer are all real; only the work is fabricated, and
# it is labelled as fabricated in the payload it produces.


MOCK_SOURCE_LABEL = "MOCK DATA - no live search"
"""Stamped into every mock result, the same marker `run_mock.py` uses, so a mock
run is identifiable in the database long after the process that made it is gone."""


class MockState(TypedDict, total=False):
    """The mock graph's state schema.

    A `TypedDict` rather than a bare `dict`, and that is not cosmetic: with an
    untyped `dict` schema LangGraph **replaces** the state with each node's return
    value instead of merging per channel, so a key one node wrote vanishes the
    moment the next node returns without it. The real `graph/state.py` is a
    `TypedDict` for the same reason; the double has to share the property it is
    standing in for.
    """

    description: str
    steps: list[str]
    approved: str


MOCK_STEP_SECONDS = float(os.environ.get("WORKFLOW_MOCK_STEP_SECONDS", "0"))
"""How long each mock node takes. Zero by default, so tests stay fast.

It exists because a graph that finishes in 250ms cannot be used to demonstrate
the one thing the durable executor is *for*: a worker killed **mid-node**, its
lease expiring, and a replacement continuing from the committed checkpoint. With
no pacing the run reaches its gate before an operator can kill anything, and the
demonstration silently becomes "restart a finished run", which proves nothing.

The same reasoning as `run_mock.py`'s pacing delays and its `--fast` flag: a
deterministic double sometimes has to take realistic *time* to stand in for the
thing it replaces. It changes no behaviour and no output -- only duration."""


def _mock_step(name: str, phase: str, message: str) -> Callable[[MockState], dict]:
    def node(state: MockState) -> dict:
        from langgraph.config import get_stream_writer

        get_stream_writer()({"type": "progress", "phase": phase, "message": message})
        if MOCK_STEP_SECONDS:
            # Slept in slices so a SIGKILL lands inside the node rather than
            # between two of them.
            deadline = time.monotonic() + MOCK_STEP_SECONDS
            while time.monotonic() < deadline:
                time.sleep(0.25)
        return {"steps": [*(state.get("steps") or []), name]}

    return node


def _mock_gate(state: MockState) -> dict:
    decision = interrupt({"gate": "A", "question": "Approve the mock decomposition?"})
    return {"approved": decision, "steps": [*(state.get("steps") or []), "gate_a"]}


def build_mock_graph(checkpointer: Any) -> Any:
    """A trivial three-node graph with one `interrupt()`.

    Enough to exercise every transition, the lease, the reconcile path, the
    attempt telemetry and the event log, and deliberately independent of the
    verification graph so this gate does not depend on later ones.
    """
    builder = StateGraph(MockState)
    builder.add_node("understand", _mock_step("understand", "understand", "Reading the invention."))
    builder.add_node("gate_a", _mock_gate)
    builder.add_node("analyze", _mock_step("analyze", "analyze", "Writing the analysis."))
    builder.add_edge(START, "understand")
    builder.add_edge("understand", "gate_a")
    builder.add_edge("gate_a", "analyze")
    builder.add_edge("analyze", END)
    return builder.compile(checkpointer=checkpointer)


MOCK_ADAPTER = GraphAdapter(
    build=build_mock_graph,
    initial_input=lambda run: {"steps": [], "description": run["description"]},
    result_payload=lambda run, state: {
        "title": run["description"][:45] + "...",
        "date": "Today",
        "source": MOCK_SOURCE_LABEL,
        "steps": state.get("steps") or [],
        "approved": state.get("approved"),
    },
    gate_of=lambda value: WorkflowGate((value or {}).get("gate", "A")),
    review_payload=lambda run, gate, value: {
        "summary": {"question": (value or {}).get("question", "")},
        "payload_url": f"/api/runs/{run['run_id']}/gates/{gate}",
    },
    resume_value=lambda run, resumed: (
        (resumed or {}).get("payload", {}).get("decision", "approved")
    ),
)
"""The deterministic adapter. `MockExecutor` is `Executor` bound to it -- one
executor, one state machine, no second implementation to drift."""


def MockExecutor(**kwargs: Any) -> Executor:  # noqa: N802 - reads as a class
    """An `Executor` driving :data:`MOCK_ADAPTER`.

    A function rather than a subclass on purpose: there is exactly one executor
    implementation, and a subclass would be somewhere for a second one to grow.
    """
    kwargs.setdefault("adapter", MOCK_ADAPTER)
    return Executor(**kwargs)


INPROCESS_THREAD_NAME = "workflow-executor"
"""The daemon thread's name. Used to detect an already-running loop, so a
second startup cannot produce two executors inside one process."""


class InprocessExecutor:
    """The executor hosted on a daemon thread inside the API process.

    **For local development.** It is the *same* loop `python -m workflow.worker`
    runs -- same class, same lease, same reconcile, same recovery after a restart
    -- hosted differently. There is one executor implementation; this only chooses
    where it runs, which is why it is a thin wrapper around `Executor` and not a
    second implementation to drift.

    Production runs `worker`, a separate workload. A thread inside the API shares
    the API's fate: a rolling update, an OOM kill, or a crash in an unrelated
    request handler takes the executor with it. The lease makes that *recoverable*
    -- another holder reclaims the run -- but it does not make it a good place to
    run work that takes minutes.

    Not `BackgroundTasks`, which dies with the request, has no lease, no recovery
    and no visibility.
    """

    def __init__(self, *, executor: Executor) -> None:
        self.executor = executor
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> threading.Thread:
        """Start the loop, refusing to start a second one.

        Two executors in one process would both poll, both try to acquire, and --
        because the lease is correct -- one would simply lose every race. Harmless
        but pure waste, and a symptom of a lifecycle bug worth failing on rather
        than absorbing.
        """
        if self.running:
            raise ExecutorUnavailable(
                "an in-process executor is already running in this process"
            )
        for thread in threading.enumerate():
            if thread.name == INPROCESS_THREAD_NAME and thread.is_alive():
                raise ExecutorUnavailable(
                    f"a thread named {INPROCESS_THREAD_NAME!r} is already running; "
                    "refusing to start a second in-process executor"
                )
        self._stop.clear()
        self._thread = threading.Thread(
            target=self.executor.run_forever,
            args=(self._stop,),
            name=INPROCESS_THREAD_NAME,
            daemon=True,
        )
        self._thread.start()
        logger.info("in-process executor %s started", self.executor.owner_id)
        return self._thread

    def stop(self, timeout: float = 30.0) -> None:
        """Signal the loop and wait for it to finish its current claim.

        The stop event is checked between claims, not inside one, so a run being
        executed is allowed to reach its next commit point. A claim still in
        flight when the timeout expires keeps its lease until it expires and is
        then reclaimed -- the designed recovery path, not an error.
        """
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
            if thread.is_alive():
                logger.warning(
                    "in-process executor did not stop within %ss; its lease will "
                    "expire and the run will be reclaimed",
                    timeout,
                )
        self._thread = None


def run_inprocess(
    stop: Optional[threading.Event] = None,
    *,
    adapter: Optional[GraphAdapter] = None,
) -> InprocessExecutor:
    """Build and start an :class:`InprocessExecutor`.

    The adapter comes from `WORKFLOW_GRAPH_ADAPTER` unless one is passed
    explicitly. **There is no mock fallback here**: an API configured for
    `inprocess` with no adapter fails startup, exactly as the worker does. Quietly
    substituting the deterministic adapter would let a deployment believe it was
    running real verification while it fabricated everything.
    """
    executor = Executor(adapter=adapter or load_adapter(settings.workflow_graph_adapter))
    host = InprocessExecutor(executor=executor)
    if stop is not None:
        # A caller-supplied event drives the same loop, for tests that want to
        # own the lifecycle.
        host._stop = stop
    host.start()
    return host
