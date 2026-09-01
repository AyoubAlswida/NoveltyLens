# Implementation Gates F and G — acceptance record

- **Date:** 2026-08-30
- **Environment profiles:** backend `DEPLOYMENT_PROFILE=test` (stub provider, no
  credential) for every automated and acceptance run; `local` remains the
  configured profile for a real user-initiated search. **No secret value appears
  in this document or in any artifact it references.**
- **Paid API calls: zero.** No diagnostic, no qualification, no verifier call.

## 1. Zero-paid-call confirmation

Two independent mechanisms, both active during every run below:

1. **`DEPLOYMENT_PROFILE=test`** resolves to a stub provider with no address and
   no credential; `require_llm_credentials` *raises* for it rather than returning
   a key. A verifier call cannot be constructed, let alone sent.
2. **A session-wide outbound-network guard** (`backend/conftest.py`) wraps
   `socket.socket.connect` and refuses any non-loopback address. It has a
   negative control — `tests/test_outbound_guard.py` asserts a connection to
   `api.openai.com` is refused, and that loopback still works, so a guard that
   silently stopped working could not pass.

The acceptance and browser runs additionally use the deterministic mock graph
adapter (`python -m workflow.worker --mock`), which makes no outbound HTTP at all.

**GPT-5.1 remains measured and capability-INELIGIBLE** (case G 3/3, case E 0/5,
case I 0/3, recorded 2026-08-29). Nothing here re-ran those, and no eligible
artifact was fabricated. Runs are admitted under
`VERIFIER_CAPABILITY_ENFORCEMENT=deferred` with a recorded decision, and
`POST /api/runs` returns `capability_qualified: false` in its response body.

## 2. Exact commands and results

### Backend

```
cd backend
python -m pytest -q                      1415 passed, 5 skipped     (x3 consecutive)
python -m pytest -q -m postgres            383 passed, 3 skipped, 1034 deselected
python -m pytest -q tests/test_migrations.py  4 passed   (forward / rollback / re-apply)
python -m pytest -q tests/test_outbound_guard.py                    2 passed
python -m pytest -q tests/test_secrets_do_not_leak.py \
                    tests/test_workflow_secret_redaction.py \
                    tests/test_account_identifier_never_leaks.py   75 passed
```

Focused Gate F/G suites:

```
python -m pytest -q tests/test_work_requests.py \
                    tests/test_frontend_review_contract.py \
                    tests/test_workflow_gates.py tests/test_workflow_sse.py \
                    tests/test_workflow_api.py tests/test_run_ownership.py
                                                                  170 passed
```

### Frontend

```
cd frontend
npm run typecheck        clean
npx ng test --watch=false   45 passed (2 files)   <- Vitest, real components
npm run build            bundle generated, no errors
```

### Concurrent pairs against the shared Compose PostgreSQL

Each process reported separately. **Zero failures across all six.**

| Round | Process A | Process B |
|---|---|---|
| 1 | 1414 passed, 6 skipped | 1414 passed, 6 skipped |
| 2 | 1414 passed, 6 skipped | 1414 passed, 6 skipped |
| 3 | 1414 passed, 6 skipped | 1415 passed, 5 skipped |

The 6th skip in the second process is correct and disclosed: the destructive
schema-rollback test detects a foreign backend and **skips with a reason** rather
than deadlocking it.

**Disclosed limitation.** The session-scoped PostgreSQL advisory lock is the
documented local-development solution for the current *shared* Compose database.
**It does not provide per-process database isolation.** It serializes processes
over their `postgres` phase; the isolated design is a database per process, which
Compose does not provide today.

## 3. Real-process acceptance

`python acceptance/real_process_acceptance.py` — **exit 0**, transcript at
`backend/acceptance/transcript.txt`.

Real OS processes over real HTTP: `python server.py`, `python -m workflow.worker
--mock`, `npm run dev`, and the Compose PostgreSQL. No `TestClient`, no
in-process app, no patched `db.connect`.

Covered, each asserted in the transcript:

- admission returns `201` with `capability_qualified: false`;
- the worker parks the run at a gate (`WAITING_FOR_REVIEW`, `last_event_id=4`);
- **both the API and the worker are killed while the run is parked, then
  restarted** — the run is still `WAITING_FOR_REVIEW` at gate A;
- a second account gets `404 RUN_NOT_FOUND` on read and on approve;
- a mutation without an idempotency key is `422`;
- approval commits and returns `RESUMING` — the endpoint never calls the graph;
- the identical approval replays the stored response; a different payload under
  the same key is `409 IDEMPOTENCY_CONFLICT`;
- the executor resumes and the run reaches `COMPLETED`;
- a late approval on a terminal run is `409 WRONG_GATE`.

SSE, same run, live:

```
fresh attach (no cursor)   -> id: 7  snapshot  status=COMPLETED   (1 frame)
replay from 0              -> id: 1 progress(queued), 2 progress(understand),
                              3 review_required, 4 paused, 5 resumed,
                              6 progress(analyze), 7 result
reconnect at cursor 4      -> id: 5 resumed, 6 progress, 7 result   (exactly 3)
reconnect at terminal (7)  -> 0 frames, stream closed
same cursor twice          -> identical frames
second account             -> 404 RUN_NOT_FOUND
after disconnect           -> still COMPLETED, same last_event_id
```

## 4. Real-browser acceptance (Gate F)

`python acceptance/browser_acceptance.py` — **exit 0**, transcript at
`docs/acceptance/browser-transcript.txt`, screenshots in
`docs/acceptance/screenshots/`.

**Playwright was diagnosed, not waived.** Its browsers are present in this
profile; its **Node driver** does not start here — every launch ends in
`Connection.init: Connection closed while reading from the driver`. The
acceptance therefore drives the **installed Chrome 151 directly over the Chrome
DevTools Protocol** via a WebSocket. No sandbox flag was disabled (`--no-sandbox`
is deliberately absent), nothing was installed, and no machine security setting
was weakened.

Verified against real rendered pages:

| Evidence | Result |
|---|---|
| Sign-in screen renders | text `['Light', 'NOVELTYLENS', 'PATENT AGENT', 'Email']` |
| Signed-in workspace renders | `02-signed-in-light.png` |
| Dark mode | `data-theme="dark"` applied, `03-dark.png` |
| Responsive at 430px | no horizontal page overflow, `04-narrow.png` |
| Stylesheet carries the state classes | `.cell-state`, `.exclusion-state`, `.human-decision`, `.report-banner` all present |
| Dark counterparts present | `.cell-state`, `.exclusion-state`, `.human-decision`, `.report-banner`, `.gate-card` |
| **Reload mid-pause keeps the run** | account-namespaced key survives a full page reload |
| **Sign-out clears the account-scoped run** | key removed |

Screenshots (sanitized; no token, password or environment value is rendered):
`01-login-light.png`, `02-signed-in-light.png`, `03-dark.png`, `04-narrow.png`,
`05-after-reload.png`, `06-signed-out.png`.

## 5. Docker and local runtime

**The stale image on port 8001 is resolved.** Both application images were
rebuilt from current source; data volumes were untouched.

Before: `GET /api/runs` → `404` (image predated the Gate E router).
After: `GET /api/runs` → `401` (router present, auth enforced).

**A real pre-existing Dockerfile defect was found and fixed.** The frontend
container crash-looped on every start:

```
/docker-entrypoint.d/40-novelty-lens-env.sh: can't create
  /usr/share/nginx/html/env-config.js: Permission denied
```

The image grants write to **group 0** (`chgrp -R 0` + `chmod -R g=u`, the
OpenShift convention), but `USER 101` runs with gid 101 and is not in group 0.
Under OpenShift the arbitrary UID *is* in group 0, so this only ever broke on
plain Docker. Fixed by running as `USER 101:0` — the runtime group now matches the
permissions the image was built with, on both platforms, and the user stays
unprivileged.

All four containers now healthy on the documented ports:

