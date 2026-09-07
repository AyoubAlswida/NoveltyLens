# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Keeping this file current

The LangGraph migration described in `docs/LangGraph-Multi-Agent-Architecture-Plan.docx`
(diagrams in `docs/assets/`) is **complete**, and the linear pipeline it replaced has been
deleted. Keep the Architecture section below describing what the code actually does: if
you add or remove a major dependency, data source, or persistence layer, or change the SSE
event contract, reflect it here before you finish the task, not as an afterthought.

**Verify against the source before trusting a paragraph here.** This file has gone stale
against the code more than once (see the SSE contract note below for one that survived a
full cycle). Where this file and the code disagree, the code wins and this file is the bug.

## What this project is

A patent novelty-search tool: a user describes an invention in natural language, the
backend generates search queries, retrieves candidate prior-art patents from live
external APIs, ranks them by similarity, and returns an LLM-written rationale per match
plus drafting recommendations. There are two independent processes — a FastAPI backend
and an Angular frontend — with no shared tooling; each is run and developed separately.

**Two ways to run a search.**

- **Quick Search** (`POST /api/analyze[/stream]`) — the description goes straight to the
  pipeline. Inline execution driven by the HTTP request: no run row, no lease, no event log.
- **Advanced Search** (`POST /api/runs`) — the same search, preceded by guided editing. The
  system proposes a technical summary and a focused flat list of searchable technical
  limitations; a person edits and approves them; the system proposes queries that combine
  compatible limitations; the person edits and approves those;
  the approved queries are then executed by the *same* pipeline and the *same* report is
  displayed. Durable: a run row, a lease, an event log and a separate worker process.

**What the product does not do**, as of 2026-09-01
(`docs/decisions/2026-09-01-advanced-search-three-stages.md`):

- **No feature-disclosure verification.** The five-gate reviewed workflow — the candidate
  set, the feature × publication matrix, its cells, the evidence passages and the drafted
  report — was withdrawn. Its code, tables, migrations and rows are all retained and
  unreachable; see "Advanced Search: the guided branch" below.
- **No numeric similarity score, and no overall novelty verdict.** `pct`, `top_score` and
  `verdict` are gone from the API, the database, the UI and every export. Cosine similarity
  is still computed — it is how ranking works — and is used for exactly two things before
  being discarded: it orders the matches, and it selects a per-patent High/Medium/Low
  **novelty risk** rating. A report with no matches carries **no rating at all**.

## Commands

**Backend** (from `backend/`):
- Install deps: `pip install -r requirements.txt`
- Run the API: `python server.py` — serves on `http://127.0.0.1:8001` (hardcoded host/port in `server.py`'s `__main__` block)
- Apply the database schema: `python migrate.py` (yoyo, against `settings.database_url`). Needed once after `docker compose up`, and again whenever `backend/migrations/` gains a file.
- **Then apply LangGraph's checkpoint tables: `python checkpointer.py setup`.** Separate on
  purpose — `PostgresSaver` owns `checkpoints`, `checkpoint_blobs`, `checkpoint_writes` and
  `checkpoint_migrations` and tracks its own version in the last of them, and two migration
  systems owning the same tables conflict the first time either side changes a column.
- Run the durable-workflow executor natively: `python -m workflow.worker` — a **second process**,
  not a thread in the API. `--mock` drives a deterministic no-HTTP graph; `--once` claims at
  most one run and exits. Setting `WORKFLOW_EXECUTOR=inprocess` instead hosts the same loop
  on a daemon thread inside `python server.py`, for local development only. Compose starts
  the same worker automatically as its `worker` service.
  See "Durable workflow execution" below.
- Compact finished runs' progress events: `python -m workflow.cleanup` (`--dry-run` first).
- **Run it without spending quota: `python run_mock.py`** (`--fast` to drop the pacing delays, `--port` to move it). Serves the real app on 8001 with only the metered boundaries stubbed. See the mock-mode section below before changing it.
- Run the tests: `python -m pytest` from `backend/` (config in `backend/pytest.ini`, which sets `pythonpath = .` because the modules import each other flat, e.g. `from config import settings`). The suite is `backend/tests/`. `backend/tests/conftest.py` stubs embeddings, Qdrant **and Postgres** (autouse, for every test) alongside the existing `_llm_chat`/`SOURCES` stubs — `compare.py`'s ranking, the semantic-search path and `persist_run` are all unconditionally on the happy path, so the suite still needs no credentials, makes no network calls, and needs no running database. `test_phase3_agents.py` covers Semantic Search, Fusion, and Validation individually; `test_history.py` covers persistence and `GET /api/history`; `test_auth.py` covers password hashing, token signing/expiry, and the endpoint gate. This is not yet the full P3.8 failure-path matrix (LLM failure, single-provider failure, Qdrant/embeddings outage, zero-similarity are covered for the parts that existed pre-Phase-3; a dedicated outage-injection suite for the new agents is still open).
- **The Postgres double is a dict, not a SQL engine.** `tests/fakes.py`'s `FakeReportStore` stands in at the `reports.py` seam, so it pins the *call* contract and nothing about the SQL. A CHECK constraint, a foreign key, or a column that does not exist is invisible to it — schema changes have to be exercised against the real Compose Postgres by hand.
- **Some tests need the real database and say so.** Everything marked `postgres` (`tests/test_lease_races.py`, `test_run_status_machine.py`, `test_event_authority.py`, `test_lease_lifecycle.py`, `test_terminal_replay.py`, `test_node_attempts.py`, `test_workflow_executor.py`, `test_migrations.py`, and the rest of the workflow suite) asserts a property of the SQL — a constraint, a trigger, the mandatory lock order, a lease race, an expiry boundary, a cascade — that the in-memory double structurally cannot exhibit. They **skip with a printed reason** when the Compose Postgres is unreachable or unmigrated, and a skipped run of them is a skipped run, never evidence that they passed.
- Still no lint config and no `pyproject.toml`.

**Frontend** (from `frontend/`) — **Angular 22**, standalone components, no NgModules:
- Install deps: `npm install`
- Dev server: `npm run dev` (alias of `npm start` / `ng serve`) — `http://localhost:4200`; no dev-server proxy is configured
- Build: `npm run build` — output in `dist/novelty-lens/`
- Typecheck: `npm run typecheck` (`tsc --noEmit`). Angular's build typechecks templates too, so a bad binding fails the build rather than failing silently at runtime — this is new; the React version had no `tsconfig.json` at all.
- Run frontend tests: `npm test` (Vitest through Angular's unit-test builder).

**Local infrastructure** (from the repo root): `docker compose up -d --wait` starts
Postgres, Qdrant, the API, the Advanced Search worker and the frontend, so `--wait`
returns only once they are actually serving:

- **Qdrant 1.18.3** (`127.0.0.1:6333` HTTP, `6334` gRPC) — the graph's `semantic_search`
  node reads it and `index_patents` writes it, neither with a fallback path.
- **PostgreSQL 17.10** (`127.0.0.1:5432`, user/password/db all `agenticai`) — holds the
  login accounts and every completed run.

After the first `up`, apply the schema once: `cd backend && python migrate.py`. That also
seeds the demo login (see the auth section below). Both services are **required to run the
backend at all**, not just for local convenience — a request writes its report to Postgres
before sending the result, and `GET /api/history` and the login lookup both read it.

**Postgres was removed in `15914d10` and brought back deliberately, not restored
wholesale.** What came back: `db.py`, `migrate.py`, `migrations/`, `reports.py`,
`GET /api/history`, and the frontend History screen. What did **not**: `cache.py`, the
`query_cache` table, and `find_recent_report`'s description-level report cache. Caching is
still the fresh design decision the previous version of this file called it — reusing a
stored report silently changes what a search returns, and nothing today does that. The
deleted caching code is in git history at `15914d10^`.

Keep the Qdrant image version in step with `qdrant-client` in `requirements.txt` — the
client warns when the server is more than a minor version away.

**Qdrant capacity.** Vectors live in RAM (1024-dim float32 = 4 KB/point): ~0.04 GB per
10k patents, ~0.4 GB per 100k, and ~3.8 GB per 1M. The current
`BAAI/bge-large-en-v1.5` model is 1024-dimensional. The container is capped at
`mem_limit: 2g`, which holds roughly 500k bare vectors, so a runaway
corpus fails in one place instead of taking the host down. **Disk is the
faster-growing number**: `on_disk_payload` is true and the payload stores each patent's
*full* text (~273 KB average) even though only the first 600 words are embedded — roughly
2.7 GB of disk per 10k patents. Growth is bounded by *distinct* patents, since
`store_patents` keys on `uuid5(patent.id)` and re-indexing overwrites rather than
duplicating.

**Running the app natively** still needs a separate `python -m workflow.worker` process.
Compose supplies that process as the restartable `worker` service. It is required for
Advanced Search —
`POST /api/runs` admits one (Implementation Gate E), and without a worker it sits `QUEUED`
for ever. Adding API replicas does not help, because the API never executes a graph.
Search-only runs (`POST /api/analyze`, `/api/analyze/stream`) still need only the API. The frontend calls the
backend at an absolute URL — `window.NOVELTY_LENS_API_URL` if set, otherwise
`http://127.0.0.1:8001` — and no dev-server proxy is configured, so by default the backend
must be running on that exact host/port. That constant lives in `frontend/src/app/config.ts`
and replaces the React build's `import.meta.env.VITE_API_URL`. Setting the global before the
bundle loads (a small script in `index.html`, or devtools) points the UI at another backend
**without a rebuild**, which is what the Playwright checks use to aim at a stub.

**Env vars** (backend, via `backend/.env` — see `backend/.env.example`):
`EMBEDDING_API_URL` (**required, no default** — ranking embeds on every request),
`EMBEDDING_API_FORMAT` (`openai` or `huggingface`, explicit rather than URL-inferred), and
`LENS_API_KEY` (Lens.org — **it is the only enabled provider**; `SERPAPI_KEY` is read but
Google Patents is commented out of `SOURCES`, and this file said the opposite until
Implementation Gate A corrected it), plus `QDRANT_URL` / `QDRANT_API_KEY`. **Two credentials with a fallback chain**:
`LLM_API_KEY` falls back to `HF_TOKEN`, and `EMBEDDING_API_KEY` falls back to `LLM_API_KEY`
then `HF_TOKEN`. The current `.env` binds `EMBEDDING_API_KEY=${HF_TOKEN}` explicitly so
the Aramco LLM key is never sent to Hugging Face. `EPO_CONSUMER_KEY` /
`EPO_CONSUMER_SECRET` are read but currently unused (the EPO provider is commented out —
see below). Optional: `LLM_API_URL`, `LLM_MODEL`, `EMBEDDING_MODEL`, and `TRACE=0` to
silence the per-step trace `backend/tracing.py` prints to stdout on every request (it is
**on by default**). `OPENAI_MODEL` and `OPENAI_REASONING_EFFORT` are the **selected local verifier runtime**
(owner decisions, 2026-08-31): **`gpt-5.4-mini-2026-03-17`** and `medium`. Both are part of the capability
identity, so changing either re-runs the §14.2.1 gate and invalidates every prior artifact.
**The accepted `OPENAI_REASONING_EFFORT` values are per model family, not global**:
`gpt-5.4-mini` takes `none|low|medium|high|xhigh`, `gpt-5.1` takes `none|low|medium|high`,
and `gpt-5`/`o1`/`o3`/`o4` take `minimal|low|medium|high` — `gpt-5.1` removed `minimal` and
added `none`, and `gpt-5.4-mini` kept `none` and added `xhigh`. **Prefix matching is at a
version boundary** (`deployment._matches_family`), so `gpt-5` cannot annex `gpt-5.4-mini`:
a bare `startswith` handed it the older family's vocabulary, allowance and timeout floor
silently, and because the families agree on the token parameter and on omitting
`temperature` — and `medium` is legal in both — nothing would have failed. A `gpt-5.x`
version with no declared row now matches **nothing** and falls to the classic dialect,
which a reasoning model rejects with a deterministic 400; failing loudly on an undeclared
model is the intended outcome and is the signal to add a row. It is checked
twice, because those are two questions: `read_reasoning_effort` validates against the union
of every declared dialect (itself *derived* from `MODEL_DIALECTS`, so it cannot claim a value
no dialect accepts), and `build_chat_payload` then validates against **the configured model's
own** vocabulary before the request is built. Both raise `ConfigError` **before** any paid
call, and a family that cannot honour the setting **refuses rather than dropping or
downgrading it** — a dropped setting runs at an effort the operator does not believe is in
force, and a substituted one puts a runtime nobody chose into the capability artifact. The durable-workflow settings — `WORKFLOW_EXECUTOR`,
`WORKFLOW_GRAPH_ADAPTER`, `LEASE_TTL_SECONDS`, `LEASE_HEARTBEAT_SECONDS`,
`WORKFLOW_POLL_INTERVAL_SECONDS`, `WORKFLOW_EVENT_POLL_MS`, `SSE_CLIENT_BUFFER`,
`SSE_WRITE_TIMEOUT_SECONDS`, `WORKFLOW_EVENT_RETENTION_DAYS` — all have working
defaults except `WORKFLOW_GRAPH_ADAPTER`, which deliberately has none. **The lease
pair is validated at import and the worker refuses to start on a bad one**: both must
be positive and the heartbeat strictly below the TTL, or the lease lapses between
beats and a healthy worker fences itself mid-run. `PATENTSVIEW_API_KEY`/`uspto_api_key` remains dead code — see the
USPTO note below.

## Architecture

### Backend: one pipeline, two branches

`psycopg[binary]` and `yoyo-migrations` are back in `backend/requirements.txt` and are
**live**: `db.py`/`reports.py` and `migrate.py` respectively. `rank_bm25` is gone along
with the rest of the Phase 2 pre-staging. `sentence-transformers` is not listed and would
be unused *by design* anyway: `backend/embeddings.py` calls a **hosted** model over the
network (see below), not a local `sentence-transformers` model — do not
"fix" this by importing it. `langgraph` and `qdrant-client` are live and load-bearing.
`langgraph-checkpoint-postgres` and `psycopg-pool` were added for the durable executor's
`PostgresSaver` (`backend/checkpointer.py`) — the Postgres saver is a separate package
from `langgraph-checkpoint`, and it pulls in the pool.
`scikit-learn` was removed from `requirements.txt` — `compare.py`'s TF-IDF fallback (the
only thing that used it) is gone.

**`backend/graph/` is the only pipeline.** The legacy generator (`_run_pipeline` in
`server.py`, plus the `PIPELINE` flag and `_active_pipeline` that used to select it) has
been deleted outright — not deprecated, not kept as a parity reference. `server.py` calls
`run_graph_pipeline` from `graph/pipeline.py` directly for both `POST /api/analyze/stream`
(SSE) and `POST /api/analyze` (drains the same generator, returns only the final JSON).
`graph/pipeline.py`'s own docstring says as much: *"The only pipeline: the legacy generator
this was once signature-compatible with, and the `PIPELINE` flag that chose between them,
are both gone."* `search_patents` (the legacy fan-out function) and the parity test it
backed (`test_pipeline_parity.py`) are gone with it; see the `backend/graph/` section below
for what actually runs. Every mention of "legacy" elsewhere in this file describing a live
fallback or a parity target is stale — treat it as history, not as code that still runs.

**The frontend consumes the real SSE stream.** This file used to say the opposite — that
the (then-React) frontend called the non-streaming `POST /api/analyze` and fake-animated
progress with a `setInterval`, unrelated to `progress.phase`. That was true of the old
`App.tsx` and is not true of the Angular app: `frontend/src/app/services/analysis.ts` does
a raw `authorizedFetch` POST to `/api/analyze/stream` and reads the SSE body directly (see
that file's own comment on why `EventSource` is unusable — it can carry neither a POST body
nor an auth header). `frontend/src/app/models/report.ts` declares separate chronological
phase orders for Quick Search and Advanced Search; both are subsets of the backend's
phase vocabulary and drive the running/error checklists for the selected branch. The SSE
contract is load-bearing production behavior now, not a spec kept correct for a future
wiring-up. Every event has a
`type` of `progress` | `result` | `error`. `progress.phase` is one of the values in
`backend/graph/events.py`'s `PHASES` tuple. The stream must end with
exactly one `result` event (data shape:
`patents[].{num,title,risk,rationale,key_overlap,url,source}` plus `title`, `date`,
`concepts`, `recommendations`, `query_count`) or a client reading
the stream would throw.

**Adding a field to `patents[]` is a three-place change, not one.** It has to be produced
(`graph/ranking.py`'s `format_matches`), *persisted* (`reports.py` — `save_report` writes it
and `list_history` reads it back, usually needing a migration), and declared on the
frontend's `PatentMatch`. Miss the middle one and the live run renders while the same report
reopened from History does not, because both go through the same component. That happened
with `key_overlap`; `tests/test_history.py` now asserts the whole `patents[]` key set
round-trips rather than named fields, so the next omission fails there instead of in the
browser.

### `backend/graph/` — the LangGraph pipeline

**Semantic Search, `index_patents`, and Fusion are currently disabled, all three
together**, per `graph/pipeline.py`'s own module docstring: *"TEMP: single-branch only."*
Their node registrations and edges are commented out in `build_graph()`, not deleted —
`keyword_search` feeds `validate` directly on a single edge. With `semantic_search` off,
`fuse` would have nothing to fuse and `index_patents` had nothing reading Qdrant to justify
writing to it, so all three went together. **This directly contradicts any paragraph below
(or elsewhere in this file) that describes them as live, parallel-running nodes — trust
this note and the code over that prose until someone re-enables them.** Re-enabling is
documented inline in `pipeline.py`: uncomment the three node registrations, the
`search_orchestrator -> semantic_search`, `keyword_search -> index_patents`,
`semantic_search -> index_patents`, and `index_patents -> fuse -> validate` edges, and
remove the direct `keyword_search -> validate` edge. The module-level descriptions of
`searching.py` and `fusion.py` immediately below describe what those nodes *do when
enabled* — accurate as design intent, not as what a request currently exercises.

- `state.py` — `GraphState`, the `TypedDict` every node reads and writes. It carries the
  `backend/models.py` dataclasses rather than re-flattening them. `total=False`, so later
  tickets can add keys additively. **It has no error field on purpose** — under the
  zero-tolerance policy a node that cannot produce a genuine result raises, and an error
  slot would invite nodes to record a problem and continue to a degraded answer.
- `events.py` — `PHASES` plus `emit_progress`, which **raises `SSEContractError` on an
  unknown phase name**, and the chronological `QUICK_SEARCH_PHASES` and
  `ADVANCED_SEARCH_PHASES` routes. The verification branch adds `decompose`, both review
  gates, and `strategy` before it rejoins the ordinary search route.
  `semantic_search` and `fuse` stay in the tuple even while their nodes are disabled —
  `emit_progress` validates against this vocabulary, not against which nodes currently run,
  and the phase names need to still be valid the moment those nodes are re-enabled. The
  frontend's phase orders in `frontend/src/app/models/report.ts` omit `semantic_search`
  and `fuse` (those nodes don't run, so the backend never emits them). **`filter` was
  missing too and has been added**: the live
  `keyword_search` node emits it on every search run, so the checklist was skipping a step
  the run genuinely performed. `tests/test_workflow_gates.py` and
  `tests/test_verification_integration.py` now assert the two lists agree in **both**
  directions — a phase the backend cannot emit stalls the checklist, and one it does emit
  that the frontend omits hides real work.

- `understanding.py` — Query Understanding + Query Expansion, over `llm.py` unchanged.
  Contains no `except`: a failed or malformed LLM response fails the request.
- `searching.py` — Search Orchestrator, Keyword Search, and Semantic Search. Keyword search
  fails the whole request if any one provider fails, via `search_all_providers_strict` in
  `backend/search/search.py`. It also enriches results with full text
  (`enrich_with_full_text`, skipped entirely when nothing was found) — no `except`, so an
  unreachable provider fails the whole request. **`index_patents_node` and
  `semantic_search_node` are defined here but not wired into the graph** (see the disabled
  note above); when re-enabled, Semantic Search queries whatever Qdrant already holds from
  *earlier* requests, not this request's own keyword hits, which aren't indexed yet when it
  runs. A Qdrant collection that doesn't exist yet (cold start) returns an empty list rather
  than raising (`search/vector_store.py`); an unreachable Qdrant still raises.
- `fusion.py` — Result Fusion, defined but **not currently wired into the graph** (see
  above). When enabled it runs Reciprocal Rank Fusion (`backend/ranking.py`,
  `reciprocal_rank_fusion`, pure and unit-tested directly) over the keyword and semantic
  result sets, deduped by patent ID. With fusion disabled, `validation.py` reads
  `candidates` (the keyword-search output) directly instead — see that module for why it
  needs no change either way.
- `validation.py` — Validation. One batched LLM call (`llm.validate_relevance`) judges
  relevance over the top of the candidate set (capped at `MAX_CANDIDATES = 30`, to keep the
  prompt bounded). No `except`: a missing or malformed verdict for any candidate fails the
  request rather than being treated as an implicit keep or drop. An empty candidate set
  skips the LLM call entirely rather than validating nothing.
- `ranking.py` — Ranking + Explanation. Calls `compare.py`'s embedding-based scoring (no
  TF-IDF fallback — see `compare.py` below) and `analyze_all_matches` with no `except`. The
  displayed score is the real one, truncated and clamped to 0–100 and nothing else — a
  dissimilar patent shows an honest `0`. Also holds `format_matches` and `verdict_for`,
  which builds the `patents[]` entries. Split into two node
  functions, `rank_node` (phase `compare`) and `explain_node` (phase `analyze`).

- `pipeline.py` — graph assembly (`build_graph`), the terminal `result` event, and
  `run_graph_pipeline`, the generator `server.py` consumes. **It catches nothing**: a
  raising node propagates out to `server.py`'s wrapper, which emits one `error` event.
  There is deliberately no point at which a partial result could be assembled and sent. The
  single "found anything?" routing check (`_route_after_validate`) runs after `validate`,
  since search and validation can each reduce the candidate set to zero.

Nodes emit progress through LangGraph's custom stream writer, which preserves call
ordering.

Zero results is **not** an error — reaching every provider and finding nothing is a
genuine answer, and so is validation filtering the candidate set down to nothing. The graph
skips ranking and explanation in either case (running them would emit
`Scoring 0 candidates…`, describing work that is not happening) and the report goes out
with real `concepts` and `query_count`, an **empty** recommendation list, and **no rating
at all** — nothing was scored, so nothing is rated, and printing "Novelty risk: Low" over an
empty result set would state a finding the run did not make.

### Advanced Search: the guided branch, and the approval boundary

**Feature-disclosure verification was withdrawn from the product on 2026-09-01**
(`docs/decisions/2026-09-01-advanced-search-three-stages.md`). What replaced it is
**Advanced Search**: a standard prior-art search preceded by guided editing. The system
proposes a technical summary and a focused flat list of searchable technical limitations;
a person edits and approves them; the system proposes queries that normally combine two or
three compatible limitations; the person edits and approves those;
the approved queries are then executed by the ordinary search pipeline and the ordinary
report is displayed. It performs **no** evidence verification, makes **no** legal
conclusion, and presents **no** numeric score.

**One conditional edge at `START` separates the two branches.** `_route_from_start` sends
an Advanced Search directly to the reviewable understanding proposal (`decompose`) and a
Quick Search to its one-shot LLM understanding (`understand`). Advanced Search does not run
Quick Search's `understand -> expand_queries` first: those calls produced throwaway queries
before the user had approved what the invention meant. **A Quick Search still traverses
exactly its original route and emits exactly its original phases.**

The flag is `advanced_search`, renamed from `verification_requested` with **no alias and no
dual-read period**: it is an internal contract with one repository consumer.

```
START -> understand -> expand_queries ----------------------┐
START -> decompose -> gate_a -> propose_strategy            │
       -> gate_strategy -> prepare_approved_search ----------┤
                                                            v
             search_orchestrator -> keyword_search -> validate
             -> rank -> explain -> END
```

`prepare_approved_search` performs no model call. It converts the approved summary,
limitations and query strings into the ordinary `InventionAnalysis`/`invention_text`
consumed by ranking and reporting, so downstream work uses reviewed content only.

**An Advanced Search run ends after `explain`, exactly as a search-only run does.** The
durable executor then persists the report and the terminal result atomically; see
"Machine evidence is persisted" below.

**Removed from the topology, not commented out:** `normalize_publications`, `screen_dates`,
`enrich_epo`, `build_passages`, `confirm_eligibility`, `rank_and_reduce`, `gate_b`,
`verify_features`, `gate_c`, `draft_report`, `gate_d`, `absorb_manual_candidates`,
`rerun_cells`, `regenerate_understanding`, `regenerate_strategy`, and the routers
`_route_after_explain` and `_route_after_eligibility`. The `# TEMP: disabled` style in
`_assemble` is reserved for the semantic-search trio, which is genuinely coming back; these
are not, and leaving them commented would invite someone to uncomment a path whose API,
screens and tests no longer exist. Git history is the record.

**The approval boundary survives unchanged, and is the point of the branch.**
`backend/workflow/approvals.py` is still the **only** module that writes an approved
artifact, and `tests/test_approval_boundary.py` enforces that at the AST level. A row in
`feature_decompositions` or `approved_search_strategies` exists because a named human, at a
recorded time, said it should. Reads filter `is_approved` **in SQL**: an unapproved row is
not fetched and then rejected, it is not fetched.

**The resume value is a signal, never a decision.** Each gate node discards `interrupt()`'s
return value and re-reads the approved artifact through those readers. It travels through an
HTTP request, an event row and a checkpoint; if it carried the approval, forging it at any of
those three points would be approving. `tests/test_workflow_gates.py` asserts at the AST
level that no gate node captures the return value and that none calls an approval writer.

**`search_orchestrator` has exactly two incoming edges** — `expand_queries` (the Quick
Search branch) and `prepare_approved_search` (the Advanced branch, reachable only after
`gate_strategy`). That is what makes "no provider call before both approvals" a property of
the graph rather than a convention, and
`test_no_provider_call_is_reachable_before_both_approvals` asserts the count by name. The
chain into it is now *tighter* than it was: `propose_strategy` is reachable only from
`gate_a`, and `gate_a` only from `decompose`, because regeneration is gone.

**What the two gates approve, and how it is stored.** The public payloads are flat
(spec §4.2): gate `A` carries `original_description`, `technical_summary` and
`technical_elements` (a list of **strings**); gate `STRATEGY` carries `queries` (a list of
**strings**). No capability objects, no `core`, no relationships, no per-element or per-query
ids, no enable/disable, no coverage claim. `api/runs.py` translates that into storage:

- one `feature_decompositions` row (one technical group, `F1`), one `decomposition_elements`
  row per element, ids minted **on write** as `E1..En`;
- **`core = TRUE` for every element.** The column is `NOT NULL` with no default and a
  reviewer no longer marks anything core, so every approved element counts. That is what
  makes the internal `NO_CORE_ELEMENT` condition **unreachable** rather than deleted and
  hoped for;
- `decomposition_relationships` gets **no rows**. Relationships are gone from the product,
  and `llm.regenerate_relationships`, `RELATIONSHIP_PROMPT_MARKER` and the
  `DANGLING_RELATIONSHIP_REFERENCE` check went with them;
- one `approved_strategy_queries` row per query, ids minted `Q1..Qn`, **every one enabled**.

Core semantics are **never exposed**: no API response, no screen and no export mentions
core, and `tests/test_flat_public_payloads.py` greps the two payload builders and fails on
a hit.

**Exactly three blocking rules, and nothing else blocks** (spec §3.5):

1. the technical summary must not be empty after trimming;
2. at least one non-empty technical element;
3. at least one non-empty search query.

`workflow/review.py` is the binding check; the reviewer's on-screen list pairs one-to-one
and in the same order with `GATE_A_RULES` / `GATE_STRATEGY_RULES`, and
`tests/test_frontend_review_contract.py` compares the counts. **Both sides shrink
together** — a check with no rule beside it refuses an approval for a reason the screen
never showed.

**The coverage rule is gone, and that is a real reduction worth stating.** It guaranteed
every core element was searched for, so "no prior art found" could not rest on a search
nobody performed for part of the invention. Advanced Search makes no such guarantee and
states no such conclusion: it shows the queries to a person and asks them to approve them.

**The inventor's description is never rewritten.** Not by a proposal, not by an edit, not by
an approval. `verification_runs.description` is the source input; the approved summary is
stored **beside** it. A test asserts the description is byte-identical after an approval,
and the stage-1 screen renders it as a `<p>`, never as a form control.

### Persistence: PostgreSQL stores completed runs and the durable-workflow schema

**Thirty-four tables now, created by `backend/migrations/` (yoyo, applied with
`python migrate.py`), plus four more that yoyo deliberately does not own.** The first four
below are the original search schema and are the only ones the *search* endpoints touch.
Migrations `0006`-`0023` add the durable-workflow schema; `0021` adds the invention
understanding stage's three tables and the two optional Stage 1 columns on
`verification_runs` (see "The Invention Understanding stage" below), and `0022`/`0023` add
the search strategy stage (see "The Search Strategy stage").

**`0022` exists only to add one enum label, and it is deliberately non-transactional.**
`ALTER TYPE ... ADD VALUE` cannot have its new label *used* by any statement in the
transaction that added it, and yoyo wraps each migration in one — so the label
(`workflow_gate` gains `STRATEGY`) is added and committed by itself, and `0023` creates the
tables in a later transaction. The forward file carries `-- transactional: false`, which is
the form that is safe on every PostgreSQL version rather than only on 12+. Its rollback is a
documented **no-op**: PostgreSQL has no `ALTER TYPE ... DROP VALUE`, and removing one would
mean rebuilding the type and re-pointing every dependent column *and function signature*
(`pause_run_for_review` takes a `workflow_gate`) — a destructive rewrite in the rollback of
a purely additive change. The label is inert with no row using it, `0006`'s own rollback
drops the type wholesale, and re-applying `0022` is a no-op by `IF NOT EXISTS`. Verified by
rolling `0023`/`0022` back and re-applying against the Compose database.

- **`patents`** — every patent that has appeared in a stored report, keyed by the same
  `id.strip().upper()` `merge_patents()` dedupes on. **It is not a cache.** Nothing reads
  it to skip a provider call; it exists so `search_run_matches` can render a title months
  later. Its `full_text` column is preserved on conflict when a later write carries an
  empty one, so a bare record can never erase an enriched one.
- **`search_runs`** — one row per completed run, carrying everything the `result` event's
  data object holds except the per-patent rows. It stores `description`/`claims` rather
  than the payload's `title`/`date`, because those two are display values (`"Today"` is
  literally hardcoded) and meaningless read back weeks later.
