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

## Commands

**Backend** (from `backend/`):
- Install deps: `pip install -r requirements.txt`
- Run the API: `python server.py` — serves on `http://127.0.0.1:8001` (hardcoded host/port in `server.py`'s `__main__` block)
- Apply the database schema: `python migrate.py` (yoyo, against `settings.database_url`). Needed once after `docker compose up`, and again whenever `backend/migrations/` gains a file.
- **Run it without spending quota: `python run_mock.py`** (`--fast` to drop the pacing delays, `--port` to move it). Serves the real app on 8001 with only the metered boundaries stubbed. See the mock-mode section below before changing it.
- Run the tests: `python -m pytest` from `backend/` (config in `backend/pytest.ini`, which sets `pythonpath = .` because the modules import each other flat, e.g. `from config import settings`). The suite is `backend/tests/`. `backend/tests/conftest.py` stubs embeddings, Qdrant **and Postgres** (autouse, for every test) alongside the existing `_llm_chat`/`SOURCES` stubs — `compare.py`'s ranking, the semantic-search path and `persist_run` are all unconditionally on the happy path, so the suite still needs no credentials, makes no network calls, and needs no running database. `test_phase3_agents.py` covers Semantic Search, Fusion, and Validation individually; `test_history.py` covers persistence and `GET /api/history`; `test_auth.py` covers password hashing, token signing/expiry, and the endpoint gate. This is not yet the full P3.8 failure-path matrix (LLM failure, single-provider failure, Qdrant/embeddings outage, zero-similarity are covered for the parts that existed pre-Phase-3; a dedicated outage-injection suite for the new agents is still open).
- **The Postgres double is a dict, not a SQL engine.** `tests/fakes.py`'s `FakeReportStore` stands in at the `reports.py` seam, so it pins the *call* contract and nothing about the SQL. A CHECK constraint, a foreign key, or a column that does not exist is invisible to it — schema changes have to be exercised against the real Compose Postgres by hand.
- Still no lint config and no `pyproject.toml`.

**Frontend** (from `frontend/`) — **Angular 22**, standalone components, no NgModules:
- Install deps: `npm install`
- Dev server: `npm run dev` (alias of `npm start` / `ng serve`) — `http://localhost:4200`; no dev-server proxy is configured
- Build: `npm run build` — output in `dist/novelty-lens/`
- Typecheck: `npm run typecheck` (`tsc --noEmit`). Angular's build typechecks templates too, so a bad binding fails the build rather than failing silently at runtime — this is new; the React version had no `tsconfig.json` at all.
- `npm test` exists from the Angular scaffold but there are **no frontend tests**; it will find no specs.

**Local infrastructure** (from the repo root): `docker compose up -d --wait` starts
**both** services, each with a persisted named volume and a healthcheck, so `--wait`
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
10k patents, ~0.4 GB per 100k, ~3.8 GB per 1M. The container is capped at `mem_limit: 2g`
so a runaway corpus fails in one place instead of taking the host down. **Disk is the
faster-growing number**: `on_disk_payload` is true and the payload stores each patent's
*full* text (~273 KB average) even though only the first 600 words are embedded — roughly
2.7 GB of disk per 10k patents. Growth is bounded by *distinct* patents, since
`store_patents` keys on `uuid5(patent.id)` and re-indexing overwrites rather than
duplicating.

**Running the app**: start both processes manually in separate terminals, with Qdrant up.
There is no root script that runs them together, and no README. The frontend calls the
backend at an absolute URL — `window.NOVELTY_LENS_API_URL` if set, otherwise
`http://127.0.0.1:8001` — and no dev-server proxy is configured, so by default the backend
must be running on that exact host/port. That constant lives in `frontend/src/app/config.ts`
and replaces the React build's `import.meta.env.VITE_API_URL`. Setting the global before the
bundle loads (a small script in `index.html`, or devtools) points the UI at another backend
**without a rebuild**, which is what the Playwright checks use to aim at a stub.

**Env vars** (backend, via `backend/.env` — see `backend/.env.example`): `HF_TOKEN`
(HuggingFace router auth, required — backs both the LLM calls and the embedding calls),
`SERPAPI_KEY` (Google Patents, required — it is the only enabled provider), and
`QDRANT_URL` / `QDRANT_API_KEY`. `EPO_CONSUMER_KEY` / `EPO_CONSUMER_SECRET` are read but
currently unused (the EPO provider is commented out — see below). Optional:
`LLM_API_URL`, `LLM_MODEL`, `EMBEDDING_MODEL`, and `TRACE=0` to silence the per-step trace
`backend/tracing.py` prints to stdout on every request (it is **on by default**).
`PATENTSVIEW_API_KEY`/`uspto_api_key` remains dead code — see the USPTO note below.

## Architecture

### Backend: two pipelines, selected by the `PIPELINE` flag

`psycopg[binary]` and `yoyo-migrations` are back in `backend/requirements.txt` and are
**live**: `db.py`/`reports.py` and `migrate.py` respectively. `rank_bm25` is gone along
with the rest of the Phase 2 pre-staging. `sentence-transformers` is not listed and would
be unused *by design* anyway: `backend/embeddings.py` calls a HuggingFace-**hosted**
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

**The SSE contract used to be described as spanning `backend/server.py` and
`frontend/src/app/App.tsx` — that is no longer true, and this section previously said
otherwise.** The frontend does not consume the SSE stream at all: `App.tsx` calls the
non-streaming `POST /api/analyze` and fakes progress with a hardcoded `STEPS` array driven
by a `setInterval` timer, unrelated to `progress.phase`. There is no `PHASES` array in the
frontend today. The SSE contract (`POST /api/analyze/stream`) is real, tested
(`backend/tests/test_sse_transport.py`), and worth keeping correct for the day the frontend
is wired to it — but changing it cannot currently break the running app. Every event has a
`type` of `progress` | `result` | `error`. `progress.phase` is one of the values in
`backend/graph/events.py`'s `PHASES` tuple (nine now — see below). The stream must end with
exactly one `result` event (data shape:
`patents[].{num,title,pct,risk,rationale,key_overlap,url,source}` plus `title`, `date`,
`concepts`, `top_score`, `verdict`, `recommendations`, `query_count`) or a client reading
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

**This is the default pipeline** — it serves every request unless `PIPELINE=legacy` says
otherwise. As of Phase 3 it genuinely branches and does more than legacy on the happy
path, so it is **no longer byte-identical to legacy's SSE stream** the way Phase 1 left
it — see the divergence table below. `keyword_search` and `semantic_search` both run off
`search_orchestrator` and converge at `fuse`, which feeds `validate`, which feeds `rank`.

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
  the generator `server.py` consumes. **It catches nothing**: a raising node propagates out
  to `server.py`'s wrapper, which emits one `error` event. There is deliberately no point
  at which a partial result could be assembled and sent. The single "found anything?"
  routing check runs after `validate` (not right after `keyword_search`, since both search
  branches and validation can each reduce the candidate set to zero).

Nodes emit progress through LangGraph's custom stream writer, which preserves call
ordering.

**How each failure behaves:**

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

### Persistence: PostgreSQL stores completed runs, and nothing else

Four tables, created by `backend/migrations/` (yoyo, applied with `python migrate.py`):

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

**There is still no caching of any kind**, and re-adding it is a design decision, not a
restoration. See the Local infrastructure section for exactly what was and was not brought
back from `15914d10^`.

### Auth: one seeded account, no signup

`POST /api/auth/login` is the only endpoint that answers without a token. `/api/analyze`,
`/api/analyze/stream`, `/api/history` and `/api/auth/me` all sit behind
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

### Mock mode: `python run_mock.py`, for looking at the app without paying for it

**Use this for any frontend or UI work.** It serves the real application on 8001 with only
the metered boundaries replaced, so a full run — login, SSE progress, report, History —
costs nothing instead of ~25 SerpAPI searches. `--fast` drops the pacing delays; `--port`
moves it. Postgres and Qdrant still need to be up, and the frontend needs no change,
because the mock takes the port the frontend already targets.

Faked: `llm._llm_chat`, `compare.embed_texts`, `search.vector_store.embed_texts`,
`search.search.SOURCES`, and both `graph.searching.enrich_*` functions. **Everything else
is real** — the FastAPI app, the auth gate, the graph and its routing, the SSE framing,
prompt construction, JSON parsing, dedup, chunking, cosine scoring, verdict thresholds,
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
- **The mock embedder emits 1024-dim vectors**, matching bge-large. A Qdrant collection
  fixes its width on creation, so a narrower vector would fail against a `patents`
  collection a real run already made. Nothing writes today (indexing is disabled in
  `graph/pipeline.py`), which is exactly why the mismatch would be baffling later.

The LLM double keys off real prompt substrings (`"PatentScout AI"`, `"screening N
candidate"`, `"expert patent examiner"`), so the real prompt-building code still runs and a
prompt edit that breaks those markers raises there rather than passing silently. The same
markers back `tests/test_pipeline_contract.py`; changing a prompt means checking both.

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
  workspace is gated on `auth.signedIn()`** — signed out, the sidebar shows only the mark and
  the theme toggle, and `LoginScreen` replaces the `@switch` entirely.

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
- `config.ts` — `API_BASE_URL`, replacing `import.meta.env.VITE_API_URL`. See the running
  section above for the `window.NOVELTY_LENS_API_URL` override.
- `models/report.ts` — `AnalysisReport`, `HistoryEntry`, `PHASE_ORDER`, `PHASE_LABELS`,
  `PHASE_ICONS`, and the `StreamEvent` union. `PHASE_ORDER` is the mirror of
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

**The product has its own mark now — `novelty-lens-mark.svg` — and it ships in three
cuts.** It is not the Aramco AI mark; it borrows that mark's palette (the
blue→teal→green gradient already in `theme.css`) and nothing else. The drawing is a lens
ring holding a weighted cluster of prior art, with the invention as a green ring outside
the glass; the gap between them is the novelty score, which is the one thing the product
actually measures. All three cuts are hand-written SVG on a 64-unit grid, optically
centred on 32,32, and each carries a comment explaining what it is for:

- `novelty-lens-mark.svg` — the colour cut. Used by `shared/chrome.ts`
  (`.brand-mark`) on every screen, **and on the rail in dark mode**.
- `novelty-lens-mark-white.svg` — a **drawn** reversed cut, used on the rail in **light
  mode only**. It is not the colour cut with a filter over it. `globals.css`'s
  `.rail-brand img` used to carry `filter: brightness(0) invert(1)`, which knocks
  everything to a single white value and loses the cluster's opacity weighting against the
  tile's mid-tone gradient; the reversed cut lifts those opacities (0.44–0.72 → 0.60–0.86)
  instead. **The filter is gone — do not put it back.**

  `app.ts` picks between the two off the `theme()` signal, and the split tracks the tile,
  not the theme: light still paints the blue→green gradient behind the mark, which needs
  the reversed cut; dark made `.rail-brand` transparent when it was flattened, so the mark
  sits on flat black and shows its real colours. If either tile ever changes, the pairing
  changes with it.
- `novelty-lens-favicon.svg` — the small-size cut for `<link rel="icon">` in
  `src/index.html`, with the cluster cut from seven dots to three and every stroke
  thickened. Below ~32px the full cluster fills in to a smudge. Use the full mark above
  that size.

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
and `.verdict-strong`. Recolouring those to brand teal would destroy the one thing their
colour communicates. If you are adding an accent, use teal; if you are adding a status,
match the existing scale. The dark `.result-count` / `.verdict-strong` rule was split for
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
threw away everything the stream had already said. `ErrorScreen` now renders the backend's
`error` detail **verbatim** in a mono block, names the phase it died in, and shows how many
of the nine phases completed — `startRunning` tracks `lastPhase` in a local declared
*outside* the `try` precisely so the `catch` can read it without waiting on a signal update.
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