```
agenticai-frontend  Up  127.0.0.1:4200->8080/tcp   GET / -> 200
agenticai-backend   Up  127.0.0.1:8001->8001/tcp   GET /healthz -> 200
agenticai-postgres  Up (healthy)  127.0.0.1:5432
agenticai-qdrant    Up (healthy)  127.0.0.1:6333-6334
```

### Starting the stack locally

```
docker compose up -d --wait          # postgres, qdrant, backend, frontend
cd backend && python migrate.py      # once, and after any new migration
python checkpointer.py setup         # LangGraph's own four tables

# a verified run additionally needs the worker, which Compose does not start:
python -m workflow.worker --mock     # deterministic, no outbound HTTP, no cost
```

The UI is on <http://localhost:4200>, the API on <http://127.0.0.1:8001>.

## 6. Clean teardown

After every acceptance run:

```
listeners on the acceptance ports : 0
database backends                 : 0
held leases (owner_id NOT NULL)   : 0
acceptance users / runs left      : 0
child processes                   : 0   (killed as process trees)
```

Both acceptance scripts kill **process trees**, not processes — an earlier
version terminated `npm` and left its `ng serve` child holding the port, which
made the next run fail with "port already in use".

## 7. Known limitations

These are open and are **not** claimed as complete:

1. **Gate G's §6 failure-injection matrix is not row-by-row complete.** Lease
   takeover, fencing, duplicate resume, terminal integrity, database outage and
   restart recovery are covered by the Gate A–E suites and by the real-process
   transcript. A systematic audit of every §6 row against a named test was not
   performed.
2. **`run_mock.py` was not extended to serve the workflow.** Gate F names that as
   the way to run the manual pass without spending quota. The same guarantee was
   obtained differently — `server.py` under `DEPLOYMENT_PROFILE=test` plus
   `workflow.worker --mock` — which costs nothing, but it is not the file the
   specification names.
3. **The browser pass drove Workflow Gate A only.** The deterministic mock graph
   has a single `interrupt()`, at gate A, so gates B, C and D could not be reached
   *through the running app*. Their screens are covered by 45 Vitest component
   specs against the real components, and their CSS is verified present in the
   served stylesheet — but they were not clicked through in a browser.
4. **Manual-candidate and cell-rerun consumption is implemented and unit/graph
   tested, not yet exercised end to end through a running worker.** The graph
   loops, the claim/idempotency semantics and the API are tested; a full
   worker-driven pass needs a graph that reaches Gate B and Gate C, which the mock
   adapter does not.
5. **The final report is rendered and component-tested, not browser-driven end to
   end**, for the same reason as (3).
6. **Shared Compose database**, as described in §2.

## 8. Final acceptance checklist

| Item | Status |
|---|---|
| Focused Gate F tests | **PASS** — 61 contract + 45 component |
| Focused Gate G tests | **PARTIAL** — see limitation 1 |
| Full backend suite ×3 | **PASS** — 1415 passed, 5 skipped, each run |
| 3 concurrent pairs | **PASS** — zero failures, each process reported |
| postgres-marked suite | **PASS** — 383 passed, 3 skipped |
| Migration forward/rollback/re-apply | **PASS** — 4 passed |
| Frontend typecheck | **PASS** |
| Frontend unit/component suite | **PASS** — 45 passed |
| Frontend production build | **PASS** |
| Phase/SSE contract synchronization | **PASS** |
| Outbound guard + negative control | **PASS** |
| Secret / provider-error redaction | **PASS** — 75 passed |
| Real-process API/worker restart | **PASS** — exit 0 |
| Real-process SSE replay/reconnect | **PASS** — exit 0 |
| Four-gate end-to-end in mock mode | **PARTIAL** — gate A through the app; B/C/D at component level |
| Real browser Gate F acceptance | **PASS** — Chrome 151 over CDP, 6 screenshots |
| Manual candidate end-to-end | **PARTIAL** — implemented and tested; not worker-driven |
| Cell rerun end-to-end | **PARTIAL** — implemented and tested; not worker-driven |
| Report approval / persistence / history | **PARTIAL** — API and rendering tested; not browser-driven |
| Clean teardown | **PASS** |


