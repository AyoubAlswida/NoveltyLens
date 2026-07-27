# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Keeping this file current

This project is mid-migration from the linear pipeline described below to the LangGraph
multi-agent architecture proposed in `docs/LangGraph-Multi-Agent-Architecture-Plan.docx`
(diagrams in `docs/assets/`). **When you implement any part of that plan** — a
`backend/graph/` package, `backend/embeddings.py`, `backend/vectorstore.py`, the
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
- Run the tests: `python -m pytest` from `backend/` (config in `backend/pytest.ini`, which sets `pythonpath = .` because the modules import each other flat, e.g. `from config import settings`). The suite is `backend/tests/` and covers legacy↔LangGraph SSE parity; it stubs only `_llm_chat` and `SOURCES`, so it needs no credentials and makes no network calls. Ticket P3.8 extends it into full failure-path coverage.
- Still no lint config and no `pyproject.toml`. `pytest-asyncio` remains unused — the tests are all synchronous.

**Frontend** (from `frontend/`):
- Install deps: `npm install`
- Dev server: `npm run dev` — Vite on its default port (`http://localhost:5173`); no `server.proxy` is configured
- Build: `npm run build`
- No lint, test, or typecheck script is defined, and there is no `tsconfig.json` — `npm run build` (Vite) is the closest thing to a correctness check today.

**Local infrastructure** (from the repo root): `docker compose up -d --wait` starts
PostgreSQL 17.10 (`127.0.0.1:5432`) and Qdrant 1.18.3 (`127.0.0.1:6333` HTTP, `6334`
gRPC), both with persisted named volumes and healthchecks, so `--wait` returns only once
both are actually serving. **No application code connects to either service yet** — they
exist so the Phase 2 (persistence) and Phase 3 (semantic search) migration tickets can
build against a real service. The backend and frontend are not containerized, and the app
as it exists today runs fine without these services.

Keep the Qdrant image version in step with `qdrant-client` in `requirements.txt` — the
client warns when the server is more than a minor version away.

**Running the app**: start both processes manually in separate terminals. There is no
root script that runs them together, and no README. The frontend calls the backend at a
hardcoded absolute URL (`http://127.0.0.1:8001`), so the backend must be running on that
exact host/port. The Docker services are not required to run the app as it exists today.

**Env vars** (backend, via `backend/.env` — see `backend/.env.example`): `HF_TOKEN`
(HuggingFace router auth, required for all LLM calls), `SERPAPI_KEY` (Google Patents),
`EPO_CONSUMER_KEY` / `EPO_CONSUMER_SECRET` (EPO OPS OAuth2). Optional: `LLM_API_URL`,
`LLM_MODEL`, plus `DATABASE_URL` / `QDRANT_URL` / `QDRANT_API_KEY`, whose defaults already
match the Compose services and which nothing reads yet.

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

`qdrant-client`, `psycopg[binary]`, `yoyo-migrations`, `rank_bm25` and
`sentence-transformers` sit in `backend/requirements.txt` but are **imported nowhere** —
they are pre-staged for Phases 2 and 3. `langgraph` is live and **is the default**.

**`backend/graph/` serves requests now**; the legacy generator below runs only under
`PIPELINE=legacy`. Legacy is nonetheless unchanged and must stay that way — it is the
parity reference `backend/tests/` diffs the graph against, and the escape hatch if the
graph misbehaves in production. Retiring it is a team decision, not a cleanup. The legacy
flow is a single generator, `_run_pipeline` in `backend/server.py`, reached through
`_active_pipeline` by both `POST /api/analyze/stream` (SSE) and `POST /api/analyze`
(drains the same generator, returns only the final JSON). It runs, in strict sequence:

