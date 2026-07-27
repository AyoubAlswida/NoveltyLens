# Phase Prompts — LangGraph Migration

Ready-to-paste prompts for Claude Code and Codex, one per phase of
`docs/Team-Collaboration-Plan.docx` (phases 0–3).

## How to use these

1. **Paste Block A (shared preamble) first, then one phase prompt.** The preamble carries
   the constraints that apply to every ticket; the phase prompt carries only what that
   phase needs. Keeping them separate is deliberate — it stops each prompt from carrying
   three phases' worth of irrelevant context.
2. **One prompt run = one ticket = one branch.** Each phase prompt has a `TICKET:` line.
   Fill in exactly one ID (`P1.2`, not `P1.2–P1.4`). This matches the collab plan's
   "one branch per ticket, not per phase" rule — a PR that lands all of Phase 1 is not
   reviewable, especially when most of the diff came from a prompt.
3. **Both tools get the same text.** Claude Code reads `CLAUDE.md` automatically; Codex
   reads `AGENTS.md`, which points at `CLAUDE.md`. The preamble makes the constraints
   explicit anyway, so neither tool depends on having loaded the file.
4. Fill the `{{...}}` placeholders before sending.

---

## Block A — Shared preamble (paste above every phase prompt)

```
<role>
You are a senior Python engineer working on AgenticAI, a patent novelty-search tool.
You are executing one ticket of an approved migration from a linear pipeline to a
LangGraph multi-agent StateGraph.
</role>

<non_negotiable_constraints>
These four rules override any instinct to be helpful, and they apply to every line of
code you write in this repo. Violating any one of them fails the PR.

1. ZERO-TOLERANCE FAILURE POLICY. No agent may ever invent a result. If a step cannot
   produce a genuine output, the request MUST fail with a clear error. Never substitute
   fabricated, placeholder, degraded, or "good enough" output. No silent fallbacks. No
   default/zero vectors. No canned text. No partial-success-counts-as-success. We would
   rather show the user nothing than show them something untrue.

2. THE SSE CONTRACT IS SHARED STATE. It spans backend/server.py and
   frontend/src/app/App.tsx with no schema enforcing it. Every event has
   type = "progress" | "result" | "error". progress.phase must be a value the frontend's
   PHASES array knows (frontend/src/app/App.tsx:10-16). An unrecognized phase silently
   falls back to the first checklist item instead of erroring — so drift here is invisible
   in testing and broken in production. The stream must end with exactly one "result"
   event or the frontend throws. If you change event shape or phase names, you change
   BOTH files in the SAME commit.

3. USPTO IS OUT OF SCOPE. No API access is available. Do not add a USPTO provider, do not
   add PatentsView code, do not "prepare" for one. The uspto_api_key field in
   backend/config.py is dead code.

4. READ BEFORE YOU WRITE. Never speculate about code you have not opened. Open every file
   you are about to modify and every file it imports from. Do not assume CLAUDE.md's
   description of a function matches the current code — verify against the source. Where
   they disagree, the source wins and you fix CLAUDE.md.
</non_negotiable_constraints>

<scope_discipline>
Make only the changes this ticket requires. Do not refactor adjacent code, do not add
abstractions for one-time operations, do not add features nobody asked for, do not
"improve" modules outside the ticket's Definition of Done. A reviewable diff is the point.
If you believe an out-of-scope change is genuinely necessary, stop and say so instead of
making it.
</scope_discipline>

<repo_facts>
Two independent processes, no shared tooling:
- Backend: FastAPI, run from backend/ via `python server.py` on http://127.0.0.1:8001
  (hardcoded). Deps in backend/requirements.txt. Config in backend/config.py, loaded from
  backend/.env (see backend/.env.example).
- Frontend: Vite/React, run from frontend/ via `npm run dev`. The entire app is
  frontend/src/app/App.tsx (~700 lines, inline styles, imports only useState).
  frontend/src/app/components/ui/ and most of package.json (MUI, recharts, react-dnd,
  react-router) are unused Figma-scaffolding leftovers — do not extend them.

Current backend pipeline — a single generator, `_run_pipeline` in backend/server.py,
shared by POST /api/analyze/stream (SSE) and POST /api/analyze (drains it, returns the
final JSON). In strict sequence:
  1. understand_invention()          backend/llm.py       one Llama call -> concepts, keywords, 6 queries
  2. search_patents()                backend/search/search.py  fans queries across SOURCES, dedupes by uppercased ID
  3. compare_invention_to_patents()  backend/compare.py   TF-IDF cosine similarity
  4. analyze_all_matches()           backend/llm.py       one batched Llama call -> per-patent rationale + shared recommendations

All LLM calls go through `_llm_chat` in backend/llm.py — a raw requests.post to an
OpenAI-compatible chat-completions endpoint (settings.llm_api_url, default the HuggingFace
Router; settings.llm_model, default meta-llama/Llama-3.1-8B-Instruct:novita). Not Ollama,
not Together, not Groq. Any OpenAI-compatible endpoint is a drop-in replacement — keep it
that way; do not leak provider-specific quirks into pipeline code.

Search providers normalize into the PatentRecord dataclass in backend/models.py.
backend/search/google.py = Google Patents via SerpAPI. backend/search/epo.py = EPO OPS,
OAuth2 client-credentials, XML parsing, fetches a fresh token every call.
A provider is any `(query: str, size: int) -> list[PatentRecord]` registered in the
SOURCES tuple in backend/search/search.py.

There is no database, no cache, and no vector store in the current code. Every search
re-fetches live. `settings.pipeline` (env PIPELINE, default "legacy") exists in
backend/config.py but nothing branches on it yet.

langgraph, rank_bm25, sentence-transformers, pytest and pytest-asyncio are in
backend/requirements.txt but imported nowhere — they are pre-staged for this migration.
There is no test suite, no lint config, no pyproject.toml, no tsconfig.json.
</repo_facts>

<workflow>
- Branch from main: one branch per ticket, named for the ticket ID.
- Never push directly to main. Never force-push main. Every change lands through a PR.
- Keep main deployable at every commit. That is what the PIPELINE flag is for.
- If your ticket touches the shared graph-state schema (P1.1's output), say so explicitly
  in the PR description — the schema owner must review it, and that review is not
  reassignable.
- Update the Architecture section of CLAUDE.md in the SAME PR as the code, not as a
  follow-up. If you add or remove a major dependency, data source, or persistence layer,
  or change the SSE event contract, CLAUDE.md reflects it before you call the task done.
</workflow>
```

