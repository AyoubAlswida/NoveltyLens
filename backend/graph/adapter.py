"""The production :class:`~workflow.executor.GraphAdapter` for verified runs.

**This is the seam between the durable executor and the graph, and it is data
rather than a subclass.** `workflow/executor.py` contains no graph-specific logic
at all: it knows how to claim a lease, reconcile, execute, pause and finalize, and
it asks this object what to build, what to feed it, what a pause means and what a
result looks like. That separation is what lets the executor's own tests drive a
deterministic two-node graph while production drives the real one, without either
knowing the other exists.

## What this module deliberately does NOT do

- **It does not decide anything.** `gate_of` reads which gate an `interrupt()`
  payload names; `resume_value` builds a *signal*, never a decision. The gate
  nodes re-read the human decision from the database through the approved-only
  readers in `workflow/approvals.py`, which filter `is_approved` in SQL. If the
  resume value were the decision, forging it would be approving.
- **It writes nothing.** No `approved_*` field, no event, no status. Only
  `workflow/approvals.py` writes an approval, and only the four authorized event
  writers append an event.
- **It never enables the guided branch by accident.** `initial_input` sets
  `advanced_search` from the run row's own existence: every row in
  `verification_runs` *is* an Advanced Search run. A search-only run has no such
  row and never reaches this adapter at all.

## Why `build` compiles per claimed run

A compiled LangGraph is bound to the checkpointer it was compiled with, and the
executor builds one inside the `checkpointer()` context manager that owns the
psycopg pool. Caching a compiled graph across runs would outlive the pool it
points at, so `build` compiles and does not cache. The *topology* is shared with
the inline search path through `graph.pipeline._assemble`, so the two cannot drift
apart.
"""

from typing import Any, Optional

from graph.pipeline import build_graph_with_checkpointer, build_result_event
from graph.state import GraphState
from reports import save_report
from workflow.executor import GraphAdapter
from workflow.types import WorkflowGate


def build(checkpointer: Any) -> Any:
    """The real novelty-search graph, compiled against the Postgres checkpointer.

    `interrupt()` needs a checkpointer to write the pause into, so a verified run
    cannot execute without one -- which is exactly right: a pause nobody can
    resume is worse than no pause.
    """
    return build_graph_with_checkpointer(checkpointer)


def initial_input(run: dict[str, Any]) -> dict[str, Any]:
    """The graph input for a `QUEUED` verified run.

    Every field comes from the run row, which is the record of what the owner
    asked for and the only thing that survives a restart. Nothing is defaulted
    here: the date and text-version policies have no defaults anywhere (spec
    section 3.6), they were validated at admission, and re-supplying a default at
    this point would let a run execute under a policy nobody recorded.
    """
    return {
        "description": run["description"],
        "claims": run.get("claims") or "",
        # A row in `verification_runs` IS an Advanced Search run. The flag is
        # derived from that fact rather than passed in beside it, so the two
        # cannot disagree.
        "advanced_search": True,
        "run_id": run["run_id"],
        # The owner, so worker-side reads stay ownership-scoped rather than
        # running with an empty actor.
        "created_by": run.get("created_by") or "",
        # **Normalized to an ISO string here, at the seam.** `verification_runs.
        # cutoff_date` is a SQL `DATE`, so psycopg hands back a `datetime.date`,
        # while `GraphState.cutoff_date` is typed `str | None` and
        # `evidence/dates.py::parse_patent_date` calls `.strip()` on it. Passing
        # the row value straight through raised `AttributeError: 'datetime.date'
        # object has no attribute 'strip'` in `screen_dates`, which failed the
        # run between Gate A and Gate B.
        #
        # Converting here rather than teaching the parser to accept both: the
        # parser's job is to read what a *provider* returned, and widening it
        # would make it accept a shape no provider ever sends.
        # Optional Stage 1 context. Coerced to "" for graph state, whose keys are
        # typed `str` -- the None/"" distinction that matters for a report is
        # preserved in the run row, which is where a report reads it from.
        "problem_solved": run.get("problem_solved") or "",
        "inventor_novelty_statement": run.get("inventor_novelty_statement") or "",
        "cutoff_date": _iso_date(run.get("cutoff_date")),
        "date_policy": run["date_policy"],
        "text_version_policy": run["text_version_policy"],
    }


def _iso_date(value: Any) -> Optional[str]:
    """A `date` (or anything) as the ISO string graph state expects, or `None`."""
    if value is None or value == "":
        return None
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value)


