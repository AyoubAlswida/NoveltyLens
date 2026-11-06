"""What migrations 0006-0023 create, and how to ask the database whether it is there.

**This module exists because the workflow schema has two readers with different
jobs, and a second hand-maintained copy of the list would go stale in the
direction that hurts.** `tests/test_migrations.py` asserts the rollbacks reverse
exactly this set and re-applying restores it; `tests/conftest.py` asks, once per
session, whether the set is actually present before it lets a `postgres` test run.
Those are different questions about the same list, so the list lives here and both
import it.

## Why the session-start check has to be this thorough

`test_the_rollbacks_reverse_the_workflow_schema_and_re_apply` **drops this entire
schema and re-applies it**. That is the point of the test, and it restores the
schema in a `finally` so an assertion failure inside the window cannot leave a
developer's database broken.

A `finally` cannot survive the process being killed. Interrupt a run inside that
window -- Ctrl-C, an editor's stop button, a CI step timeout -- and the workflow
tables, functions and enums stay dropped. The *next* run then fails every
`postgres` test that sorts alphabetically before `test_migrations.py` with
`UndefinedFunction`, reaches `test_migrations.py`, and is **silently healed** by
that test's own `finally` -- so everything after it passes and the run looks like
a flaky subset rather than a database that started out broken.

That failure mode was observed, not theorised: a killed run left the schema
dropped, and the next full run failed 16 tests across `test_evidence_persistence`,
`test_lease_races` and `test_migrations` -- every one of them at or before
`test_migrations.py` in collection order, and not one of the ten `postgres` files
after it. The run that followed was green with no intervention.

**Checking one table is not enough**, which is what the guard used to do. The
damage is not all-or-nothing: yoyo rolls back and re-applies migration by
migration, and a process killed partway leaves tables present while functions and
enums from later migrations are missing. `to_regclass('verification_runs')` is
satisfied by that state, so the guard passed and the suite failed confusingly
instead of skipping clearly. The set below is the whole schema for that reason.
"""

from typing import Iterable

FIRST_WORKFLOW_MIGRATION = "0006"

WORKFLOW_TABLES: tuple[str, ...] = (
    "verification_runs",
    "epo_request_clock",
    "run_status_transitions",
    "run_leases",
    "node_attempts",
    "workflow_events",
    "idempotency_records",
    "feature_decompositions",
    "decomposition_elements",
    "decomposition_relationships",
    "decomposition_requirement_ids",
    "candidate_sets",
    "candidate_set_members",
    "publication_dates",
    "publication_exclusions",
    "evidence_passages",
    "verification_cells",
    "verification_results",
    "cell_reviews",
    "cell_waivers",
    "review_audit",
    "report_versions",
    # 0020: the two human work-request tables the API records and the worker
    # carries out (POST /candidates, POST /cells/{id}/rerun).
    "manual_candidates",
    "cell_rerun_requests",
    # 0021: the invention understanding stage. Machine proposals and human edits,
    # the approved summary, and the regeneration work request.
    "understanding_proposals",
    "approved_understandings",
    "understanding_regeneration_requests",
    # 0023: the search strategy stage, same three shapes for the same three
    # reasons -- a proposal that cannot record an approval, the approved artifact,
    # and the regeneration work request.
    "search_strategy_proposals",
    "approved_search_strategies",
    "approved_strategy_queries",
    "strategy_regeneration_requests",
)

WORKFLOW_FUNCTIONS: tuple[str, ...] = (
    "_append_event_locked",
    "_lock_run_and_require_lease",
    "_terminal_replay_or_null",
    "acquire_run_lease",
    "append_api_event",
    "append_worker_event",
    "commit_node_attempt",
    "create_run",
    "enforce_lease_monotonicity",
    "enforce_review_audit_append_only",
    "enforce_run_status_rules",
    "enforce_terminal_agreement",
    "finalize_run_failure",
    "finalize_run_success",
    "heartbeat_run_lease",
    "pause_run_for_review",
    "release_run_lease",
    "sync_decomposition_requirement_ids",
)

WORKFLOW_ENUMS: tuple[str, ...] = (
    "attempt_outcome",
    "cell_state",
    "date_certainty",
    "exclusion_state",
    "locator_type",
    "review_status",
    "run_status",
    "text_version_source",
    "workflow_event_type",
    "workflow_gate",
)

PRE_EXISTING_TABLES: tuple[str, ...] = (
    "patents",
    "search_runs",
    "search_run_matches",
    "users",
)


def tables(conn) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )
    }


def functions(conn) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            """
            SELECT proname FROM pg_proc p
              JOIN pg_namespace n ON n.oid = p.pronamespace
             WHERE n.nspname = 'public'
            """
        )
    }


def enums(conn) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            """
            SELECT typname FROM pg_type t
              JOIN pg_namespace n ON n.oid = t.typnamespace
             WHERE n.nspname = 'public' AND typtype = 'e'
            """
        )
    }


def missing_workflow_objects(conn) -> dict[str, tuple[str, ...]]:
    """Every declared workflow object the database does not currently have.

    Returned per kind rather than as one flat set so the reason a run skips names
    what is actually absent. An empty mapping means the schema is whole; anything
    else means a migration has not been applied, or a destructive run was killed
    partway through re-applying one.
    """
    present_tables, present_functions, present_enums = (
        tables(conn),
        functions(conn),
        enums(conn),
    )
    missing = {
        "tables": tuple(t for t in WORKFLOW_TABLES if t not in present_tables),
        "functions": tuple(f for f in WORKFLOW_FUNCTIONS if f not in present_functions),
        "enums": tuple(e for e in WORKFLOW_ENUMS if e not in present_enums),
    }
    return {kind: names for kind, names in missing.items() if names}


def describe_missing(missing: dict[str, Iterable[str]]) -> str:
    """A one-line summary naming what is absent, for a skip reason or a warning."""
    return "; ".join(
        f"{kind}: {', '.join(sorted(names))}" for kind, names in sorted(missing.items())
    )