1. `understand_invention` (`backend/llm.py`) — one Llama call that returns concepts, keywords, synonyms, and exactly 6 search queries.
2. `search_patents` (`backend/search/search.py`) — fans every query out across all providers in `SOURCES` (currently Google Patents + EPO only; USPTO was evaluated and is intentionally **out of scope** — no API access), isolates per-provider failures, dedupes by uppercased patent ID.
3. `compare_invention_to_patents` (`backend/compare.py`) — TF-IDF cosine similarity; its own docstring notes it's a deliberate placeholder for a future embedding-based ranker.
4. `analyze_all_matches` (`backend/llm.py`) — one batched Llama call producing a rationale per patent plus shared recommendations (batched deliberately, to stay friendly to provider rate limits).

Each stage `yield`s a progress event before/after it runs; `server.py` wraps these as
`data: <json>\n\n` SSE frames.

**The SSE contract is the most fragile cross-cutting constraint in this codebase** — it
spans `backend/server.py` and `frontend/src/app/App.tsx` and there is no schema enforcing
it. Every event has a `type` of `progress` | `result` | `error`. `progress.phase` must be
one of exactly five values — `understand`, `queries`, `search`, `compare`, `analyze` — because
the frontend hard-codes this vocabulary in a `PHASES` array to drive a live checklist UI;
an unrecognized phase silently falls back to the first checklist item instead of
erroring. The stream must end with exactly one `result` event (data shape:
`patents[].{num,title,pct,rationale}` plus `concepts`, `recommendations`, `query_count`)
or the frontend throws. Any change to event shape or phase names must update both files
together.

### `backend/graph/` — the LangGraph pipeline

Phase 1 re-expressed the four steps above as a LangGraph `StateGraph`. **This is the
default pipeline** — it serves every request unless `PIPELINE=legacy` says otherwise — and
it emits byte-identical SSE events to legacy on the happy path. The graph is linear today,
matching legacy step for step; Phase 3 makes it branch, with semantic search parallel to
keyword search converging at fusion.

- `state.py` — `GraphState`, the `TypedDict` every node reads and writes. It carries the
  `backend/models.py` dataclasses rather than re-flattening them, and reserves fields for
  Phase 3 (`semantic_patents`, `fused_patents`, `validated_patents`). `total=False`, so
  later tickets add keys additively. **It has no error field on purpose** — under the
  zero-tolerance policy a node that cannot produce a genuine result raises, and an error
  slot would invite nodes to record a problem and continue to a degraded answer.
- `events.py` — `PHASES` plus `emit_progress`, which **raises `SSEContractError` on a
  phase name the frontend does not know**. The frontend's silent
  fall-back-to-first-checklist-item behavior makes phase drift invisible in backend
  testing, so this is the one place it can still be caught. Keep this tuple identical to
  the frontend's `PHASES` array.

- `understanding.py` — Query Understanding + Query Expansion (P1.2), over `llm.py`
  unchanged. Contains no `except`: a failed or malformed LLM response fails the request.
- `searching.py` — Search Orchestrator + Keyword Search (P1.3). **One deliberate behavior
  change from legacy**: one provider failing fails the whole request, via
  `search_all_providers_strict` in `backend/search/search.py` (added alongside
  `search_patents`, which is unchanged). Under legacy, an EPO outage halves the corpus and
  still returns a confident verdict. Finding *nothing* is not a failure — an empty result
  set is a genuine answer and flows through to an empty report.

- `ranking.py` — Ranking + Explanation (P1.4). Calls `compare.py`'s TF-IDF **unchanged**
  (P3.6 owns replacing it) and `analyze_all_matches` with no `except`. The displayed score
  is the real one, truncated and clamped to 0–100 and nothing else — a dissimilar patent
  shows an honest `0`. Also holds `format_matches` and `verdict_for`, which build the
  `patents[]` entries and the novelty verdict.

- `pipeline.py` — graph assembly, the terminal `result` event, and `run_graph_pipeline`,
  the generator `server.py` consumes. Signature-compatible with `_run_pipeline` so the
  flag can swap them. **It catches nothing**: a raising node propagates out to
  `server.py`'s existing wrapper, which emits one `error` event. There is deliberately no
  point at which a partial result could be assembled and sent.

