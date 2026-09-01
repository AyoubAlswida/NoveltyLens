# Advanced Search — Three-Stage Guided Search: Implementation Prompt

**Status: authoritative implementation prompt. One document, no short version.**
**Audience: the implementation agent. Read this file completely before editing anything.**

You are converting the existing five-gate *feature verification* workflow into a **three-stage
guided prior-art search** called **Advanced Search**, and removing every numeric similarity
score from the product's outputs.

**Reference convention.** `§N` on its own means a section of **this** document.
`CLAUDE.md §<name>` means a named section of the repository guide. `workflow spec §N` means
`docs/prompts/integrated-feature-verification-workflow-implementation.md`. `verifier spec §N`
means `docs/prompts/feature-disclosure-verification.md`. The three documents overlap in section
numbering and mean entirely different things — do not read one as another.

**The documents you need open.** This one, `CLAUDE.md`, and the workflow spec (for the
guarantees you are *keeping*: the lease, the event log, the approval boundary, ownership
scoping, idempotency). You are narrowing the workflow's **scope**; you are not relaxing any of
its execution-integrity guarantees.

---

## 0. CRITICAL CONSTRAINTS — read these first, and re-read them at every gate

These are the project's standing rules. This change does not suspend any of them. Every one has
a live test or a documented incident behind it; violating one is not a style disagreement.

1. **Zero-tolerance failure policy. No fallback, ever.** If a step cannot produce a genuine
   result it raises and the request fails with a clear `error` event. Never substitute
   fabricated, placeholder or degraded output. This is the single most consequential contract
   in the project (`CLAUDE.md §Migration status and standing constraints`). Removing scores
   from the *display* is not permission to invent a rating when there is nothing to rate — see
   §5.6.

2. **No provider call before the human approves the queries.** Today this is a property of the
   graph topology, not a convention:
   `tests/test_workflow_gates.py::test_no_provider_call_is_reachable_before_both_approvals`
   asserts `search_orchestrator` has exactly two incoming edges. That test must keep passing,
   with the same strength, after your change. Weakening it to a comment is a failure.

3. **The approval boundary is asymmetric and stays asymmetric.** Machine output is never
   sufficient. `backend/workflow/approvals.py` remains the **only** module that writes an
   approved artifact, and `tests/test_approval_boundary.py` enforces that at the AST level.
   You are changing what gets approved, never who may approve it or where it is written.

4. **Single-writer rules stand.** `workflow/events.py` is the only writer of `workflow_events`;
   `reports.py` is the only read/write surface for the four search tables; `workflow/store.py`
   run-scoped functions all take `actor_email` and filter **in SQL**. The executor's persistence
   callback in §4.6 delegates to `reports.py`; it does not write report SQL itself. Do not add a
   generic append, do not fetch-then-compare ownership in Python, and do not let anything in
   `api/` reach a `_system_*` function.

5. **A non-owner gets `404 RUN_NOT_FOUND`, never `403`.** Any endpoint you add obeys this, and
   `tests/test_run_ownership.py` walks the whole catalogue with a second user's token.

6. **Failures are sanitized at the persistence and streaming boundaries**
   (`workflow/redaction.py`). Anything you add that stores or streams an error text goes
   through it. A provider body carries credentials; this has happened for real.

7. **Never delete data to make a change tidy.** Legacy Gate B/C/D tables, migrations and rows
   stay. You remove *UI and active runtime paths*, not history. Migrations are forward-only
   with a rollback file; the only destructive DDL you write is the column drop in §5.4, and it
   is deliberate, reviewed and reversible.

8. **`CLAUDE.md` is part of the deliverable, not an afterthought.** It currently describes the
   five-gate verified workflow at length as live behaviour. After your change most of that is
   false. Where this document and `CLAUDE.md` disagree *after* you are done, `CLAUDE.md` is the
   bug and you fix it in the same change (`CLAUDE.md §Keeping this file current`).

9. **Do not over-engineer.** Delete rather than abstract. No compatibility shims for a public
   contract that has one consumer in this repository. No feature flag selecting between the old
   and new workflow — the deleted `PIPELINE` flag is the cautionary tale
   (`CLAUDE.md §Backend: two pipelines`).

10. **Read before you edit.** Every file path and line number in §1 was verified against the
    working tree on 2026-09-01. Re-verify before you rely on one; do not speculate about a file
    you have not opened.

---

## 1. Ground truth: the repository as it actually is

Verified against the working tree. Line numbers will drift as you edit — treat them as
directions, not addresses.

### 1.1 The scoring code you are removing

| Fact | Location |
|---|---|
| `verdict_for(top_score)` → `Weak` ≥75, `Moderate` ≥25, else `Strong` | `backend/graph/ranking.py:29-31` |
| `"pct": score` written into every match | `backend/graph/ranking.py:51` |
| `"risk": "High" if score >= 75 else "Low" if score <= 25 else "Medium"` | `backend/graph/ranking.py:52` |
| Matches sorted by `int(item["pct"])` descending | `backend/graph/ranking.py:61` |
| `save_report(..., top_score, verdict, ...)`; `INSERT INTO search_runs (… top_score, verdict …)` | `backend/reports.py:68,91` |
| `INSERT INTO search_run_matches (run_id, rank, patent_id, pct, risk, …)` | `backend/reports.py:104` |
| `list_history` selects `top_score, verdict` and `srm.pct`, ordered by `srm.rank` | `backend/reports.py:165,185,190` |
| `search_runs.top_score` / `.verdict` columns | `backend/migrations/0002.create-search-runs.sql` |
| `search_run_matches.pct` column | `backend/migrations/0003.create-search-run-matches.sql` |
| `PatentMatch.pct`, `AnalysisReport.top_score`, `AnalysisReport.verdict`, `type Verdict` | `frontend/src/app/models/report.ts:5-25` |