- **`search_run_matches`** — one row per `patents[]` entry, in rank order, FK'd to both.
- **`users`** — login accounts. See the auth section below.

The durable-workflow tables, from migrations `0006`-`0015`. `backend/workflow/` is their
only read/write surface, the way `reports.py` is for the four above:

- **`verification_runs`** — one reviewed workflow execution, owned by `created_by`. Carries
  the event-log sequence allocator and the replay watermark. **Not** a search-only run;
  `search_runs` still records those.
- **`run_status_transitions`** — the nine permitted status *updates*, as data. A trigger
  reads it. Creation is absent by design (see the section below).
- **`run_leases`** — one permanent row per run: the fencing counter. Never deleted while
  the run exists.
- **`node_attempts`** — one row per *physical* execution attempt, scoped by
  `execution_key`. At most one `COMMITTED` per logical execution; `FENCED` rows are kept so
  a duplicate provider read is visible rather than hidden.
- **`workflow_events`** — the replayable per-run event log. Closed type vocabulary, one
  terminal, nothing after it.
- **`idempotency_records`** — bookkeeping for the §8.2 transaction. Keyed by the
  authenticated user as well as the resource.
- **`feature_decompositions`**, **`decomposition_elements`**,
  **`decomposition_relationships`**, **`decomposition_requirement_ids`** — the
  human-**approved** decomposition only. Machine output is never stored here.
- **`candidate_sets`**, **`candidate_set_members`** — the human-**approved** candidate set.
  A ranked list is machine output and is not one of these.
- **`publication_dates`**, **`publication_exclusions`** — date screening and the three
  publication-scoped `EXCLUDED_*` states. An exclusion is never a matrix cell.
- **`evidence_passages`** — the passage of record. A CHECK allows only `CLAIMS` and
  `DESCRIPTION`, so no application bug can route an abstract into a prompt.
- **`verification_cells`**, **`verification_results`** — the matrix and its immutable
  machine results.
- **`cell_reviews`**, **`cell_waivers`**, **`review_audit`** — human overlays stored
  *beside* the machine result, never over it. `review_audit` is append-only.
- **`report_versions`** — draft and approved report versions.
- **`search_strategy_proposals`** — the search stage's machine output and human edits.
  **No `is_approved` column and no approver columns**, exactly like
  `understanding_proposals`: a row here cannot record an approval because there is no field
  in which one could be recorded. `workflow/strategy.py` is its only writer.
- **`approved_search_strategies`**, **`approved_strategy_queries`** — the human-**approved**
  strategy, and one row per approved query. **Disabled queries are stored, not dropped**:
  "we deliberately did not search for this" is part of what was approved and part of what
  the report has to be able to state. `covered_element_ids` is the coverage claim.
- **`strategy_regeneration_requests`** — the work request behind
  `POST /api/runs/{id}/strategy/regenerate`.

**Four more tables belong to LangGraph, not to yoyo**: `checkpoints`, `checkpoint_blobs`,
`checkpoint_writes` and `checkpoint_migrations`, created by `python checkpointer.py setup`.
See the durable-workflow section for why two migration systems must not share them.

`backend/reports.py` is the whole read/write surface: `save_report` and `list_history`.
`backend/db.py` is a one-function psycopg3 helper (autocommit connection per request);
`save_report` opens its own transaction inside that, because a half-inserted run would
render in History as a truncated report indistinguishable from a genuine short one.

**Writes happen before the `result` event is sent.** `graph/pipeline.py`'s `persist_run`
is called between `build_result_event` and the `yield`, with no `try` around it — that is
the zero-tolerance policy applied to persistence. A database that cannot store the run
fails the request rather than reporting a successful search whose report was dropped on
the floor. A run that found nothing is stored too: "we searched and there was no prior
art" is a real answer and belongs in History next to the others.

**`DELETE /api/history` (`clear_history`) is the one destructive endpoint.** It drops every
`search_runs` row — matches follow through `ON DELETE CASCADE` — and then the `patents`
rows no run references any more. That second statement matters: with no query cache, the
`patents` table exists *only* to give a stored match its title and url, so skipping it
would leave "clear all" quietly holding the full text of everything ever searched. It is
**not scoped to the caller**, because there is one seeded account and no ownership column;
**if accounts ever become per-user this needs a WHERE clause before it ships**, or one
user's Clear all wipes everyone's history.

**There is still no caching of any kind**, and re-adding it is a design decision, not a
restoration. See the Local infrastructure section for exactly what was and was not brought
back from `15914d10^`.

### Durable workflow execution: the executor, the lease, and the event log

**`POST /api/runs` now admits a durable run, the gates are wired into the graph, and
`backend/api/` serves the verified-run surface** (Implementation Gates D and E).
`POST /api/analyze` and `/api/analyze/stream` are unchanged: same inline execution, no run
row, no lease, no event log, and **no `id:` line** (spec §3.2).

Below is what Gate A built and still describes accurately.
`POST /api/analyze` and `POST /api/analyze/stream` keep their exact behaviour — inline
execution driven by the HTTP request, no run row, no lease, no event log, no `id:` lines —
and there is no `POST /api/runs` to create a durable run with. What exists is the machinery
a *verified* run will need, built and tested ahead of the graph that will use it
(Implementation Gate A of
`docs/prompts/integrated-feature-verification-workflow-implementation.md`).

**`server.py` is not untouched, and it is worth being precise about how.** It participates
in exactly one way: as an **optional local host** for the executor. Its `lifespan` reads
`WORKFLOW_EXECUTOR`, and on `inprocess` it starts the executor loop on a daemon thread and
stops it at shutdown. On `worker` — the default, and production — it starts nothing. No
request handler executes a graph on the executor's behalf, no endpoint's behaviour changed,
and `BackgroundTasks` is not used for any of it. Production runs `python -m workflow.worker`
as a separate workload; the in-process host exists so a developer can drive the whole thing
from one terminal.

**The standalone verifier is still not connected to any of this.** `backend/verification/`
is complete and tested, and nothing in `graph/`, the executor, or the API calls it — see its
own section below for why that is deliberate at the end of Gate A.