**Phases 1 and 3 have landed.** The `StateGraph`, the graph steps as nodes, and the
`PIPELINE` flag that switches between them (Phase 1); Semantic Search, Result Fusion, and
Validation as live graph nodes, plus embedding-based ranking with no TF-IDF fallback
(Phase 3, P3.1–P3.6). **Still open within Phase 3**: P3.7 (the frontend does not consume
the SSE stream at all today, so there is no `PHASES` array to extend in lockstep — see the
SSE contract note earlier in this file) and the full P3.8 failure-path test matrix (a
handful of focused tests exist per new agent; a dedicated outage-injection suite does
not). **Phase 2 is now half-landed**: persisted history is live (Postgres, `reports.py`,
`GET /api/history`, the History screen), caching is not and is deliberately open — see the
Persistence section above for the line between them. Two constraints govern all of it:

- **Zero-tolerance failure policy**: no fallback, ever. If a step can't produce a genuine
  result, it must fail the request with a clear `error` event — never substitute
  fabricated or placeholder output. Commit `e96edfac` removed a fabricated score and the
  `_fallback_rationale` / `_fallback_recommendations` pair in `backend/llm.py`; do not go
  looking for them. The two fallbacks that outlived it — "succeed if any provider works"
  and the canned `"No reference targets found…"` recommendation — lived in the legacy path
  and went with it. **There are now no fallbacks anywhere in the backend.** Adding one back
  is the single most consequential way to break this project's contract with its users.
- **USPTO is out of scope** — no API access is available. Don't add a USPTO provider.
