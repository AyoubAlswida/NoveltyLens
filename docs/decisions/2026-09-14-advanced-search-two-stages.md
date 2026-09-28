# Advanced Search uses two current review stages

Date: 2026-09-14  
Status: Accepted  
Supersedes the current-flow portion of `2026-09-01-advanced-search-three-stages.md`.

## Decision

New Advanced Search runs present two human-review stages:

1. Summary.
2. Invention Requirements (Claims).

After Stage 2 is approved, the worker generates and validates the search queries and
continues directly into patent search. There is no query-review pause for a new run.
The wording “Claims” is a product label for searchable invention requirements; it is
not legal claim drafting.

## Execution and audit boundary

The generated strategy remains machine output in `search_strategy_proposals`.
`select_generated_queries` verifies that it targets the approved understanding version
and that every query is non-empty and enabled before copying query text into execution
state. It never writes `approved_search_strategies` or attributes an approval to a user.
Invalid generated output fails the run before any patent-provider call.

## Compatibility

Migration `0026` backfills existing runs with `advanced_search_flow_version = 1` and
changes the default for new runs to version 2. Version 1 retains the `STRATEGY` gate,
API, UI, and checkpoint path. Version 2 routes directly through automatic validation.
A missing version is treated as version 1 so an old checkpoint cannot silently skip a
review it was admitted with.

Quick Search, report payloads, and History are unchanged.