**Why it exists at all.** A LangGraph Postgres checkpoint stores state; it does not
continue execution. Nothing in LangGraph restarts a run after the process driving
`graph.stream()` goes away — and in the search pipeline that driver *is* the HTTP request,
so a closed browser tab ends the run. That is fine for a 60-second search and fatal for a
run that pauses for human review measured in minutes to days.

**The executor is a separate, database-driven loop** (`backend/workflow/executor.py`):
poll → acquire a lease → **reconcile** → execute → transition → release.

- **`python -m workflow.worker` is a second workload**, deployed alongside the API
  (`docker-compose.yml`'s `worker` service and `openshift/06-worker.yaml`, same backend
  image with the command overridden). Without it a run admitted
  by the API sits `QUEUED` forever; adding API replicas does not help, because the API
  never executes a graph.
- **`WORKFLOW_EXECUTOR=worker|inprocess` selects the host process, not the
  implementation, and `server.py`'s `lifespan` honours it.** `inprocess` starts the
  identical loop on a daemon thread at API startup and stops it at shutdown — still
  lease-protected, still database-driven, still recovering after a restart — and refuses
  to start a second one in the same process. `worker` (the default) starts nothing at
  all. **This is not the deleted `PIPELINE` flag pattern**: that flag chose between two
  parallel implementations; this chooses where one implementation runs, and
  `InprocessExecutor` is a thin host around the same `Executor` class rather than a
  second one. An unknown value, an invalid lease pair, or `inprocess` with no
  `WORKFLOW_GRAPH_ADAPTER` **fails API startup** — there is deliberately no mock
  fallback, because silently substituting the deterministic adapter would let a
  deployment believe it was running real verification while it fabricated everything.
- **FastAPI `BackgroundTasks` is forbidden as the executor**, even temporarily. It dies
  with the process, has no lease, no recovery and no visibility. A test greps for it.
- **The worker refuses to start** on an invalid lease configuration or with no
  `WORKFLOW_GRAPH_ADAPTER` configured. Neither is silently defaulted: a default that
  quietly overrides a deliberate setting is how a misconfiguration survives a deploy, and
  an executor with no graph would claim runs it cannot advance. There is no verification
  graph yet, so `python -m workflow.worker --mock` drives a deterministic adapter that
  makes no outbound HTTP calls.

**Run states**: `QUEUED`, `RUNNING`, `WAITING_FOR_REVIEW`, `RESUMING`, `COMPLETED`,
`FAILED`. **Creation is not a transition** — a run is *born* `QUEUED` by insert invariant,
because a primary-key column is implicitly `NOT NULL` so a `(NULL, 'QUEUED')` row cannot
exist, and a synthetic `NEW` state would put a value in the enum nothing ever observes.
The nine permitted **updates** live in the `run_status_transitions` table and are enforced
by a trigger; `COMPLETED` and `FAILED` have no outgoing rows at all, which is what makes
"terminal" a database property. **A lease takeover does not change status** — status tracks
workflow position, the lease tracks who may advance it.

**The lease is a permanent fencing counter, not a record of the current holder.** One row
per run, created *with* the run at token 0, released by **clearing the holder**, and
removed only by the run's cascade. Release leaves `fence_token` exactly where it was, so a
superseded token is burned forever. An earlier design deleted the row on release and the
next acquisition inserted a fresh one at token 1 — and a recycled token is
indistinguishable from a live one, so a worker fenced at token 3 whose `owner_id` happened
to regenerate identically could commit under authority it never held.

- **One expiry boundary, everywhere.** Active is `expires_at > now()`; claimable is
  `expires_at <= now()`. Exact negations, so there is no instant at which the old lease is
  inactive and the new worker cannot yet claim it.
- **A heartbeat extends an active lease and can never revive a dead one.** Reactivation is
  `acquire_run_lease`'s job alone, and it always increments. A failed beat returns `FALSE`
  and changes nothing at all — the documented asymmetry, because it runs on a thread whose
  job is to answer "do we still hold this?". Every *other* lease-sensitive path raises
  `FN001`.
- **A stale or repeated release raises `FN001`**, not a silent no-op: a worker releasing a
  lease it no longer holds has been operating on a false belief about its own authority.
- **Nothing deletes a lease row.** There is deliberately **no** `BEFORE DELETE` trigger —
  a row-level one fires for cascaded rows too and would have made deleting an approved
  parent run impossible. The guarantee is a source contract plus privilege revocation where
  the deployment has roles.

**One mandatory lock order, held to commit, in every lease-sensitive operation:**
`verification_runs FOR UPDATE`, then `run_leases FOR UPDATE`. Holding the lease row — not
merely reading it — is what makes fencing hold for the whole transaction; validating and
then writing leaves a window a takeover slips through. One order also prevents deadlock,
and `tests/test_lease_races.py` asserts both against real PostgreSQL with two connections.

**Four authorized event writers, and no generic append.**

| Writer | Authority | May append |
|---|---|---|
| `create_run` | admission — no lease exists yet | the seq-1 `queued` progress event, with the run row |
| `append_worker_event` | worker, **fenced**, `RUNNING` only | `progress`, `verification_progress` |
| `pause_run_for_review` | worker, fenced | `review_required` + `paused`, with the transition and the release, in one transaction |
| `append_api_event` | API, **ownership-scoped** | `resumed`, and nothing else |
| `finalize_run_success` / `finalize_run_failure` | worker, fenced **and atomic** | `result` / `error` — the only terminal writers |

A generic `append_workflow_event(run_id, type, ...)` would take no owner and no fence
token, so a fenced worker could keep writing and a terminal event could exist without the
status transition that belongs with it. Both are execution-integrity holes, and a test
greps the backend to keep it deleted. `is_terminal` is a CHECK-enforced function of the
event type, a partial unique index allows **at most one** terminal per run, and nothing of
any type may be appended after it. **`workflow/events.py` is the only module that writes
`workflow_events`**, the way `reports.py` is the only module that writes the search tables.

**What the lease guarantees, and what it cannot.** At most one current holder, fence tokens
that only ever increase, exactly one authorized state writer, and a fenced worker that
persists nothing. It does **not** make external calls exactly-once: a worker can lose its
lease while blocked inside an outbound call, and that call still completes and is still
billed. **External calls are at-least-once across recovery.** Guard the commit, record the
attempt, disclose the duplicate read — and never write "exactly once" about this system.

`node_attempts` is what makes the duplicate read *visible*: one row per **physical**
attempt, carrying an `execution_key` that identifies the **logical** invocation. At most one
attempt per key may be `COMMITTED` (partial unique index). Scoping by `(run_id, node)`
alone would forbid every legitimate repeat — a cell rerun, a second `result_version`, a
re-verification against changed passage text, an ordinary LangGraph loop. For a graph node
the key is the **stable checkpoint task id**, which LangGraph keeps identical across a crash
recovery and across a resumed interrupt. The executor derives it from trusted graph state;
a client-supplied key is rejected.

**All four attempt mutations are fenced** — opening, committing, failing and closing —
through SQL functions taking the same mandatory lock order (migrations `0007` and `0016`).
Only `commit_node_attempt` was fenced originally, and the gap that mattered was
`fail_node_attempt`: **`FAILED` and `FENCED` are different facts**, so a stale worker
marking its own attempt `FAILED` overwrote the `FENCED` reconciliation was about to apply
and erased the very duplicate-read record this table exists for. A fenced worker now gets
`FN001` and leaves its attempt `RUNNING`, which is exactly the state
`close_superseded_node_attempts` looks for — and that function is itself fenced, so the one
operation whose purpose is recording who lost authority is authority-checked too. **A
fenced worker never writes telemetry using the authority it just lost**, and the marking is
always done by the *new* holder under its own valid lease.

**Reconcile runs before anything else on every acquisition**, because LangGraph commits its
checkpoint in its own transaction which this work does not join. Three crash boundaries
exist between that commit and ours, and one `pause_run_for_review` call repairs all three,
because the dedupe keys are functions of the **checkpoint** rather than of the attempt. The
event log is repaired **forward only** — never rewritten, never deleted, never renumbered,
because a client that already received event 17 must never be told 17 now means something
else.

**Idempotency is a transaction, not a unique index** (`workflow/idempotency.py`). A unique
index prevents a duplicate row; it cannot make a multi-statement mutation replay-safe,
cannot return the original response body, and cannot tell a retry from a conflicting reuse
of the same key. The loser of a race **blocks** on `SELECT … FOR SHARE` until the winner
commits, so concurrent identical requests produce exactly one mutation, and a rolled-back
transaction leaves no record and frees the key.

**Every run is owned.** `verification_runs.created_by` is the ownership key, and every
run-scoped function in `workflow/store.py` takes an `actor_email` and filters on it *in the
same query that fetches the run* — never fetch-then-compare in Python, where a missed
comparison is a data leak while a missed `WHERE` clause is merely a no-result. Another
user's run answers `RUN_NOT_FOUND`, never 403. Executor-only readers are named `_system_*`
and a test asserts the HTTP surface never reaches one. **With one shared seeded account
this scoping is real but attributes every decision to the same person** — a disclosed
limitation, not a solved problem.

**Every persisted failure is sanitized at the last boundary** (`workflow/redaction.py`).
The executor writes a failure's text into the terminal `error` event and into
`verification_runs.failure_detail`, and the API reads both back — so that is where an
untrusted exception is scrubbed, *in addition to* whatever the provider did. It has to be:
`_safe_detail` originally returned the first line of `str(error)` unchanged, and an ordinary
SerpAPI quota failure carries the request URL, which carries the key. Credential-bearing URL
parameters, DSN userinfo, `Authorization` headers, bearer tokens, response bodies and
tracebacks are all removed; the actionable facts — provider, HTTP status, workflow step —
survive, because "SerpAPI returned 429" is what makes a stored failure diagnosable at all.
**Those facts no longer reach the end user**: the frontend renders its own copy from
`shared/public-errors.ts` and treats the sanitized detail as diagnostic data (see the
`error` screen below). Redaction still matters exactly as much — the detail is persisted to
`verification_runs.failure_detail`, written into the terminal `error` event, logged, and
served back by the API, so an unscrubbed credential would reach all four. A
`WorkflowError` is *trusted* and keeps its (redacted) message, since the SQL functions
interpolate run ids, statuses and tokens and nothing else; any other exception is **rebuilt**
from allow-listed facts rather than passed through, because pattern-matching is never
complete. Redaction never softens the failure: a sanitized failure is still `FAILED`, with
its code.

**No in-memory fallback anywhere.** If the lease store, the checkpointer or the event log is
unreachable, the worker **stops**. It does not continue in memory and it does not present
success. There is no `error` event in that case either, and saying otherwise would be a
claim the system cannot honour — an `error` event cannot be persisted while the event log
is unreachable. The run keeps its last committed state, and a later reconcile records the
sanitized infrastructure failure when there is finally somewhere to write it.

**`CheckpointUnavailable` means the checkpointer, and only the checkpointer.** The
`checkpointer()` context manager guards construction and teardown; the `yield` is
deliberately *outside* the guard. An earlier revision wrapped the body too, so every
exception a caller raised came back through the generator and was relabelled — a
`FencedError` (a worker correctly discovering it lost its lease) was reported as a database
outage, and so was a plain `ValueError` from a graph node. That changed *behaviour*, not
just wording: `run_forever` treats `CheckpointUnavailable` as "persistence is gone, wait and
retry" rather than "this run failed". Its message is built from the **parsed** DSN — host,
port, database — never echoed from the driver, so no password or query-string credential can
reach a log or a stored `failure_detail`.

**The checkpoint is not the event log, and LangGraph owns its own tables.** A checkpoint
answers "where is execution?"; `workflow_events` answers "what has the client been told?".
`PostgresSaver.setup()` creates and migrates `checkpoints`, `checkpoint_blobs`,
`checkpoint_writes` and `checkpoint_migrations`, tracking its own version in the last of
them — **yoyo deliberately does not own them**, because two migration systems on the same
tables conflict the first time either side changes a column. Run it separately:

```
python migrate.py            # yoyo, backend/migrations/
python checkpointer.py setup # LangGraph's four checkpoint tables
```

**`config.Settings` keeps every credential out of its own `repr`.** `repr(settings)` used
to print five live credentials — `HF_TOKEN`, `SERPAPI_KEY`, `LLM_API_KEY`,
`EMBEDDING_API_KEY` and `AUTH_SECRET` — so anything that rendered the object carried them: a
pytest failure report, a log line, an f-string in an exception. It was found exactly that
way, by an unrelated test failure printing the real keys. Every credential-bearing field is
now `field(..., repr=False)`, `database_url` included because a DSN carries its password in
the userinfo. `workflow/redaction.py` scrubs at the *persistence* boundary and never sees an
object's own string form, so these are two different defences and both are needed. A new
secret field needs the same declaration — `tests/test_secrets_do_not_leak.py` asserts it on
the dataclass, not on today's values, because an empty field would pass a value check while
still leaking wherever it is set.

### The three stages, and a real frontend test runner

`frontend/src/app/screens/advanced-search-stages.ts` is **one component owning all three
stages**, plus `gate-shell.ts` (the shared frame, the **twelve** states, `STAGE_LABELS` and
`classify`). It replaced `understanding-review.ts` and `strategy-review.ts`, which replaced
one generic editor before them.

**Why one component for three stages.** Stages 1 and 2 are a single backend approval (the
understanding) and stage 3 is a second one (the strategy) — two approvals, three screens —
but the three screens are one flow a person walks forward and back through, so the drafts
live in one place. Splitting them would put the drafts behind a router or a shared store
purely so that pressing Back could find them again.

- **Stage 1** — the inventor's description read-only beside an editable technical summary.
- **Stage 2** — a flat, editable list of searchable technical limitations: add, edit,
  delete. The machine aims for 4–7 compound limitations, keeps relationships/triggers in
  the same line, and uses fewer rather than padding with generic components.
- **Stage 3** — a plain, editable list of search query strings. The proposal normally
  combines 2–3 compatible approved limitations per query instead of emitting one query per
  isolated noun.
- Each renders `1 / 3`, `2 / 3`, `3 / 3` as **rendered text**, not an aria-label.

**Navigation preserves every edit.** Only two transitions mutate anything: leaving stage 2
the first time, and leaving stage 3. `1 -> 2` calls no
approval endpoint, which is asserted rather than assumed — a stage that quietly approved on
its way past would make "Next" an approval the reviewer never recognised as one.

**The shell owns one durable SSE watcher for the whole run.** After the understanding
approval the component emits which approval committed; the shell reconnects from the last
event id and changes the same mounted component's `gate` input when STRATEGY pauses. The
component does not run its own poller, so there is one authority for progress, pauses,
terminal results and failures. Keeping the component mounted preserves stage 1 and 2 drafts.
`understandingApproved` makes a later `2 -> 3` plain navigation, so walking back and forward
cannot attempt a second approval.

**Twelve states, and collapsing any two would report one situation as another** — the
backend's zero-tolerance policy, moved into the UI: `loading` (not `empty`), `empty` (a real
result, not a failed fetch), `review`, `validating`, `waiting`, `resuming` (the executor has
*not* picked it up yet), `blocked` (**every** condition listed), `stale` (never resubmitted),
`forbidden` (`RUN_NOT_FOUND`, and the screen does not guess which kind), `reconnecting`
(**a client problem, never a run failure**), `failed`, `completed`.

`paused` was removed **by proof, not by guess**: it meant "parked at a gate, shown from the
running screen", and no surviving screen sets it — the SSE `paused` *event* is a different
thing the running screen handles itself. None of the other twelve were collapsed.

**A run parked at a withdrawn gate fails honestly.** A run left at `B`, `C` or `D` before the
withdrawal still holds that value in `verification_runs.current_gate`, and the backend still
returns it. `WorkflowGate` is narrowed to `'A' | 'STRATEGY'` — what the app renders — while
the wire field is typed `StoredGate`, which is wider. That split is what forces the
`@default` arm in `app.ts` that says "This run cannot be continued" rather than rendering a
blank review screen. The run's data is retained; there is simply no way to carry it forward.

**`npm test` runs Vitest through `@angular/build:unit-test`** — **50** specs covering the API
client, the SSE reader, the cursor, account-namespaced run persistence, the three stages and
the report. `vitest.config.ts` pins `pool: 'threads'` because the default `forks` pool never
starts a worker on this machine. `@types/node` is a declared devDependency: `tsconfig.spec.json`
requires the `node` type library, and without it the specs do not compile after a clean install.

`backend/tests/test_frontend_review_contract.py` remains as a **second** layer for what a
component test cannot see: the dark-mode pairing for every state class, and the
backend/frontend vocabulary agreement.

**`frontend/src/app/models/vocabulary.ts` is the one place a machine code becomes words.**
It now holds `STATUS_TERMS` only. `OUTCOME_TERMS`, `CELL_STATE_TERMS`, `EXCLUSION_TERMS`,
`CELL_CLASS_TERMS` and `REVIEW_STATUS_TERMS` were removed with the screens that rendered
them — each named part of the verification matrix, and a translation table for words no
screen shows reads as evidence that some screen still shows them. The Python enums behind
them are retained.

### The three-stage mock, and how an Advanced Search run is exercised for free

**`python run_mock.py --workflow` is the deterministic three-stage mock**, and it is what
makes every claim above checkable at zero cost. It serves the real application with the real
graph, the real executor, the real lease, the real event log and the real database, and stubs
only the metered boundaries: discovery, EPO enrichment, and the LLM. A run admitted against
it pauses at Workflow Gate A (stages 1 and 2) and at the search strategy stage (stage 3), and
completes with the standard report.

Three things about it are load-bearing:

- **It hosts the executor in-process** (`WORKFLOW_EXECUTOR=inprocess`), and that is forced
  rather than chosen. The provider stubs are applied inside `run_mock.py`'s own process; a
  separate `python -m workflow.worker` would not inherit them and would make live calls. It
  is the same `Executor` class running the same claim/lease/reconcile/finalize loop on a
  daemon thread. The **separate-process** worker is exercised by
  `acceptance/real_process_acceptance.py`, which kills and restarts it.
- **The env vars are set at import time**, before `config` is imported, because
  `DEPLOYMENT_PROFILE` and `WORKFLOW_GRAPH_ADAPTER` are read at import and have no defaults.
  **No verifier policy is set any more**: this block used to supply `DATE_SCREENING_POLICY`,
  `TEXT_VERSION_POLICY` and a capability deferral because admission refused a run without
  them, and admission now consults none of it.
- **Patches are applied at each consuming module's import site.** `from llm import _llm_chat`
  binds a local name, so patching `llm._llm_chat` never reaches such a caller.

