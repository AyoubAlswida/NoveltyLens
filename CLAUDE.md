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
- No lint config, no test files, and no `pyproject.toml` exist yet, despite `pytest` / `pytest-asyncio` being listed in `requirements.txt` — they are unused placeholders for the planned migration (see below), not a currently working test suite.

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

`PIPELINE` (default `"legacy"`) selects which pipeline serves requests. It is **validated
but not yet consumed**: `backend/config.py` raises `ConfigError` at import time unless the
value is exactly `legacy` or `langgraph`, so a typo fails at startup instead of silently
running the wrong pipeline — but no code branches on it. Wiring that branch is ticket P1.5.
`PATENTSVIEW_API_KEY`/`uspto_api_key` remains dead code — see the USPTO note below.

## Architecture

### Backend: the legacy linear pipeline still serves every request

`qdrant-client`, `psycopg[binary]`, `yoyo-migrations`, `rank_bm25` and
`sentence-transformers` sit in `backend/requirements.txt` but are **imported nowhere** —
they are pre-staged for Phases 2 and 3. `langgraph` is now used, by `backend/graph/`
(below), but that package does not serve requests yet. The actual request flow remains
a single generator, `_run_pipeline` in `backend/server.py`, called by both
`POST /api/analyze/stream` (SSE) and `POST /api/analyze` (drains the same generator,
returns only the final JSON). It runs, in strict sequence:

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

### `backend/graph/` — the LangGraph pipeline, under construction

The Phase 1 migration re-expresses the four steps above as a LangGraph `StateGraph` in
`backend/graph/`. It is **not wired to any request path yet** (ticket P1.5 does that);
`_run_pipeline` above stays the parity reference until it is.

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

Nodes emit progress through LangGraph's custom stream writer, which preserves call
ordering — the same interleaving the legacy generator got from `yield`.

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

### Frontend: one file, and most of its dependencies are unused

`frontend/src/app/App.tsx` (~700 lines) is the entire application — all screens, state,
API calls, and styling (hand-rolled inline styles). It imports only `useState`.
**`frontend/src/app/components/ui/` (the shadcn/Radix component library) and most of
`package.json`'s dependencies (MUI, recharts, react-dnd, react-router, etc.) are unused
Figma-scaffolding leftovers** — don't assume they reflect the real UI or extend them
without checking whether `App.tsx` actually uses them first. History (the fourth screen)
is in-memory `useState` only; it does not survive a page refresh, and there is no backend
endpoint for it yet.

### No persistence layer exists yet — but the services are now provisioned locally

Every search re-fetches live from external APIs. There is still no database access, no
cache, and no vector store **in the code**: nothing imports `psycopg` or `qdrant-client`,
there are no migrations, and no Qdrant collection is created.

What changed in ticket P0.1 is only the environment around the code — the root
`docker-compose.yml` runs PostgreSQL and Qdrant locally, `backend/config.py` carries
`database_url`/`qdrant_url`/`qdrant_api_key`, and `PIPELINE` is now validated at import.
All of it is unread by the pipeline. Ticket P2.1 adds the schema and migrations, P2.2
wires the cache, and P3.2 creates the Qdrant collection.

### Planned migration (see `docs/`)

An approved plan (not yet implemented) re-architects the backend pipeline above into a
LangGraph multi-agent `StateGraph` — adding Qdrant semantic search (embeddings via a
HuggingFace-hosted model, not a local model), Reciprocal Rank Fusion, and an LLM
validation gate, behind the `PIPELINE` flag, while keeping Llama and the SSE contract's
happy-path behavior unchanged. Two constraints from that plan apply to any future work on
this pipeline, even before the migration lands:

- **Zero-tolerance failure policy**: no fallback, ever. If a step can't produce a genuine
  result, it must fail the request with a clear `error` event — never substitute
  fabricated or placeholder output. Commit `e96edfac` already removed the fabricated
  score in `_format_and_rank` and the `_fallback_rationale` / `_fallback_recommendations`
  pair in `backend/llm.py`; do not go looking for them. Two fallbacks remain in the legacy
  path, both of which the LangGraph path drops:
  - `search_patents`'s "succeed if any provider works" (`backend/search/search.py`) —
    a provider outage silently narrows the corpus and still reports success.
  - the canned `"No reference targets found…"` recommendation `_run_pipeline` emits when
    search returns zero patents (`backend/server.py`) — drafting advice presented as
    analysis output that no LLM produced.
- **USPTO is out of scope** — no API access is available. Don't add a USPTO provider.
