# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Keeping this file current

This project is mid-migration from the linear pipeline described below to the LangGraph
multi-agent architecture proposed in `docs/LangGraph-Multi-Agent-Architecture-Plan.docx`
(diagrams in `docs/assets/`). **When you implement any part of that plan** — a
`backend/graph/` package, `backend/embeddings.py`, `backend/search/vector_store.py`, the
`PIPELINE` flag going live, PostgreSQL persistence, etc. — update the Architecture
section below in the same edit so it describes what the code actually does, not the
pre-migration pipeline. More generally: if you add or remove a major dependency, data
source, or persistence layer, or change the SSE event contract, reflect it here before
you finish the task, not as an afterthought.

## What this project is

A patent novelty-search tool: a user describes an invention in natural language, the
backend generates search queries, retrieves candidate prior-art patents from live
external APIs, ranks them by similarity, and returns an LLM-written rationale per match
plus drafting recommendations. There are two independent processes — a FastAPI backend
and a Vite/React frontend — with no shared tooling; each is run and developed separately.

## Commands

**Backend** (from `backend/`):
- Install deps: `pip install -r requirements.txt`
- Run the API: `python server.py` — serves on `http://127.0.0.1:8001` (hardcoded host/port in `server.py`'s `__main__` block)
- Apply Postgres migrations: `python migrate.py` (needs `docker compose up -d --wait` from the repo root first; safe to re-run, no-ops if already applied). Only matters if you're running `PIPELINE=legacy` — see below.
- Run the tests: `python -m pytest` from `backend/` (config in `backend/pytest.ini`, which sets `pythonpath = .` because the modules import each other flat, e.g. `from config import settings`). The suite is `backend/tests/`. `backend/tests/conftest.py` stubs embeddings, Qdrant, and (as of ticket P2.2) Postgres (all autouse, every test) alongside the existing `_llm_chat`/`SOURCES` stubs — `compare.py`'s ranking, the semantic-search path, and the legacy pipeline's new `connect()` call are all on the happy path now, not just their own tickets' tests, so the suite still needs no credentials, no network calls, and no running database. `tests/fakes.py`'s `FakePostgresDB` recognizes exactly the SQL `cache.py`/`reports.py` issue, the same narrow-scope approach `FakeQdrantClient` already used. `test_pipeline_parity.py` covers legacy↔LangGraph parity (no longer byte-identical on the happy path — see below); `test_phase3_agents.py` covers Semantic Search, Fusion, and Validation individually; `test_search_cache.py` covers the P2.2 cache (a repeat query must not call a provider twice, and a later enrichment write must not be clobbered by an earlier, pre-enrichment cache write for the same patent). This is not yet the full P3.8 failure-path matrix (LLM failure, single-provider failure, Qdrant/embeddings outage, zero-similarity are covered for the parts that existed pre-Phase-3; a dedicated outage-injection suite for the new agents is still open).
- Still no lint config and no `pyproject.toml`. `pytest-asyncio` remains unused — the tests are all synchronous.

**Frontend** (from `frontend/`):
- Install deps: `npm install`
- Dev server: `npm run dev` — Vite on its default port (`http://localhost:5173`); no `server.proxy` is configured
- Build: `npm run build`
- No lint, test, or typecheck script is defined, and there is no `tsconfig.json` — `npm run build` (Vite) is the closest thing to a correctness check today.

**Local infrastructure** (from the repo root): `docker compose up -d --wait` starts
PostgreSQL 17.10 (`127.0.0.1:5432`) and Qdrant 1.18.3 (`127.0.0.1:6333` HTTP, `6334`
gRPC), both with persisted named volumes and healthchecks, so `--wait` returns only once
both are actually serving. **PostgreSQL is read by the legacy pipeline only, as of ticket
P2.2** — `_run_pipeline` opens a connection as its first action and fails the request
cleanly if it's unreachable, so `PIPELINE=legacy` now requires a running, migrated
Postgres; `PIPELINE=langgraph` (the default) does not touch Postgres at all. **Qdrant runs
the other way round**: it's read/written by the graph's `keyword_search`/`semantic_search`
nodes (`backend/search/vector_store.py`), so it's `PIPELINE=langgraph` — the default — that
requires a Qdrant-reachable environment to run at all, not just for local development
convenience; legacy doesn't touch Qdrant. The backend and frontend are not containerized.

Keep the Qdrant image version in step with `qdrant-client` in `requirements.txt` — the
client warns when the server is more than a minor version away.

**Running the app**: start both processes manually in separate terminals. There is no
root script that runs them together, and no README. The frontend calls the backend at a
hardcoded absolute URL (`http://127.0.0.1:8001`), so the backend must be running on that
exact host/port. The Docker services are not required to run the app as it exists today.

**Env vars** (backend, via `backend/.env` — see `backend/.env.example`): `HF_TOKEN`
(HuggingFace router auth, required for all LLM calls), `SERPAPI_KEY` (Google Patents),
`EPO_CONSUMER_KEY` / `EPO_CONSUMER_SECRET` (EPO OPS OAuth2), `QDRANT_URL` /
`QDRANT_API_KEY` (required for `PIPELINE=langgraph`, the default), and `DATABASE_URL`
(required for `PIPELINE=legacy` only, as of ticket P2.2). Optional: `LLM_API_URL`,
`LLM_MODEL`, `EMBEDDING_MODEL`. All the service URLs default to values matching the local
Compose services.

`PIPELINE` selects which pipeline serves requests. **It defaults to `langgraph` as of
ticket P1.5** — the LangGraph graph is what serves a request unless you say otherwise.
`backend/config.py` raises `ConfigError` at import time unless the value is exactly
`legacy` or `langgraph`, so a typo fails at startup instead of silently running the wrong
pipeline. `server.py` resolves `_active_pipeline` once at import from that value, and
`backend/graph/` is imported only when `langgraph` is selected, so `PIPELINE=legacy`
remains a working escape hatch even if the `langgraph` install is broken.
`PATENTSVIEW_API_KEY`/`uspto_api_key` remains dead code — see the USPTO note below.

## Architecture

### Backend: two pipelines, selected by the `PIPELINE` flag

`psycopg[binary]`, `yoyo-migrations`, and `rank_bm25` sit in `backend/requirements.txt` but
are **imported nowhere** — they are pre-staged for Phase 2. `sentence-transformers` is
listed but also unused *by design*: `backend/embeddings.py` calls a HuggingFace-**hosted**
model over the network (see below), not a local `sentence-transformers` model — do not
"fix" this by importing it. `langgraph` and `qdrant-client` are live and load-bearing.
`scikit-learn` was removed from `requirements.txt` — `compare.py`'s TF-IDF fallback (the
only thing that used it) is gone.