`python acceptance/advanced_search_acceptance.py` drives it end to end over HTTP: admission,
both approvals, an edited element and a replaced query, the search, the standard report,
ownership 404s, idempotent replay, one History row carrying the same matches, and every
withdrawn route answering 404. **53 checks, zero paid calls**, transcript at
`docs/acceptance/advanced-search-transcript.txt`. It **replaces** `four_gate_acceptance.py`
and `journey_acceptance.py`, which are deleted along with their transcripts — a transcript
path that keeps working while the thing it describes has been withdrawn is how an acceptance
claim quietly stops meaning anything. `acceptance/browser_acceptance.py` drives the same
workflow through **real Chrome**.

### The durable report is persisted, and the lease travels in the config

**A durable run does not execute `run_graph_pipeline`**, so it never reaches that module's
`persist_run`. `workflow/executor.py::GraphAdapter` therefore carries a **required**
`persist_result(conn, run, final_state, result_data)` callback — no default and no optional
fallback, because an optional one would let a run complete silently with its report never
stored. The production adapter implements it by delegating to `reports.py::save_report`;
`reports.py` stays the only SQL writer for `patents` / `search_runs` / `search_run_matches`.
Executor-only test adapters may pass an **explicit** no-op; the production adapter may never.

**`_succeed` opens one outer transaction** and, in order: `persist_result`, then
`finalize_run_success` with the terminal `result` event, then the lease release. **All three
or none.** `save_report` keeps its own transaction block, which becomes a savepoint inside
this one. There is deliberately no `try`: if report persistence fails the run does **not**
become `COMPLETED`, no terminal `result` is committed, and no partial History row survives.
A run that reported success while its report was dropped is the substitution the
zero-tolerance policy forbids, and here it would be *silent* — a client reading the stream
would see a completed run and only discover the gap when History was next opened.
`tests/test_run_report.py` injects a `reports.py` failure and asserts all three rolled back.

The result data is built **once** and used for both the History row and the terminal event,
so the report a reviewer sees live and the one History replays cannot disagree.

**The lease comes from `config["configurable"]`, never from `GraphState`.** The executor puts
`owner_id` and `fence_token` there on every acquisition. A checkpointed fence token is stale
the moment a different worker resumes the run, so the first write after any recovery would
fence itself against a token it had merely remembered. Note LangGraph injects the config **by
annotation type** — a parameter named `config` annotated `Optional[dict]` is silently not
passed; it must be annotated `RunnableConfig`.

`graph/durable.py` and `workflow/evidence_store.py` are **retained and unreachable**: they
wrote the verification matrix and the passages of record, and no compiled graph calls them.

### Real-process acceptance evidence

`python acceptance/real_process_acceptance.py` (from `backend/`) starts the API,
the worker and the Angular dev server as **actual OS processes** and drives them
over real HTTP: admission, a gate pause, **a kill and restart of both the API and
the worker while the run is parked**, ownership rejection from a second account,
idempotent replay, conflict on a reused key, resume to completion, then the whole
SSE story — snapshot, replay from a cursor, reconnection at the terminal, and a
disconnect that changes nothing. It writes `acceptance/transcript.txt`.

It uses the **deterministic mock graph adapter** (`--mock`), which is disclosed
rather than glossed: the real verification graph calls OpenAI and this evidence
must cost nothing. What is stubbed is the *content* of the work. The lease, the
checkpointer, the event log, the status machine, ownership, idempotency, the SSE
framing and restart recovery are all real. It also pins `DEPLOYMENT_PROFILE=test`
for the children, whose stub provider has no credential at all, so a paid call is
structurally impossible rather than merely unlikely.

It binds **8099/4299 rather than 8001/4200**, because a `docker compose` backend
already publishes 8001 here and runs an older image — it answered `/healthz` and
then 404'd `/api/runs`, which is how a stale listener produces a baffling
acceptance failure. The script refuses to start if its port is already in use, so
it can never test a process it did not start.

### Docker: the frontend container needs `USER 101:0`

The image grants write to **group 0** (`chgrp -R 0` + `chmod -R g=u`, the
OpenShift convention) because the entrypoint writes `env-config.js` into the web
root on every start. `USER 101` runs with gid 101 and is **not** in group 0, so on
plain Docker that write failed with "Permission denied" and nginx never started --
a crash loop that never appeared under OpenShift, where the arbitrary UID is
always a member of group 0. `USER 101:0` makes the runtime group match the
permissions the image was built with, on both platforms, and keeps the user
unprivileged. Do not drop the `:0`.

### Running the tests: one process owns the database at a time

`backend/tests/conftest.py` takes a **session-scoped exclusive advisory lock**
(`SCHEMA_LOCK_KEY`) for any process that runs a `postgres`-marked test. A second
process reaching its first `postgres` test waits until the first has finished all of
them; non-`postgres` tests never take it, so most of the suite is unaffected.

**It exists because two processes sharing one Compose PostgreSQL broke the suite in two
different ways**, and the second is the one that is easy to misdiagnose:

1. **Schema DDL against live data.** `test_the_rollbacks_reverse_the_workflow_schema_and_re_apply`
   drops the whole workflow schema. A concurrent `create_run` deadlocks it — the rollback
   holds `AccessExclusiveLock` on the table it is dropping while `create_run` waits for
   `RowShareLock` on `users`, the foreign key behind `verification_runs.created_by`.
   PostgreSQL kills one side, and *which* side is why this surfaced as unrelated
   assertion failures across the workflow suite rather than as one legible error.
2. **Tests that assert on globally-shared state.** Rows keyed by the one seeded account,
   a fixed `idempotency_key`, or "the newest run" are shared by every process running the
   same suite. A *different pair* of tests failed on each concurrent run — the signature
   of a class, not of four bugs.

Scoping an assertion to the rows its own test created fixes an individual case and two
were genuinely imprecise and were fixed that way. **Serializing the processes removes the
class**, without weakening any assertion. Verified: three concurrent full-suite pairs, zero
failures; previously two failures per pair, different tests each time.

The destructive schema test additionally **skips with a reason** when it detects a
non-cooperating session (a `psql` window, a hand-started API or worker), because dropping
the schema underneath one reproduces the deadlock with none of the explanation.

**This is not test isolation.** The properly isolated design is a database per process,
which the Compose setup does not provide; this makes the shared one behave correctly and
says so.

### The Invention Understanding stage (Workflow Gate A: stages 1 and 2)

**Workflow Gate A is unchanged as a mechanism and rebuilt as an experience.** It is the
same `interrupt()` node at the same place in the graph, with the same approval boundary and
the same single approval writer. What changed is what a person is shown and what they are
allowed to approve. The gate letters `A`-`D` are internal and **appear nowhere on any
screen** — `gate-shell.ts`'s `STAGE_LABELS` maps them to the stage names, and
`tests/test_frontend_review_contract.py` plus a component spec assert that "gate a",
"decomposition", "checkpoint" and "verification cell" never reach the rendered DOM.

**A verified run now carries three inputs, not one.** `POST /api/runs` accepts
`problem_solved` and `inventor_novelty_statement` alongside the required `description`, and
they are written in the **same transaction** as the run row (`store.set_submission_context`)
so a run never exists without the submission it was created from. They are context for the
analysis and are **never treated as evidence** — the prompt says so in as many words, because
what an inventor believes is new is the claim the run exists to test. Both columns are
nullable: `NULL` means "this run predates the column" and `""` means "the inventor left it
blank", and a report that could not tell those apart would state the second while meaning
the first.

**The inventor's description is never rewritten.** Not by a proposal, not by an edit, not by
an approval. `verification_runs.description` is the source input; the approved summary is
stored **beside** it in `approved_understandings`, at its own version. That is the point of
the side-by-side screen: the inventor is being asked whether the system understood them,
which they cannot answer if what they wrote has been replaced by the system's version of it.
A test asserts the description is byte-identical after an approval.

**`llm.decompose_features` is now `llm.propose_understanding`** and returns
`{"summary", "features"}`. An empty summary **raises** rather than rendering an empty pane.
`llm.regenerate_relationships` is new. `DECOMPOSITION_PROMPT_MARKER` is unchanged and both
prompts carry it, so `RELATIONSHIP_PROMPT_MARKER` was added and is checked **first** in
`run_mock.py` and `tests/test_pipeline_contract.py` — matching only on the shared marker
would answer a relationship request with a whole decomposition, the same ordering trap the
"screening" / "expert patent examiner" checks already have.

**Relationships are regenerated when elements change, never patched.** The inventor edits
*elements*; relationships stay internal verifier requirements and are never shown. But a
relationship reading "E1 is coupled to E4" that survives the deletion of E4 is a requirement
the verifier would be asked to find among elements that no longer exist — a fabricated
requirement with a citation slot attached. So the screen **drops** a capability's
relationships the moment its elements are touched, and `workflow/review.py` refuses an
approval whose relationship text names an absent element id (`DANGLING_RELATIONSHIP_REFERENCE`).
That backend check is **textual and says so**: `decomposition_relationships` stores text and
an ordinal with no foreign key to elements, so the only detectable dangling reference is one
the prose names. It is a guard, never a proof; regenerating is the real defence.

**Element ids are minted above the highest already present, not from the count.** Deleting
E1 from `[E1, E2]` and adding one produces E3, not a second E2. The screen no longer lets
anybody type an id at all, which is a stronger guarantee than the editable-id screen it
replaced — that one could only warn about the collision. `workflow/review.py` still enforces
cross-kind uniqueness, because a UI that cannot produce a collision is not the same thing as
a backend that would refuse one.

**Five named limits, but only two affect the live proposal path.**
`backend/graph/limits.py`:

- `MAX_CAPABILITIES` = `MATRIX_CELL_CAP // MAX_APPROVED_CANDIDATES` = **5**. Derived, never
  written as a literal. **Now inert**: capability grouping was removed with feature
  verification, the approval writer stores every element under one technical group, and no
  blocking rule reads this. `MATRIX_CELL_CAP` gates no live path either.
- `MAX_ELEMENTS_PER_CAPABILITY` = **8** and `MAX_ELEMENTS_TOTAL` = **25**. **Chosen** (owner
  decision, 2026-08-30) and explicitly *not* matrix-derived — elements do not multiply cells.
  They remain for retained verification code and the legacy payload field; the simplified
  flat-list approval has no maximum-element blocking rule.
- `MAX_PROPOSED_SEARCH_LIMITATIONS` = **7**. **Chosen** (owner decision, 2026-09-06) for
  the machine proposal shown at Stage 2. The prompt aims for four to seven compound,
  independently searchable limitations, permits fewer when the description cannot support
  four, and forbids padding. `propose_understanding` rejects more than seven, multiple
  feature groups, exact duplicate texts, empty texts, and separate relationship records;
  it never silently merges, drops or repairs model output.
- `MAX_STRATEGY_QUERIES` = **10**. **Chosen, and NOT an owner decision yet** — recorded as
  outstanding in the constant's own docstring rather than presented as settled, because what
  it should be depends on the discovery provider's rate and quota behaviour and measuring
  Lens.org is out of scope. A bound exists at all because every enabled query is a metered
  provider call plus enrichment, so an unbounded list is an unbounded spend that one
  approval commits the deployment to.

The live approval rules remain deliberately simple: non-empty summary, at least one
non-empty limitation, and at least one non-empty query. The seven-item bound is therefore a
machine-proposal quality guard, not a restriction on what a reviewer may approve after
editing. The Stage 2 screen explains the 4–7 target and the difference between a compound
limitation and a generic standalone component.

**Six blocking conditions, up from four.** `NO_SUMMARY`, `NO_FEATURES`, `NO_ELEMENTS`,
`EMPTY_REQUIREMENT_TEXT`, `DUPLICATE_REQUIREMENT_ID`, `NO_VERSION` — plus `NO_CORE_ELEMENT`,
`DANGLING_RELATIONSHIP_REFERENCE`, `TOO_MANY_CAPABILITIES`, `TOO_MANY_ELEMENTS` and
`TOO_MANY_ELEMENTS_TOTAL`. `NO_CORE_ELEMENT` is checked across the **whole** understanding,
not per capability: a supporting capability may legitimately have no core element of its own,
but an understanding with none anywhere cannot support a novelty finding.

**Regeneration is a durable work request, exactly like a manual candidate.**
`POST /api/runs/{id}/understanding/regenerate` records a row and returns **202**; the
**worker** carries it out. Generating in the HTTP handler would put an LLM call on a request
thread with no lease, no fencing and no recovery. The graph gained one conditional edge:

```
decompose -> gate_a -> [route_after_gate_a] -> regenerate_understanding -> decompose
                                            -> propose_strategy
```