**Frontend files that reference `pct`, `top_score` or `verdict`:** `app.ts`,
`models/report.ts`, `models/vocabulary.ts`, `screens/evidence-review.ts`,
`screens/gates.spec.ts`, `screens/history-screen.ts`, `screens/report-screen.ts`,
`screens/verification-report.ts`, `services/export.ts`, `services/history.ts`.

**The risk thresholds are the thresholds you keep.** `>= 75 → High`, `<= 25 → Low`, otherwise
`Medium`. Copy them exactly, boundaries included. Do not "clean up" `<= 25` into `< 25`; that
moves a real patent between two ratings.

### 1.2 The graph

`backend/graph/pipeline.py::_assemble` is the single topology, compiled twice
(`build_graph()` without a checkpointer for the inline search path,
`build_graph_with_checkpointer()` for the executor). Relevant routers:

- `_route_after_understanding` (`:92`) — rename `verification_requested` to
  `advanced_search`; `advanced_search` → `decompose`, else
  `search_orchestrator`.
- `_route_after_validate` (`:80`) — the "found anything?" check.
- `_route_after_explain` (`:104`) — currently sends a verified run into
  `normalize_publications`; **this is the edge you cut** (§4.3).
- `_route_after_eligibility` (`:114`) — evidence-layer routing, becomes unreachable.

Nodes registered for the verified branch: `decompose`, `gate_a`, `regenerate_understanding`,
`propose_strategy`, `gate_strategy`, `regenerate_strategy`, `normalize_publications`,
`screen_dates`, `enrich_epo`, `build_passages`, `confirm_eligibility`, `rank_and_reduce`,
`gate_b`, `verify_features`, `gate_c`, `draft_report`, `gate_d`,
`absorb_manual_candidates`, `rerun_cells`.

### 1.3 The API surface

`backend/api/runs.py` route inventory (line numbers as verified):

| Line | Route | Fate |
|---|---|---|
| 136 | `POST /api/runs` | **keep**, drop two body fields (§4.1) |
| 230 | `GET /api/runs` | keep |
| 238 | `GET /api/runs/{id}` | keep |
| 251 | `GET /api/runs/{id}/stream` | keep, unchanged §9 semantics |
| 285 | `GET /api/runs/{id}/gates/A` | **keep, reshaped** (§4.2) |
| 364 | `POST /api/runs/{id}/gates/A/approve` | **keep, reshaped** (§4.2) |
| 463 | `POST /api/runs/{id}/understanding/regenerate` | **remove** (§10.2) |
| 549 | `GET /api/runs/{id}/gates/STRATEGY` | **keep, reshaped** (§4.2) |
| 616 | `POST /api/runs/{id}/gates/STRATEGY/approve` | **keep, reshaped** (§4.2) |
| 725 | `POST /api/runs/{id}/strategy/regenerate` | **remove** (§10.2) |
| 811, 843 | `GET/POST /api/runs/{id}/gates/B[/approve]` | **remove** |
| 920 | `POST /api/runs/{id}/candidates` | **remove** |
| 990 | `POST /api/runs/{id}/cells/{cell_id}/rerun` | **remove** |
| 1078 | `GET /api/runs/{id}/gates/C` | **remove** |
| 1099 | `GET /api/runs/{id}/cells/{cell_id}` | **remove** |
| 1154, 1199 | `POST .../cells/{id}/review`, `/waiver` | **remove** |
| 1239 | `POST /api/runs/{id}/gates/C/complete` | **remove** |
| 1317 | `GET /api/runs/{id}/report` | **repurposed** — §4.4 |
| 1331 | `POST /api/runs/{id}/gates/D/approve` | **remove** |

### 1.4 The frontend

- `frontend/src/app/screens/`: `understanding-review.ts`, `strategy-review.ts`,
  `candidate-review.ts`, `evidence-review.ts`, `draft-review.ts`, `gate-shell.ts`
  (shared frame, thirteen states, `STAGE_LABELS`, `classify`), `verification-report.ts`,
  plus `login`/`submit`/`running`/`report`/`history`/`error`.
- `frontend/src/app/services/runs.ts` — the run API client; one method per route above.
- `frontend/src/app/app.ts`:
  - `followToGate(runId)` at `:406`, called from exactly one place, `:385`.
  - **`afterDecision()` at `:463` sets `reviewGate` to null and `screen` to `'running'` and
    does not restart `followToGate`.** That is the whole bug behind "the user has to refresh
    after approving" — the poller that would notice the next gate is never started again.
  - `problemSolved` / `noveltyStatement` signals, ~`:517-519`.
- `frontend/src/app/screens/submit-screen.ts`: `:85` `Run Novelty Lens`, `:95`
  `Run with feature verification`, `:99` the explanatory paragraph, `:109`
  `<summary>Add context for feature verification (optional)</summary>`.
- `npm test` runs Vitest through `@angular/build:unit-test` — **86 specs today**, including
  `screens/gates.spec.ts` and `services/runs.spec.ts`. `vitest.config.ts` pins
  `pool: 'threads'`; leave that alone.

### 1.5 The storage shapes you are reusing

- `decomposition_elements` (`migrations/0010`) has **`core BOOLEAN NOT NULL`**. It has no
  default. A flat element list must supply a value — see §4.2.3.
- `decomposition_requirement_ids` is maintained by a trigger and enforces cross-kind
  uniqueness. Do not write it directly.
- `understanding_proposals`, `approved_understandings`,
  `understanding_regeneration_requests` — `migrations/0021`.
- `search_strategy_proposals`, `approved_search_strategies`, `approved_strategy_queries`,
  `strategy_regeneration_requests` — `migrations/0023`.