---

## Phase 0 — Foundations

One person, first. Blocking. Nothing user-facing changes.

```
TICKET: P0.1

<task>
Prepare the environment for the LangGraph migration. This ticket adds capability only —
after it lands, the application must behave exactly as it does today.

1. Add the missing dependencies to backend/requirements.txt. langgraph is already listed;
   qdrant-client is not. Add qdrant-client and whatever PostgreSQL driver + migration tool
   you select in step 3. Do not remove the currently-unused entries (rank_bm25,
   sentence-transformers, pytest, pytest-asyncio) — later phases claim them.

2. Wire the PIPELINE flag through backend/config.py so it is a validated, typed switch
   rather than a bare string. Today it is `pipeline: str = os.getenv("PIPELINE", "legacy")`
   and nothing reads it. After this ticket it must:
   - accept exactly "legacy" or "langgraph" and reject anything else at startup with a
     clear error (zero-tolerance applies to config too — do not silently coerce a typo to
     "legacy"),
   - still default to "legacy",
   - still have no caller. Wiring the branch is P1.5's job, not yours.

3. Add a local Docker Compose file bringing up Qdrant and PostgreSQL for development.
   Pin image versions. Bind ports that do not collide with the backend's 127.0.0.1:8001.
   Persist volumes so a restart does not wipe local data. Add the connection settings to
   backend/config.py and backend/.env.example following the existing pattern in those
   files, with local-dev defaults that work against the Compose services.
</task>

<definition_of_done>
- `docker compose up` brings both services healthy from a clean checkout.
- `pip install -r backend/requirements.txt` succeeds.
- `python server.py` starts and serves on 127.0.0.1:8001 exactly as before.
- PIPELINE=legacy remains the default; an invalid PIPELINE value fails loudly at startup.
- A full analyze request through the frontend produces byte-identical behavior to main.
- Nothing user-facing changed.
</definition_of_done>

<decisions_to_surface>
You are choosing tools the next three phases inherit. State your choice and one-line
reasoning for each in the PR description, and flag anything you are unsure about rather
than deciding silently:
- PostgreSQL driver (sync vs async — note the backend is currently sync end to end)
- migration tool
- Qdrant and PostgreSQL image versions
Do not build an abstraction layer over either service in this ticket. Phase 2 and Phase 3
decide what they need.
</decisions_to_surface>

<before_you_finish>
1. Confirm the app still runs and behaves identically — actually start it, do not assume.
2. Confirm PIPELINE still defaults to "legacy" and that nothing branches on it yet.
3. Update the Architecture section of CLAUDE.md: new dependencies, new local
   infrastructure, the flag's new validation. Same PR.
4. State plainly in the PR what you added, what you deliberately did NOT add, and every
   version you pinned.
</before_you_finish>
```