**The approved branch goes to `propose_strategy`, not to the search.** The search strategy
stage sits between this approval and `search_orchestrator` (see "The Search Strategy
stage"), so the invariant this route guards -- nothing is searched for that a person did not
approve -- now has two approvals in front of the first provider call rather than one.

`gate_a_node` checks for a pending regeneration **before** reading any approved artifact and
returns `{}` if it finds one — a rejection is not an approval, and reading one there would
either fail or, far worse, pick up a version approved earlier and treat it as this review's
outcome. A regeneration takes the **next** version; the rejected one stays in
`understanding_proposals` untouched. `claim_understanding_regeneration` stamps `processed_at`
under the lease, so at-least-once execution cannot produce two re-analyses for one request,
and the claimed row is **kept** as the evidence that a person asked and the worker acted.

**Three new tables (migration `0021`), and one of them is deliberately unable to record an
approval.**

- **`understanding_proposals`** — machine output and human edits, stored as separate rows at
  the same version (`LLM` and `USER_EDIT`). **No `is_approved` column and no approver
  columns exist here**, so a row cannot record an approval however it is written. Keeping
  only the human's version would lose the half that shows the human changed anything.
  `workflow/understanding.py` is its only writer.
- **`approved_understandings`** — the approved summary, versioned in step with
  `feature_decompositions`, same approval CHECK, same single writer
  (`workflow/approvals.py::approve_understanding_by_owner`, which writes the summary **and**
  the decomposition in one act so there is never a version of one that no version of the
  other was approved beside). `tests/test_approval_boundary.py`'s grep now covers it.
- **`understanding_regeneration_requests`** — the work request above.

**The `patents` / `search_runs` / `search_run_matches` / `users` search schema is untouched,
and so is Quick Search.** `POST /api/analyze` and `/api/analyze/stream` traverse exactly the
nodes they did before and emit exactly the same phases; `_route_from_start` sends Quick
Search to `understand`, then `expand_queries` leads to `search_orchestrator`, and
`tests/test_verification_integration.py` still asserts both.

**Frontend.** `screens/understanding-review.ts` replaces `screens/decomposition-review.ts`,
which is deleted. `classify` moved to `gate-shell.ts` — it is shared by every gate screen and
now lives with the shared frame rather than in one screen the others imported from.
`violations()` is a **method, not a `computed()`**: it reads a signal *and* plain nested
objects, and a computed tracks only signals, so editing the summary never invalidated it and
the approve control stayed enabled after the summary was cleared. `summary` is a signal for
the same reason. Every nested edit goes through `touch()`, which re-publishes the
capabilities array. The new CSS classes all carry dark counterparts in `styles/gates.css`, written in
the same change rather than discovered later — see the specificity trap note below.

### The Search Strategy stage (what runs between the analysis and the search)

**A verified run now pauses a second time, before any provider is contacted.** The
inventor approves what the invention is; then a person approves *what will be searched
for*. `POST /api/analyze` and `/api/analyze/stream` are untouched — a search-only run
still goes straight from `expand_queries` to `search_orchestrator` and emits exactly the
phases it did before.

`propose_strategy` reads the **approved** summary and limitations and proposes a flat list
of query strings. `gate_strategy` pauses; on resume it re-reads the approved strategy from
the approval store. `prepare_approved_search` then constructs the shared search state from
those reviewed artifacts without another LLM call. A proposal is never dispatched.

The strategy screen intentionally has no enable flags, coverage map, element ids, or
regeneration loop. Every non-empty query the person approves executes. Its single blocking
condition is `NO_QUERIES`; the understanding gate separately requires a non-empty summary
and at least one non-empty technical limitation.
**`llm.propose_search_strategy` is the component**, named in `llm.STRATEGY_COMPONENT` for
the same reason `DECOMPOSITION_COMPONENT` is: the specification does not assign an owner to
it. Its marker, `STRATEGY_PROMPT_MARKER`, deliberately shares no wording with the
decomposition markers — those two overlap on purpose and needed an ordering rule at the
double; this one is a different component and can be matched in any order.

The retained persistence tables still separate machine proposals from approved artifacts;
withdrawn regeneration and coverage columns may remain in historical schema, but no live
route or screen exposes them.

**Frontend.** Stage 3 of `advanced-search-stages.ts` edits the flat query list. It is the
same mounted component as stages 1 and 2, so Back can revisit the approved understanding
without fetching or reconstructing the drafts.

### Human-in-the-Loop: the two gates, and who may resume a run

An Advanced Search run pauses **twice** and a person decides whether it continues.
`graph/gates.py` holds the two `interrupt()` nodes and they are wired into
`graph/pipeline.py`:

```
decompose -> gate_a -> propose_strategy -> gate_strategy
          -> prepare_approved_search -> search_orchestrator -> … -> explain -> END
```

Gate A sits **before** the strategy that is built from the understanding, and the strategy
gate **before** the search that dispatches its queries — so nothing is searched for that a
person did not approve. `gates.GATE_NODES` maps the **live** gates only (`A`, `STRATEGY`).

**`WorkflowGate` still has `B`, `C` and `D`, and that is deliberate.** Runs parked at those
gates before the withdrawal hold them in `verification_runs.current_gate`; removing the
members would turn a legacy row into an enum error at exactly the moment the product is
supposed to explain that the run cannot be continued.

**One graph, two compilations.** `graph/pipeline.py::_assemble` is the single topology;
`build_graph()` compiles it without a checkpointer for the inline search path, and
`build_graph_with_checkpointer()` compiles the identical builder for the executor.
`interrupt()` needs a checkpointer, and the only nodes that call it are the gates — which a
search-only run never reaches.

**No endpoint resumes the graph.** An approval commits five things in one transaction — the
human decision, the audit row, the idempotency record, the `resumed` event, and
`WAITING_FOR_REVIEW -> RESUMING` — and returns. The **executor** picks the run up on its next
poll and issues `Command(resume=…)`. That separation is what makes a duplicate approval
harmless and a crash mid-approval recoverable, and
`tests/test_workflow_api.py::test_an_approval_never_builds_or_calls_the_graph` patches the
graph and proves it was never invoked.

**Regeneration is gone** (spec §10.2). Both `*/regenerate` endpoints, their frontend client
methods and affordances, their graph registrations and their routers were removed; editing
the summary, the elements and the queries replaces asking the model for another attempt. The
two `*_regeneration_requests` tables, their migrations and any existing rows are **kept** —
nothing is deleted to tidy the product.

**Three checks, in one order, on every mutating endpoint** (`api/runs.py`):

1. **ownership** — before any write, so a non-owner never creates a row;
2. **idempotency** — before the gate precondition, so a *retry* of an identical approval
   returns the stored response. After the first approval the run is `RESUMING`, which is
   exactly when a client whose connection dropped retries;
3. **the gate precondition** — before the mutation, so approving the wrong gate is a
   `409 WRONG_GATE` rather than a unique-constraint violation surfacing as a `500`.

`graph/adapter.py` is the production `GraphAdapter`
(`WORKFLOW_GRAPH_ADAPTER=graph.adapter:ADAPTER`). It decides nothing: it names which gate an
interrupt represents, builds the `review_required` body from **counts and a URL** rather than
the artifact, derives `advanced_search` from the run row existing, and persists the finished
report through `reports.py`.

**Verifier capability qualification is not consulted at all.** `VERIFIER_CAPABILITY_ENFORCEMENT`,
`VERIFIER_CAPABILITY_DEFERRAL_DECISION`, `DATE_SCREENING_POLICY` and `TEXT_VERSION_POLICY` are
no longer read on the Advanced Search path (spec §10.4), and admission returns no
`capability_qualified` field. Advanced Search runs no verifier and screens no dates, so
refusing a search over that configuration would block a feature on settings for a feature
that no longer exists. The qualification runner and its artifacts are retained as standalone
legacy tooling.

### The Advanced Search SSE contract and API

`backend/api/` is the HTTP surface and `backend/workflow/stream.py` is the reader.
**Nothing in `api/` may reach a `_system_*` store function** — those skip ownership because
no human is behind the call — and `tests/test_workflow_store.py` asserts that at the AST
level.

`GET /api/runs/{run_id}/stream` implements §9: every frame carries its persisted `sequence`
as the SSE **`id:`**, a reconnecting client sends the highest it saw back through a
`Last-Event-ID` header **or** `?last_event_id=` (the header wins, and the query parameter
exists because the frontend uses raw `fetch`, which sends no header), replay is
`sequence > it`, and a client attaching with no id gets a **`snapshot`** first. `snapshot`
is connection-local and deliberately **not** in the `workflow_event_type` enum.

- **Correctness is polling.** The API process serving a stream is usually not the worker
  executing the run. No pub/sub, no shared memory, no sticky sessions.
- **A client below `events_compacted_below` gets a `snapshot`**, not a partial replay
  presented as a complete one.
- **Terminal semantics:** reconnecting at or above the terminal sequence delivers nothing
  and closes; below it delivers the single persisted terminal exactly once. At most one
  terminal frame per connection.
- **`paused` ends the connection, not the run.** Holding it open across a human review
  would occupy an API worker for minutes to days to deliver nothing.
- **A slow client is disconnected, never buffered** (`SSE_WRITE_TIMEOUT_SECONDS`), and
  nothing is lost: every frame is still in `workflow_events`.
- **The SSE reader never guesses cancellation from a disconnect.** The product instead
  issues authenticated `DELETE /api/runs/{id}` on `pagehide`, and repeats that cancellation
  on startup from the account-scoped remembered id if the unload request was lost. The
  delete removes only a live run; completed and failed runs remain in history. Removing the
  run cascades to its lease, so the next heartbeat fences the worker. An external request
  already in flight can return, but it cannot commit after the fence.
- **Leaving the first review is explicit cancellation.** Back opens an in-app confirmation;
  confirming calls the same authenticated delete before returning to the submit screen.
  Dismissing it keeps the reviewer and the run exactly where they were.
- **A cancellation race never kills the executor.** If deletion wins after a worker acquires
  the lease but before reconcile reads the run, `_system_get_run` raises `RunNotFoundError`.
  `run_forever` treats that narrow condition like the existing missing-run foreign-key form:
  it logs the abandonment and polls again. There is no run left to mark failed and no event
  log left to append to; turning the disappearance into a failure would fabricate state,
  while letting it escape would strand every later run in `QUEUED`.

**Two endpoints record a request and do none of the work**, and their `202` is the
design rather than a courtesy. `POST /api/runs/{id}/candidates` (case T) and
`POST /api/runs/{id}/cells/{cell_id}/rerun` (case U) write a row to
`manual_candidates` / `cell_rerun_requests` (migration `0020`) and return; the
**worker** carries them out. The API cannot fetch a publication, cannot build a
passage, and above all cannot make anything eligible — a manually added
publication goes through the same normalization, date screening, enrichment and
eligibility checks a discovered one does, and approving it into the set is still a
separate human act. A rerun never edits a result: it produces a **new**
`result_version` linked to the one it came from, and it must name the version the
reviewer was looking at — a rerun composed against a superseded result is
`409 STALE_RESULT_VERSION`, the same rule §13.4 check 16 applies to a review.

**A `WorkflowError`'s message reaches the caller; anything else is rebuilt.**
`api/deps.py::_detail` is the one place that decides. The workflow errors are
constructed by this codebase and by SQL functions that interpolate run ids,
statuses and fence tokens and nothing else, so a 409 says *"run X is RESUMING, not
WAITING_FOR_REVIEW"* rather than *"an unexpected IllegalStateError occurred"* —
which is safe and useless. Every other exception is rebuilt from allow-listed
facts, because pattern-matching unknown prose is never complete and this is the
boundary where a provider body or a URL bearing a key would reach a browser.

**`paused` ends the connection only while the run is still parked.** The rule
exists so a connection is not held open across a review that may take days. A
`paused` event the run has already moved past is history, and stopping on it would
cost a catching-up client one reconnect per historical pause — four for a
multi-stage run, never reaching the terminal on the first attempt.

Errors carry `{code, detail}` **at the top level** (`api/deps.py` maps §10.2, and
`server.py` installs the handler that stops FastAPI nesting it under `detail`). A run the
caller does not own answers **404 `RUN_NOT_FOUND`**, identical to an unknown run — there is
no `403` anywhere in this surface, and `tests/test_run_ownership.py` walks the whole
catalogue with a second user's token to prove it.

### Auth: one seeded account, no signup

`POST /api/auth/login` is the only endpoint that answers without a token. `/api/analyze`,
`/api/analyze/stream`, `GET`/`DELETE /api/history` and `/api/auth/me` all sit behind
`auth.require_user` and return **401** — never 403 — because the frontend treats exactly
one status as "your session is gone, show the login screen."

The seeded account is **`testnovelty@gmail.com` / `ABC123`**, inserted by
`migrations/0004.create-users.sql`. **That password is public** — it is in the migration
and in this file, both checked in. The login screen used to print it too and no longer
does; that was a presentation change and nothing more, so do not read the tidier screen as
the credentials having become secret. Treat any backend reachable from outside localhost
as having no access control until that row is replaced.

- **Passwords**: PBKDF2-HMAC-SHA256 from the standard library — no bcrypt/argon2
  dependency for a demo login. The stored string is self-describing
  (`pbkdf2_sha256$<iterations>$<salt>$<key>`) so `ITERATIONS` can be raised later without
  invalidating existing rows. Add an account with
  `python -c "from auth import hash_password; print(hash_password('...'))"` and an INSERT.
- **Tokens**: stateless HMAC-SHA256 over `{"sub", "exp"}`, keyed by `settings.auth_secret`.
  Verifying one touches no database, which is why the gate can guard a 60-second SSE run
  without holding a connection open. The cost is that a token cannot be revoked before it
  expires — `AUTH_SESSION_HOURS` (default 12) is the only bound on a leaked one, and
  rotating `AUTH_SECRET` is the only way to invalidate every issued token at once.
- **`AUTH_SECRET` defaults to a constant checked into `config.py`.** Anyone who has read
  this repository can mint a valid token against a backend that did not set it.

Why gate it at all: a single novelty search spends roughly 25 of a 250/month SerpAPI
allowance plus HuggingFace credits. This keeps a passer-by from spending them. It is not a
security boundary — there is no rate limiting, no password reset, and no revocation.

### Deployment profiles: `local` is OpenAI, `aramco` is deferred

**Approved amendment, 2026-08-28** — recorded in
`docs/prompts/integrated-feature-verification-workflow-implementation.md` §0a. Development on
personal machines uses the **official OpenAI API**; the Aramco-hosted model is considered and
qualified only when the application is ready to move into that environment.

`DEPLOYMENT_PROFILE` is **required and has no default**, and it is **declared, never
inferred** — not from an endpoint URL, a hostname, an API key or a model name. Inference is
how a process ends up believing it is somewhere it is not, and what that belief gates here is
whether a capability qualification performed elsewhere may be reused. Unset or unknown raises
`ConfigError` at import (`backend/deployment.py`).

| Profile | Provider | Settings | Status |
|---|---|---|---|
| `local` | official OpenAI API, official SDK | `OPENAI_*` | development |
| `aramco` | OpenAI-compatible gateway, `requests` | `LLM_API_URL`, `LLM_MODEL`, `LLM_API_KEY` | **deferred and blocked** |
| `test` | in-process stub, no address | none | fixtures only |

**One profile selects one provider.** There is deliberately no separate `LLM_PROVIDER`
setting — a second knob is a second thing to drift, and "profile says local, provider says
gateway" is the internal inconsistency this removes rather than detects. `backend/conftest.py`
pins the suite to `test`, whose stub has no credential, so a boundary somebody forgot to stub
raises instead of reaching a paid API.

**Everything goes through `llm._llm_chat`.** The OpenAI path lives *inside* it, so no graph or
workflow node knows which provider is live, and the contract does not vary by provider:
truncation still raises `LLMTruncatedError` and is never retried, the strict §10 schema is
still sent as `response_format`, and the eighteen §9 checks still run. **No fallback** — not to
another model, not to another provider, not to a looser output mode. A deterministic 4xx is not
retried; a refusal is a real answer and is never parsed as content.

**Embeddings did not move, and Qdrant did not change.** The Hugging Face embedding path stays
exactly as approved, configured separately (`EMBEDDING_*`). A change of LLM provider is not a
reason to enable semantic search.

**Capability qualification is scoped to the exact runtime** (`backend/verification/capability.py`).
An artifact records `deployment_profile`, `provider`, a parsed `endpoint`, `model`,
`model_revision`, `adapter_revision`, `schema_version` (canonical), `wire_schema_version`,
`validator_version`, `inference_config_fingerprint` and `runner_version`, and admission compares
**all ten** identity fields
field by field (`model_revision` additionally, whenever the configured model is pinned). The
last two were added by the 2026-08-29 amendment: with the provider now sent a wire *rendering*
and the omitted constraints enforced by the canonical validator, a capability result is a
statement about all three layers together, so a change to the rendering or to the validator
invalidates every prior record. `adapter_revision` carries
`llm.OPENAI_ADAPTER_BEHAVIOUR_VERSION` alongside the SDK version, because what
`response_format` is built from and which statuses are retried are decided in `llm.py` and can
change while `openai.__version__` stands still.

**`inference_config_fingerprint` covers every setting that can change what the model returns** —
reasoning effort, whether and what `temperature` is sent, the token-parameter name, the effective
ceiling, the declared dialect (including that family's accepted effort vocabulary, so a family
gaining or losing a value is itself a runtime change), the timeout floor, and
`top_p`/`seed`/`verbosity` should any of them ever be used (`deployment.InferenceConfig`). It exists because a measurement made the gap visible:
`gpt-5-mini-2025-08-07` was measured ineligible with `reasoning_effort` unset, the owner then set
it to `high`, and without this field the older artifact would have matched the new runtime on
every other field while describing a configuration nobody had tested. **A provider default that
affects behaviour is a value in the fingerprint, never an absence from it** — `reasoning_effort:
""` records "the provider chose", which is a different runtime from one where we chose.
`tests/test_inference_identity.py` mutates each of the twelve fields independently and asserts
admission fails on each, in both directions. **Every artifact currently on disk lacks
`inference_config_fingerprint`, and the oldest lacks `wire_schema_version` and
`validator_version` too**, so no identity can be built from any of them and they qualify
nothing — they stay on disk and stay accurate about what they measured.
`tests/test_historical_artifacts_do_not_qualify.py` asserts that against the real
`docs/measurements/` directory rather than in the abstract, including that supplying a
record's missing key and relabelling its model still does not qualify it. A successful `local` OpenAI artifact therefore **can
never** qualify `aramco` — that is structural, not a matter of remembering, and
`tests/test_capability_admission.py` pins every field separately because a comparison that
skipped one would still pass a test that only changed the model.

**Verified-run admission fails closed.** `evidence/policy.py` calls `require_capability`
alongside the policy decisions: no artifact, no match, or a matching-but-*ineligible* artifact
all refuse. There is no `SKIP_CAPABILITY_CHECK`, no debug flag, no auto-approval — an AST test
greps for all of them. **Search-only runs never reach admission**, so `POST /api/analyze` and
`/api/analyze/stream` are untouched by the amendment.

**A model that is not pinned to a snapshot leaves a drift window**, stated rather than hidden:
the provider can move an alias to a new revision with no configuration change, and no static
comparison can see it. The default `OPENAI_MODEL` is a pinned snapshot for that reason, and
`model_pinned` records the answer either way.

### LLM: an OpenAI-compatible endpoint, not a local model

All LLM calls go through `_llm_chat` in `backend/llm.py`, a raw `requests.post` to an
OpenAI-compatible chat-completions endpoint (`LLM_API_URL`) using `LLM_MODEL`, authorized
with `LLM_API_KEY`. It is not Ollama, Together, or Groq; any OpenAI-compatible endpoint is
a drop-in replacement, and **changing provider is an `.env` change, not a code change**.

The committed defaults are the HuggingFace Router with
`meta-llama/Llama-3.1-8B-Instruct:novita`, and `LLM_API_KEY` falls back to `HF_TOKEN` so
that combination keeps working with no extra config. **`backend/.env` currently overrides
the LLM URL, model, and credential** to an internal Aramco LiteLLM gateway
(`https://dev-metabrain-api.aramco.com.sa/v1/chat/completions`, model `minimaxm3`), which
resolves only from inside Aramco's network — the defaults are what a machine outside it
falls back to, so a `Could not resolve host` here is a network-location symptom, not a
misconfiguration. `minimaxm3` is a **reasoning model**: it returns its chain of thought in
a separate `reasoning_content` field alongside `content`. `_llm_chat` reads `content` only,
so this needs no handling — but reasoning tokens still bill against the response, so watch
for `LLMTruncatedError` and raise `max_tokens` if it appears.

Three functions call it:
`understand_invention`, `analyze_all_matches` (both restored from a regression that had
briefly deleted `analyze_all_matches` and replaced it with a silent-fallback
`rationale_for_match`/`recommendations_for` pair — if you find yourself reintroducing
either of those, stop), and `validate_relevance` (Phase 3's Validation agent, same
batched-call-with-no-`except` shape as `analyze_all_matches`).

**All three go through `_llm_json`, not `_llm_chat` directly, and none of them parse JSON
themselves.** Two production failures traced to one cause worth understanding before
touching this file:

- **`finish_reason == "length"` now raises `LLMTruncatedError`.** The provider reports
  truncation and the old code discarded that field, so a response cut off at `max_tokens`
  surfaced as a baffling `JSONDecodeError` pointing at a syntax problem in the middle of
  the document. Worse, when the cut happened to land after a complete element, the old
  greedy `\{.*\}` regex matched to the *last* closing brace and **parsed it successfully** —
  verified live at `max_tokens=120`. `understand_invention` would have returned two search
  queries instead of five and reported success. That regex is gone; the whole cleaned
  response is parsed. Truncation is deliberately **not retried** — the same prompt at the
  same ceiling truncates again.
- **`max_tokens` scales with the workload.** `analyze_all_matches` used a flat 3200 while
  asking for 3-5 sentences per patent plus ten-plus recommendations; at the `MAX_RESULTS`
  cap of 15 that genuinely needs ~2700, close enough that normal variation truncated it.
  It observably did, on a real 14-match run. Now `1200 + 250 * count`; `validate_relevance`
  scales the same way.

Also: requests set `response_format: {"type": "json_object"}` (verified accepted by both
the HF Router + Novita endpoint and the Aramco gateway's `minimaxm3` — turn it off with
`json_object=False` if you point `LLM_API_URL`
at a provider that rejects it), and a malformed-but-complete response is **re-asked** once
at a nudged temperature, never repaired. Repairing would mean inventing content no model
produced. `tests/test_llm_json_robustness.py` pins all of it.

The `understand_invention` prompt no longer asks for `synonyms`. Nothing has ever read that
field, and being the largest and most deeply nested part of the response it was the most
likely thing to push it over the limit.

### Embeddings and Qdrant: hosted, not local, corpus grows from live traffic

`backend/embeddings.py` calls a **hosted** embedding model using three explicit settings:
`EMBEDDING_API_URL` (**required, no default**), `EMBEDDING_MODEL`, and
`EMBEDDING_API_FORMAT`. `EMBEDDING_API_KEY` falls back to `LLM_API_KEY` then `HF_TOKEN`, but
the current `.env` binds it directly to `HF_TOKEN` because the LLM and embedding providers
are different. Not `sentence-transformers`, which stays an unused dependency by design (see
above). Failures raise; there is no zero-vector fallback. `chunk_text` splits long documents
into overlapping word-count windows (capped at `MAX_CHUNKS_PER_DOCUMENT`), used by
`compare.py`'s ranking and `vector_store`'s truncation.

**The wire protocol is declared, never guessed from the URL.** `openai` sends
`{"model", "input": [...]}` and reads `{"data": [{"index", "embedding"}]}`, re-sorting by
`index` before callers map vectors back to patents. `huggingface` sends
`{"inputs": [...], "normalize": true, "truncate": true}` to the Inference Providers
feature-extraction endpoint and reads its bare vector array. Response counts, dimensions,
and numeric contents are validated before anything is scored.

**The current model is `BAAI/bge-large-en-v1.5` at 1024 dimensions, and that width is
load-bearing.** Qdrant fixes a collection's vector size at creation, so any `patents`
collection created with the previous 2560-dimensional Qwen3 model is **incompatible and has
to be dropped and rebuilt, not migrated**, before semantic indexing is re-enabled. There is
no bulk re-ingestion step, so the corpus rebuilds from live traffic. `run_mock.py`'s
`EMBED_DIM` is 1024 as well (it tiles a 64-dim hash, so the width must stay an exact multiple
of 64). `MIN_SIMILARITY_SCORE` (0.6) and `QUERY_INSTRUCTION` are the values previously
calibrated for this BGE model. Semantic search remains disabled for independent workflow
reasons; re-probe those values if the model, prefix, or corpus distribution changes.

`backend/search/vector_store.py` wraps a `qdrant-client` `QdrantClient` (`settings.qdrant_url`
/ `settings.qdrant_api_key`) around a single `patents` collection, created lazily on first
write (`store_patents`) — **there is no bulk-ingestion step**. The collection growing at
all is a side effect of `graph/searching.py`'s `index_patents_node` indexing every
enriched keyword result the request found. `backend/search/retrieve.py`'s `retrieve_similar_patents` is the
read side, used by `semantic_search_node`. A collection that doesn't exist yet returns an
empty result (a legitimate cold-start state); an unreachable Qdrant still raises. Queries
go through `client.query_points(...).points` — **not** `client.search(...)`, which
`qdrant-client` removed in 1.10+ (installed here: 1.18). `requirements.txt` doesn't pin a
version, so a fresh install always gets current `qdrant-client`; if you see
`'QdrantClient' object has no attribute 'search'`, that's why.

**The corpus grows from the free surplus, not just from what the report shows.** SerpAPI
bills per *call*, not per result, and its Google Patents engine returns 10 for the price
of one — `search_google` was requesting 10 and discarding all but `RESULTS_PER_QUERY`
(4), throwing away ~30 already-paid-for patents per search. `search_all_providers_strict`
now splits them (`FETCH_PER_QUERY = 10`):

- **`candidates`** — top 4 *per query*, taken per-call rather than off the head of the
  flattened list, so one query cannot monopolize the set and silently drop the
  perspectives the other four generated queries exist to cover. These get enriched
  (a paid Details credit each), fused, validated, ranked, and shown.
- **`index_only`** — the remaining ~30. Never enriched, never ranked, never displayed;
  written straight to Qdrant so a *later* search can find them semantically. Corpus
  growth per search goes from ~20 to ~50 patents at **zero extra provider cost**.

`index_patents_node` filters `index_only` through `vector_store.known_ids` first. That is
not just an embedding-cost saving: `index_only` records are abstract-only, and a patent
stored with enriched full text by an earlier request would otherwise have its richer
vector overwritten by a poorer one every time it reappeared as a cheap extra — the corpus
would degrade the more the tool was used. Enriched `keyword_patents` are always rewritten,
since that copy can only improve what is stored.

**Retrieval is asymmetric, deliberately.** `semantic_search` embeds the query with
`QUERY_INSTRUCTION` prepended; `store_patents` embeds documents with no prefix. The current
BGE model uses the fixed query sentence
(`"Represent this sentence for searching relevant passages: "`). It is a retrieval
instruction to the model, not content. Do not "make these consistent" by prefixing
documents too: the shared
prefix would pull every stored vector toward the same region and flatten the very gap
`MIN_SIMILARITY_SCORE` has to cut through. `tests/test_phase3_agents.py` pins both halves.
The prefix is applied to queries only and does **not** require re-indexing an existing
collection, since document vectors are unchanged by it. Note `tests/fakes.py`'s
`fake_embed_texts` strips the prefix before hashing — a bag-of-words double would score
its five content words as ordinary query terms and dilute the real ones, an artifact of
the stub that production doesn't have (same reasoning as that module's `_STOPWORDS`).

