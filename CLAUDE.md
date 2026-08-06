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
and a Vite/React frontend — with no shared tooling; each is run and developed separately.

## Commands

**Backend** (from `backend/`):
- Install deps: `pip install -r requirements.txt`
- Run the API: `python server.py` — serves on `http://127.0.0.1:8001` (hardcoded host/port in `server.py`'s `__main__` block)
- Run the tests: `python -m pytest` from `backend/` (config in `backend/pytest.ini`, which sets `pythonpath = .` because the modules import each other flat, e.g. `from config import settings`). The suite is `backend/tests/`. `backend/tests/conftest.py` stubs embeddings and Qdrant (autouse, every test) alongside the per-module `_llm_chat`/`SOURCES` stubs — `compare.py`'s ranking and the semantic-search path are unconditionally on the happy path, so the suite needs no credentials, no network calls, and no running services. `test_pipeline_contract.py` covers the SSE phase vocabulary, the terminal payload shape, and the zero-tolerance failure behaviors; `test_sse_transport.py` covers the same contract as real `data:` frames through `server.py`; `test_phase3_agents.py` covers Semantic Search, Fusion, and Validation individually. Still open: a dedicated outage-injection suite for the Phase 3 agents (the old P3.8 matrix).
- Still no lint config and no `pyproject.toml`.

**Frontend** (from `frontend/`):
- Install deps: `npm install`
- Dev server: `npm run dev` — Vite on its default port (`http://localhost:5173`); no `server.proxy` is configured
- Build: `npm run build`
- No lint, test, or typecheck script is defined, and there is no `tsconfig.json` — `npm run build` (Vite) is the closest thing to a correctness check today.

**Local infrastructure** (from the repo root): `docker compose up -d --wait` starts
Qdrant 1.18.3 (`127.0.0.1:6333` HTTP, `6334` gRPC) with a persisted named volume and a
healthcheck, so `--wait` returns only once it is actually serving. **Qdrant is required to
run the backend at all**, not just for local convenience: the graph's `semantic_search`
node reads it and `index_patents` writes it, and neither has a fallback path. The corpus
it holds is the only persistent state this project keeps.

PostgreSQL used to be the second service here, backing the legacy pipeline's patent
cache, query cache, and stored reports. **Both PostgreSQL and the legacy pipeline have
been removed** — along with `cache.py`, `reports.py`, `db.py`, `migrate.py`,
`migrations/`, the `PIPELINE` flag, and `GET /api/history`. There is one pipeline now and
no relational database. If you are adding caching back, it is a fresh design decision, not
a restoration; the deleted code is in git history.

Keep the Qdrant image version in step with `qdrant-client` in `requirements.txt` — the
client warns when the server is more than a minor version away.

**Qdrant capacity.** Vectors live in RAM (1024-dim float32 = 4 KB/point): ~0.04 GB per
10k patents, ~0.4 GB per 100k, ~3.8 GB per 1M. The container is capped at `mem_limit: 2g`
so a runaway corpus fails in one place instead of taking the host down. **Disk is the
faster-growing number**: `on_disk_payload` is true and the payload stores each patent's
*full* text (~273 KB average) even though only the first 600 words are embedded — roughly
2.7 GB of disk per 10k patents. Growth is bounded by *distinct* patents, since
`store_patents` keys on `uuid5(patent.id)` and re-indexing overwrites rather than
duplicating.

**Running the app**: start both processes manually in separate terminals, with Qdrant up.
There is no root script that runs them together, and no README. The frontend calls the
backend at an absolute URL — `import.meta.env.VITE_API_URL` if set, otherwise
`http://127.0.0.1:8001` — and no `server.proxy` is configured, so by default the backend
must be running on that exact host/port. `VITE_API_URL` is declared in
`frontend/src/vite-env.d.ts`; setting it in `frontend/.env` is the supported way to point
the UI at a backend somewhere else.

**Env vars** (backend, via `backend/.env` — see `backend/.env.example`): `HF_TOKEN`
(HuggingFace router auth, required — backs both the LLM calls and the embedding calls),
`SERPAPI_KEY` (Google Patents, required — it is the only enabled provider), and
`QDRANT_URL` / `QDRANT_API_KEY`. `EPO_CONSUMER_KEY` / `EPO_CONSUMER_SECRET` are read but
currently unused (the EPO provider is commented out — see below). Optional:
`LLM_API_URL`, `LLM_MODEL`, `EMBEDDING_MODEL`, and `TRACE=0` to silence the per-step trace
`backend/tracing.py` prints to stdout on every request (it is **on by default**).
`PATENTSVIEW_API_KEY`/`uspto_api_key` remains dead code — see the USPTO note below.

## Architecture

### Backend: one pipeline

`sentence-transformers`, `psycopg[binary]`, `yoyo-migrations`, `rank_bm25`, and
`pytest-asyncio` have all been **removed from `backend/requirements.txt`** — the first was
unused by design (`backend/embeddings.py` calls a HuggingFace-*hosted* model over the
network, not a local one), the next three went with Postgres, and the last was never used
because the tests are synchronous. Do not add `sentence-transformers` back to "fix"
`embeddings.py`; it is deliberately a network call. `scikit-learn` was removed earlier —
`compare.py`'s TF-IDF fallback, the only thing that used it, is gone. `langgraph` and
`qdrant-client` are live and load-bearing.

**`backend/graph/` is the whole backend.** `backend/server.py` is pure transport: it owns
the HTTP surface and the SSE framing and nothing else, calling `run_graph_pipeline`
directly. The legacy `_run_pipeline` generator, the `_active_pipeline` indirection, and
the `PIPELINE` flag that chose between them are gone; `backend/config.py` no longer has a
`pipeline` or `database_url` setting and no longer raises `ConfigError` (the class remains
for future use).

**The SSE contract spans `backend/server.py` and `frontend/src/app/App.tsx`.** An earlier
version of this section said the frontend didn't consume the SSE stream at all and just
faked progress with a `setInterval` timer — that was true once, but a prior edit here went
stale against the code (per this file's own "verify against the source" rule) and nobody
caught it. As of the current `App.tsx`: `streamAnalysis()` reads `POST /api/analyze/stream`'s
real body via `fetch` + `getReader()` (not `EventSource`, which can't carry a POST body),
and `PHASE_ORDER` (top of the file) is a real, maintained mirror of
`backend/graph/events.py`'s `PHASES` tuple — the comment directly above it says so. If you
find this paragraph disagreeing with `App.tsx` again, trust `App.tsx` and fix this file,
not the other way around. Every event has a `type` of `progress` | `result` | `error`.
`progress.phase` is one of the nine values in `PHASES`; an unrecognized phase does not
error client-side, it just never advances `phaseIdx` past it (`App.tsx`'s `indexOf`
returns -1, silently ignored) — so a phase-vocabulary change with no matching frontend
update is a silent bug, not a loud one. The stream must end with exactly one `result`
event (data shape: `patents[].{num,title,pct,risk,rationale,source}` plus `title`, `date`,
`concepts`, `top_score`, `verdict`, `recommendations`, `query_count`) or a client reading
the stream would throw.

### `backend/graph/` — the LangGraph pipeline

**This is the only pipeline** — it serves every request. `keyword_search` and
`semantic_search` both run off `search_orchestrator` and converge at `index_patents`,
which feeds `fuse`, then `validate`, then `rank`. `build_graph()` is `lru_cache`d: the
topology is a constant and the compiled graph holds no request state, so it is compiled
once and shared.

**The two search branches are not isolated from each other — do not assume they are.**
LangGraph runs a superstep's sync nodes *sequentially*, so whichever branch runs first has
its side effects visible to the other. That is why Qdrant indexing lives in its own
`index_patents` node behind the join instead of at the end of `keyword_search`, where it
used to be: with the write inside `keyword_search`, semantic search retrieved the patents
keyword search had just indexed, so on a cold or small corpus the semantic branch was a
pure echo of the keyword branch, and RRF then credited those echoes from *both* branches
and ranked them above patents only one branch found. If you add a node that writes shared
external state, put the write behind the join too.

**Do not depend on which branch runs first.** An earlier version of this section asserted
`keyword_search` always does, "verified directly" — a timed run of the real pipeline showed
the opposite (semantic completed at 4.7s, keyword not until 18.1s). It does not matter,
and that is exactly the point of putting the write behind the join: the guarantee comes
from the graph's topology, not from the scheduler.

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
- `searching.py` — Search Orchestrator, Keyword Search, and Semantic Search. One provider
  failing fails the whole request, via `search_all_providers_strict` in
  `backend/search/search.py`. That function dispatches its (query, provider) calls
  **concurrently** but collects results in query order — `merge_patents` keeps the first
  record it sees for an id, so collecting in completion order would let network timing
  decide which copy of a duplicate survives. It returns a `KeywordSearchResults`
  named tuple, **not a list** — see the corpus-growth note below. It also enriches
  results with full text (`enrich_with_full_text`, skipped entirely when nothing was
  found) — no `except`, so a failure there fails the whole request. Indexing into Qdrant
  (`store_patents`) is **not** part of keyword search; it is `index_patents_node`, which
  the graph runs after both branches join, so this request's hits reach Qdrant strictly
  after Semantic Search has read it (see the branch-isolation note above). No `except`
  there either — an unreachable Qdrant fails the request rather than silently shipping a
  result whose corpus never grew. Semantic Search (`semantic_search_node`) therefore
  queries only what Qdrant holds from *earlier* requests. A Qdrant collection that simply doesn't exist yet (cold
  start) returns an empty list rather than raising (`search/vector_store.py`); an
  unreachable Qdrant still raises.
- `fusion.py` — Result Fusion. Reciprocal Rank Fusion (`backend/ranking.py`,
  `reciprocal_rank_fusion`, pure and unit-tested directly) over the keyword and semantic
  result sets, deduped on `id.strip().upper()` — the same key `merge_patents` uses, which
  the two must keep in step or one patent reaches the report as two cards. When both
  branches carry the same patent, fusion keeps whichever copy has `full_text` rather than
  whichever came last: `compare.py` scores on `full_text or abstract`, so letting a Qdrant
  payload indexed before enrichment clobber a freshly enriched record would quietly drop
  that patent to abstract-only scoring.
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
  the generator `server.py` consumes. **It catches nothing**: a raising node propagates out
  to `server.py`'s wrapper, which emits one `error` event. There is deliberately no point
  at which a partial result could be assembled and sent. The single "found anything?"
  routing check runs after `validate` (not right after `keyword_search`, since both search
  branches and validation can each reduce the candidate set to zero).

Nodes emit progress through LangGraph's custom stream writer, which preserves call
ordering.

**How each failure behaves:**

| Condition | Behavior |
|---|---|
| Any search provider fails | `PatentSearchError`, wrapper emits one `error` event |
| Zero keyword results | honest report with `recommendations: []` |
| Qdrant unreachable | raises, request fails |
| LLM call fails (understand / validate / analyze) | raises, request fails |
| Embedding call fails | raises, request fails |

Zero results is **not** an error — reaching every provider and finding nothing is a
genuine answer, and so is validation filtering the fused set down to nothing. The graph
skips ranking and explanation in either case (running them would emit
`Scoring 0 candidates…`, describing work that is not happening) and the report goes out
with real `concepts` and `query_count`, a measured `top_score` of 0, and an **empty**
recommendation list.

`test_pipeline_contract.py` (formerly `test_pipeline_parity.py`, which diffed this
pipeline against legacy) asserts these directly now rather than as a diff: the phase
vocabulary and ordering, the terminal payload's full key set, and each failure row above.

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

Also: requests set `response_format: {"type": "json_object"}` (verified accepted by the HF
Router + Novita endpoint — turn it off with `json_object=False` if you point `LLM_API_URL`
at a provider that rejects it), and a malformed-but-complete response is **re-asked** once
at a nudged temperature, never repaired. Repairing would mean inventing content no model
produced. `tests/test_llm_json_robustness.py` pins all of it.

The `understand_invention` prompt no longer asks for `synonyms`. Nothing has ever read that
field, and being the largest and most deeply nested part of the response it was the most
likely thing to push it over the limit.

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
`QUERY_INSTRUCTION` (`"Represent this sentence for searching relevant passages: "`)
prepended; `store_patents` embeds documents with no prefix. That is what
BAAI/bge-large-en-v1.5 was trained for — the prefix is a retrieval instruction to the
model, not content. Do not "make these consistent" by prefixing documents too: the shared
prefix would pull every stored vector toward the same region and flatten the very gap
`MIN_SIMILARITY_SCORE` has to cut through. `tests/test_phase3_agents.py` pins both halves.
The prefix is applied to queries only and does **not** require re-indexing an existing
collection, since document vectors are unchanged by it. Note `tests/fakes.py`'s
`fake_embed_texts` strips the prefix before hashing — a bag-of-words double would score
its five content words as ordinary query terms and dilute the real ones, an artifact of
the stub that production doesn't have (same reasoning as that module's `_STOPWORDS`).

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

**EPO is currently commented out of `SOURCES`** — its OPS auth is being throttled — so
Google Patents is the only live provider and `search_all_providers_strict` is effectively
strict over one source. `search/epo.py` is intact and the credentials are still read from
the environment; re-enabling it is uncommenting one line. Note its no-token-caching
behavior will cost an extra OAuth round trip per call when it comes back.

Enrichment (`enrich_with_full_text` in `search/google.py`) costs **two** outbound HTTP
calls per patent: the Details API does not inline descriptions, only a `description_link`
to a separate hosted HTML page. It runs 8-wide in a `ThreadPoolExecutor`; passing
`include_description=False` halves the call count at the cost of claims-only text.

**Never call `response.raise_for_status()` on a SerpAPI response.** SerpAPI authenticates
by query parameter, and `requests`' `HTTPError` message embeds the full request URL —
which `search_all_providers_strict` interpolates into `PatentSearchError` and `server.py`
forwards verbatim to the browser as the SSE `error` detail. A plain 429 therefore printed
a live API key into the user's devtools console; this was observed for real on an
exhausted free-tier quota, not theorized. Use `search/google.py`'s `_raise_for_status`
instead, and see `tests/test_secrets_do_not_leak.py`, which pins both the fix and the
reason it is needed.

SerpAPI's free plan allows **250 searches/month**, and one novelty search spends roughly
25 of them (~5 searches + ~20 Details calls). That is about ten searches a month before
the key stops working — worth knowing before running anything in a loop against it.

### Both external quotas are small — check them before iterating

This project runs on two metered free tiers and a full novelty search is expensive against
both. Neither degrades gracefully, by design: an exhausted quota fails the request.

- **SerpAPI** — 250 searches/month, ~25 per novelty search. `GET https://serpapi.com/account?api_key=...`
  reports `plan_searches_left`. Exhaustion returns HTTP 429.
- **HuggingFace Router** — a monthly included-credit allowance shared by *both* the LLM
  calls and the embedding calls, so running out stops the pipeline at `understand`, well
  before search. Exhaustion returns HTTP 402 with
  `"You have depleted your monthly included credits"` at every request size, not just large
  ones — so a sudden 402 is a billing state, not something a smaller `max_tokens` will fix.

Check both before any benchmarking or repeated live runs; it is very easy to spend a
month's allowance in an afternoon of testing.

### Frontend: one file, and most of its dependencies are unused

`frontend/src/app/App.tsx` (~650 lines) is the entire application — all screens, state,
and API calls. **Styling is no longer inline**: as of the Aramco-branded redesign, the
visual layer lives in `frontend/src/styles/globals.css` (~1000 lines of hand-written CSS,
imported last by `styles/index.css` after `fonts`/`tailwind`/`theme`), and `App.tsx`
carries `className`s rather than style objects. Only one inline `style` remains, for the
score-bar width. `App.tsx` imports `useState` plus icons from `lucide-react` — that one
package is genuinely used; **`frontend/src/app/components/ui/` (the shadcn/Radix component
library) and most of `package.json`'s other dependencies (MUI, recharts, react-dnd,
react-router, etc.) are still unused Figma-scaffolding leftovers** — don't assume they
reflect the real UI or extend them without checking whether `App.tsx` actually uses them
first.