**`backend/graph/` serves requests now**; the legacy generator below runs only under
`PIPELINE=legacy`. Legacy is nonetheless unchanged and must stay that way — it is the
parity reference `backend/tests/` diffs the graph against, and the escape hatch if the
graph misbehaves in production. Retiring it is a team decision, not a cleanup. The legacy
flow is a single generator, `_run_pipeline` in `backend/server.py`, reached through
`_active_pipeline` by both `POST /api/analyze/stream` (SSE) and `POST /api/analyze`
(drains the same generator, returns only the final JSON). It runs, in strict sequence:

1. `understand_invention` (`backend/llm.py`) — one Llama call that returns concepts, keywords, and exactly 5 search queries (the prompt also asks for synonyms; nothing downstream reads them).
2. `search_patents` (`backend/search/search.py`) — fans every query out across all providers in `SOURCES` (currently Google Patents + EPO only; USPTO was evaluated and is intentionally **out of scope** — no API access), isolates per-provider failures, dedupes by uppercased patent ID.
3. `enrich_with_full_text` (`backend/search/google.py`) — fetches full description + claims text for Google-sourced results via the Details API (records with no `serpapi_id`, i.e. non-Google sources, are left with an empty `full_text` and downstream code falls back to the abstract). Feeds richer input to step 4 than an abstract alone.
4. `compare_invention_to_patents` (`backend/compare.py`) — embedding-based cosine similarity (chunked, via `backend/embeddings.py`). Raises on failure; there is no TF-IDF fallback.
5. `analyze_all_matches` (`backend/llm.py`) — one batched Llama call producing a rationale per patent plus shared recommendations (batched deliberately, to stay friendly to provider rate limits).