The query also passes `score_threshold=MIN_SIMILARITY_SCORE` (0.6). The live-Qdrant probe
behind it measured the current BAAI/bge-large-en-v1.5 model: unrelated queries scored
0.51-0.56 and related ones 0.68-0.76. There is no exact-zero baseline for arbitrary English
text, so the threshold sits in the observed gap rather than at zero. Re-probe it if the
model, prefix, or corpus changes. Without
it, `query_points` always returns its `limit` nearest neighbors even when the
closest thing in a small or unrelated corpus still isn't actually similar —
"the least-bad options available" would otherwise flow into validation and
ranking looking like real candidates. `validate_node` and `rank_node` would
likely still catch a weak match downstream (an honest low score, or an LLM
relevance rejection), but the threshold cuts it off at the source instead of
relying on that safety net.

### The evidence layer: enrichment, passages, and honest exclusions

**Nothing in the running application calls this yet** — `graph/`, `server.py`'s
endpoints and the frontend are untouched by it. These are pure modules
(`backend/evidence/`, `backend/search/enrichment.py`, `backend/search/epo_fulltext.py`)
that Implementation Gate C will register as graph nodes. Keeping them pure means their
tests need no `StateGraph` and Gate C's wiring has nothing left to get wrong except the
wiring.

**EPO is an enrichment source only, and that is a different role from discovery.**
`SOURCES` is untouched — Lens.org still does discovery, EPO search is still commented
out. The defect this distinction fixes was real: `search/epo.py`'s `enrich_with_full_text`
selects targets with `patent.source == "European Patent Office"`, so under the approved
workflow — where every candidate arrives from the *discovery* provider — that target list
is **empty**, enrichment silently no-ops, and every candidate falls to
`EXCLUDED_NO_FULL_TEXT`: a total failure indistinguishable from a legitimate coverage
result. `search/enrichment.py` has **no source guard at all**; the only thing it filters
out is a record with no parseable publication number.

**Three enrichment states, because two of them used to be one value.**
`get_patent_full_text` caught `Exception` and returned `""`, so a timeout and a genuine
empty response were the same thing. Now: `TEXT` (the provider answered and had eligible
sections), `NO_TEXT` (the provider answered and had none → `EXCLUDED_NO_FULL_TEXT`, a real
answer about the document), and `FAILED` (timeout, 5xx, transport, auth → **the run
fails**). Bounded retry is permitted for transient errors; converting exhaustion into an
exclusion is not, because that records a provider outage as a fact about a patent.

**The bibliographic call has the same three states, and that was a real defect.**
`_fetch_biblio` returned `("", "")` for every non-200 *and* every exception, so a 401, a
404, a 429, a 5xx, a timeout, a transport error and malformed XML all became "no
publication date" -- and from there `EXCLUDED_INELIGIBLE`. That is a **provider failure
converted into an exclusion**: the run reports a publication as ineligible when nobody
ever asked the provider successfully, and it understates the product's own coverage using
the provider's downtime. The three outcomes now are 200-with-a-date (the trusted text
version), **200, well-formed, genuinely no date** (the one honest absence -> `NONE` ->
`EXCLUDED_INELIGIBLE`), and **anything else -> the run fails**. 400/401/403/404/405/406/410
are not retried, and malformed XML is not retried either -- a body that did not parse will
not parse on the second attempt. `tests/test_epo_enrichment.py` pins each branch.

**Claim evidence comes from EPO and from nowhere else.** `build_passages(enrichment)`
takes **no `PatentRecord`**, and that signature is the rule. It used to take one and build
claim passages out of `PatentRecord.claims` -- *Lens discovery metadata* -- while stamping
them `source="EPO OPS"` with an `epo:` document id, and it did so even when EPO had
returned no claims section at all. That is a citation to an EPO document attached to text
EPO never served. `SectionText.claims` now carries the provider's own numbered
`ProviderClaim`s with offsets into the exact retrieved section, so every claim passage
slices back out of the text whose SHA-256 the row records. A claims section with no
per-claim structure produces **no** claim passages rather than numbering a flat blob by
position.

**Publication numbers are normalized to epodoc form before every lookup** — country plus
document number, no kind code. EPO's full-text endpoints 404 on the suffixed form, so
without this every call 404s and every candidate is excluded. Both forms are recorded, and
an unparseable number **raises** rather than being guessed at: a number quietly corrected
into a different real document would attach that document's text to this candidate.

**Locators are citations, so there are exactly three kinds and no approximate one:**
`CLAIM_NUMBER` (the claim's own number → `claim 4`), `EPO_PARAGRAPH` (a provider `<p>` id
or num → `paragraph [0031]`), and `SOURCE_RANGE` (character offsets into the exact
retrieved section text → `description characters 4120-5233`). **A `paragraph [NNNN]` the
provider did not supply is never emitted** — a range that says what it is beats a
paragraph number that lies. `evidence/segmenter.py` is a pure, versioned function of the
exact text, so slicing the stored offsets out of the stored section reproduces the passage.

**Eligibility is derived, never asserted.** A publication is eligible **exactly when at
least one valid passage was actually built for it**. The "substantive description"
heuristic can therefore only *reject*; it can never assert eligibility on its own. That
inversion is what makes "eligible with nothing to cite" structurally impossible rather
than merely discouraged — `EligibilityResult` refuses to construct it.

**The date policy is a recorded human decision, and the code refuses to run without one.**
`DATE_SCREENING_POLICY` and `TEXT_VERSION_POLICY` have **no default**, and each needs its
`*_DECISION` recorded too — same shape as `VERIFIER_GENERIC_JSON_DECISION`. The approved
answers: the evaluated publication's **own publication date**, **exclusive** (`date <
cutoff`), an uncertain date **never silently passes** (`EXCLUDED_DATE` + `UNCERTAIN`,
surfaced at Gate B), and a passage's text version is the served document's **publication
date** from EPO biblio. The priority date is stored and displayed and **never screens**.
`retrieved_at` is never substituted for a text version. Patent dates are date-only and no
timezone shift is applied — a shift can move a publication across the cutoff by a day.

**Every evidence write is replay-safe in the database, not in the caller** (migration
0018). Recovery re-runs a node, so both writes are replayed sooner or later, and both
handled it wrongly. `record_exclusion` was a plain INSERT into a table with **no
natural-key constraint at all**, so a replay did not raise -- it silently wrote a *second*
exclusion row for the same publication, possibly with a different reason, both claiming to
be why it was excluded. `record_passages` upserted with a SET list covering only the text
and its hashes, so a replay carrying different evidence **overwrote the text while keeping
the original text-version date, locator, section, language and provenance** -- immutable
machine evidence mutated in place, and a stored verdict left citing text it was never
validated against. `record_publication_exclusion` and `record_evidence_passage` now
compare **every** content and provenance column under lock: an identical replay is a no-op
(the existing id, or zero rows written), and anything different raises `FN003`. Both are
fenced through `_lock_run_and_require_lease`, and both refuse the excluded-*and*-cited
contradiction from either write order. `retrieved_at` is deliberately outside the identity
comparison **and never updated** -- two identical re-fetches legitimately differ in clock
time, and the first retrieval is the one that produced the stored evidence.

**The policy decisions are recorded, with an owner and a date**, in
`docs/decisions/2026-08-28-evidence-layer-policies.md`: date screening, the uncertain-date
rule, the text-version rule, the "a failed bibliographic request is not an absent date"
rule, and the acceptance of EPO-only enrichment. They were approved on **2026-08-28** and
not before; `backend/.env.example` carries the same dates and points at that file.

**Measured EPO coverage: 17.6%** (`docs/measurements/epo-coverage-2026-08-28.md`, 34
distinct publications taken from already-stored `patents` rows, EPO called and nothing
else). The jurisdiction split is the part that matters: EP 75%, CA and WO 100%, and **US
0/18, KR 0/6, CN 0/2, DK 0/1**. EPO's full-text service covers EP/WO-family documents, so
a filing that never entered that family has nothing for EPO to serve. On that sample the
approved workflow would exclude **82.4%** of what discovery finds before any verification
call. That is a **product decision** the artifact states and does not make. Accepting EPO-only enrichment for the MVP obliges the product and the report to
say so: every exclusion is visible and reasoned, excluded publications are stated to have
**not been evaluated** for feature disclosure, the result is **never** described as a
complete or exhaustive novelty search, and no claim that no prior art exists is ever made
on the basis that EPO lacked full text. **A second enrichment source is a post-MVP
priority; discovery is not narrowed to EPO's coverage.**

### Data sources normalize to one shape

`backend/search/google.py` (Google Patents via SerpAPI) and `backend/search/epo.py` (EPO
OPS, OAuth2 client-credentials, XML response parsing, fetches a fresh token on every
call — no caching) both normalize into the `PatentRecord` dataclass in `backend/models.py`.
Add new sources by writing a `(query: str, size: int) -> list[PatentRecord]` function and
registering it in the `SOURCES` tuple in `backend/search/search.py`.

Both providers can also fetch full text, each with its own `enrich_with_full_text(patents)`
function that mutates `patent.full_text` in place and internally skips records from the
other provider (matched by `patent.source`), so both are safe to call on the same mixed
list — see `graph/searching.py`'s `keyword_search_node`. EPO's uses the `epodoc` published-
data endpoints (`.../publication/epodoc/{id}/description` and `.../claims`); the `id` EPO's
`search_epo` builds is deliberately **country + doc-number with no kind code**
(e.g. `"EP4778509"`, not `"EP4778509A1"`) — the full-text endpoints 404 on the kind-suffixed
form, confirmed against the live API. EPO's full-text service only actually holds text for
EP/WO-family documents — a US/CN/DE/KR-only filing that shows up in EPO's *search* results
(a worldwide bibliographic index) legitimately has no full text EPO can serve, and
`keyword_search_node` **drops** any candidate, from either provider, that comes out of
enrichment with no full text at all, rather than silently falling back to scoring it
against just the abstract.

**EPO's search endpoint uses HTTP 404 to mean "zero results," not "not found."** A query
that legitimately matches nothing in EPO's index returns 404 with a
`<fault><code>SERVER.EntityNotFound</code></fault>` body (confirmed against the live API).
`search_epo` checks for exactly that fault code and returns `[]` — a genuine empty result,
the same as Google returning no `organic_results` — rather than raising and failing the
whole request over one query that simply didn't match anything. Any other 404 (or any
other error) still raises normally; the carve-out is narrow and checks the fault code, not
just the status.

**EPO is currently commented out of `SOURCES`** — its OPS auth is being throttled — so
Google Patents is the only live provider and `search_all_providers_strict` is effectively
strict over one source. `search/epo.py` is intact and the credentials are still read from
the environment; re-enabling it is uncommenting one line. Note its no-token-caching
behavior will cost an extra OAuth round trip per call when it comes back.

**Lens.org claim numbering fails loudly rather than guessing.** `search/lens.py`'s
`_parse_claims` returns individually numbered `PatentClaim`s, because an evidence locator
has to be able to say "claim 4" — which is what `docs/prompts/feature-disclosure-verification.md`
§17.1 builds `chunk_id: PAT-{pub}-CLM-{number:03d}` and `locator: claim {number}` from. The
number comes from the claim's own leading digits; **1-based position is only a fallback, and
only where nothing else claims that number.**

That qualifier is load-bearing and was a real bug. Numbering used to be assigned in one pass,
so `["2. A method.", "A further method."]` produced two claims both numbered 2 — two locators
reading "claim 2" and two identical chunk IDs — and `["3. A method.", "A further method."]`
produced claims 3 and 2 with no complaint. Numbering is now resolved in two passes and raises
`ClaimNumberingError` when it cannot be resolved honestly: two claims stating the same number,
or an unnumbered claim whose position is a number some other claim states. **It never
allocates an unused number to make the set pass** — that would invent a claim number the
patent does not have, and a locator naming the wrong claim is fabricated evidence with a
citation attached. The error propagates through `search_lens` into `PatentSearchError`, which
fails the request, per the zero-tolerance policy. Structurally malformed input (a missing or
non-list `claims` field) still returns `[]`; "this provider returned no claims" is a different
statement from "this numbering cannot be trusted". `tests/test_lens_claims.py` pins every
branch, including that a successfully parsed set can never carry a duplicate number.

Enrichment (`enrich_with_full_text` in `search/google.py`) costs **two** outbound HTTP
calls per patent: the Details API does not inline descriptions, only a `description_link`
to a separate hosted HTML page. It runs 8-wide in a `ThreadPoolExecutor`; passing
`include_description=False` halves the call count at the cost of claims-only text.

**Never call `response.raise_for_status()` on a SerpAPI response.** SerpAPI authenticates
by query parameter, and `requests`' `HTTPError` message embeds the full request URL —
which `search_all_providers_strict` interpolates into `PatentSearchError`. At the time this
was found, `server.py` forwarded that string verbatim as the SSE `error` detail, so a plain
429 printed a live API key into the user's devtools console — observed for real on an
exhausted free-tier quota, not theorized. **Two layers have been added since and neither
retires this rule**: `server.py` now sends `safe_detail(error)` rather than the raw string,
and the frontend renders its own copy instead of the detail. The key would still reach the
SSE frame, the event log, `failure_detail` and the server log, because the exception is
built long before either layer sees it. Use `search/google.py`'s `_raise_for_status`
instead, and see `tests/test_secrets_do_not_leak.py`, which pins both the fix and the
reason it is needed.

SerpAPI's free plan allows **250 searches/month**, and one novelty search spends roughly
25 of them (~5 searches + ~20 Details calls). That is about ten searches a month before
the key stops working — worth knowing before running anything in a loop against it.

### External quotas are small — check them before iterating

A full novelty search is expensive against every metered boundary it touches, and none of
them degrade gracefully, by design: an exhausted quota fails the request.

- **SerpAPI** — 250 searches/month, ~25 per novelty search. `GET https://serpapi.com/account?api_key=...`
  reports `plan_searches_left`. Exhaustion returns HTTP 429. This one is unchanged by the
  LLM/embedding provider switch and remains the tightest limit in the project.
- **HuggingFace Router** — a monthly included-credit allowance used by the current BGE
  embedding calls. Exhaustion returns HTTP 402 with
  `"You have depleted your monthly included credits"` at every request size, not just large
  ones — so a sudden 402 is a billing state, not something a smaller `max_tokens` will fix.
  The LLM currently points at the Aramco gateway instead, whose limits are not documented
  here. `HF_TOKEN` is bound explicitly to `EMBEDDING_API_KEY`; it remains the fallback for
  the committed Hugging Face LLM defaults as well.

Check before any benchmarking or repeated live runs; it is very easy to spend a month's
allowance in an afternoon of testing.

### `backend/verification/` — feature-disclosure verification, WITHDRAWN and retained

