"""The durable-workflow vocabulary: run states, gates, event types, attempt
outcomes, the transition table, and the exception hierarchy the SQL layer's
custom SQLSTATEs map onto.

Nothing here talks to a database. It is the shared vocabulary `store.py`,
`events.py`, `lease.py`, `attempts.py` and `executor.py` all import, kept in one
place so the enum values, the transition table and the error codes cannot drift
between them.

**The transition table below is a mirror, not the authority.** The authority is
`run_status_transitions` in migration 0006 and the trigger that reads it: a
transition rejected there is rejected no matter what this module says. This copy
exists so Python code can reason about a transition before issuing it, and
`tests/test_run_status_machine.py` asserts the two agree row for row.
"""

from enum import StrEnum


class RunStatus(StrEnum):
    """The six run states. `COMPLETED` and `FAILED` are terminal, with no
    outgoing transitions at all."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_FOR_REVIEW = "WAITING_FOR_REVIEW"
    RESUMING = "RESUMING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


TERMINAL_STATUSES: frozenset[RunStatus] = frozenset(
    {RunStatus.COMPLETED, RunStatus.FAILED}
)

CLAIMABLE_STATUSES: tuple[RunStatus, ...] = (
    RunStatus.QUEUED,
    RunStatus.RUNNING,
    RunStatus.RESUMING,
)
"""The only statuses `acquire_run_lease` will lease. A terminal run has nothing
left to execute, and a `WAITING_FOR_REVIEW` run is parked on a human -- leasing
it would let a worker resume work nobody approved."""


class WorkflowGate(StrEnum):
    """A **human review pause** inside a run. Never abbreviated to bare `gate` in
    code: this is not an Implementation Gate (a stage of the build) and not an
    executor concept.

    `STRATEGY` is spelled out where the others are letters, and that is
    deliberate rather than untidy. The four letters are historical positions in
    the specification; a value added afterwards would have to be `E` and would
    then sit in the enum naming a stage that runs *second*, between `A` and `B`.
    A label that says what the pause is about cannot be misread that way, and the
    letters never reach a screen anyway -- `gate-shell.ts` maps every one of them
    to a stage name a person sees.

    Members are listed in the order a run reaches them.
    """

    A = "A"
    STRATEGY = "STRATEGY"
    B = "B"
    C = "C"
    D = "D"


class WorkflowEventType(StrEnum):
    """The closed persisted-event vocabulary.

    `snapshot` is deliberately absent: it is connection-local, synthesised by the
    stream reader for a client attaching with no `last_event_id`, and never
    written to `workflow_events`.
    """

    PROGRESS = "progress"
    VERIFICATION_PROGRESS = "verification_progress"
    REVIEW_REQUIRED = "review_required"
    PAUSED = "paused"
    RESUMED = "resumed"
    RESULT = "result"
    ERROR = "error"


TERMINAL_EVENT_TYPES: frozenset[WorkflowEventType] = frozenset(
    {WorkflowEventType.RESULT, WorkflowEventType.ERROR}
)
"""`is_terminal` is a *function* of the event type, not an independent flag, and
the database says so with a CHECK constraint. This is the Python side of that
same rule."""


class AttemptOutcome(StrEnum):
    """One PHYSICAL execution attempt's outcome.

    `COMMITTED` is the only one that may be rendered as a result. A `FENCED`
    attempt is recorded so an operator can see that a provider was queried twice
    during a recovery -- two attempts, one committed result -- and is never
    counted in coverage and never cited as evidence.
    """

    RUNNING = "RUNNING"
    COMMITTED = "COMMITTED"
    FENCED = "FENCED"
    FAILED = "FAILED"


ALLOWED_TRANSITIONS: frozenset[tuple[RunStatus, RunStatus]] = frozenset(
    {
        (RunStatus.QUEUED, RunStatus.RUNNING),
        # QUEUED -> FAILED is for failures discovered AFTER admission only: an
        # executor that cannot start, or an adapter revision that changed between
        # admission and execution. Policy validation never reaches this edge -- it
        # refuses the request before a run row exists at all.
        (RunStatus.QUEUED, RunStatus.FAILED),
        (RunStatus.RUNNING, RunStatus.WAITING_FOR_REVIEW),
        (RunStatus.RUNNING, RunStatus.COMPLETED),
        (RunStatus.RUNNING, RunStatus.FAILED),
        (RunStatus.WAITING_FOR_REVIEW, RunStatus.RESUMING),
        (RunStatus.WAITING_FOR_REVIEW, RunStatus.FAILED),
        (RunStatus.RESUMING, RunStatus.RUNNING),
        (RunStatus.RESUMING, RunStatus.FAILED),
    }
)
"""UPDATES ONLY. Creation is not a transition: a run is born `QUEUED` by insert
invariant (migration 0006's trigger), and lease takeover appears nowhere here
because it does not change status -- status tracks workflow position, the lease
tracks who may advance it."""


# --- Errors ---------------------------------------------------------------
#
# The SQL layer raises custom SQLSTATEs so callers map failures to HTTP codes
# instead of parsing messages. `raise_for_sqlstate` below is the one translation
# point; nothing else in the package inspects a psycopg exception.


class WorkflowError(RuntimeError):
    """Base for every durable-workflow failure.

    Carries a stable `code` a future HTTP layer maps to a status; the message is
    already sanitized by the SQL functions, which never interpolate a payload, a
    credential or a provider body.
    """

    code = "WORKFLOW_ERROR"


class FencedError(WorkflowError):
    """FN001 -- no live lease for this owner and token.

    The worker has been fenced: it stops scheduling work immediately and writes
    nothing further. Not telemetry, not a pause, not a terminal.
    """

    code = "CHECKPOINT_UNAVAILABLE"


class TerminalExistsError(WorkflowError):
    """FN002 -- a terminal event already exists. Nothing follows a terminal."""

    code = "RUN_NOT_RESUMABLE"


class ReplayConflictError(WorkflowError):
    """FN003 -- a replay whose payload, type, key or failure code conflicts with
    what is already stored. Returning the stored row would answer a different
    question than the caller asked."""

    code = "IDEMPOTENCY_CONFLICT"


class RunNotFoundError(WorkflowError):
    """FN004 -- unknown run, or a run the caller does not own.

    The two are deliberately indistinguishable: 403 would confirm the run exists
    and let an attacker enumerate ids.
    """

    code = "RUN_NOT_FOUND"


class IllegalStateError(WorkflowError):
    """FN005 -- the run's status is incompatible with this operation."""

    code = "WRONG_GATE"


class StaleResultVersionError(WorkflowError):
    """A human decision was composed against a result that has since been superseded.

    Spec §10.2 maps this to `409 STALE_RESULT_VERSION`, and §13.4 check 16 is the
    same rule for a review overlay: **a reviewer approved specific evidence, not a
    conclusion in perpetuity.** Attaching their decision to a later result would
    credit them with having seen something they never saw.

    Symmetric in both directions -- a decision made against version 1 cannot be
    replayed onto version 2, and one made against version 2 cannot be applied
    backwards to version 1.
    """

    code = "STALE_RESULT_VERSION"


class LeaseLifecycleError(WorkflowError):
    """FN006 -- a lease invariant was broken: a run with no lease row, or a fence
    token that would go backwards.

    Not a caller mistake. It means the invariant broke, which is why it maps to a
    500 rather than to a 4xx.
    """

    code = "LEASE_INVARIANT_BROKEN"


class InvalidLeaseConfiguration(WorkflowError):
    """A non-positive TTL or heartbeat, or a heartbeat at or above the TTL.

    A heartbeat interval at or above the TTL guarantees the lease lapses between
    beats and the worker fences itself mid-run. An invalid value stops the worker
    from starting; it is never silently replaced with a default, because a default
    that quietly overrides a deliberate setting is how a misconfiguration survives
    a deploy unnoticed.
    """

    code = "INVALID_LEASE_CONFIGURATION"


class CheckpointUnavailable(WorkflowError):
    """The checkpointer, the lease store or the event log is unreachable.

    There is no in-memory continuation. The worker stops, the run keeps its last
    committed state, and a later reconcile records the sanitized infrastructure
    failure when there is finally somewhere to write it.
    """

    code = "CHECKPOINT_UNAVAILABLE"


class UnapprovedArtifactError(WorkflowError):
    """`verify_features` was reached without an approved artifact.

    Raised when the approved decomposition or the approved candidate set is
    **absent, unapproved, or version-mismatched** against the cells being built.
    It is not recoverable and there is no flag that relaxes it: the verifier
    consumes a *human-approved* feature, and a machine-produced candidate is not
    one no matter how good it looks.

    This exists as a distinct error, rather than a `ValueError`, because it is the
    boundary the whole workflow is built around. A run that raises this has failed
    safe -- it spent no LLM quota and produced no cell asserting something about a
    patent that nobody approved a question for.
    """

    code = "ARTIFACT_NOT_APPROVED"


class ExecutorUnavailable(WorkflowError):
    """The executor cannot start -- no graph adapter is configured for it.

    Distinct from `CheckpointUnavailable`: the database is fine, the executor is
    not deployable as configured. It refuses to start rather than admitting runs
    it cannot advance.
    """

    code = "EXECUTOR_UNAVAILABLE"


_SQLSTATE_ERRORS: dict[str, type[WorkflowError]] = {
    "FN001": FencedError,
    "FN002": TerminalExistsError,
    "FN003": ReplayConflictError,
    "FN004": RunNotFoundError,
    "FN005": IllegalStateError,
    "FN006": LeaseLifecycleError,
}


def raise_for_sqlstate(error: Exception) -> None:
    """Re-raise a psycopg error as its `WorkflowError` when it carries one of the
    package's custom SQLSTATEs, and otherwise do nothing so the caller's own
    ``raise`` re-raises the original.

    Only the SQL function's own message travels -- those messages interpolate run
    ids, statuses and tokens and never a payload, a credential or a provider
    body, so they are safe to surface. A psycopg error from anywhere else is left
    alone rather than being flattened into a workflow code it does not mean.
    """
    sqlstate = getattr(error, "sqlstate", None)
    workflow_error = _SQLSTATE_ERRORS.get(sqlstate or "")
    if workflow_error is None:
        return
    raise workflow_error(_first_line(str(error))) from error


def _first_line(message: str) -> str:
    """psycopg appends CONTEXT (the plpgsql call stack) to a raised message. That
    is a stack trace by another name, so only the RAISE line itself is kept."""
    return message.strip().splitlines()[0] if message.strip() else message