def result_payload(run: dict[str, Any], final_state: GraphState) -> dict[str, Any]:
    """The terminal `result` event's `data` object.

    Built through `build_result_event`, the **same** function the inline search
    path uses, so a verified run's envelope is the search envelope plus a
    `verification` key rather than a parallel shape maintained twice. Spec section
    9.7: a search-only run never carries that key, and `RESULT_KEYS` is asserted
    with set equality.
    """
    return build_result_event(final_state)["data"]


def persist_result(
    conn: Any,
    run: dict[str, Any],
    final_state: GraphState,
    result_data: dict[str, Any],
) -> None:
    """Store the finished report through the **standard** History data model.

    An Advanced Search run does not execute `run_graph_pipeline`, so it never
    reaches that module's `persist_run`; without this the run would complete with
    nothing in History. Both paths call the same `reports.py::save_report`, which
    stays the only SQL writer for `patents` / `search_runs` /
    `search_run_matches` -- so a durable run and a quick search land in the same
    tables, in the same shape, and open in the same report screen. There is no
    second row and no parallel table.

    **This runs inside the executor's outer transaction** (see
    `workflow/executor.py::_succeed`): the connection is passed in rather than
    opened here, and `save_report`'s own transaction block becomes a savepoint.
    Nothing is caught -- a database that cannot store the report fails the run
    instead of completing it with the report dropped.

    `result_data` is the exact object the terminal `result` event carries, so the
    stored report and the streamed one cannot disagree.
    """
    comparisons = final_state.get("comparisons") or []
    save_report(
        conn,
        description=run["description"],
        claims=run.get("claims") or None,
        concepts=result_data["concepts"],
        query_count=result_data["query_count"],
        recommendations=result_data["recommendations"],
        matches=result_data["patents"],
        patents=[comparison.patent for comparison in comparisons],
    )


def gate_of(interrupt_value: Any) -> WorkflowGate:
    """Which workflow gate an `interrupt()` payload represents.

    Reads the `gate` field the gate node put there. A payload naming no gate, or
    naming something outside the enum, **raises**: the executor is about to
    persist a `review_required` event and transition the run to
    `WAITING_FOR_REVIEW`, and a pause whose gate nobody can name is a pause no
    endpoint can resume.
    """
    payload = _payload(interrupt_value)
    raw = str(payload.get("gate", "")).strip()
    if not raw:
        raise ValueError(
            "an interrupt payload reached the executor with no `gate` field. Every "
            "gate node sets one; a pause whose gate cannot be named cannot be "
            "resumed by any endpoint, so it is refused rather than persisted."
        )
    try:
        return WorkflowGate(raw)
    except ValueError:
        raise ValueError(
            f"{raw!r} is not a workflow gate. Known: "
            + ", ".join(gate.value for gate in WorkflowGate)
        ) from None


def review_payload(
    run: dict[str, Any], gate: WorkflowGate, interrupt_value: Any
) -> dict[str, Any]:
    """The `review_required` event body (spec section 9.7).

    **Summary counts and a URL, never the artifact itself.** The event log is
    replayed to every reconnecting client and is retained for the life of the
    run; putting a decomposition or a matrix in it would duplicate mutable state
    into an append-only store, where the copy immediately starts going stale. The
    client follows `payload_url` to a gate endpoint that reads the live rows.

    Nothing here carries a lease token, an owner id, a checkpoint blob or any
    other executor internal (section 9.8).
    """
    payload = _payload(interrupt_value)
    run_id = run["run_id"]
    return {
        "gate": gate.value,
        "summary": _summary(gate, payload),
        "payload_url": f"/api/runs/{run_id}/gates/{gate.value}",
    }


def resume_value(
    run: dict[str, Any], resumed_event: Optional[dict[str, Any]]
) -> dict[str, Any]:
    """What `Command(resume=...)` carries back into the paused node.

    **A signal, never a decision.** It says "a human decision was committed for
    this gate -- go and read it", and the gate node then reads the approved
    artifact from the database through a reader that filters `is_approved` in SQL.

    It deliberately carries no approval, no version, no reviewer and no payload.
    This value travels through an HTTP request, an event row and a LangGraph
    checkpoint; anything load-bearing inside it would be forgeable at three
    separate points. The gate that consumes it already ignores it.
    """
    event = resumed_event or {}
    body = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    return {
        "resumed": True,
        "run_id": run["run_id"],
        # Echoed for a human reading a trace, and read by nothing.
        "gate": str(body.get("gate", "")) or None,
    }


