"""Verified-run SSE: replay what a client missed, then tail what happens next.

Spec section 9. This module is the reader side of `workflow_events`; nothing here
writes an event, and nothing here can advance a run.

## The property the whole design exists for

**A client that disconnects for an hour reconnects and misses nothing.** Every
persisted event carries a gapless-within-commit `sequence`, the stream sends it as
the SSE `id:` line, and a reconnecting client hands the highest id it saw back.
Replay is then `sequence > last_event_id`, in order, followed by a live tail.

**The browser will not do that for you.** `services/analysis.ts` uses raw `fetch`
rather than `EventSource`, so there is no automatic `Last-Event-ID` header and no
automatic reconnection (spec section 2.2). The id is therefore accepted from the
header **or** a `?last_event_id=` query parameter, header winning, and the client
tracks it itself.

## Correctness is polling, not notification

The API process serving a stream is usually **not** the worker executing the run
(spec section 9.4). There is no in-process pub/sub, no shared memory and no sticky
session: the reader selects `sequence > last` every `WORKFLOW_EVENT_POLL_MS` and
any API process can serve any run's stream. `LISTEN`/`NOTIFY` may be added later
to cut latency, and it would still be best-effort -- the poll stays the mechanism.

## A client is never allowed to matter to the run

- **Disconnecting does nothing.** No cancellation, no status change, no event. The
  worker has never heard of the connection.
- **A slow client is disconnected, not buffered.** Nothing is lost by that: every
  frame is still in `workflow_events`, and the client reconnects with its last id
  and catches up.
- The `paused` event ends the *connection*, not the run. The run stays
  `WAITING_FOR_REVIEW` with no lease held, and the client reconnects when the user
  submits their review.

## Terminal semantics

Exactly one terminal event (`result` or `error`) is ever persisted, by the
finalizers alone, in the same transaction as the `COMPLETED` / `FAILED`
transition. This module's job is only to deliver it correctly:

- reconnecting with `last_event_id` **at or above** the terminal sequence replays
  nothing and closes immediately;
- reconnecting **below** it delivers that single terminal once -- the client had
  not seen it, and withholding it would strand the UI;
- at most one terminal frame per connection, always.
"""

import json
from typing import Any, Iterator, Optional

import db
from config import settings
from workflow import events as event_log
from workflow import store
from workflow.types import RunStatus

TERMINAL_TYPES = frozenset({"result", "error"})
"""Ends the run. Persisted only by `finalize_run_success` / `finalize_run_failure`."""

CONNECTION_TERMINAL_TYPES = frozenset({"paused"})
"""Ends the **connection** and not the run.

`paused` means the run is parked on a person, with no lease held and nothing to
tail until they act. Holding the connection open would keep an API worker and a
database poll busy for however long the human takes -- minutes to days -- to
deliver nothing. The client reconnects with its last id when it submits.

**Only while the run is still parked**, though. A `paused` event that the run has
already moved past is history, and stopping on it would make replaying a finished
run cost one reconnect per historical pause -- four for a four-gate run, and the
client would never reach the terminal on its first attempt."""


class StreamBackpressure(RuntimeError):
    """The client could not keep up and its connection is being closed.

    Not an error about the run, which is unaffected and unaware. Every frame the
    client missed is still in `workflow_events`; it reconnects with its last id
    and catches up.
    """


def frame(sequence: int, payload: dict[str, Any]) -> str:
    """One SSE frame: an `id:` line, a `data:` line, and the blank line.

    The `id:` is the persisted sequence, which is what makes reconnection
    correct. `snapshot` is the one frame carrying an id it did not allocate --
    it borrows the current maximum so the client's *next* reconnect is right.
    """
    return f"id: {sequence}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"


def _event_payload(run_id: str, event: dict[str, Any]) -> dict[str, Any]:
    """The `data` object for a persisted event.

    Every event carries `run_id` (spec section 9.8). Nothing carries a lease
    token, an owner id, a checkpoint blob or any other executor internal -- the
    stored payload is built by the authorized writers and never contains one, and
    this function adds only the type and the run.
    """
    payload = dict(event.get("payload") or {})
    payload["type"] = event["event_type"]
    payload["run_id"] = run_id
    return payload


def snapshot_payload(conn, *, run_id: str, actor_email: str) -> tuple[int, dict[str, Any]]:
    """The `snapshot` frame for a client attaching with no id, and its sequence.

    **Not persisted**, and deliberately not a member of the `workflow_event_type`
    enum: it is connection-local, describing the run's current state rather than
    something that happened. Its `id:` is the current maximum sequence, so the
    client's next reconnect asks for everything after it.

    Also the correct answer for a client whose `last_event_id` is below
    `events_compacted_below`: the replay it is asking for no longer exists in
    full, and a partial replay presented as a complete one would silently drop
    events. That is why the watermark is a column rather than an implicit
    consequence of deletion.
    """
    run = store.get_run(conn, run_id=run_id, actor_email=actor_email)
    last = event_log.max_sequence(conn, run_id=run_id)
    return last, {
        "type": "snapshot",
        "run_id": run_id,
        "status": str(run["status"]),
        "current_gate": None if run["current_gate"] is None else str(run["current_gate"]),
        "last_event_id": last,
        "events_compacted_below": run["events_compacted_below"],
    }


def _is_terminal_status(status: Any) -> bool:
    return RunStatus(str(status)) in (RunStatus.COMPLETED, RunStatus.FAILED)


