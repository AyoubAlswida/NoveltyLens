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
- Run the tests: `python -m pytest` from `backend/` (config in `backend/pytest.ini`, which sets `pythonpath = .` because the modules import each other flat, e.g. `from config import settings`). The suite is `backend/tests/`. `backend/tests/conftest.py` stubs embeddings, Qdrant, and EPO's full-text enrichment (autouse, for every test) alongside the existing `_llm_chat`/`SOURCES` stubs — EPO's real enrichment keys off `source`/`id` alone, both of which test fixtures set, unlike Google's (gated on `serpapi_id`, which test fixtures never set and so skip naturally) — without the stub it would make real calls to EPO's auth and full-text endpoints during the suite. `compare.py`'s ranking is on the happy path for both pipelines now, not just Phase-3-specific tests, so the suite still needs no credentials and makes no network calls. `test_pipeline_parity.py` covers legacy↔LangGraph parity (no longer byte-identical on the happy path — see below); `test_phase3_agents.py` covers the title/abstract filter, Semantic Search, Fusion, and Validation individually (Semantic Search's own tests exercise `search/vector_store.py` and `search/retrieve.py` directly, independent of the node being wired into the graph or not). This is not yet the full P3.8 failure-path matrix (LLM failure, single-provider failure, Qdrant/embeddings outage, zero-similarity are covered for the parts that existed pre-Phase-3; a dedicated outage-injection suite for the new agents is still open).
- Still no lint config and no `pyproject.toml`. `pytest-asyncio` remains unused — the tests are all synchronous.

**Frontend** (from `frontend/`):
- Install deps: `npm install`
- Dev server: `npm run dev` — Vite on its default port (`http://localhost:5173`); no `server.proxy` is configured
- Build: `npm run build`
- No lint, test, or typecheck script is defined, and there is no `tsconfig.json` — `npm run build` (Vite) is the closest thing to a correctness check today.

**Local infrastructure** (from the repo root): `docker compose up -d --wait` starts
PostgreSQL 17.10 (`127.0.0.1:5432`) and Qdrant 1.18.3 (`127.0.0.1:6333` HTTP, `6334`
gRPC), both with persisted named volumes and healthchecks, so `--wait` returns only once
both are actually serving. **PostgreSQL is still unread by application code** — it exists
so the Phase 2 (persistence) migration tickets can build against a real service. **Qdrant
is different as of Phase 3**: `backend/search/vector_store.py` and the graph's
`keyword_search`/`semantic_search` nodes connect to it on every request (see Architecture
below), so a Qdrant-reachable environment is required for the LangGraph pipeline to run at
all, not just for local development convenience. The backend and frontend are not
containerized.

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

**The SSE contract genuinely does span `backend/server.py` and `frontend/src/app/App.tsx`
now** — this section used to say the frontend didn't consume the stream at all (calling the
non-streaming `POST /api/analyze` and faking progress with a `setInterval` timer); that has
since been rewired. `App.tsx` now `fetch`es `POST /api/analyze/stream` directly and reads
the response body as a stream (`streamAnalysis`, parsing `data: {...}\n\n` frames by hand —
native `EventSource` can't be used since it only supports GET with no body, and this
endpoint is a POST carrying the description/claims). `App.tsx`'s `PHASE_ORDER` constant
must stay identical to `backend/graph/events.py`'s `PHASES` tuple; the UI then collapses
that into 5 higher-level `DISPLAY_STAGES` for the checklist rather than showing raw phase
names, but the underlying progress tracking is real, not simulated. Every event has a
`type` of `progress` | `result` | `error`. `progress.phase` is one of the values in
`PHASES` (ten now — see below). The stream must end with exactly one `result` event (data
shape: `patents[].{num,title,pct,risk,rationale,source}` plus `title`, `date`, `concepts`,
`top_score`, `verdict`, `recommendations`, `query_count`) or the frontend throws.

### `backend/graph/` — the LangGraph pipeline

**This is the default pipeline** — it serves every request unless `PIPELINE=legacy` says
otherwise. As of Phase 3 it genuinely branches and does more than legacy on the happy
path, so it is **no longer byte-identical to legacy's SSE stream** the way Phase 1 left
it — see the divergence table below. `keyword_search` feeds `fuse` (semantic search would
be the other input, see below), which feeds `validate`, which feeds `rank`.

**Semantic Search / Qdrant is temporarily disabled** — commented out of the graph wiring
in `pipeline.py` and the Qdrant-write side effect in `searching.py`, not deleted, same
pattern EPO used while it was disabled earlier. `semantic_search_node`, `fusion.py`, and
`validation.py` are all fully intact and still run (fusion over an empty semantic list is a
harmless no-op reordering; validation still checks keyword-only candidates) — only the
Qdrant read/write side is switched off. Re-enable by restoring the commented import, node,
and two edges in `pipeline.py`.

- `state.py` — `GraphState`, the `TypedDict` every node reads and writes. It carries the
  `backend/models.py` dataclasses rather than re-flattening them. `semantic_patents`,
  `fused_patents`, and `validated_patents` are no longer reserved-for-later fields — every
  one is written by a live node when semantic search is enabled (see above for its current
  disabled state). `total=False`, so later tickets can still add keys additively. **It has
  no error field on purpose** — under the zero-tolerance policy a node that cannot produce
  a genuine result raises, and an error slot would invite nodes to record a problem and
  continue to a degraded answer.