- **The next migration number is `0024`.**

### 1.6 Tests and acceptance scripts that will break

You must handle each deliberately; a deleted test is a removed guarantee and needs a stated
reason.

- `backend/tests/test_workflow_gates.py`, `test_verification_integration.py`,
  `test_approval_boundary.py`, `test_frontend_review_contract.py`, `test_history.py`,
  `test_draft_report.py`, `test_matrix_traceability.py`, `test_stability_coverage.py`.
- `backend/tests/test_matrix_traceability.py` **parses verifier spec §6, maps all 164 rows to
  named tests, and resolves every name against the real suite.** Retire this test and its
  generated table together under the resolved decision in §10.1; do not leave behind a
  traceability claim for a withdrawn product feature.
- `backend/acceptance/four_gate_acceptance.py`, `journey_acceptance.py`,
  `browser_acceptance.py`, `real_process_acceptance.py`, `matrix_traceability.py`.
- `backend/run_mock.py --workflow` (the four-gate mock).

---

## 2. What Advanced Search is, in one paragraph

**Advanced Search is a standard prior-art search preceded by guided editing.** The user writes
a description; the system proposes a technical summary and a flat list of technical elements;
the user edits and approves them; the system proposes search queries; the user edits and
approves those; the approved queries are then executed by the ordinary search pipeline and the
ordinary report is displayed and exported. It performs **no** evidence verification, makes
**no** legal conclusion, and presents **no** numeric score. Anywhere the old product implied
otherwise — a screen, a label, an export, a paragraph of `CLAUDE.md` — that implication is now
false and must go.

**The standard "Run Novelty Lens" button and `POST /api/analyze[/stream]` are untouched.** Same
inline execution, same nodes, same phases, no run row, no lease, no event log. Every change
below is either inside the verified branch or inside the display/persistence of scores. If you
find yourself editing `_route_after_understanding`'s search-only arm, stop.

The button that launched feature verification is renamed **Advanced Search**, and the
**Add context for feature verification (optional)** section and its two fields are removed
(`submit-screen.ts:95,99,109`).

---

## 3. The three stages: user-experience contract

The internal gate letters `A` and `STRATEGY` remain internal and **appear nowhere on any
screen** — `gate-shell.ts`'s `STAGE_LABELS` already maps them, and
`tests/test_frontend_review_contract.py` plus a component spec assert that "gate a",
"decomposition", "checkpoint" and "verification cell" never reach the rendered DOM. That
assertion set stays and gains the vocabulary you are removing (§8.1 row 11).

### 3.1 Progress indicator

Every stage shows a plain `1 / 3`, `2 / 3`, `3 / 3`. It is rendered text, not an aria-label
only, and a component spec asserts it.

### 3.2 Stage 1 — Review the understanding

- The **original description, read-only.** It is never rewritten — not by a proposal, not by an
  edit, not by an approval. `verification_runs.description` stays byte-identical through the
  whole run and a test already asserts that. Keep it.
- An **editable technical summary**, proposed by the LLM.
- Controls: **Back**, **Next**.

### 3.3 Stage 2 — Edit the technical elements

- A **flat, editable list of technical element strings.** Add, edit, delete.
- **Removed:** capability grouping, Core flags, relationships, and the "Not what you meant?"
  section.
- Element ids are minted by the screen, never typed by the user, and always **above the highest
  id already present** — deleting `E1` from `[E1, E2]` and adding one produces `E3`, not a
  second `E2`. That rule already exists and is load-bearing; keep it.

### 3.4 Stage 3 — Edit and approve the search queries

- A **plain list of editable query strings.** Add, edit, delete.
- **Removed:** enable/disable switches, rationales, element-to-query coverage mapping and its
  panel, and the "What this step requires" section.
- Approving this stage is what releases the run into the search.

### 3.5 Validation — exactly three rules

1. The technical summary must not be empty (after trimming).
2. At least one non-empty technical element.
3. At least one non-empty search query.

Nothing else blocks. Every blocking condition not on this list is removed from
`workflow/review.py` for these two gates and from the screens. **The rules a reviewer reads on
screen must still pair one-to-one and in the same order with the server's list**, which is the
existing weak-but-real check — keep it and shrink both sides together.

### 3.6 Navigation and edit preservation

- Back and forward between stages preserves edits **within the browser session**, without a
  server round trip and without re-proposing.
- Stages 1 and 2 are **one backend approval** (the understanding). Stage 3 is a second
  approval (the strategy). Two approvals, three screens.
- Moving from Stage 1 to Stage 2 must not call any approval endpoint. Only the transition out
  of Stage 2, and the transition out of Stage 3, mutate anything.

### 3.7 Resume monitoring after every approval

**Fix `afterDecision()`.** After a decision commits, the run is `RESUMING` and the executor
picks it up on its next poll. `afterDecision()` currently switches to the running screen and
starts nothing, so nothing ever notices the next stage or the terminal result and the user
refreshes to make progress appear.

`afterDecision()` must resume watching the same run — restart `followToGate(runId)` for the run
the decision was made on — so that the next stage, the completed report, or the failure arrives
with no user action. A component or app-level spec asserts this by driving two consecutive
approvals with a faked run API and no reload.

### 3.8 Completion

When the run reaches `COMPLETED`, the app fetches the run result (§4.4) and renders it with the
**existing `ReportScreen`**, including the existing PDF / Word / JSON export controls. A page
reload on a completed run restores the same thing — `restoreActiveRun()` already has a
`COMPLETED` branch; it must now open `ReportScreen` rather than `verification-report`.

---

## 4. Backend and public-contract changes

### 4.1 Admission