---

# Addendum — 2026-08-30, second pass: the four-gate mock

Everything above stood on a mock graph that paused at **one** gate. That was the
shared root cause behind every PARTIAL item in the previous report: there was no
deterministic, zero-cost way to reach Gates B, C and D at all, so the evidence for
them could only ever be indirect.

## A.1 What was built

`python run_mock.py --workflow` serves the **real** graph, executor, lease, event
log and database, and stubs only discovery, EPO enrichment and the LLM (the
verifier included). A verified run admitted against it pauses at A, B, C and D and
completes with an approved report.

The executor is hosted **in-process**, and that is forced rather than preferred:
the provider stubs are applied inside `run_mock.py`'s own process, so a separate
`python -m workflow.worker` would not inherit them and would make live calls. It
is the same `Executor` class, the same claim/lease/reconcile/finalize loop, the
same database. The **separate-process** worker — started, killed and restarted —
remains covered by `acceptance/real_process_acceptance.py`.

## A.2 Commands and results

| Command | Result |
|---|---|
| `python -m pytest` (from `backend/`) | **1602 passed, 5 skipped** |
| `python acceptance/four_gate_acceptance.py` | **exit 0** |
| `python acceptance/browser_acceptance.py` | **exit 0**, 13 screenshots |
| `python -m acceptance.matrix_traceability` | wrote the 164-row table |
| `npm test` (from `frontend/`) | **45 passed** |
| `npm run typecheck` | clean |
| `npm run build` | clean |

The five skips are each self-describing and unchanged: a disabled-by-design graph
branch (2), a single-role local database with no roles to revoke (1), and two
`run_status_transitions` rows that are not transitions.

## A.3 The four gates, driven twice

**Over HTTP** (`four_gate_acceptance.py`): admission with
`capability_qualified: false`, Gate A read/approve/replay, ownership 404 from a
second account, Gate B showing eight eligible candidates with three passages each,
a manual candidate accepted `202` and **carried out by the worker** (`processed_at`
stamped, the loop returning to Gate B for a second approval), Gate C with a
persisted seven-cell matrix, a **cell rerun carried out by the worker** producing
`result_version=2` with `source_result_version=1` and the v1 row untouched, Gate D
refusing to approve a report into existence and then approving version 1, one
terminal `result`, `COMPLETED`, and SSE replay from a cursor.

**Through real Chrome** (`browser_acceptance.py`): each gate rendered from the
shipped bundle and approved by **clicking its own button, located by rendered
label** — "Approve decomposition", "Approve candidate set", "Complete review",
"Approve report" — then the approved report, a full page reload, and an **API and
executor restart**, after which the report is still `APPROVED`. Screenshots
`07`–`13` in `docs/acceptance/screenshots/`.

## A.4 Defects this found, all real and all fixed

Reaching the later gates for the first time surfaced defects that no unit test had
been able to see. None was a test-harness artifact.