def replay(
    conn, *, run_id: str, actor_email: str, after_sequence: int, limit: int = 500
) -> list[dict[str, Any]]:
    """Persisted events after `after_sequence`, oldest first, ownership-scoped.

    A thin pass-through kept as its own function because replay and tail are the
    same query with different callers, and having one of them drift into
    `ORDER BY` something else would deliver a client's backlog out of order.
    """
    return store.list_events(
        conn,
        run_id=run_id,
        actor_email=actor_email,
        after_sequence=after_sequence,
        limit=limit,
    )


def stream_frames(
    *,
    run_id: str,
    actor_email: str,
    last_event_id: Optional[int],
    poll_interval: Optional[float] = None,
    max_frames: Optional[int] = None,
    sleeper=None,
    monotonic=None,
) -> Iterator[str]:
    """Replay then tail, as SSE frames, until a terminal or the client leaves.

    Args:
        last_event_id: the highest id the client has already seen. `None` means a
            fresh attachment, which gets a `snapshot` first.
        poll_interval: seconds between polls. Defaults to
            `WORKFLOW_EVENT_POLL_MS`.
        max_frames: stop after this many frames. Exists so a test can bound a
            tail without wall-clock sleeping; production leaves it `None`.
        sleeper: injected `sleep`, so no test ever waits on a real clock.
        monotonic: injected clock, for the same reason -- the backpressure bound
            is measured with it and a test must be able to move it.

    **Ownership is checked before a single frame is produced**, by
    `snapshot_payload` / `replay`, both of which resolve the run with
    `actor_email` in the same query that fetches it. A run the caller does not own
    raises `RunNotFoundError` here, and the endpoint turns that into `404` -- the
    same answer an unknown run gets, so ids cannot be enumerated.
    """
    import time

    sleep = sleeper or time.sleep
    clock = monotonic or time.monotonic
    write_timeout = max(float(settings.sse_write_timeout_seconds), 0.0)
    interval = (
        poll_interval
        if poll_interval is not None
        else max(settings.workflow_event_poll_ms, 1) / 1000.0
    )
    buffer_limit = max(int(settings.sse_client_buffer), 1)

    sent = 0
    with db.connect() as conn:
        if last_event_id is None:
            cursor, payload = snapshot_payload(conn, run_id=run_id, actor_email=actor_email)
            yield frame(cursor, payload)
            sent += 1
        else:
            run = store.get_run(conn, run_id=run_id, actor_email=actor_email)
            if last_event_id < run["events_compacted_below"]:
                # The backlog it is asking for has been compacted away. A partial
                # replay would look complete and silently drop events, so it gets
                # the current state instead and resumes from there.
                cursor, payload = snapshot_payload(
                    conn, run_id=run_id, actor_email=actor_email
                )
                yield frame(cursor, payload)
                sent += 1
            else:
                cursor = last_event_id

    while True:
        with db.connect() as conn:
            batch = replay(
                conn,
                run_id=run_id,
                actor_email=actor_email,
                after_sequence=cursor,
                limit=buffer_limit,
            )
            run = store.get_run(conn, run_id=run_id, actor_email=actor_email)

        for event in batch:
            cursor = int(event["sequence"])

            # **Backpressure, measured around the yield.** A generator resumes
            # only when the consumer asks for the next frame, so the wall clock
            # spent inside `yield` IS the time the client took to accept the
            # previous one. A reader that has stopped accepting shows up here as
            # a gap longer than `SSE_WRITE_TIMEOUT_SECONDS`.
            #
            # The connection is then closed rather than buffered, and **nothing
            # is lost by that**: every frame is still in `workflow_events`, and
            # the client reconnects with its last id and catches up. Buffering
            # without a bound is how one stalled browser tab turns into an API
            # process holding an unbounded queue.
            #
            # The run is untouched. It has never heard of this connection.
            handed_over = clock()
            yield frame(cursor, _event_payload(run_id, event))
            if write_timeout and (clock() - handed_over) > write_timeout:
                raise StreamBackpressure(
                    f"a client took longer than {write_timeout:g}s to accept a "
                    f"frame for run {run_id}; closing the connection. Nothing is "
                    f"lost -- every frame is still in workflow_events and the "
                    f"client can reconnect with its last id."
                )
            sent += 1

            if event["event_type"] in TERMINAL_TYPES:
                # At most one terminal frame per connection, always.
                return
            if event["event_type"] in CONNECTION_TERMINAL_TYPES:
                # **Only while the run is STILL parked.** The rule exists so a
                # connection is not held open across a human review that may take
                # days -- and if the run has already moved past this pause, there
                # is no wait to avoid and the rest of the log is sitting right
                # there.
                #
                # Without this check, replaying a finished run's history costs one
                # reconnect per historical pause: a four-gate run would need four
                # connections to read what happened, and a client catching up
                # would never reach the terminal on its first attempt.
                if RunStatus(str(run["status"])) is RunStatus.WAITING_FOR_REVIEW:
                    return
            if max_frames is not None and sent >= max_frames:
                return

        if max_frames is not None and sent >= max_frames:
            return

        if not batch:
            # Nothing new. If the run has already ended and its terminal is at or
            # below the cursor, the client is fully caught up: close rather than
            # poll a finished run forever.
            if _is_terminal_status(run["status"]):
                return
            if _is_terminal_status(run["status"]) is False and run["status"] == RunStatus.WAITING_FOR_REVIEW:
                # Parked on a person with no lease held. Nothing will arrive until
                # they act, and the `paused` frame that said so has already been
                # delivered on this or an earlier connection.
                return
            sleep(interval)


__all__ = [
    "CONNECTION_TERMINAL_TYPES",
    "StreamBackpressure",
    "TERMINAL_TYPES",
    "frame",
    "replay",
    "snapshot_payload",
    "stream_frames",
]