---

## Phase 1 — LangGraph engine (parity)

Re-express the current pipeline as graph nodes and strip the fallback behavior it still
has. First user-facing milestone and the gate everything after depends on.

**Schema first.** P1.1 defines the state schema every later node reads and writes. It gets
one owner, not four independent guesses. Do not start P1.2–P1.4 until P1.1 has landed and
the team has agreed to it — retrofitting nodes onto a schema that moves underneath them is
the failure mode this sequencing exists to prevent.

```
TICKET: {{P1.1 | P1.2 | P1.3 | P1.4 | P1.5 — exactly one}}

<phase_goal>
Re-express today's four-step pipeline as a LangGraph StateGraph, behind PIPELINE=langgraph,
with the fallback behavior removed. On the happy path the two pipelines must emit
BYTE-IDENTICAL SSE events. On the failure path they diverge by design: the new pipeline
emits a clean error where legacy degraded silently.

New code goes in a backend/graph/ package. Do not modify the legacy path in
backend/server.py's `_run_pipeline` — it stays as the parity reference until P1.5 passes.
Extract shared logic rather than duplicating it where that is clean; duplicate rather than
contorting the legacy path where it is not.
</phase_goal>

<tickets>
P1.1  Define the shared graph state schema. GATES EVERYTHING BELOW.
      Depends on: P0.1
      DoD: Schema documented and agreed by the team BEFORE anyone builds a node against
      it. It must carry everything the current pipeline threads through _run_pipeline
      (description, claims, the InventionAnalysis fields, PatentRecord lists,
      ComparisonResult scores, MatchAnalysis rationales + recommendations) AND leave
      room for Phase 3's semantic results, fused ordering, and validation verdicts without
      a breaking change. Read backend/models.py first — the existing dataclasses are the
      starting vocabulary, and field names should not drift from them for no reason.
      The owner of this ticket reviews every later PR touching shared state.

P1.2  Wrap Query Understanding + Query Expansion as graph nodes (existing backend/llm.py logic).
      Depends on: P1.1
      DoD: The node RAISES on LLM failure. No truncated-description fallback query, ever.

P1.3  Wrap Search Orchestrator + Keyword Search as graph nodes (existing backend/search/search.py logic).
      Depends on: P1.1
      DoD: A SINGLE provider failure fails the request. This is a real behavior change —
      today `search_patents` counts the run as successful if any provider worked and
      swallows the rest in a try/except. That "succeed if any provider works" behavior is
      slated for removal, not extension. Read the function before changing it.

P1.4  Wrap Ranking (TF-IDF baseline) + Explanation as graph nodes.
      Depends on: P1.1
      DoD: The real similarity score is always shown. Explanation RAISES rather than
      falling back to canned text. Keep the TF-IDF algorithm in backend/compare.py
      unchanged — P3.6 replaces it with embeddings, and changing it here would corrupt
      P1.5's parity test.

P1.5  Parity test: diff the SSE event stream between the legacy and LangGraph pipelines
      for a sample invention.
      Depends on: P1.2, P1.3, P1.4
      DoD: Byte-identical events on the happy path. THIS IS THE GATE THAT FLIPS
      PIPELINE=langgraph ON. This ticket also wires the flag to actually branch
      (P0.1 deliberately left it unread). Note the two pipelines call a
      non-deterministic LLM — the test must isolate that (stub the LLM, or record and
      replay) rather than accepting "close enough".
</tickets>

<sse_contract_reference>
Do NOT change any of this in Phase 1. Phase 3 (P3.7) is the only ticket allowed to extend it.

progress.phase is exactly one of: understand | queries | search | compare | analyze
The frontend hard-codes this vocabulary in PHASES at frontend/src/app/App.tsx:10-16 to
drive a live checklist. An unrecognized phase does NOT error — it silently selects the
first checklist item. Read backend/server.py's `_run_pipeline` for the exact message
strings and the extra per-phase fields (count, current, total, found); reproduce them
character for character.

The stream ends with exactly one event of type "result", whose `data` object carries:
title, date, concepts, patents[], top_score, verdict, query_count, recommendations.
Each entry in patents[] carries: num, title, pct, risk, rationale, source.
Verify this against the current source before you rely on it — CLAUDE.md's summary of the
result shape is abbreviated and does not list every field.
</sse_contract_reference>

<stale_documentation_warning>
CLAUDE.md still describes three fallbacks as present: a hash-based fake score in
`_format_and_rank`, and a `_fallback_rationale` / `_fallback_recommendations` pair in
backend/llm.py. Commit e96edfac already removed all three — they are not in the code.
The remaining fallback this phase must remove is `search_patents`'s partial-success
behavior (P1.3). Verify the current state yourself before removing anything, and correct
CLAUDE.md as part of your PR.
</stale_documentation_warning>

<before_you_finish>
1. Confirm you touched only your ticket's scope. If your ticket is not P1.1, confirm you
   did not change the state schema; if you had to, stop and escalate to the schema owner.
2. Confirm the legacy path in backend/server.py still runs unmodified and PIPELINE still
   defaults to "legacy" (until P1.5).
3. Re-read your node's error handling against constraint 1. Any `except` that lets the
   request continue with less than a genuine result is a bug in this codebase.
4. Confirm you changed zero SSE phase names, message strings, and result fields.
5. Update CLAUDE.md's Architecture section in the same PR — the backend/graph/ package now
   exists and the pre-migration pipeline description is no longer the whole truth.
6. In the PR description, answer: "Does this touch the SSE contract, the zero-tolerance
   policy, or the graph-state schema? Confirm your tool had the relevant CLAUDE.md section
   in context before generating."
</before_you_finish>
```