- `events.py` — `PHASES` plus `emit_progress`, which **raises `SSEContractError` on an
  unknown phase name**. `PHASES` now has ten entries: `understand`, `queries`, `search`,
  `filter`, `enrich`, `semantic_search`, `fuse`, `validate`, `compare`, `analyze`.
  `semantic_search` stays in the tuple even while its node is disabled — `emit_progress`
  validates against this vocabulary, not against which nodes currently run, and the phase
  name needs to still be valid the moment the node is re-enabled. The frontend now
  consumes this over the real SSE stream and collapses it into 5 display stages (see the
  SSE contract note below) rather than showing raw phase names.

- `understanding.py` — Query Understanding + Query Expansion (P1.2), over `llm.py`
  unchanged. Contains no `except`: a failed or malformed LLM response fails the request.
- `searching.py` — Search Orchestrator and Keyword Search (Semantic Search's node is
  defined here too but not wired in — see above). Keyword search keeps its one deliberate
  behavior change from legacy: one provider failing fails the whole request, via
  `search_all_providers_strict` in `backend/search/search.py` (added alongside
  `search_patents`, which is unchanged and still backs legacy). `RESULTS_PER_QUERY` is 10
  (both providers already fetch at least that many per call regardless, so this costs
  nothing extra — see `search/google.py`). After the raw fan-out, keyword search runs a
  **title/abstract relevance pre-filter** (`compare.rank_by_title_abstract`, capped at
  `FILTER_TOP_N = 20`) *before* fetching full text — full-text enrichment is the single
  biggest source of outbound API calls in the pipeline (one call per surviving candidate,
  per provider), so filtering first on fields already in hand from the search response
  bounds that cost regardless of how many raw results the fan-out produced. Only the
  survivors get enriched: `search.google.enrich_with_full_text` and
  `search.epo.enrich_with_full_text` both run on the same mixed list and each internally
  skips records from the other provider (matched by `patent.source`), so calling both in
  sequence is safe. None of search, filter, or enrichment is wrapped in `except`.
- `fusion.py` — Result Fusion. Reciprocal Rank Fusion (`backend/ranking.py`,
  `reciprocal_rank_fusion`, pure and unit-tested directly) over the keyword and semantic
  result sets (currently keyword-only while semantic search is disabled), deduped by
  patent ID.
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
| Qdrant unreachable | n/a — legacy has no semantic search | n/a while semantic search is disabled (see above); raises when re-enabled |
| LLM call fails (understand/analyze) | raises (already) | raises |
| Validation LLM call fails | n/a — legacy has no validation step | raises, request fails |
| **Happy path** | keyword-only corpus (Google + EPO), TF-IDF-free embedding score against it, no pre-filter or full-text for EPO | title/abstract-filtered corpus (Google + EPO, both with full text), LLM-validated, same scorer — **result contents can differ, this is intentional** |

Zero results is **not** an error — reaching every provider and finding nothing is a
genuine answer, and so is validation filtering the fused set down to nothing. The graph
skips ranking and explanation in either case (running them would emit
`Scoring 0 candidates…`, describing work that is not happening) and the report goes out
with real `concepts` and `query_count`, a measured `top_score` of 0, and an **empty**
recommendation list.

The Phase 1 parity tests that asserted the two streams were byte-identical on the happy
path (`test_pipeline_parity.py`) were rewritten for this: they now assert the graph emits
every phase legacy does, plus exactly `filter`/`fuse`/`validate` (currently — would add
`semantic_search` back once that's re-enabled), and that the terminal payload's *shape*
still matches — not its values, which are now allowed to differ by design.

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

### No PostgreSQL persistence yet — but Qdrant is live

Every search still re-fetches live from external APIs, and there is no PostgreSQL access
and no cache: nothing imports `psycopg`, and there are no migrations. `database_url` in
`backend/config.py` remains unread. **Qdrant is different as of Phase 3**: every keyword
search indexes its results, and semantic search reads from it — see the Embeddings and
Qdrant section above. Ticket P2.1 still owns the PostgreSQL schema and migrations, and
P2.2 the cache.

### Remaining migration (see `docs/`)

**Phases 1 and 3 have landed, including P3.7.** The `StateGraph`, the graph steps as nodes,
and the `PIPELINE` flag that switches between them (Phase 1); Semantic Search, Result
Fusion, and Validation as graph nodes (Semantic Search currently disabled, not removed —
see the Architecture section above), embedding-based ranking with no TF-IDF fallback, and
the frontend now genuinely consuming the SSE stream with its own `PHASE_ORDER` kept in
lockstep with the backend's `PHASES` (Phase 3, P3.1–P3.7). **Still open**: the full P3.8
failure-path test matrix (a handful of focused tests exist per new agent; a dedicated
outage-injection suite does not), and Phase 2 (PostgreSQL caching and persisted history),
which has not started. Two constraints govern all of it:

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