`frontend/public/` holds the brand assets the UI references by absolute path:
`aramco-ai-mark.png` (from `App.tsx`) and `aramco-wave-background.jpg` (from
`globals.css`). `aramco-ai-logo.png` is the **retired** full lockup — it baked the
wordmark into the image, so renaming the product meant rebuilding the lockup as the
standalone mark plus live text (`BrandHeader`'s `.brand-wordmark`). The file is kept
for reference; nothing loads it. Vite copies `public/` to the build root
verbatim, so those paths work in both dev and `dist/`. Note `index.html` no longer carries
the inline `html, body, #root { height: 100% }` rule — `globals.css` owns it now, so
dropping that import would collapse the layout.

**Typography is Aramco Ghawar, served from `public/fonts/`, with no webfont CDN.**
`styles/fonts.css` is the only place fonts are declared: nine `@font-face` blocks (Hairline
100 through Black 900, OTF release `Ghawar 1.312-Beta 4.0`) plus `--font-sans`,
`--font-display`, and `--font-mono`. All three variables resolve to Ghawar, and **every**
`font-family` in `globals.css`, `theme.css`, and the PDF-export stylesheet inside `App.tsx`
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

There are **four** screens — `submit`, `running`, `report`, and `error`. The History screen
was removed along with `GET /api/history` and the Postgres layer behind it; it had never
shown anything on the default path, since only the retired legacy pipeline ever wrote a run.
Nothing persists client-side either, so a completed report is gone once you start another
search. The `buildReport` helper that used to sit near the top of the file is **gone** as
of the redesign (it had been dead code since the Phase 3 rework); `startRunning` builds
the report inline from the `result` event.

`error` is the newest of the four and the only screen not in the original design handoff.
A failed run used to fire a native `alert()` and drop the user back on `submit`, which
threw away everything the stream had already said. `ErrorScreen` now renders the backend's
`error` detail **verbatim** in a mono block, names the phase it died in, and shows how many
of the nine phases completed — `startRunning` tracks `lastPhase` in a local declared
*outside* the `try` precisely so the `catch` can read it without waiting on a state update.
It offers retry and back, and never renders a partial report. Keep it that way: the
zero-tolerance policy makes the error path a first-class result, not an afterthought, and
paraphrasing or prettifying the detail string would hide exactly the information (an HTTP
429, a depleted-credits 402) that tells the user which quota ran out.

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
  characters and bge-large caps at 512 tokens, so a 20-patent batch shipped ~5.5 MB to
  compute vectors reflecting each document's first ~2,000 characters. `MAX_INDEX_WORDS`
  in `search/vector_store.py` now truncates above the model's own cap, which leaves the
  resulting vectors unchanged. See that constant's docstring.
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

**The migration is done.** Phase 1 (the `StateGraph` and the graph steps as nodes) and
Phase 3 (Semantic Search, Result Fusion, and Validation as live nodes, plus embedding-based
ranking with no TF-IDF fallback, and the frontend consuming the real SSE stream) both
landed. Phase 2's Postgres persistence landed for the legacy pipeline and was then
**deleted along with it** — the project deliberately has no relational database and no
caching now.

Note the `docs/` phase prompts describe a world with two pipelines, a `PIPELINE` flag, and
a Postgres layer. They are a historical record of how the migration was planned, not a
description of the current code. Where they disagree with this file or the source, they
are out of date.

**Still open:**
- A dedicated outage-injection test suite for the Phase 3 agents (the old P3.8 matrix).
  Focused per-agent tests and the failure table above exist; systematic fault injection
  does not.
- Caching, in some form. Every request currently redoes all provider, LLM, and embedding
  work from scratch. Whatever replaces it is a fresh design decision.
- Chunk-and-average for indexed vectors, so a Qdrant entry represents a whole patent
  rather than its first page (see `MAX_INDEX_WORDS`). This is a retrieval-quality change
  with a corpus re-index attached.

Two constraints govern all of it:

- **Zero-tolerance failure policy**: no fallback, ever. If a step can't produce a genuine
  result, it must fail the request with a clear `error` event — never substitute
  fabricated or placeholder output. Commit `e96edfac` removed a fabricated score and the
  `_fallback_rationale` / `_fallback_recommendations` pair in `backend/llm.py`; do not go
  looking for them. The two fallbacks that outlived it — "succeed if any provider works"
  and the canned `"No reference targets found…"` recommendation — lived in the legacy path
  and went with it. **There are now no fallbacks anywhere in the backend.** Adding one back
  is the single most consequential way to break this project's contract with its users.
- **USPTO is out of scope** — no API access is available. Don't add a USPTO provider.