`POST /api/runs` stays the Advanced Search admission endpoint. Its accepted business fields are
exactly `description` and optional `claims`. **Remove `problem_solved`,
`inventor_novelty_statement`, and the unused `cutoff_date` from the accepted request body.** The
corresponding nullable columns on `verification_runs` stay (constraint 7: no destructive
deletion). `store.set_submission_context` may keep writing `NULL` for its two legacy context
columns, and `create_run` writes `NULL` to the legacy `cutoff_date`; nothing collects or exposes
any of the three.

Admission performs no verifier-capability, date-screening or text-version-policy check and does
not return `capability_qualified`; use the explicit legacy-column treatment in §10.4. Define a
strict request model so unknown fields are refused rather than ignored.

An unknown field in the body is refused rather than ignored — a request still sending the old
fields is a client that has not been updated, and silently accepting it hides that.

### 4.2 The two public payloads

#### 4.2.1 Understanding (Stages 1 and 2)

`GET /api/runs/{id}/gates/A` carries the original input needed by Stage 1 plus the editable
business payload:

```json
{
  "original_description": "…",
  "technical_summary": "…",
  "technical_elements": ["…", "…"]
}
```

`original_description` is read-only and is not accepted by the approval endpoint. The approval
body contains `technical_summary`, `technical_elements`, and the existing required
`idempotency_key`. `technical_elements` is a **list of strings**. No capability objects, no
`core`, no relationships, no per-element ids in the public shape. If the GET response retains
generic transport metadata such as `editable` or `completion_rules`, keep it outside this
business payload and do not revive removed concepts through it.

#### 4.2.2 Strategy (Stage 3)

`GET /api/runs/{id}/gates/STRATEGY` exposes this editable business payload:

```json
{ "queries": ["…", "…"] }
```

A list of strings. The approval body contains `queries` and the existing required
`idempotency_key`. No `enabled`, no `rationale`, no `covered_element_ids`, no
`understanding_version` coverage claim in the public shape. Generic GET metadata may remain
outside the business payload under the same rule as Stage 1.

#### 4.2.3 What the storage does with that

The approval writer (`workflow/approvals.py`, still the only one) may internally store the flat
list as **one technical group** — one `feature_decompositions` row, one
`decomposition_elements` row per element. `decomposition_elements.core` is `NOT NULL` with no
default; **write `TRUE` for every element.** That is the honest encoding of "all approved
elements count", and it is what makes the internal `NO_CORE_ELEMENT` condition unreachable
rather than deleted-and-hoped-for.

**Core semantics are never exposed.** No API response, no screen, no export mentions core.
Write a test that greps the two public payload builders for `core` and fails on a hit.

`decomposition_relationships` gets no rows. Relationships are gone from the product, so
`DANGLING_RELATIONSHIP_REFERENCE` and `llm.regenerate_relationships` have nothing to guard;
remove the check and the caller together, and say so in the commit.

The strategy approval stores one row per approved query in `approved_strategy_queries`. With
enable/disable gone, **every stored query is enabled**; keep the column if dropping it costs a
migration you would rather not write, and set it `TRUE` — but do not leave a screen or an API
field that pretends the user can set it.

### 4.3 Graph topology

Target shape for a verified (Advanced Search) run:

```
understand -> expand_queries -> decompose -> gate_a
           -> propose_strategy -> gate_strategy -> search_orchestrator
           -> keyword_search -> validate -> rank -> explain -> END
```

- **`_route_after_explain` ends the graph.** An Advanced Search run now terminates after
  `explain`, exactly as a search-only graph execution does. The durable executor persists the
  report and terminal result atomically after the graph returns (§4.6).
- **Remove from the active topology:** `normalize_publications`, `screen_dates`, `enrich_epo`,
  `build_passages`, `confirm_eligibility`, `rank_and_reduce`, `gate_b`, `verify_features`,
  `gate_c`, `draft_report`, `gate_d`, `absorb_manual_candidates`, `rerun_cells`, and
  `_route_after_eligibility`. Also remove the regeneration branches and registrations for
  `regenerate_understanding` and `regenerate_strategy`; editing replaces regeneration (§10.2).
- **Remove, do not comment out.** The `# TEMP: disabled` style in `_assemble` is reserved for
  the semantic-search trio, which is genuinely coming back. Gates B–D are not; leaving them
  commented invites someone to uncomment a path whose API, screens and tests no longer exist.
  Git history is the record.
- `gate_strategy_node` remains the **only** thing that writes `queries` on the verified branch,
  and it writes the approved query strings. `search_orchestrator` keeps exactly two incoming
  edges — `expand_queries` (search-only) and `gate_strategy` (Advanced Search) — and constraint
  2's test keeps proving it.
- Rename the graph-state branch flag from `verification_requested` to `advanced_search` in
  `graph/state.py`, `graph/adapter.py`, the routers, comments, tests and fixtures in this same
  change. Do not add an alias or compatibility shim; this is an internal name with one
  repository consumer (§10.5).
- **The resume value stays a signal, never a decision.** Every gate node discards
  `interrupt()`'s return value and re-reads the approved artifact through
  `workflow/approvals.py`'s SQL-filtered readers. The AST test asserting this stays and must
  still pass for the two surviving gates.

### 4.4 Retrieving a completed run's result

Add an **owner-scoped** endpoint returning the standard report for a completed Advanced Search
run. `GET /api/runs/{id}/report` currently returns the Gate D verification report and is the
natural path to repurpose — repurpose it rather than adding a second one.

- Owner-scoped through `workflow/store.py` with `actor_email` filtered **in SQL**; a non-owner
  and an unknown run both get `404 RUN_NOT_FOUND`.
- Returns the same object the terminal `result` event carries and the same object
  `services/history.ts` maps into `AnalysisReport` — one display path, as today.