Each stage `yield`s a progress event before/after it runs; `server.py` wraps these as
`data: <json>\n\n` SSE frames.

**The SSE contract spans `backend/server.py` and `frontend/src/app/App.tsx` again, for
real this time.** An earlier version of this section said the frontend didn't consume the
SSE stream at all and just faked progress with a `setInterval` timer — that was true once,
but a prior edit here went stale against the code (per this file's own "verify against the
source" rule) and nobody caught it. As of the current `App.tsx`: `streamAnalysis()` reads
`POST /api/analyze/stream`'s real body via `fetch` + `getReader()` (not `EventSource`,
which can't carry a POST body), and `PHASE_ORDER` (top of the file) is a real, maintained
mirror of `backend/graph/events.py`'s `PHASES` tuple — the comment directly above it says
so. If you find this paragraph disagreeing with `App.tsx` again, trust `App.tsx` and fix
this file, not the other way around. Every event has a `type` of `progress` | `result` |
`error`. `progress.phase` is one of the values in `PHASES` (nine now — see below); an
unrecognized phase does not error client-side, it just never advances `phaseIdx` past it
(`App.tsx`'s `indexOf` returns -1, silently ignored) — so a phase-vocabulary change with no
matching frontend update is still a silent bug, not a loud one. The stream must end with
exactly one `result` event (data shape: `patents[].{num,title,pct,risk,rationale,source}`
plus `title`, `date`, `concepts`, `top_score`, `verdict`, `recommendations`, `query_count`)
or a client reading the stream would throw.

### `backend/graph/` — the LangGraph pipeline

**This is the default pipeline** — it serves every request unless `PIPELINE=legacy` says
otherwise. As of Phase 3 it genuinely branches and does more than legacy on the happy
path, so it is **no longer byte-identical to legacy's SSE stream** the way Phase 1 left
it — see the divergence table below. `keyword_search` and `semantic_search` both run off
`search_orchestrator` and converge at `fuse`, which feeds `validate`, which feeds `rank`.

- `state.py` — `GraphState`, the `TypedDict` every node reads and writes. It carries the
  `backend/models.py` dataclasses rather than re-flattening them. `semantic_patents`,
  `fused_patents`, and `validated_patents` are no longer reserved-for-later fields — every
  one is written by a live node (see below). `total=False`, so later tickets can still add
  keys additively. **It has no error field on purpose** — under the zero-tolerance policy a
  node that cannot produce a genuine result raises, and an error slot would invite nodes to
  record a problem and continue to a degraded answer.
- `events.py` — `PHASES` plus `emit_progress`, which **raises `SSEContractError` on an
  unknown phase name**. `PHASES` now has nine entries: `understand`, `queries`, `search`,
  `enrich`, `semantic_search`, `fuse`, `validate`, `compare`, `analyze`. There is no
  frontend `PHASES` array to keep this in lockstep with today (see the SSE contract note
  above) — extending this tuple is backend-only and safe until that changes.

- `understanding.py` — Query Understanding + Query Expansion (P1.2), over `llm.py`
  unchanged. Contains no `except`: a failed or malformed LLM response fails the request.
- `searching.py` — Search Orchestrator, Keyword Search, and Semantic Search. Keyword search
  keeps its one deliberate behavior change from legacy: one provider failing fails the
  whole request, via `search_all_providers_strict` in `backend/search/search.py` (added
  alongside `search_patents`, which is unchanged and still backs legacy). It also enriches
  results with full text (`enrich_with_full_text`, skipped entirely when nothing was
  found) and indexes them into Qdrant (`store_patents`) — no `except` around either, so an
  unreachable Qdrant fails the whole request rather than silently shipping a keyword-only
  result. Semantic Search (`semantic_search_node`) runs in parallel and queries whatever
  Qdrant already holds from *earlier* requests — not this request's own keyword hits, which
  aren't indexed yet when it runs. A Qdrant collection that simply doesn't exist yet (cold
  start) returns an empty list rather than raising (`search/vector_store.py`); an
  unreachable Qdrant still raises.
- `fusion.py` — Result Fusion. Reciprocal Rank Fusion (`backend/ranking.py`,
  `reciprocal_rank_fusion`, pure and unit-tested directly) over the keyword and semantic
  result sets, deduped by patent ID.
- `validation.py` — Validation. One batched LLM call (`llm.validate_relevance`) judges
  relevance over the top of the fused set (capped at `MAX_CANDIDATES = 30` candidates, to
  keep the prompt bounded). No `except`: a missing or malformed verdict for any candidate
  fails the request rather than being treated as an implicit keep or drop. An empty fused
  set skips the LLM call entirely rather than validating nothing.
- `ranking.py` — Ranking + Explanation (P1.4). Calls `compare.py`'s embedding-based scoring
  (no TF-IDF fallback as of Phase 3 — see `compare.py` below) and `analyze_all_matches` with
  no `except`. The displayed score is the real one, truncated and clamped to 0–100 and
  nothing else — a dissimilar patent shows an honest `0`. Also holds `format_matches` and
  `verdict_for`, which build the `patents[]` entries and the novelty verdict.

- `pipeline.py` — graph assembly, the terminal `result` event, and `run_graph_pipeline`,
  the generator `server.py` consumes. Signature-compatible with `_run_pipeline` so the
  flag can swap them. **It catches nothing**: a raising node propagates out to
  `server.py`'s existing wrapper, which emits one `error` event. There is deliberately no
  point at which a partial result could be assembled and sent. The single "found anything?"
  routing check now runs after `validate` (not right after `keyword_search`, since both
  search branches and validation can each reduce the candidate set to zero).

Nodes emit progress through LangGraph's custom stream writer, which preserves call
ordering — the same interleaving the legacy generator got from `yield`.

**Where the two pipelines diverge.** Since LangGraph is now the default, the right-hand
column is what users actually get — the left is what `PIPELINE=legacy` falls back to:

| Condition | Legacy | LangGraph |
|---|---|---|
| One search provider fails | narrows the corpus, returns a confident verdict | `PatentSearchError` |
| All providers fail | one `error` event with a friendly message | `PatentSearchError`, wrapper emits `error` |
| Zero keyword results | canned recommendation | identical report, but `recommendations: []` |
| Qdrant unreachable | n/a — legacy has no semantic search | raises, request fails |
| LLM call fails (understand/analyze) | raises (already) | raises |
| Validation LLM call fails | n/a — legacy has no validation step | raises, request fails |
| **Happy path** | keyword-only corpus, TF-IDF-free embedding score against it | broader corpus (keyword + semantic, fused, LLM-filtered), same scorer — **result contents can differ, this is intentional** |

Zero results is **not** an error — reaching every provider and finding nothing is a
genuine answer, and so is validation filtering the fused set down to nothing. The graph
skips ranking and explanation in either case (running them would emit
`Scoring 0 candidates…`, describing work that is not happening) and the report goes out
with real `concepts` and `query_count`, a measured `top_score` of 0, and an **empty**
recommendation list.

The Phase 1 parity tests that asserted the two streams were byte-identical on the happy
path (`test_pipeline_parity.py`) were rewritten for this: they now assert the graph emits
every phase legacy does, plus exactly `semantic_search`/`fuse`/`validate`, and that the
terminal payload's *shape* still matches — not its values, which are now allowed to
differ by design.

### LLM: Llama via HuggingFace Router, not a local model

All LLM calls go through `_llm_chat` in `backend/llm.py`, a raw `requests.post` to an
OpenAI-compatible chat-completions endpoint (`LLM_API_URL`, default the HuggingFace
Router) using `LLM_MODEL` (default `meta-llama/Llama-3.1-8B-Instruct:novita` — Llama 3.1
8B served through HF's Router via the Novita provider). It is not Ollama, Together, or
Groq; any OpenAI-compatible endpoint is a drop-in replacement. Three functions call it:
`understand_invention`, `analyze_all_matches` (both restored from a regression that had
briefly deleted `analyze_all_matches` and replaced it with a silent-fallback
`rationale_for_match`/`recommendations_for` pair — if you find yourself reintroducing
either of those, stop), and `validate_relevance` (Phase 3's Validation agent, same
batched-call-with-no-`except` shape as `analyze_all_matches`).

### Embeddings and Qdrant: hosted, not local, corpus grows from live traffic

`backend/embeddings.py` calls a HuggingFace-**hosted** embedding model
(`settings.embedding_model`, default `BAAI/bge-large-en-v1.5`) over the HF Inference
Router, reusing `settings.hf_token` the same way `_llm_chat` does — not
`sentence-transformers`, which stays an unused dependency by design (see above). Raises on
failure; there is no zero-vector fallback. `chunk_text` splits long documents into
overlapping word-count windows (capped at `MAX_CHUNKS_PER_DOCUMENT`), used by both
`compare.py`'s ranking and nothing else.

`backend/search/vector_store.py` wraps a `qdrant-client` `QdrantClient` (`settings.qdrant_url`
/ `settings.qdrant_api_key`) around a single `patents` collection, created lazily on first
write (`store_patents`) — **there is no bulk-ingestion step**. The collection growing at
all is a side effect of `graph/searching.py`'s `keyword_search_node` indexing every
enriched result it finds. `backend/search/retrieve.py`'s `retrieve_similar_patents` is the
read side, used by `semantic_search_node`. A collection that doesn't exist yet returns an
empty result (a legitimate cold-start state); an unreachable Qdrant still raises. Queries
go through `client.query_points(...).points` — **not** `client.search(...)`, which
`qdrant-client` removed in 1.10+ (installed here: 1.18). `requirements.txt` doesn't pin a
version, so a fresh install always gets current `qdrant-client`; if you see
`'QdrantClient' object has no attribute 'search'`, that's why.

The query also passes `score_threshold=MIN_SIMILARITY_SCORE` (0.6 — see the
constant's docstring for the live-Qdrant probe it's based on: unrelated
queries scored 0.51-0.56, related ones 0.68-0.76, against this project's own
BAAI/bge-large-en-v1.5 embeddings; there's no exact-zero baseline for this
model, so the threshold has to clear that floor rather than sit at 0). Without
it, `query_points` always returns its `limit` nearest neighbors even when the
closest thing in a small or unrelated corpus still isn't actually similar —
"the least-bad options available" would otherwise flow into validation and
ranking looking like real candidates. `validate_node` and `rank_node` would
likely still catch a weak match downstream (an honest low score, or an LLM
relevance rejection), but the threshold cuts it off at the source instead of
relying on that safety net.

### Data sources normalize to one shape

`backend/search/google.py` (Google Patents via SerpAPI) and `backend/search/epo.py` (EPO
OPS, OAuth2 client-credentials, XML response parsing, fetches a fresh token on every
call — no caching) both normalize into the `PatentRecord` dataclass in `backend/models.py`.
Add new sources by writing a `(query: str, size: int) -> list[PatentRecord]` function and
registering it in the `SOURCES` tuple in `backend/search/search.py`.

That module has two orchestrators over the same `SOURCES` and `merge_patents`:
`search_patents` (legacy, succeeds if any provider works) and
`search_all_providers_strict` (LangGraph, any failure fails the request). They coexist
until the team retires the legacy path; the strict one is the intended behavior.

### Frontend: one file, and most of its dependencies are unused

`frontend/src/app/App.tsx` (~800 lines) is the entire application — all screens, state,
API calls, and styling (hand-rolled inline styles). It imports `useState` and, as of
ticket P2.3, `useEffect` (loads history on mount).
**`frontend/src/app/components/ui/` (the shadcn/Radix component library) and most of
`package.json`'s dependencies (MUI, recharts, react-dnd, react-router, etc.) are unused
Figma-scaffolding leftovers** — don't assume they reflect the real UI or extend them
without checking whether `App.tsx` actually uses them first. History (the fourth screen)
is backed by `GET /api/history` (ticket P2.3 — see the PostgreSQL persistence section
below) and survives a backend restart; `App.tsx`'s `history` state is populated by
`refreshHistory()` on mount and again after each completed search, not accumulated
client-side. Note `buildReport` (defined near the top of the file) is dead code as of the
Phase 3 frontend rework — `startRunning` builds the report inline instead — don't assume
it's still on the call path.

**The zero-tolerance policy applies to this file too.** It used to invent content whenever
the backend sent nothing: three hardcoded "recommendations" when the list was empty, a
per-patent rationale reading *"Shares common vocabulary or systemic overlap with your
primary description."*, and an `"Extracted Concept Cluster"` placeholder concept. All
three are gone — the empty case now says so plainly. Two lesser defaults remain in
`buildReport` for malformed provider data (`"US Unknown"` and `"Patent Prior Art Record"`
for a patent with no id or title); they are reachable only if a provider returns a record
the backend's `PatentRecord` normalization left blank.

### PostgreSQL persistence: patent cache, query cache, and stored reports (Phase 2, legacy-path only)

`backend/migrations/` holds yoyo-migrations SQL migrations (with matching `.rollback.sql`
files). Apply them with `python backend/migrate.py` (rewrites `DATABASE_URL`'s
`postgresql://` scheme to `postgresql+psycopg://` so yoyo selects the psycopg3 backend
already pinned in `requirements.txt` — yoyo's default scheme resolves to psycopg2, which
isn't installed). Four tables:

- `patents` — a cache of `PatentRecord`, keyed by `id` normalized exactly as
  `merge_patents()` dedupes (`patent.id.strip().upper()`). Carries `full_text`/
  `serpapi_id` too, mirroring Phase 3's enrichment fields — a cached patent is exactly as
  useful to `compare.py`'s embedding ranking as a freshly-fetched one, not a downgrade
  back to title+abstract. `fetched_at` backs a 30-day TTL (`backend/cache.py`'s
  `PATENT_TTL`).
- `query_cache` — added in P2.2, not anticipated by P2.1: `search_google`/`search_epo`
  have no ID-only lookup, only search-by-query, so an ID-keyed cache alone cannot skip a
  live provider call. Maps a `.strip()`ped query string to the patent IDs it returned, 7-day
  TTL (`QUERY_TTL`, shorter than `PATENT_TTL` since which patents match a query changes as
  new ones publish). An exact repeat query hits this table and skips both the search
  provider **and** `enrich_with_full_text`'s Details API calls (the latter via a
  `not patent.full_text` guard in `search/google.py`) — measured directly: a repeat query
  went from 4.68s live to 0.00s cached, identical patent IDs.
- `search_runs` / `search_run_matches` — one row per completed `_run_pipeline` run and its
  ranked matches, enough to rebuild the `result` SSE payload. Written by
  `backend/reports.py`'s `save_report`, called from `_run_pipeline` **before** the `result`
  event — a write failure raises, so the caller's exception handler emits `error` instead
  of a `result` the report never actually backed. `search_run_matches.patent_id`
  foreign-keys to `patents.id`; caching runs before every match, so the FK never dangles.

**This only covers the legacy pipeline.** `search_patents(conn, ...)` (legacy) gained the
cache; `search_all_providers_strict` (LangGraph's `keyword_search_node`) did not, and
`graph/pipeline.py`'s `run_graph_pipeline` opens no Postgres connection at all. Since
`PIPELINE` defaults to `langgraph`, **out of the box this persistence layer does nothing
for a real request** — `PIPELINE=legacy` is required to exercise it. Wiring caching and
report persistence into the LangGraph path is unstarted, deliberately: it touches
`graph/state.py` (the P1.1 schema) and `graph/searching.py`, both squarely in the "say so
explicitly, schema owner reviews it" territory the team's own workflow calls out. Whoever
picks that up should also know Qdrant's `patents` collection (see above) is a **separate**
corpus from this one — Phase 3's semantic search does not read from or write to Postgres,
so this cache is not what "grows" Qdrant.

Zero-tolerance applies here specifically: a cache **miss** is normal and falls through to
a live call; a **database error** (connection failure, broken query) is never treated as a
miss — it propagates and fails the whole request. Verified directly: stopping Postgres
mid-session turned every request into a clean `error`/`502`, not a degraded live-only
result, and requests recovered automatically once Postgres came back.

`GET /api/history` (`reports.py`'s `list_history`) returns the 50 most recent runs, most
recent first, each with its full `patents[]` array already joined in — full reports, not
summaries, so the frontend's History screen can select a row and show it with no second
fetch. `id`/`created_at` (real values) stand in for the SSE payload's placeholder
`title`/`date`; the frontend derives its own display title/date from the raw description,
same as it already does for a live result.

**Description-level report cache.** `_run_pipeline` checks `reports.py`'s
`find_recent_report` before doing anything else: an exact resubmission of the same
`description`+`claims` within 24h (`REPORT_TTL`) returns the stored report directly — zero
LLM calls, zero provider calls, zero embedding calls. This exists because the pipeline is
*not* actually deterministic even at `temperature=0.0` (query understanding) — observed
directly, the same description produced different generated queries on two separate live
runs — so without this, "search the same invention twice" could silently return a
different verdict each time, which is a strange thing for a user to hit and undermines
trust in the tool. `patents[]` entries reconstructed this way carry only
`num/title/pct/risk/rationale/source` (`CORE_PATENT_FIELDS` in `test_pipeline_parity.py`)
— the debug-only `full_text_chars` field a live run's entries also carry isn't persisted,
so it's absent on a cache hit; harmless, since nothing reads it. Same legacy-only
limitation as everything else in this section: `PIPELINE=langgraph` (the default) doesn't
call `_run_pipeline` at all, so this cache is inactive unless `PIPELINE=legacy` is set.

### Remaining migration (see `docs/`)

**Phases 1 and 3 have landed; Phase 2 has landed for the legacy pipeline only** (see above
— the LangGraph path has no persistence yet). Phase 1: the `StateGraph`, the graph steps
as nodes, and the `PIPELINE` flag that switches between them. Phase 3: Semantic Search,
Result Fusion, and Validation as live graph nodes, plus embedding-based ranking with no
TF-IDF fallback (P3.1–P3.6), **and P3.7** — the frontend now consumes the real SSE stream
(`frontend/src/app/App.tsx`'s `PHASE_ORDER`, kept in lockstep with `graph/events.py`'s
`PHASES`) instead of a simulated timer; an earlier version of this file said otherwise and
was wrong by the time it was read — the source (`App.tsx`) is the authority, not this
paragraph, if the two ever disagree again. **Still open**: the full P3.8 failure-path test
matrix (a handful of focused tests exist per new agent; a dedicated outage-injection suite
does not), and wiring Phase 2 persistence into the LangGraph path (see above). Two
constraints govern all of it:

- **Zero-tolerance failure policy**: no fallback, ever. If a step can't produce a genuine
  result, it must fail the request with a clear `error` event — never substitute
  fabricated or placeholder output. Commit `e96edfac` already removed the fabricated
  score in `_format_and_rank` and the `_fallback_rationale` / `_fallback_recommendations`
  pair in `backend/llm.py`; do not go looking for them. Two fallbacks remain **in the
  legacy path only** — the LangGraph path already drops both, and they are the reason to
  finish retiring legacy:
  - `search_patents`'s "succeed if any provider works" (`backend/search/search.py`) —
    a provider outage silently narrows the corpus and still reports success.
  - the canned `"No reference targets found…"` recommendation `_run_pipeline` emits when
    search returns zero patents (`backend/server.py`) — drafting advice presented as
    analysis output that no LLM produced.
- **USPTO is out of scope** — no API access is available. Don't add a USPTO provider.