**WITHDRAWN FROM THE PRODUCT on 2026-09-01, and retained in full.** Implements
`docs/prompts/feature-disclosure-verification.md`. Nothing in the running application calls
it — not `server.py`, not `graph/`, not the executor, not the API, not the frontend — and no
compiled graph, route or acceptance journey reaches it.

That gap used to be *deliberate but temporary*: the component was built ahead of the four-gate
workflow that would host it. It is now **permanent**. Feature verification was withdrawn
because no runtime ever qualified against §14.2.1 — four were measured and all four failed —
so the reviewed workflow's central act could not be performed. See
`docs/decisions/2026-09-01-advanced-search-three-stages.md`.

The package, its tests, the standalone qualification runner
(`python -m verification.capability_test`) and every measurement artifact are kept for
historical reference and possible future work. **Do not partially delete this subsystem**,
and do not read anything below as describing live product behaviour: it describes what the
component does when something calls it, and nothing does.

What it does: answers whether **1–5 supplied passages** from **one publication** state each
element and relationship of **one human-approved feature**. It is not a novelty judgment and
not a statement about the patent — only about the passages it was shown. That scoping is the
whole design, and the vocabulary carries it: `NOT_FOUND_IN_SUPPLIED_PASSAGES` and
`NO_DISCLOSURE_FOUND_IN_SUPPLIED_PASSAGES` never mean "not disclosed". Shortening either in a
UI or export is the same class of defect the zero-tolerance policy forbids.

Eight things about it are load-bearing:

- **A validated `machine_result` is deeply immutable, not just `frozen=True`.** Pydantic's
  `frozen=True` blocks field *reassignment* and nothing else, so while the §10.1 models used
  `list` collections a stored verdict could still be edited in place —
  `result.elements.clear()`, `result.missing_element_ids.append(...)`, and worst of all
  `result.elements[0].evidence.append(...)`, which attaches evidence to a requirement no model
  cited. Every collection is now a `tuple` of frozen models. **This is a representation change,
  not a contract change**: pydantic reads a JSON array into a tuple and writes it back out as
  an array, and §10 — the authority — still says `"type": "array"` throughout. Do not convert
  back to lists after validation.
- **A human review is bound to one cell and one result version.** `ReviewOverlay` carries
  `source_cell_id` and `source_result_version`, and §13.4's checks 15–18 compare both against
  the cell the review is being saved into, plus the machine result's own feature id, feature
  version and publication number. `source_result_version` previously existed and was compared
  to nothing — an unenforced invariant. Without those checks a review of result version 1
  attaches cleanly to version 2, and a review of F2 against publication A attaches to F2
  against publication B, because the requirement IDs are identical. `CellResult` therefore
  requires `cell_id` and `result_version`: a result that cannot say which cell it is cannot be
  bound to anything.
- **A waiver is bound too, and changes nothing.** Check 18 pins its cell, reviewer, requirement
  and result version. `waive_cell` is the path for the states that can carry no overlay
  (`VALIDATION_FAILURE`, `NOT_EVALUATED`); it returns the cell with the waiver recorded and the
  state, `machine_result`, failing check and reason all untouched. A waiver is a disclosed gap,
  never a verdict.
- **The prompt constants are byte-identical copies of the spec**, not a paraphrase.
  `verification/prompt.py` holds nothing but `SYSTEM_PROMPT` and `USER_PROMPT_TEMPLATE`, and
  `tests/test_verification_spec_consistency.py` re-extracts both from the markdown on every
  run and asserts equality — along with the §10 JSON Schema, the §2 decomposition, the §4.1
  and §4.3 rendered blocks, the §6.1 outcome table, and the §13.2 worked example, which is
  validated as a fixture through the same code production uses. **Edit the specification
  first**; the module follows it, never the reverse.
- **`derive_outcome` is the application's, not the model's.** The model returns an `outcome`
  and the application recomputes it and rejects the response on mismatch (check 15). The same
  function derives the human-reviewed outcome, so a reviewed outcome contradicting its own
  reviewed statuses cannot be represented.
- **Every quotation faces a plain substring test** against passage text canonicalized once,
  before the call (§8). Nothing normalizes, case-folds, re-punctuates, or fuzzy-matches a
  quote afterwards — and a *human reviewer's* quotation faces the identical test.
- **The response contract is bare JSON, and nothing unwraps it.** §3 says "ONE JSON object and
  nothing else: no markdown fences, no commentary before or after", so the raw response goes
  straight to `json.loads` and a fenced or prose-wrapped reply fails check 1. The verifier used
  to call `_strip_fences` first; that repaired the most visible symptom of a provider ignoring
  structured output, which is exactly the evidence §14.2.1's capability test needs — a model
  whose fences are removed for it looks compliant. The one permitted re-ask (§9.1) is held to
  the same contract; there is no looser second pass.
- **Passage eligibility is enforced on the section, not on a flag.** Only `CLAIMS` and
  `DESCRIPTION` can be evidence (§1.2.3). An `ABSTRACT` is refused **even when the caller sets
  `evidence_eligible=True`**, and so is `OTHER` — the bucket a title, a bibliographic block or
  a search snippet lands in. That flag is supposed to come from the full-text eligibility
  classifier §17.2/§17.3 record as not built, and a boolean nobody computes is not a safety
  boundary.
- **Retrieval scores never reach the model** (§5). They select and order passages and go in
  the audit record; the rendered prompt carries no score, rank, date, or passage count.
- **Structured output is an eligibility requirement, not a preference** (§14.1). The default
  `VERIFIER_STRUCTURED_OUTPUT=json_schema` sends the **wire rendering** of the §10 schema as
  `response_format` (see the canonical/wire split above — the canonical schema itself is
  rejected by OpenAI strict mode with a deterministic HTTP 400, and every keyword the rendering
  omits is enforced by the canonical validator on the way back in). Setting it to `json_object`
  additionally requires `VERIFIER_GENERIC_JSON_DECISION` to be non-empty, because the spec
  permits generic-JSON mode only as a recorded decision. Anything else raises a capability error
  and fails the stage. There is no prompt-only fallback and no runtime downgrade. **The §14.2.1
  gate has now been run against the real OpenAI API and `gpt-4o-2024-08-06` is INELIGIBLE** —
  probes 1-6 pass, probe 7 measures a 0.25 malformed-response rate at §9 check 15. `minimaxm3`
  on the Aramco gateway has never been probed at all and remains unverified.

Two changes it required outside the package, both backward compatible:

- **`_llm_chat` gained keyword-only `system` and `response_format`.** The system/user split is
  the injection defence: untrusted passages must sit in a different message from the rules
  governing them. A caller passing neither gets a byte-identical payload to before.
- **`run_mock.py` and `tests/test_pipeline_contract.py` both register the verifier marker**
  (`PROMPT_MARKER`, imported rather than duplicated as a literal). Its double returns an
  all-`NOT_FOUND` response that passes all eighteen checks — a double that fabricated a
  `FOUND` would have to fabricate a verbatim quotation with it.

**Not implemented**, and named here so it is not rediscovered as a surprise: the four gates,
the feature × publication matrix, any persistence or SSE surface, and the §1.2.3 full-text
eligibility classifier — that one sits at the enrichment boundary the spec's §17.2/§17.3 flag
as blocking, and its "substantive description" branch is an unapproved judgment call (§16
decision 13). The retrieval floor for `NOT_EVALUATED` is a caller-supplied parameter with no
default, because §16 decision 6 requires a passage-level calibration. The whole-patent
Qdrant threshold is not evidence for that separate boundary.

### Mock mode: `python run_mock.py`, for looking at the app without paying for it

**`--workflow` additionally serves the verified reviewed workflow** — see "The
three-stage mock" section above for what that stubs and why the executor has to be
in-process for it. Without the flag, `run_mock.py` is exactly what it always was:
the search pipeline, no run row, no gates.

**Use this for any frontend or UI work.** It serves the real application on 8001 with only
the metered boundaries replaced, so a full run — login, SSE progress, report, History —
costs nothing instead of ~25 SerpAPI searches. `--fast` drops the pacing delays; `--port`
moves it. Postgres and Qdrant still need to be up, and the frontend needs no change,
because the mock takes the port the frontend already targets.

Faked: `llm._llm_chat` (including the feature-disclosure verifier — see that section above),
`compare.embed_texts`, `search.vector_store.embed_texts`,
`search.search.SOURCES`, and both `graph.searching.enrich_*` functions. **Everything else
is real** — the FastAPI app, the auth gate, the graph and its routing, the SSE framing,
prompt construction, JSON parsing, dedup, chunking, cosine scoring, the risk thresholds,
Postgres persistence, and `GET /api/history`. It is a stub at the network boundary, not a
mock of the pipeline, which is what makes it worth trusting for UI work.

Four things about it are load-bearing:

- **It is a developer tool and must never become a runtime mode.** It patches from the
  outside and is reachable only by running that file; nothing in `server.py` or `graph/`
  knows it exists. Faking *inputs* in a separate script is a different thing from the
  product substituting output for a failed step, and it stays different only while it
  stays out of the application.
- **An outbound-HTTP guard enforces the "costs nothing" claim.** `requests.Session.request`
  is wrapped to raise `PaidCallAttempted` on any non-local host, so a boundary someone
  forgets to mock fails loudly instead of quietly spending quota. `qdrant-client` (httpx)
  and Postgres (psycopg) are untouched and stay real.
- **Every fabricated record carries `MOCK DATA - no live search` in `source`**, which the
  report screen prints beside each publication number. Mock runs are written to the real
  database, so that string is also how you find them again:
  `DELETE FROM search_runs WHERE id IN (SELECT run_id FROM search_run_matches WHERE source LIKE 'MOCK DATA%');`
- **The mock embedder emits 1024-dim vectors**, matching BAAI/bge-large-en-v1.5. A Qdrant
  collection fixes its width on creation, so a wrong-width vector would fail against a
  `patents` collection a real run already made. Nothing writes today (indexing is disabled
  in `graph/pipeline.py`), which is exactly why the mismatch would be baffling later. It
  `EMBED_DIM` has to move with the model and must stay an exact multiple of the 64-dim hash
  it tiles.

The LLM double keys off real prompt substrings (`"PatentScout AI"`, `"screening N
candidate"`, `"expert patent examiner"`, `llm.DECOMPOSITION_PROMPT_MARKER` /
`RELATIONSHIP_PROMPT_MARKER` / `STRATEGY_PROMPT_MARKER`, and the verifier's
`"PASSAGE-LEVEL TECHNICAL DISCLOSURE VERIFICATION"`, which is matched against the **system**
message rather than the user prompt), so the real prompt-building code still runs and a
prompt edit that breaks those markers raises there rather than passing silently. The same
markers back `tests/test_pipeline_contract.py`; changing a prompt means checking both.

**The strategy double reads its element ids out of the prompt rather than hardcoding
them**, the way the screening double reads its candidate count: the reviewer edits the
understanding, so the elements a strategy is asked to cover are whatever they approved, and
a fixed list would either miss a core element (failing the coverage rule for the wrong
reason) or claim coverage of ids the run does not have.

### The review screen: one component, then five, now one again

**This section used to describe `frontend/src/app/screens/review-screen.ts` — one component,
nine states — and then the five gate screens that replaced it.** Both are deleted. The
reasoning is worth keeping because it reversed twice:

1. One generic editor was tried first. It could not implement any of the gates' completion
   rules faithfully, because the gates enforced genuinely different rules.
2. Five screens replaced it, one per gate.
3. Feature verification was withdrawn, leaving two gates presented as three stages of one
   flow — and because a person walks that flow forward and back, the drafts belong in one
   component again. See "The three stages" above.

The claim from that era still worth carrying forward: **one idempotency key per user action,
not per request.** A retry of an action reuses its key so the backend replays the stored
response; a new action mints a new one. A key minted per request would make every retry a new
mutation.

### Frontend: Angular 22, standalone components, no leftover scaffolding

**The frontend was migrated from React/Vite to Angular.** The React build is gone, not
kept alongside — its last commit is `7726b66d` if you need to diff against it. Nothing
about the backend changed: same endpoint, same SSE contract, same request shape.

The migration was cheap for reasons worth knowing before any future framework change:
roughly 1,900 lines of the old app were plain hand-written CSS and ~150 lines were pure
TypeScript (the export logic), and **both ported verbatim**. Only ~560 lines were actually
React-shaped, and that code used just four `useState`, two `useEffect` and one `useRef`,
with no context, router, memo or state library to unpick.

Layout of `frontend/src/app/`:

- `app.ts` — the shell. Owns the sidebar, the theme toggle, the `screen` signal that selects
  which of the five signed-in screens renders, and `startRunning()`, which drives the SSE
  run. State is Angular signals; there is no router, because the app has never had URLs to
  restore and adding routing would be new behaviour rather than a port. **The whole
  workspace is gated on `auth.signedIn()`** — signed out, the sidebar is **not rendered at
  all** and `LoginScreen` replaces the `@switch` entirely. The rail used to render empty
  (mark + theme toggle only); it now sits inside `@if (auth.signedIn())`, which is also why
  the `@if`s that used to guard the nav and Sign out from *within* the rail are gone —
  nothing inside it can be reached signed out. The theme toggle survives as `.theme-fab`, a
  `position: fixed` button in the page's top-right corner, because it is the one control
  that still applies with no session. It is `fixed` rather than `absolute` on purpose:
  `.screen` is the scrolling box, so anchoring to it would let the toggle scroll away.

  **The sidebar collapses**, `sidebarOpen()` persisted to localStorage, 264px ↔ 72px.
  Collapsed is **icon-only**, not the old icon-over-label: at 72px a label wraps or
  truncates, and that is what made a Recents list impossible. Every control keeps a
  `title` so the collapsed state still names itself on hover. The CSS is written
  expanded-first with `.brand-rail:not(.rail-open)` overriding, because expanded is the
  default. Below 720px the sidebar becomes a horizontal top bar and the toggle, wordmark
  and Recents are all hidden — there is nothing to collapse.

  **Recents** lists the last `RECENTS_SHOWN` (8) runs by description, expanded-only, and
  clicking one opens that report. It is the reason `loadHistory()` is now called on
  sign-in (via `LoginScreen`'s `signedIn` output and `restore()`) rather than only when the
  History screen opens, and the reason a finished run *refetches* history instead of
  clearing it — dropping the cache would blank the sidebar on the one action that just
  added a row to it. It shares the History screen's three-state rule: loading, failed and
  empty stay distinct. Only the `<ul>` scrolls, and both it and `.rail-recents` need
  `min-height: 0` or a long history pushes Sign out past the bottom of the viewport
  instead of scrolling.

  **Clear all** sits beside the Recents heading and is the app's only destructive
  control. It goes through a **two-step in-sidebar confirm, not `confirm()`** — the app
  removed its last native dialog on purpose (see `ErrorScreen`), and `DELETE /api/history`
  is irreversible on both sides. Afterwards the list is *refetched* rather than emptied
  locally, because the server is the record of what exists; and if the open report came
  from history it is dropped, since it would otherwise be a rendering of a run that no
  longer exists.
- `config.ts` — `API_BASE_URL`, replacing `import.meta.env.VITE_API_URL`. See the running
  section above for the `window.NOVELTY_LENS_API_URL` override.
- `models/report.ts` — `AnalysisReport`, `HistoryEntry`, the Quick/Advanced phase orders,
  `PHASE_LABELS`, `PHASE_ICONS`, and the `StreamEvent` union. The two route orders mirror
  `backend/graph/events.py`.
- `services/auth.ts` — the session. Token in localStorage, `restore()` re-validates it
  against `GET /api/auth/me` on startup, and `authorizedFetch` attaches the header and
  **ends the session on any 401**. Every authenticated call goes through it. There is no
  signup method, because there is no signup endpoint.
- `services/analysis.ts` — the SSE reader. Raw `fetch`, deliberately; see the SSE section.
  EventSource is now doubly impossible: it can carry neither a POST body nor a header.
- `services/history.ts` — `GET /api/history`, plus `reportFromHistory`, which maps an entry
  onto `AnalysisReport` so a past run and a fresh one render through one display path. It
  derives `title`/`date` (from `description`/`created_at`) and invents nothing else.
- `services/export.ts` — PDF / Word / JSON. Ported verbatim from React, and
  `buildReportHtml`'s template literal is byte-identical, so exported documents are
  unchanged: the `.doc` and `.json` produced by the Angular build compare byte-for-byte
  equal to the React build's output for the same report. Keep it that way when editing.
- `screens/` — `login-screen`, `submit-screen`, `running-screen`, `report-screen`,
  `history-screen`, `error-screen`.
- `shared/` — `chrome.ts` (brand header only; the page footer was removed), `export-menu.ts`, `icon.ts`,
  `icon-paths.ts`.

**Icons are inlined, not a dependency.** `lucide-angular` peers on Angular 13–21 while
this app is on 22, so `shared/icon-paths.ts` holds the 22 glyphs the UI uses, extracted
verbatim from the `lucide-react` package the React build used — identical artwork, one
fewer runtime dependency. It is generated data: to add an icon, take its markup from
lucide rather than hand-drawing a path. `shared/icon.ts` renders them through
`bypassSecurityTrustHtml`, which is safe *only* because `ICON_PATHS` is a compile-time
constant in the repo and `name` can only index into it — do not extend that component to
accept arbitrary markup.

**All the Figma scaffolding is gone.** The React `package.json` carried MUI, recharts,
react-dnd, react-router, tailwind and a whole shadcn/Radix component directory that the
real UI never used. The Angular `package.json` has 8 runtime dependencies, all of them
Angular framework packages. **Tailwind was verified unused before being dropped** — the
one apparent utility class in the old markup turned out to be the custom class
`report-grid`. Dropping it is most of why the shipped CSS fell from ~104 kB to ~28 kB, and
the total bundle from ~277 kB to ~190 kB.

`frontend/public/` holds the brand assets the UI references by absolute path.
Angular copies `public/` to the build root verbatim, so those paths work in both dev and
`dist/`.

**The product has its own mark — `novelty-lens-mark.svg` — and it ships in three
cuts.** It is not the Aramco AI mark and no longer uses that mark's palette. The drawing
is four viewfinder brackets framing a 4×4 dot grid (the corpus of prior art being
scanned), split into two ranks above and below a centre scan-line that fades in from both
edges to a single focus dot at 32,32 — the comparison pass that turns "prior art found"
into the novelty score, which is the one thing the product actually measures. The brand
gradient is `#643278` → `#26A8AB`, left to right (`x1`/`x2` vary, `y1`/`y2` constant — do
not go back to a diagonal `x1,y1`→`x2,y2` sweep). All three cuts are hand-written SVG on a
64-unit grid, optically centred on 32,32, and each carries a comment explaining what it is
for:

- `novelty-lens-mark.svg` — the colour cut. Used by `shared/chrome.ts`
  (`.brand-mark`) on every screen, **and on the rail in dark mode**.
- `novelty-lens-mark-white.svg` — a **drawn** reversed cut, used on the rail in **light
  mode only**. It is not the colour cut with a filter over it. `globals.css`'s
  `.rail-brand img` used to carry `filter: brightness(0) invert(1)`, which is exactly
  what a flat white re-draw achieves anyway now that the mark is a uniform dot grid rather
  than an organically-weighted cluster — brackets and the centre dot are fully opaque, the
  sixteen grid dots sit at a flat 0.86 opacity, and the scan-line is reproduced as a plain
  white line at 0.5 opacity since it can't carry over the two-colour gradient this cut
  doesn't have. **Do not add the filter back** — the geometry is still hand-drawn per cut,
  not filtered, so the two files can diverge on purpose if a future redesign needs them to.

  `app.ts` picks between the two off the `theme()` signal, and the split tracks the tile,
  not the theme: light still paints the blue→green gradient behind the mark, which needs
  the reversed cut; dark made `.rail-brand` transparent when it was flattened, so the mark
  sits on flat black and shows its real colours. If either tile ever changes, the pairing
  changes with it.
- `novelty-lens-favicon.svg` — the small-size cut for `<link rel="icon">` in
  `src/index.html`, with the dot grid cut from sixteen dots to four and every stroke
  thickened. Below ~32px the full grid and the scan-line fill in to a smudge. Use the full
  mark above that size.

The two `aramco-ai-*.png` files are both **retired** and nothing loads either one.
`aramco-ai-logo.png` was the full lockup, which baked the wordmark into the image;
`aramco-ai-mark.png` was the standalone mark that stood in until the product had its own,
and it was also the favicon. **`aramco-wave-background.jpg` is now retired too** — it
backed the dark `.screen` until dark mode was flattened (below), and nothing references it
any more. The file is still in `public/`; deleting it is a separate call, and note Angular
copies everything in `public/` into the build whether it is used or not.

**Buttons follow the Aramco Pixel component spec**, not the old house style. Contained is
a flat solid blue, Outlined is a blue border and blue text on no fill, and disabled is flat
grey — the previous blue-to-green gradient, the lifting hover and the 45%-opacity disabled
state are all deliberately gone. Every value is a token in `:root`: `--btn-blue`,
`--btn-blue-hover`, `--btn-blue-active`, `--btn-disabled-bg`, `--btn-disabled-fg`,
`--btn-radius`, plus `--btn-blue-dark` / `--btn-blue-dark-hover` for dark mode. **Those
colours and the 8px radius were matched by eye from a photo of the Pixel Components page,
not taken from a spec** — if you get the real values, changing those tokens updates every
button, and nothing else needs touching.

**Accent colour is Aramco's secondary palette: Teal `#26A8AB`.** Real published brand
values, unlike the button hexes, which are still eyeballed. The brand sanctions exactly two
tints and no others, so they are tokens rather than something to mix per call site:
`--teal` `#26A8AB`, `--teal-70` `#67C2C4`, `--teal-50` `#92D4D5`. (The portal page
contradicts itself — its prose says "70% and 40%" while its swatches render 70% and 50%.
These follow the swatches; 40% is `#A8DCDD` if the prose turns out to be the authority.)

`--teal-ink` is the **one value here that is not published**, and it exists because the
brand's tints are all *lighter* than the base while `#26A8AB` is only 2.89:1 on white —
unusable for 11–13px chip and label type. It is the brand hue at 72% lightness
(`#1B797B`, 5.16:1 on white) in light mode, and flips to the sanctioned `#67C2C4` tint in
dark (5.89:1 on a card). **Because it is one token, every chip, pill, label and count
flips with the theme and needs no per-rule dark counterpart** — that is what it is for.
Fills, borders and icons use `--teal` itself; only type reaches for `--teal-ink`. A solid
teal fill carrying white text uses `--teal-ink` as the *background* in light and `--teal`
with a dark glyph in dark, because white on the base teal fails.

**Green did not go away, and the split is deliberate: teal is decoration, green is
meaning.** Everything still green is semantic — the `risk-low` end of the High/Medium/Low
scale (`.score-ring`, `.score-bar`, `.risk-label`, `.risk-legend-item`, `.history-score`)
and `.risk-pill.risk-low`. Recolouring those to brand teal would destroy the one thing their
colour communicates. If you are adding an accent, use teal; if you are adding a status,
match the existing scale. The dark `.result-count` / `.risk-pill` rule was split for
exactly this reason — they had shared a declaration and no longer belong together.

The light theme is **plain white** (`:root`, `.app-shell` and `.screen` all `#fff`) and
keeps its glass, shadows and gradients. The
"Typical result in under 60 seconds" note and the page footer were removed from every
screen, along with their CSS and the `Clock3` icon. (`LockKeyhole` went too, then came
back for the login screen.)

**Dark mode is flat, and that is a requirement, not a style opinion.** It was reshaped to
match aramcoMETABRAIN: a **black** rail (`--rail-black: #000`), a plain grey page
(`--aramco-gray: #2b2b2b`), and cards exactly one step lighter (`--card: #353535`) with a
1px hairline border. Three surface values, no fourth. Rail labels are **plain `#fff`**.

Because the labels are white, the rail's active state cannot be carried by colour: dark
gives `.rail-item-active` a flat `rgba(255,255,255,0.12)` fill, with an explicit
`:hover` counterpart at `0.16` — `.rail-item:hover` is (0,3,1) and would otherwise beat
the (0,3,0) active rule and make the current screen go *dimmer* under the pointer. This
was a live instance of the specificity trap below: `:root[data-theme="dark"] .rail-item`
had been silently eating `.rail-item-active` (0,1,0) since dark mode shipped, which nobody
noticed while the rail had exactly one, permanently-active item.

**Every visual effect is switched off in dark**, each with an explicit `none` rather than
being left to a shorthand — the wave photograph, `.glass-card`'s `backdrop-filter`, every
decorative `box-shadow` (rail, cards, export dropdown, the active phase-icon halo, the
failed-label dot), the `.rail-brand` gradient tile, `.rail-item:hover`'s `translateY` lift,
and `.brand-mark`'s `drop-shadow`. A new dark surface gets a flat colour and a border,
nothing else.

Two things are deliberately **not** treated as effects and must stay: the focus rings
(`box-shadow: 0 0 0 4px`) and the loading spinner. The input `:hover, :focus` rules were
split precisely so hover could go flat while focus keeps its ring — recombining them and
setting `none` would be an accessibility regression wearing a style change's clothes.

The green-tinted recommendations card and the gradient logo tile are gone from dark for
the same reason. `--shadow` resolves to `none` in the dark block, so anything reading that
token is already flat.

**A specificity trap has bitten four times in this stylesheet, so assume it will
again.** The dark rules are written `:root[data-theme="dark"] .foo`, which is specificity
(0,3,0) — the same as or higher than a state rule like `.progress-step.complete
.phase-icon` (0,3,0) or `.rail-item-active` (0,1,0) — and the dark block sits later in the
file, so it silently wins and erases the state. It flattened the required asterisk to grey,
then every error-screen phase pip, then every completed and in-progress step in the run
list, and it had been eating the rail's active item the whole time without anyone
noticing. Each now has an explicit dark counterpart. **Any state class you add needs one
too**, and the symptom is never an error: it is a state that renders identically to its
neutral, so it only surfaces when someone looks at the right screen in the right theme.

**The other half of the same problem: a new class with no dark rule at all.** The redesign
in `3dad9f6b` landed `.patent-card`, `.risk-legend-*` and `.summary-why` with hardcoded
light greys, one of them (`#465266` on the `#353535` card, ~1.4:1) effectively invisible;
renaming `.patent-row` to `.patent-card` also orphaned two dark rules onto a class no
template uses any more. Nothing errored either time. **Renaming a class means grepping the
dark block for the old name**, and any new class carrying a literal colour needs a dark
counterpart in the same commit.

Styling is unchanged from the React build and is **global, not component-scoped**:
`src/styles.css` imports `styles/fonts.css`, `styles/theme.css` and `styles/globals.css`
in that order, and every component carries plain `class` attributes matching that CSS. The
components deliberately declare no `styleUrls`. If you move a rule into a component's
styles, Angular's view encapsulation will scope it and the rest of the app will stop
matching it — that is the main way to break this stylesheet.

**Typography is Aramco Ghawar, served from `public/fonts/`, with no webfont CDN.**
`styles/fonts.css` is the only place fonts are declared: nine `@font-face` blocks (Hairline
100 through Black 900, OTF release `Ghawar 1.312-Beta 4.0`) plus `--font-sans`,
`--font-display`, and `--font-mono`. All three variables resolve to Ghawar, and **every**
`font-family` in `globals.css`, `theme.css`, and the PDF-export stylesheet inside
`services/export.ts`
goes through one of them — do not hardcode a family anywhere, change the variable instead.
The Google Fonts `@import` that used to sit at the top of this file is **gone**, along with
IBM Plex Sans / Space Grotesk / JetBrains Mono; the fallback tail is now plain system fonts,
so nothing this app renders touches an external host.

Two properties of the font release are load-bearing and are documented at length in
`frontend/public/fonts/README.md`:

- **Each weight is its own internal family** (`Ghawar_1.312-Beta_4.0 Bold`, subfamily
  `Regular`), not nine members of one family. The nine `@font-face` blocks are precisely
  what collapse them into a single `Aramco Ghawar` family with real weight values, which is
  why `font-weight: 600` resolves at all. Collapsing them into one rule breaks every weight.
- **There are no italic files.** Italics in the UI are browser-synthesised obliques. Adding
  `font-style: italic` faces is the fix if a release with true italics ever ships.

The `local()` entry on each face embeds the font's versioned internal name, so a font update
silently stops matching it and falls through to the `url()` — degraded to "an installed copy
is no longer preferred", not a break. Note the vendor ships SemiBold misspelled as
`SmeiBold`; the file here is renamed correctly but the `local()` string keeps the typo
deliberately, because that is what is inside the font. The UI only ever requests four
weights (400/500/600/700, ~165 KB), the rest are declared so a weight change just works.
Ghawar is licensed rather than open source — see that README for the still-open question of
whether the binaries belong in git.

There are **six** screens — `login`, `submit`, `running`, `report`, `history`, and `error`,
one component each under `src/app/screens/`. `login` is outside the `screen` signal
entirely: `app.ts` branches on `auth.signedIn()` before the `@switch`, so there is no way
to reach a signed-in screen without a token. The `buildReport` helper that used to sit near
the top of the file is **gone** as of the redesign (it had been dead code since the Phase 3
rework); `startRunning` builds the report inline from the `result` event.

**Nothing persists client-side, and `history` is not an exception.** The server is the only
record of a past run; `HistoryScreen` refetches every time it is opened, and `signOut()`
clears the cached list along with the current report — leaving either one rendered behind a
login screen would leak the previous user's data to the next one. `startRunning` also drops
the cached list on completion, since the backend stored that run before sending the result.

`HistoryScreen` keeps loading, failed, and genuinely-empty as three distinct states.
Collapsing a failed fetch into "no searches yet" would report a broken database as an
ordinary empty list — the same substitution the backend's zero-tolerance policy forbids,
just moved into the UI.

`error` is the newest of the six and the only screen not in the original design handoff.
A failed run used to fire a native `alert()` and drop the user back on `submit`, which
threw away everything the stream had already said. `ErrorScreen` now renders concise,
high-level recovery guidance, names the phase it died in, and shows how many
phases in the selected Quick/Advanced route completed. Retrying preserves that route; an
Advanced Search failure cannot silently become a Quick Search. Backend details are still
sanitized before storage and transport, but the browser treats them as diagnostic data
(`server.py` passes them through `workflow/redaction.py`'s `safe_detail`). The public
message comes from the frontend's centralized mapping rather than the provider response or
workflow exception. `startRunning` tracks `lastPhase` in a local declared
*outside* the `try` precisely so the `catch` can read it without waiting on a signal update.
It offers retry and back, and never renders a partial report. Sanitized backend details
remain diagnostic data for logs and application logic; they are not rendered because
provider names, HTTP statuses, workflow states and exception classes do not help an end
user recover. `frontend/src/app/shared/public-errors.ts` is the single source for public
error copy and maps internal failure codes to short English guidance.

**The zero-tolerance policy applies to this file too.** It used to invent content whenever
the backend sent nothing: three hardcoded "recommendations" when the list was empty, a
per-patent rationale reading *"Shares common vocabulary or systemic overlap with your
primary description."*, and an `"Extracted Concept Cluster"` placeholder concept. All
three are gone — the empty case now says so plainly. The redesign briefly reintroduced the
same mistake in a new place, seeding the submit screen's concept chips with a hardcoded
`["Water treatment", "Consumer hardware"]`; that was removed on merge, and the submit
screen's chip row has since been deleted outright along with the optional Draft claims
field (the report screen still renders real concepts). Note the request body still sends
`claims: ""` — the backend accepts the field, nothing collects it any more. Two lesser
defaults survive in `startRunning`'s result mapping for malformed provider data
(`"Unknown publication"` and `"Untitled patent record"` for a patent with no id or title,
plus `"No detailed rationale was returned for this record."`); they are reachable only if a
provider returns a record the backend's `PatentRecord` normalization left blank.

### Performance: where a request's time actually goes

Measured end-to-end on a live request (13 candidates, 20 keyword hits), **123.5s before
the current round of fixes**. The progress events are emitted *before* their work, so
reading the SSE timestamps naively misattributes every phase — the real breakdown was:

**Every row below predates the current split-provider configuration** and was measured with
the HuggingFace Router for both Llama-3.1-8B and bge-large. The current LLM uses the internal
Aramco gateway while embeddings use Hugging Face BGE again, so network latency and provider
load still make the LLM and embedding rows non-predictive. Re-measure before optimizing
against them; the *shape* of the analysis (what dominates, and why) is what still holds.

| Work | Before | After (measured) |
|---|---:|---:|
| `compare.py` chunked embeddings | 55.6s | **12.8s** (small concurrent batches) |
| `store_patents` → Qdrant indexing | 21.8s | **8.1s** (documents truncated) |
| `enrich_with_full_text` (2 HTTP calls/patent, 8 workers) | 13.8s | 15-20s (unchanged; varies with provider) |
| Keyword search (5 queries) | 13.4s | **2.5-3.6s** (queries concurrent) |
| `analyze_all_matches` LLM | 10.3s | 9.5-12.3s (unchanged) |
| `validate_relevance` LLM | 4.0s | 2.8s (unchanged) |
| `understand_invention` LLM | 3.1s | 2.9s (unchanged) |
| Semantic search | 1.6s | 1.9s (unchanged) |
| **End to end** | **123.5s** | **~62s** |

Three things worth knowing before optimizing further:

- **`store_patents` was uploading ~99.3% waste.** Indexed patents average 273,000
  characters and BGE caps input at 512 tokens, so a 20-patent batch shipped ~5.5 MB to
  compute vectors reflecting each document's first ~2,000 characters. `MAX_INDEX_WORDS`
  in `search/vector_store.py` truncates at 600 words, slightly above BGE's effective token
  window so the endpoint performs the final truncation. Lowering it into the effective
  window would change stored vectors and force a re-index. See that constant's docstring.
- **Embedding concurrency needs small batches, and the two settings are coupled.** The
  first attempt used `BATCH_SIZE=32` with 4 workers and came out *slower than serial*,
  because a 32-text request under concurrency exceeded `_embed_batch`'s 30s read timeout
  and each timeout burned 30s before retrying. `BATCH_SIZE=8` / 8 workers / 60s timeout
  is benchmarked, not guessed — the numbers are in `embeddings.py`'s `BATCH_SIZE`
  docstring. Raising the batch size without raising the timeout reintroduces the bug, and
  it presents as "parallelism made things slower."
- **Indexing is on the critical path and buys the current user nothing** — it grows the
  corpus for *later* requests. Moving it behind the `result` event was considered and
  rejected: truncation already took it from 21.8s to ~8s, which does not justify a
  zero-tolerance exception for a write whose failure nobody would see.
- **There is no caching of any kind.** Repeat the identical search and it costs the full
  pipeline again: ~5 SerpAPI searches, ~20 Details calls, ~20 description-page fetches,
  4 LLM calls, and several embedding round trips. This is the single largest remaining
  source of avoidable work, and it is a deliberate open question rather than an oversight
  (the Postgres layer that used to do it was removed on purpose).

### Migration status and standing constraints (see `docs/`)

The `StateGraph` and the graph steps as nodes are fully landed and are the *only* pipeline
— the legacy generator and the `PIPELINE` flag that once chose between them are deleted,
not just superseded (see the backend architecture section above). Validation and
embedding-based ranking with no TF-IDF fallback are live on every request. **Semantic
Search, Result Fusion, and Qdrant indexing are written but currently disabled** — see the
`backend/graph/` section above for the exact mechanics; don't trust older language in this
file (or elsewhere) claiming they run in parallel on every request. The frontend now does
consume the SSE stream for real (Angular's `services/analysis.ts`), which used to be an
open item when the frontend was still React and called the non-streaming endpoint — that
gap is closed. The full outage-injection failure-path test matrix (Qdrant down,
single-provider-down, etc., beyond the handful of focused tests that exist per agent) is
still open. **Phase 2 is now half-landed**: persisted history is live (Postgres,
`reports.py`, `GET /api/history`, the History screen), caching is not and is deliberately
open — see the Persistence section above for the line between them. Two constraints govern
all of it:

- **Zero-tolerance failure policy**: no fallback, ever. If a step can't produce a genuine
  result, it must fail the request with a clear `error` event — never substitute
  fabricated or placeholder output. Commit `e96edfac` removed a fabricated score and the
  `_fallback_rationale` / `_fallback_recommendations` pair in `backend/llm.py`; do not go
  looking for them. The two fallbacks that outlived it — "succeed if any provider works"
  and the canned `"No reference targets found…"` recommendation — lived in the legacy path
  and went with it. **There are now no fallbacks anywhere in the backend.** Adding one back
  is the single most consequential way to break this project's contract with its users.
- **USPTO is out of scope** — no API access is available. Don't add a USPTO provider.