- A run that is not `COMPLETED` gets a `409` naming its actual status, in the existing
  `{code, detail}` top-level shape from `api/deps.py`.

### 4.5 Phases

`graph/events.py`'s `PHASES` loses the vocabulary of the removed nodes; `PHASE_ORDER` in
`frontend/src/app/models/report.ts` loses the same names. **The two lists must agree in both
directions** — a phase the backend cannot emit stalls the checklist, and one it emits that the
frontend omits hides real work. `tests/test_workflow_gates.py` and
`test_verification_integration.py` already assert both directions; keep those assertions
pointed at the new vocabulary.

`emit_progress` still raises `SSEContractError` on an unknown phase. Do not relax it.

### 4.6 Persistence

Advanced Search results are persisted through the **standard** History data model —
`search_runs` / `search_run_matches` / `patents` via `reports.py`. A quick search still uses
`graph/pipeline.py::persist_run` before yielding its `result` event. A durable Advanced Search
run does **not** execute `run_graph_pipeline`, so it must not rely on that call site.

Add a required persistence callback to `workflow/executor.py::GraphAdapter`, for example
`persist_result(conn, run, final_state, result_data)`. The production adapter implements it by
delegating to `reports.py::save_report`; `reports.py` remains the only SQL writer for the three
search-report tables. Small executor-only test adapters may provide an explicit no-op callback,
but there is no optional/default callback and the production adapter may never no-op.

`WorkflowExecutor._succeed` builds the terminal result data once, then opens **one outer database
transaction** and, in this order:

1. calls `adapter.persist_result(...)` with that exact result data;
2. calls `store.finalize_run_success(...)` with the terminal `result` event carrying the same
   object;
3. releases the lease.

The transaction commits all three or none. `save_report` may retain its own transaction block;
inside the executor's outer transaction it is a savepoint, not a separate commit. There is no
`try` that converts report-storage failure into success. If standard report persistence fails,
the run does not become `COMPLETED`, no terminal `result` event is committed, and no partial
History row remains. The normal executor failure path then records a sanitized failure.

Do not call `graph/pipeline.py::persist_run` from the executor and do not run the graph a second
time. The inline path and durable path share the `reports.py` writer and result shape, not an
execution wrapper. A durable run and a quick search therefore land in the same tables and open
in the same `ReportScreen` without duplicate History rows.

Adding or removing a field in `patents[]` is still a **three-place change**: produced in
`graph/ranking.py::format_matches`, persisted in `reports.py`, declared in the frontend's
`PatentMatch`. `tests/test_history.py` asserts the whole `patents[]` key set round-trips —
update that assertion to the new key set rather than to named fields.

---

## 5. Removing the percentage and the verdict

### 5.1 What the API returns

- `patents[]` entries: `num`, `title`, `risk`, `rationale`, `key_overlap`, `url`, `source`.
  **`pct` is gone.**
- The report object loses **`top_score`** and **`verdict`**.
- Everything else in the terminal `result` payload is unchanged.

### 5.2 The score becomes transient execution data

Cosine similarity is still computed — it is how ranking works. It is used for exactly two
things and then discarded:

1. **Ordering.** Sort matches by score, descending, strongest first. This is the same
   `sorted(..., reverse=True)` that exists today, reading the score directly rather than
   `int(item["pct"])`.
2. **Rating.** `risk = High if score >= 75 else Low if score <= 25 else Medium` — the existing
   thresholds, exactly.

The score must not appear in the returned object, in the persisted row, or in any export.
Write a test that walks the whole serialized report — API response and stored round-trip — and
fails on any numeric field carrying a similarity value.

**"Sort within each risk category"** and "sort by score descending" are the same ordering here,
because the rating is a monotonic function of the score. Implement the single global descending
sort; do not group first and sort inside groups, which produces the identical order by a more
fragile route.

### 5.3 Order is preserved by `rank`

`search_run_matches.rank` already stores the ordinal and `list_history` already orders by it.
That column is now the **only** record of ordering, since the score it was derived from is
gone. Do not drop it, do not reorder on read, and add an explicit test that a stored report
reads back in the same order it was written.

### 5.4 The migration

Add `backend/migrations/0024.drop-report-scores.sql` and its `.rollback.sql`:

- `ALTER TABLE search_run_matches DROP COLUMN pct;`
- `ALTER TABLE search_runs DROP COLUMN top_score, DROP COLUMN verdict;`

This is the one destructive DDL in the change and it is deliberate: leaving the columns would
leave stored scores that the product has decided not to keep, and a nullable leftover column is
how a field comes back by accident. The rollback re-adds the columns; it cannot restore the
values, and the rollback file must say so in a comment rather than implying otherwise.

Apply with `python migrate.py`. Remember the LangGraph checkpoint tables are a **separate**
setup step (`python checkpointer.py setup`) and yoyo does not own them.

### 5.5 The UI and exports

Remove: percentage rings (`.score-ring`), percentage bars (`.score-bar`), "Match level" labels,
the history screen's percentage column, and every percentage field from the PDF, Word and JSON
exports.

**Display only `Novelty risk: High / Medium / Low`** on patent cards and on the closest-result
summary.

Two standing style constraints apply to the CSS you touch:

- **Green is meaning, teal is decoration.** `risk-low` green, `risk-label`, `risk-legend-item`
  are semantic and keep their colours. Do not recolour a status to brand teal.
- **Any state class you add needs an explicit dark counterpart in the same change.** The
  specificity trap in `styles/globals.css` has bitten four times: `:root[data-theme="dark"]
  .foo` is (0,3,0), sits later in the file, and silently erases a state class. The symptom is
  never an error — it is a state that renders identically to its neutral.