---

## Phase 2 — Postgres persistence

Independent of Phase 3. Can start as soon as Phase 0's Docker Compose exists.

```
TICKET: {{P2.1 | P2.2 | P2.3 — exactly one}}

<phase_goal>
Give the system memory. Today there is no database, no cache, and no vector store — every
search re-fetches live from external APIs, and History (the fourth frontend screen) is
in-memory useState that does not survive a page refresh.
</phase_goal>

<tickets>
P2.1  Schema + migrations for cached patents and search runs/reports.
      Depends on: P0.1
      DoD: Migrations run clean against the local Docker Postgres from an empty database.
      Model cached patents on the PatentRecord dataclass in backend/models.py — it is the
      normalized shape every provider already produces. A stored run must capture enough
      to rebuild the exact "result" SSE payload: concepts, query_count, patents with their
      pct/risk/rationale/source, top_score, verdict, recommendations.

P2.2  Wire cache read/write into search; persist each generated report.
      Depends on: P2.1
      DoD: Repeat searches read from cache. Every completed run is stored.
      Read backend/search/search.py first — dedup is by uppercased patent ID
      (`merge_patents`), and the cache key must not contradict that.
      ZERO-TOLERANCE APPLIES HERE, and this is the ticket most likely to violate it: a
      cache miss is normal and must fall through to a live call, but a database that is
      unreachable or a write that fails MUST fail the request. Never serve a stale entry
      in place of a failed live call and never silently skip persistence. Decide and
      document a cache TTL — an unbounded patent cache is a correctness problem, not just
      a disk problem.

P2.3  History API endpoint(s) backing the frontend's History screen.
      Depends on: P2.2
      DoD: Search history survives a backend restart.
      The History screen currently holds records in useState in frontend/src/app/App.tsx
      with no backend endpoint. Read how it shapes and renders those records BEFORE
      designing the endpoint, and keep the response shape aligned so the frontend change
      stays small. Frontend and backend change together in this ticket.
</tickets>

<constraints>
- The backend is currently sync end to end (requests, not httpx.AsyncClient) inside async
  FastAPI handlers. Match P0.1's driver decision; do not mix sync and async DB access.
- Do not change the SSE event contract. Persistence is invisible to the stream in this
  phase. If you believe a new progress phase is warranted for a cache hit, that is P3.7's
  territory — raise it, do not add it.
- Do not touch backend/graph/ node logic beyond the minimum needed to persist results.
- Credentials go through backend/config.py and backend/.env.example like every other
  secret. No connection strings hardcoded anywhere.
</constraints>

<before_you_finish>
1. Run the migrations against an EMPTY database, then against an already-migrated one.
2. Kill and restart the backend, then confirm history is still there — that is P2.3's DoD
   and it is not satisfied by an in-process cache.
3. Verify a repeat search actually hits the cache (log it or assert it; do not assume).
4. Verify a DOWN database produces a clean error event, not a degraded live-only result.
5. Update CLAUDE.md — "No persistence layer exists yet" becomes false with this PR, and
   the section saying so must go in the same commit.
</before_you_finish>
```