Nodes emit progress through LangGraph's custom stream writer, which preserves call
ordering — the same interleaving the legacy generator got from `yield`.

**Where the two pipelines diverge**, all of it failure-path and all of it deliberate.
Since LangGraph is now the default, the right-hand column is what users actually get —
the left is what `PIPELINE=legacy` falls back to:

| Condition | Legacy | LangGraph |
|---|---|---|
| One provider fails | narrows the corpus, returns a confident verdict | `PatentSearchError` |
| All providers fail | one `error` event with a friendly message | `PatentSearchError`, wrapper emits `error` |
| Zero results found | canned recommendation | identical report, but `recommendations: []` |
| LLM call fails | raises (already) | raises |

Zero results is **not** an error — reaching every provider and finding nothing is a
genuine answer. `_route_after_search` skips ranking and explanation (running them would
emit `Scoring 0 candidates…`, describing work that is not happening) and the report goes
out with real `concepts` and `query_count`, a measured `top_score` of 0, and an **empty**
recommendation list. That empty list is the entire divergence, and a test pins it to
exactly that one field.

### LLM: Llama via HuggingFace Router, not a local model

All LLM calls go through `_llm_chat` in `backend/llm.py`, a raw `requests.post` to an
OpenAI-compatible chat-completions endpoint (`LLM_API_URL`, default the HuggingFace
Router) using `LLM_MODEL` (default `meta-llama/Llama-3.1-8B-Instruct:novita` — Llama 3.1
8B served through HF's Router via the Novita provider). It is not Ollama, Together, or
Groq; any OpenAI-compatible endpoint is a drop-in replacement.

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

`frontend/src/app/App.tsx` (~700 lines) is the entire application — all screens, state,
API calls, and styling (hand-rolled inline styles). It imports only `useState`.
**`frontend/src/app/components/ui/` (the shadcn/Radix component library) and most of
`package.json`'s dependencies (MUI, recharts, react-dnd, react-router, etc.) are unused
Figma-scaffolding leftovers** — don't assume they reflect the real UI or extend them
without checking whether `App.tsx` actually uses them first. History (the fourth screen)
is in-memory `useState` only; it does not survive a page refresh, and there is no backend
endpoint for it yet.

**The zero-tolerance policy applies to this file too.** It used to invent content whenever
the backend sent nothing: three hardcoded "recommendations" when the list was empty, a
per-patent rationale reading *"Shares common vocabulary or systemic overlap with your
primary description."*, and an `"Extracted Concept Cluster"` placeholder concept. All
three are gone — the empty case now says so plainly. Two lesser defaults remain in
`buildReport` for malformed provider data (`"US Unknown"` and `"Patent Prior Art Record"`
for a patent with no id or title); they are reachable only if a provider returns a record
the backend's `PatentRecord` normalization left blank.

### No persistence layer exists yet — but the services are now provisioned locally

Every search re-fetches live from external APIs. There is still no database access, no
cache, and no vector store **in the code**: nothing imports `psycopg` or `qdrant-client`,
there are no migrations, and no Qdrant collection is created.

What changed in ticket P0.1 is only the environment around the code — the root
`docker-compose.yml` runs PostgreSQL and Qdrant locally, `backend/config.py` carries
`database_url`/`qdrant_url`/`qdrant_api_key`, and `PIPELINE` is now validated at import.
All of it is unread by the pipeline. Ticket P2.1 adds the schema and migrations, P2.2
wires the cache, and P3.2 creates the Qdrant collection.

### Remaining migration (see `docs/`)

**Phase 1 has landed**: the `StateGraph`, the four steps as nodes, and the `PIPELINE`
flag that switches between them. Still to come — Phase 2 adds PostgreSQL caching and
persisted history, and Phase 3 adds the three genuinely new agents (Qdrant semantic
search with embeddings from a HuggingFace-hosted model, Reciprocal Rank Fusion, and an
LLM validation gate), upgrades ranking from TF-IDF to embeddings, and extends the SSE
phase vocabulary to match. Two constraints govern all of it:

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