`services/export.ts` is the one place where "ported verbatim, byte-identical" was a standing
promise. That promise is now **deliberately broken** by this change: exports lose their
percentage fields. Update the comment that claims byte-identity so it does not become a lie,
and re-pin the export tests against the new expected output.

### 5.6 The empty case

**If no patents are returned, display no Novelty risk rating at all.** Keep the explicit
empty-results state. Zero results is a genuine answer — reaching every provider and finding
nothing, or validation filtering the set to nothing — and the report goes out with real
`concepts` and `query_count` and an empty recommendation list. Rendering "Novelty risk: Low"
over an empty result set would be the zero-tolerance violation this whole product exists to
avoid: it states a finding the run did not make.

---

## 6. Removing Gates B, C and D

### 6.1 What is removed

- **Graph:** the nodes and edges in §4.3.
- **API:** the routes marked *remove* in §1.3.
- **Frontend:** `screens/candidate-review.ts`, `screens/evidence-review.ts`,
  `screens/draft-review.ts`, `screens/verification-report.ts`; their `@case` arms in `app.ts`;
  their methods in `services/runs.ts` (`gateB`, `gateC`, `cell`, `approveGateB`, `addCandidate`,
  `reviewCell`, `waiveCell`, `rerunCell`, `completeGateC`, `approveGateD`); the `'B' | 'C' |
  'D'` members of the gate type; the `verification-report` screen value; and the entries in
  `models/vocabulary.ts` for cell states, outcomes and publication exclusions that nothing
  renders any more.
- **`gate-shell.ts`:** the thirteen states shrink to the ones two stages can actually reach.
  Shrink them by *proving* a state is unreachable, not by guessing — `loading`, `empty`,
  `review`, `validating`, `waiting`, `resuming`, `blocked`, `stale`, `forbidden`,
  `reconnecting`, `failed`, `completed` mostly survive; `paused` and any cell-specific state do
  not. **Do not collapse two surviving states into one.** Each distinction exists because
  reporting one situation as another is the defect this project forbids.

### 6.2 What is kept

- Every Gate B/C/D **table and migration**, untouched, with their rows.
- `backend/verification/`, `backend/evidence/`, `backend/graph/evidence_nodes.py`,
  `verification_nodes.py`, `graph/report.py`, `graph/work_requests.py`,
  `workflow/evidence_store.py`, and `search/epo_fulltext.py` remain intact but unreachable from
  the compiled product graph (§10.3). `CLAUDE.md` must say that plainly. Do not partially delete
  this subsystem in this work.
- The standalone capability-qualification machinery remains available for historical analysis
  and future experiments, but Advanced Search admission does not call it and product deployment
  no longer requires its policy settings (§10.4).

### 6.3 Legacy runs

Runs parked at Gate B, C or D are **not resumable**. Their data stays. The product provides no
UI and no API to continue them.

This must fail **honestly**, not confusingly. A user whose remembered run is parked at a removed
gate must see a clear statement that the run cannot be continued — not a spinner, not an empty
gate screen, not a 500. `restoreActiveRun()` and `followToGate()` both need a branch for
`WAITING_FOR_REVIEW` with a `current_gate` the app no longer knows, and it says so in words.

---

## 7. Implementation gates

Work in this order. Each gate ends with the suite green and a run you have actually driven.

**Gate 1 — Scores.** §5 end to end: `ranking.py`, `reports.py`, migration `0024`,
`models/report.ts`, every frontend consumer, the exports, and the tests. This gate touches the
*quick search* path too and is the one that can ship on its own. Verify with
`python run_mock.py` (no flag) plus a real quick search through the UI.

**Gate 2 — Backend contract.** §4.1, §4.2, §4.3, §4.5, §4.6: the payload reshape, the graph
topology, the `advanced_search` rename, the phases, the flat-element storage, and the durable
executor's required report-persistence callback. Backend tests only; the frontend is still
broken at this point and that is expected.

**Gate 3 — Removal.** §6: routes, screens, services, types, vocabulary, gate-shell states.

**Gate 4 — The three stages.** §3: the two reshaped screens plus the stage-3 screen, the
`1 / 3` indicator, edit preservation, the three validation rules, `afterDecision()`, the
completed-run restore path, and the submit-screen rename.

**Gate 5 — Result retrieval and report.** §4.4 and §3.8: the owner-scoped endpoint,
`ReportScreen` for a completed Advanced Search run, exports working from it, reload restoring
it, and an end-to-end assertion that the durable result and its standard History row commit
together.

**Gate 6 — Mock, acceptance and documentation.** §8.6, §9 and the resolved decisions in §10.

---

## 8. Required test matrix

Every row is a test that must exist and pass. Where a row names an existing file, extend it
rather than starting a parallel one.

### 8.1 Stages (frontend, Vitest)

1. Stage 1 renders the description read-only and the summary editable.
2. Stage 2 renders a flat element list; add, edit and delete each work.
3. Stage 3 renders a plain query list; add, edit and delete each work.
4. Forward navigation 1→2→3 preserves every edit.
5. Backward navigation 3→2→1 preserves every edit.
6. Moving 1→2 calls no approval endpoint.
7. Empty summary blocks; empty-after-trim also blocks.
8. Zero non-empty elements blocks; zero non-empty queries blocks.
9. No other condition blocks — assert the blocking list is exactly the three rules.
10. `1 / 3`, `2 / 3`, `3 / 3` are rendered text.
11. No screen renders "gate a", "gate", "decomposition", "capability", "core", "relationship",
    "coverage", "verification cell", "checkpoint", or a percent sign in a rating position.

### 8.2 Approval flow

12. **No search-provider call occurs before Stage 3 approval** — the graph-topology test from
    constraint 2, kept at full strength, plus a run-level assertion against the mock that the
    provider stub is untouched until the strategy approval commits.