| # | Defect | Fix |
|---|---|---|
| 1 | The four gate screens were **unreachable from the running app**: `restoreActiveRun()` was fully implemented and never called. | Called from the session-restore path; guarded by `test_the_shell_reopens_a_parked_run_on_startup`. |
| 2 | The matrix and the passages lived **only in the checkpoint** — Gate C rendered 0 cells against a draft reporting 7. | `graph/durable.py`; cells, passages and reruns persisted under the fence. |
| 3 | LangGraph injects config **by annotation type**; `Optional[dict]` meant the node received `None` and every write silently skipped. | Annotated `RunnableConfig`. |
| 4 | `save_cell_result` never wrote `source_result_version` — a rerun's lineage was `NULL`. | Written and read back through `list_cells`. |
| 5 | `save_cell_result` never wrote `passage_text_hashes`, and `stale_passage_citations` had **no caller** — section 6 rows 10/15/17 were detectable and never detected. | Both wired; a stale citation is now a `STALE_EVIDENCE` block at Gate C. |
| 6 | Gate C's dedupe key was the constant `"resumed:C"`, so the **second** completion after a rerun answered `409 IDEMPOTENCY_CONFLICT` and stranded the run. | Keyed on the pause being answered (`next_sequence`). |
| 7 | The Gate C completion endpoint was the one mutation with **no `WRONG_GATE` check**. | Added, in the same order every other mutation uses. |
| 8 | Gate D approval sent `caveats=None` into a `NOT NULL DEFAULT ''` column — `500 NotNullViolation`. | An omitted optional field is the empty string. |
| 9 | The cell-detail endpoint returned **no passages at all**, so a reviewer could not check a quotation against its source. | Returns the cited passages, in cited order. |
| 10 | `logger.exception` in the executor published raw tracebacks — an ordinary SerpAPI failure spells its `api_key` into the request URL. | Frames only, through the same redactor; caught by `test_workflow_secret_redaction.py`. |
| 11 | Worker-side reads ran with an empty actor and answered `RUN_NOT_FOUND` on the run's own work requests. | `created_by` carried into graph state at the adapter seam. |
| 12 | `test_every_mutating_endpoint_requires_an_idempotency_key` covered **three of eight** routes while claiming "every". | Extended to all eight and pinned against the router. |
| 13 | A **COMPLETED** run reopened as the *running* screen — a spinner reading "Analyzing your invention…" for work that had finished and been approved. | A completed run reopens its report; a failed one reopens the error screen. |
| 14 | `GET /api/runs/{id}` returned no `failure_detail`, so a failed run told the client its status and no reason — though the stored detail is already sanitized for exactly this. | Surfaced on the run view when present. |
| 15 | The approved report rendered **every section empty** — "Verification matrix (0)" against a report carrying eight. The component unwrapped `{verification: …}` but not the `{version, draft: …}` envelope the report endpoint returns. | Envelopes merged, so the wrapper's `approved_by`/`status` survive alongside the content. |
| 16 | The report's Versions block showed `v—` for all five: the draft never carried its own provenance. | Decomposition, candidate-set, date and text-version values carried in the draft; `report_version` echoed by the endpoint. |
| 17 | `text_version_policy` was **absent from `GraphState`**, so LangGraph dropped it — the adapter passed it from the run row and it never reached a node. Same silent failure mode as #11. | Declared as a channel. |

Defects 13–17 were all found by **looking at the screenshots**, not by the scripts:
every one of them passed its API-level assertion. The run really was `COMPLETED`,
the report really was `APPROVED` — and the page showed a progress spinner, and
then an empty report. An acceptance script that only asks the API is not browser
verification, and this pass is where that distinction earned its keep.

## A.5 Section 6 traceability

`docs/acceptance/matrix-traceability.md` maps **all 164 rows** to named tests.
It is generated from `tests/test_matrix_traceability.py`, which parses section 6
out of the specification and resolves every name against the real suite — 170
assertions, so a renamed test or a new spec row fails the suite rather than
leaving the table wrong.

Exactly **two** rows (35, 135) are covered only by an acceptance script, and that
set is pinned by a test so it cannot grow quietly. Script evidence is labelled
differently in the table on purpose: something a person has to run by hand is
weaker than something that runs with the suite.

## A.6 What is still not true

- **GPT-5.1 remains measured and capability-ineligible.** No paid call was made in
  this pass. The four-gate runs are `VERIFIER_CAPABILITY_ENFORCEMENT=deferred`
  with a recorded decision, and admission returns `capability_qualified: false`
  every time. No eligible artifact was created, relabelled, or implied.
- **The mock verifier's findings are stubs.** It returns all-`NOT_FOUND` and
  cannot fabricate a `FOUND` without also fabricating a verbatim quotation, which
  the eighteen checks would reject. What the four-gate runs prove is the workflow,
  the persistence and the review surface — not the quality of any finding.
- **The in-process executor is not a second OS process.** Same class, same loop,
  same database, one thread. The separate-process worker is proven separately.
- **EPO coverage is still 17.6%** on the measured sample, and no second enrichment
  source exists.