---

## Phase 3 — Semantic intelligence

The three genuinely new agents, plus the SSE vocabulary they need.

**Do not start building nodes here until P1.1's schema is locked.** Retrofitting three
agents onto a schema that changes underneath them is worse than waiting a few days.

```
TICKET: {{P3.1 | P3.2 | P3.3 | P3.4 | P3.5 | P3.6 | P3.7 | P3.8 — exactly one}}

<phase_goal>
Complete the nine-agent workflow: add Semantic Search (new), Result Fusion (new), and
Validation (new), and upgrade Ranking from TF-IDF to embeddings. Keyword search and
semantic search run in parallel and converge at fusion.
</phase_goal>

<tickets>
P3.1  backend/embeddings.py — HuggingFace-hosted embedding client, reusing the existing
      HF token, with caching.
      Depends on: P1.1
      DoD: An embedding call RAISES cleanly on failure. No silent zero-vector fallback —
      a zero vector is fabricated data wearing a numeric disguise, and it is exactly what
      the zero-tolerance policy exists to prevent.
      HOSTED, not local. sentence-transformers is in requirements.txt but this client
      calls a HuggingFace-hosted model over the network, reusing settings.hf_token the
      same way backend/llm.py's `_llm_chat` does.
      Make the model name a config value from day one, not a hardcoded import — the
      deployment path (local -> Aramco SAIL -> GCP) may force a different provider, and a
      hardcoded model turns that into a code change plus a full corpus re-embed.

P3.2  backend/vectorstore.py — Qdrant client. The corpus grows from live search results;
      there is NO bulk ingestion step.
      Depends on: P0.1, P3.1
      DoD: Qdrant unreachable -> clean error. NOT a degraded keyword-only result that
      looks successful to the user.
      Record the embedding model and vector dimension with the collection. A model change
      invalidates the corpus on dimension mismatch, and silently querying a collection
      built by a different model is a correctness bug that produces plausible garbage.

P3.3  Semantic Search agent node — vector similarity via Qdrant.
      Depends on: P3.2
      DoD: A repeated query retrieves patents indexed by earlier searches.

P3.4  Result Fusion agent — Reciprocal Rank Fusion of the keyword and semantic result sets.
      Depends on: P1.3, P3.3
      DoD: Fused ordering DIFFERS from keyword-only on a query with semantic-only matches.
      A test that passes because both orderings happen to match has proven nothing.
      Fusion math is pure and deterministic — unit-test it directly with fixed rank lists,
      separately from any live provider call.

P3.5  Validation agent — LLM relevance check and filtering.
      Depends on: P3.4
      DoD: Same zero-tolerance policy — a failed validation call ERRORS the request. It
      does not pass results through unchecked, and it does not silently return everything.
      Goes through `_llm_chat` in backend/llm.py like every other LLM call. Batch it if
      you can: `analyze_all_matches` batches deliberately to stay friendly to the
      provider's rate limits, and one call per patent would undo that.

P3.6  Ranking upgrade — embeddings-based scoring, replacing the TF-IDF baseline.
      Depends on: P3.1, P1.4
      DoD: The score always reflects real similarity. A zero-similarity result still shows
      an HONEST ZERO — it is not floored, nudged, or hidden.
      backend/compare.py is the only module coupled to the ranking method, by design.
      Keep that property: replace its internals, do not spread embedding logic into callers.

P3.7  Extend the SSE progress vocabulary for the new agents; update the frontend's PHASES
      array in LOCKSTEP.
      Depends on: P3.3, P3.4, P3.5
      DoD: The UI checklist advances through every new step; no phase falls back to the
      first checklist item.
      THIS IS THE ONLY TICKET IN THE ENTIRE MIGRATION ALLOWED TO CHANGE THE PHASE
      VOCABULARY. backend/server.py and frontend/src/app/App.tsx:10-16 change in the SAME
      commit — not the same PR, the same commit. A phase name the frontend does not know
      does not raise; it silently selects the first item, so this failure is invisible
      unless you actually run the UI. Run it.
      Note that semantic and keyword search run in PARALLEL — decide how a linear
      checklist represents that, and say what you chose in the PR.

P3.8  Failure-path tests: LLM failure, single search-provider failure, Qdrant/embeddings
      outage, zero-similarity result.
      Depends on: P3.1–P3.6
      DoD: Every case above produces ONE clean error or a truthful score — never
      fabricated output.
      pytest and pytest-asyncio are already in requirements.txt and currently unused;
      there is no test suite, no pyproject.toml, and no lint config, so you are
      establishing the test layout. Keep it minimal and conventional.
      These tests are the enforcement mechanism for the zero-tolerance policy. Write them
      so they FAIL if someone later reintroduces a fallback.
</tickets>

<non_negotiable_for_this_phase>
"No agent may ever invent a result." If a step cannot produce a genuine output, the
request fails with a clear error — not degraded, not placeholder, not fabricated. This is
Section 4 of the architecture plan and it applies to code written by either tool.

Every ticket in this phase adds a new way for something to go wrong: a network call to an
embedding provider, a vector database, another LLM call. Each one is a temptation to add a
graceful degradation path. Do not. The degraded path IS the bug.
</non_negotiable_for_this_phase>

<before_you_finish>
1. Search your own diff for every `except`, every `or []`, every `if not X: return
   <something>`, and every default value. For each one, answer: does this let the request
   succeed with less than a genuine result? If yes, it is a bug — remove it.
2. Confirm no zero-vector, no empty-result-as-success, no "keyword only, semantic failed"
   silent path exists anywhere.
3. If your ticket is NOT P3.7: confirm you changed zero phase names in either file.
   If it IS P3.7: confirm both files changed in the same commit, and confirm you actually
   loaded the UI and watched the checklist advance.
4. Confirm the embedding model and Qdrant collection remain config-driven, not hardcoded.
5. Update CLAUDE.md's Architecture section in the same PR — this phase makes the migration
   real, and the "Planned migration" section describing it as unimplemented must shrink as
   each ticket lands.
6. In the PR description, answer: "Does this touch the SSE contract, the zero-tolerance
   policy, or the graph-state schema? Confirm your tool had the relevant CLAUDE.md section
   in context before generating."
</before_you_finish>
```

---

## Notes on how these are built

- **Constraints appear at the top and the bottom of every prompt.** Instructions in the
  middle of a long prompt get followed inconsistently; the zero-tolerance rule is stated in
  the preamble and re-checked in each `<before_you_finish>` for that reason.
- **Freedom is matched to fragility.** The SSE contract and the state schema get exact,
  low-freedom instructions ("byte-identical", "the same commit") because there is nothing
  catching a mistake. Tool selection in P0.1 and test layout in P3.8 get high freedom
  because many answers are fine.
- **Every DoD names the failure mode, not just the goal.** "No silent zero-vector fallback"
  closes a specific loophole; "handle errors properly" closes none.
- **The stale-documentation warning in Phase 1 is deliberate.** CLAUDE.md lists three
  fallbacks that commit e96edfac already removed. An agent told to "remove the fallbacks"
  would otherwise hunt for code that does not exist, or worse, invent a plausible
  replacement to remove.