13. **Only the user-approved queries execute.** Edit a proposed query, approve, and assert the
    dispatched query text is the edited one and that no un-approved proposal string appears.
14. After each approval the app transitions with **no page reload** — drive two consecutive
    approvals against a faked run API and assert the next stage renders.
15. An approval still never builds or calls the graph
    (`test_an_approval_never_builds_or_calls_the_graph` stays green).
16. A gate node still discards `interrupt()`'s return value and calls no approval writer (the
    AST test stays green for the two surviving gates).

### 8.3 Graph

17. An Advanced Search run ends after `explain` and never enters `normalize_publications`,
    `verify_features`, `gate_b`, `gate_c`, `draft_report` or `gate_d` — assert against the
    compiled graph's node set, not against a run transcript.
18. A search-only run traverses exactly the nodes it did before and emits exactly the same
    phases (`test_verification_integration.py`'s existing assertion, kept).
19. `search_orchestrator` has exactly two incoming edges, named.
20. `PHASES` and `PHASE_ORDER` agree in both directions.
21. The internal branch flag is `advanced_search`; `verification_requested` appears nowhere in
    active backend code, tests or fixtures.

### 8.4 Scores

22. `pct` appears nowhere in the API response, the stored row, or the JSON export — asserted by
    walking the serialized structure, not by checking named keys.
23. `top_score` and `verdict` likewise.
24. A stored report reads back in the order it was written, driven by `rank`.
25. The risk thresholds are pinned by value **and** at their boundaries: 75 → High, 74 →
    Medium, 26 → Medium, 25 → Low.
26. `tests/test_history.py`'s whole-key-set round-trip assertion updated to the new key set.
27. An empty result set renders the empty state and **no** risk rating.
28. Export tests re-pinned: PDF, Word and JSON contain no percentage and no "Match level".

### 8.5 API

29. Every surviving run route answers `404 RUN_NOT_FOUND` to a second user's token
    (`test_run_ownership.py` walks the catalogue — update the catalogue, keep the walk).
30. Every removed route is gone: a request to it 404s as an unknown path.
31. The result endpoint returns the standard report for a completed run, and a `409` naming the
    real status otherwise.
32. `POST /api/runs` accepts only `description` and optional `claims`; it refuses
    `problem_solved`, `inventor_novelty_statement`, `cutoff_date`, and every other unknown field,
    performs no legacy verifier-policy check, and returns no `capability_qualified` field.
33. Idempotent replay of each surviving approval returns the stored response.
34. A durable Advanced Search success creates exactly one standard History row and one terminal
    result carrying the same report object. Inject a `reports.py` persistence failure and assert
    that no History row, `COMPLETED` transition or terminal `result` survives the transaction.

### 8.6 Mock and acceptance

35. `python run_mock.py --workflow` drives the full flow at zero cost: admission → Stage 1 →
    Stage 2 → Stage 3 → search → standard report. The outbound-HTTP guard
    (`PaidCallAttempted`) stays armed; a boundary you forget to stub must fail loudly.
36. The LLM double keys off the real prompt markers (`DECOMPOSITION_PROMPT_MARKER`,
    `STRATEGY_PROMPT_MARKER`), imported not duplicated. `RELATIONSHIP_PROMPT_MARKER` goes with
    relationships. The strategy double still reads element ids **out of the prompt** rather
    than hardcoding them.
37. The acceptance journey covers the whole flow to export. Rename or replace
    `four_gate_acceptance.py` and `journey_acceptance.py` deliberately — the filenames are
    referenced in `CLAUDE.md` and acceptance documentation, and a stale transcript path is how
    an acceptance claim quietly stops meaning anything.
38. Real-process acceptance (`real_process_acceptance.py`) still kills and restarts the API and
    the worker mid-run, on its own ports, with the deterministic adapter.

---

## 9. Documentation — part of the change, not a follow-up

Update `CLAUDE.md` in the same change. The sections that are **false** after this work and must
be rewritten or removed:

- *Human-in-the-Loop: the gates, and who may resume a run* — five gates become two approval
  gates presented as three user-facing stages.
- *The verification branch* / *the approval boundary* — the approval boundary survives; the
  matrix, cells, `MATRIX_CELL_CAP`, `MATRIX_CONCURRENCY` and `RETRIEVAL_FLOOR` no longer gate
  any live path.
- *Results and evidence, and the final report* — those screens are gone.
- *The two human work requests* — gone.
- *Machine evidence is persisted* — the writes have no caller.
- *Section 6 traceability is a test* — see §10.1.
- *Gate F: the review screens* — five screens become three stages; update the spec count.
- *The four-gate mock* — three stages.
- *The evidence layer*, *`backend/verification/`*, *deployment profiles and capability
  qualification* — state plainly that the packages and standalone qualification tooling remain,
  but no compiled product graph or Advanced Search admission path calls them.
- Every reference to `pct`, `top_score`, `verdict`, "Match level", and the SSE `result` data
  shape.

Also add a short section describing **Advanced Search** as §2 defines it, and the revised
API/SSE contracts.

Write a decision record under `docs/decisions/` dated the day you do the work, recording: that
feature verification was withdrawn from the product, that numeric scores were removed from all
outputs, that legacy paused runs are not resumable, and who approved it. The repository's
convention is that a scope decision of this size is a recorded owner decision, not an
implementer's call.

---

## 10. Resolved implementation decisions

These are owner-approved scope decisions for this implementation. Do not reopen them as local
design choices and do not preserve the removed behaviour behind flags or compatibility shims.

**10.1 Retire verifier-matrix traceability.** Delete
`backend/tests/test_matrix_traceability.py`, `backend/acceptance/matrix_traceability.py`, and
the generated `docs/acceptance/matrix-traceability.md` together. Remove their references from
acceptance indexes and `CLAUDE.md`. Record openly that the 164-row traceability claim was
retired because the feature it traced was withdrawn; do not stub, skip, narrow, or regenerate
the table against a workflow that no longer contains the matrix.

**10.2 Remove regeneration from the active product.** Remove both regeneration endpoints,
their frontend client methods and affordances, their graph registrations, routes and active
tests. Editing the summary, elements and queries is the replacement. Keep the two legacy
`*_regeneration_requests` tables, their migrations and existing rows; never delete stored data
to tidy the product. The old node implementations may remain inside the uniformly retained
legacy subsystem from §10.3, but no compiled graph, route or acceptance journey may reach them.

**10.3 Keep the evidence/verification subsystem intact but unreachable.** Retain
`backend/verification/`, `backend/evidence/`, `graph/evidence_nodes.py`,
`graph/verification_nodes.py`, `graph/report.py`, `graph/work_requests.py`,
`workflow/evidence_store.py`, and `search/epo_fulltext.py` with their focused unit tests. Remove
their nodes from the compiled graph and remove product/API acceptance claims for them.
`CLAUDE.md` must label the whole subsystem as legacy, unreachable from the product, and retained
for historical reference and possible future work. Partial deletion is out of scope for this
change.

**10.4 Remove verifier-policy admission enforcement from Advanced Search.** `POST /api/runs`
must not call `evidence/policy.py::validate_admission_policies` or `require_capability`, and must
not return `capability_qualified`. Advanced Search uses neither the verifier capability nor
evidence date/text screening, so it cannot refuse admission because
`VERIFIER_CAPABILITY_ENFORCEMENT`, `VERIFIER_CAPABILITY_DEFERRAL_DECISION`,
`DATE_SCREENING_POLICY`, or `TEXT_VERSION_POLICY` is unset. Remove those requirements from the
Advanced Search deployment path, `.env.example`, acceptance setup and OpenShift manifests.

Keep the qualification runner, artifacts and policy module as standalone legacy tooling under
§10.3. The legacy `verification_runs.date_policy` and `text_version_policy` columns remain;
write an explicit constant such as `advanced_search_not_applicable` to both for new Advanced
Search rows and never expose those values as active product policy. Existing rows retain their
recorded values.

**10.5 Rename the branch flag now.** Rename `verification_requested` to `advanced_search`
through `GraphState`, the production adapter, graph routers, comments, fixtures and tests in
this implementation. This is an internal contract with one repository consumer, so add no
alias and no dual-read compatibility period. Make the rename as a focused commit within Gate 2,
before frontend integration, rather than leaving a known false name for later work.

---

## 11. Definition of done

- `python -m pytest` green from `backend/`, including the `postgres`-marked tests against the
  Compose database. **A skipped `postgres` run is a skipped run, never evidence that it
  passed** — those tests assert SQL properties the in-memory double structurally cannot
  exhibit, and migration `0024` is exactly the kind of change the double cannot see.
- `npm test` green from `frontend/`, with the new stage specs, and `npm run typecheck` clean.
- `python run_mock.py --workflow` drives admission → three stages → search → report → export
  with zero paid calls and the outbound-HTTP guard armed.
- The acceptance journey runs end to end and writes its transcript.
- A completed Advanced Search run appears exactly once in standard History; a forced report
  persistence failure commits neither that row nor a successful terminal workflow event.
- Migration `0024` applies and rolls back against the Compose Postgres, by hand, verified.
- A grep of the backend and the frontend for `pct`, `top_score`, `verdict`, `Match level`,
  `gate_b`, `gate_c`, `gate_d`, `verification_cells`, `MATRIX_CELL_CAP` returns only: the
  legacy migrations, the legacy tables, deliberately-retained dead code named in §10.3, and
  git history.
- The removed regeneration routes and frontend methods are absent, and Advanced Search starts
  with all four legacy verifier-policy environment variables unset.
- `CLAUDE.md` describes what the code does. Read the sections in §9 against the source, not
  from memory.
- A decision record exists under `docs/decisions/`.
- `verification_requested` is absent from active code and tests; any occurrence is confined to
  historical documentation that is explicitly labelled as such.

---

## 12. FINAL INSTRUCTION — read this before you start, and again at each gate

You are removing a feature that the repository documents at length as its most carefully
guarded work. Most of the guarantees in `CLAUDE.md` and the workflow spec exist because
something specific went wrong once: a credential printed to a browser console, a provider
outage recorded as a fact about a patent, a stably-wrong model answer that a stability-only
test passed, an approval that could be forged through an SSE resume value, a fenced worker
erasing the record of its own duplicate read.

**Narrowing the product's scope does not retire those lessons.** The scope shrinks; the
standards do not. Concretely, at every gate:

- If a step cannot produce a genuine result, it raises. There is still no fallback anywhere in
  this backend, and adding one back is the single most consequential way to break this
  project's contract with its users.
- A rating is a claim. Do not render one over an empty result set, and do not let a removed
  percentage come back as a nearby number wearing a different name.
- A removed test is a removed guarantee. Say which guarantee, and why it no longer applies.
- A deleted screen is not a deleted state. If a situation can still happen, it still needs to be
  reported as itself.
- `CLAUDE.md` is finished when it describes the code. Verify each paragraph you keep against the
  source; that file has gone stale against the code more than once, and this change touches
  more of it than any change before it.

When this document and the code disagree, read the code. When this document and `CLAUDE.md`
disagree about what exists **today**, read the code. When they disagree about what should exist
**after** this change, this document wins — and `CLAUDE.md` is your job to fix.