def _payload(interrupt_value: Any) -> dict[str, Any]:
    """The mapping inside an `interrupt()` value, however LangGraph wrapped it.

    **Four shapes appear in real traces**, which is why this exists rather than
    an attribute access at four call sites:

    1. the raw dict the node passed to `interrupt()`;
    2. an `Interrupt` object carrying it on `.value`;
    2b. that same `Interrupt` **serialized as a dict** -- `{"value": {...},
       "id": ...}` -- which is a dict, so an `isinstance(value, dict)` check
       returns the wrapper and the payload is never reached;
    3. a **tuple or list** of `Interrupt`s -- a task can carry more than one, and
       LangGraph returns the sequence;
    4. an `Interrupt` whose `.value` is itself one of the above.

    Shape 3 is the one that bit: the executor unwrapped `.value` once, got the
    tuple back unchanged, and `gate_of` then refused a payload with no `gate` --
    correctly, since a tuple has no `gate`. The run failed at the first pause
    with `ValueError`, which is the right behaviour for an unnameable gate and
    the wrong diagnosis of this input.

    Unwrapping is bounded rather than recursive-until-it-works: three levels is
    every shape observed, and a fourth would mean the contract changed and should
    be looked at rather than absorbed.
    """
    value = interrupt_value
    for _ in range(4):
        if isinstance(value, dict):
            # A dict is usually the payload -- but LangGraph also serializes an
            # `Interrupt` AS a dict, `{"value": {...}, "id": ...}`, and that
            # wrapper is a dict too. Descending only when the payload's own key
            # is absent keeps both readings correct without guessing: a real
            # payload always names its gate.
            if "gate" not in value and isinstance(value.get("value"), (dict, list, tuple)):
                value = value["value"]
                continue
            return value
        if isinstance(value, (list, tuple)):
            if not value:
                return {}
            # The FIRST interrupt. A task carrying several is a graph that paused
            # for more than one reason at once, which this workflow never does --
            # each gate is its own node.
            value = value[0]
            continue
        unwrapped = getattr(value, "value", None)
        if unwrapped is None:
            return {}
        value = unwrapped
    return value if isinstance(value, dict) else {}


def _summary(gate: WorkflowGate, payload: dict[str, Any]) -> dict[str, Any]:
    """Counts only, per gate. Every value is an integer the client can render
    without interpreting, and none of them is patent text or model prose."""
    if gate is WorkflowGate.A:
        decomposition = payload.get("candidate_decomposition") or {}
        features = decomposition.get("features") or []
        return {
            "features": len(features),
            "requirements": sum(
                len(feature.get("elements") or []) + len(feature.get("relationships") or [])
                for feature in features
                if isinstance(feature, dict)
            ),
        }
    if gate is WorkflowGate.STRATEGY:
        strategy = payload.get("candidate_search_strategy") or {}
        queries = strategy.get("queries") or []
        return {
            "queries": len(queries),
            # How many of the proposed queries would actually be dispatched. A
            # count, like every other value here: the queries themselves are
            # mutable state and live behind `payload_url`, never in an
            # append-only event that is replayed to every reconnecting client.
            "enabled_queries": sum(
                1 for query in queries if isinstance(query, dict) and query.get("enabled")
            ),
        }
    if gate is WorkflowGate.B:
        return {
            "ranked_candidates": int(payload.get("ranked_candidates_total") or 0),
            "excluded_publications": int(payload.get("excluded_total") or 0),
        }
    if gate is WorkflowGate.C:
        return {
            "cells_total": int(payload.get("cells_total") or 0),
            "excluded_publications": int(payload.get("excluded_publications") or 0),
        }
    return {"report_version": int(payload.get("report_version") or 0)}


ADAPTER = GraphAdapter(
    build=build,
    initial_input=initial_input,
    result_payload=result_payload,
    persist_result=persist_result,
    gate_of=gate_of,
    review_payload=review_payload,
    resume_value=resume_value,
)
"""The production adapter.

Point `WORKFLOW_GRAPH_ADAPTER` at `graph.adapter:ADAPTER`. It has **no default**
on purpose (spec Gate A): an executor with no graph would claim runs it cannot
advance, and a default would silently choose which graph a deployment runs.
"""

__all__ = [
    "ADAPTER",
    "build",
    "gate_of",
    "initial_input",
    "persist_result",
    "result_payload",
    "resume_value",
    "review_payload",
]
