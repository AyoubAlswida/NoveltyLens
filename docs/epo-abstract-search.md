# EPO abstract search rollout

The main application now uses EPO OPS abstract discovery. The portable application
is unchanged. This is preliminary proximity screening, not a legal novelty test.

## Setup

1. Set `EPO_CONSUMER_KEY` and `EPO_CONSUMER_SECRET` in backend/.env. Keep the existing
   deployment profile, LLM, embeddings, database and Qdrant configuration.
2. Start the project's PostgreSQL and Qdrant services. From backend run
   `python migrate.py` and `python checkpointer.py setup` before starting API/worker.
3. Restart API and worker together. Existing in-progress durable runs predate the
   new retrieval version and must restart; completed historical reports remain.
4. Run `python -m pytest tests/test_abstract_search.py tests/test_abstract_search_postgres.py -q -rs`.
   A skipped database check is not evidence of a successful migration.

Defaults: 6-12 initial queries; up to four expansion queries; 50 records/page;
20 successful search pages total, at most two per query; up to three extended
family lookups with at most 12 new publications per lookup; all unique available abstracts
screened in batches <=3, with up to four concurrent LLM calls; <=100 final
publications, including multiple publications from one identified family when
screened results warrant them. Both modes fill remaining slots with screened
additional publications when available. All returned results use the original
report list. Execution deadline is 30 minutes after reviews.
Global PostgreSQL throttling starts at eight search attempts/minute across workers
and API processes. Retries count as attempts, not new successful pages.

## Live acceptance

Prepare a JSON array of **at least ten genuine labelled cases**, each containing
`description` and `expected_publications` (publication numbers including kind).
Then, from backend:

```powershell
python acceptance/abstract_search_acceptance.py --cases labelled-cases.json --output abstract-benchmark.json
```

This uses real quotas and stores actual completed searches. It records time,
coverage counts and recall@100 against the supplied labels. Labels do not prove
comprehensive recall. Missing labels must not be replaced by invented cases.
Inspect source availability and EPO/LLM/embedding billing usage alongside these
results before rollout. Successful local mocks do not establish live service latency,
abstract coverage or cost.

## Live verification (2026-11-02)

Docker Desktop was started and the existing PostgreSQL/Qdrant volumes were kept.
Migration 0027 and LangGraph checkpoint setup completed. EPO OAuth and a live
abstract search both returned successfully. The embedding endpoint was already
OpenAI, but its credential referenced HF_TOKEN; the local .env reference was
corrected to OPENAI_API_KEY. The configured text-embedding-3-large endpoint then
returned a valid 3072-dimensional vector.

The live test exposed overly restrictive query plans. Initial plans now require
two broad recall angles (at most two AND groups), and expansion receives the
actual per-query hit counts. A zero-hit expansion must relax conditions.

Free-form model quotations also failed exact-copy validation in the live test.
The model now selects numbered source spans. The server slices the quote directly
from the original abstract and records its exact character offsets. Invalid span
ids still fail; there is no fuzzy matching or invented quotation.

The API and frontend health endpoints returned 200. The live worker generated the
first Advanced Search review. No human approval was submitted by the diagnostic;
its newly-created test run was removed afterwards.

The focused backend suite passed 147 tests, including real PostgreSQL report
persistence, shared throttling, executor and report API checks. Frontend: 84 tests
passed. The complete live Quick Search finished in 519.9 seconds (8m 40s):
9 queries, 14 pages, 602 raw records, 564 unique publications/abstracts, and
41 related results (1 High, 35 Medium, 5 Low), plus five drafting suggestions.
Every available unique abstract was analyzed. The known-source patent
US5109799A ranked first. All 52 returned evidence ranges were verified against
the exact saved abstracts. The authenticated History endpoint returned the
same patent results and coverage as the live pipeline.

The measured case is recorded in
`docs/measurements/epo-abstract-live-2026-11-02.json`. This is one operational
smoke test, not a representative latency distribution or a recall benchmark.
The API/worker run natively, the frontend dev server is at http://localhost:4200,
and PostgreSQL/Qdrant run in Docker with existing volumes preserved.

The ten-case recall benchmark is deferred at the user's request because no labelled
cases are ready. A single operational smoke test must not be described as a recall
benchmark or as proof of comprehensive coverage.

## Current HF-only selection (2026-11-02)

The user subsequently requested HF for chat and embeddings. The local .env now
selects DEPLOYMENT_PROFILE=huggingface, Qwen/Qwen3-235B-A22B-Instruct-2507:deepinfra
and BAAI/bge-large-en-v1.5 via hf-inference, both authenticated with HF_TOKEN.
API and worker were restarted with this selection. The OpenAI-backed successful
measurement above is historical and does not establish HF quality or latency.
HF token validation succeeded; live chat and embedding requests both returned
HTTP 402. Billing/credits must be resolved before live HF validation. No GPT
fallback is enabled. The focused provider/protocol/regression suite passed 149
unit tests, including fail-fast billing errors and no-OpenAI-fallback assertions.

## UI freeze (2026-11-02)

At the user's request, the original report cards, labels, links, styles and
PDF/Word layouts are restored. The coverage banner, evidence controls and added
pagination are removed. Backend provider, retrieval, analysis, persistence and
API metadata remain intact. Future backend changes must not alter visible UI
without an explicit request.
