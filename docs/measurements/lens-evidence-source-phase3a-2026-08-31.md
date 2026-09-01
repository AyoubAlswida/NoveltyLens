# Phase 3A — Lens.org as an evidence source: measurement report — 2026-08-31

**Status: MEASURED.** 34 of 34 publications answered, zero provider failures.

**Headline: Lens 61.8% vs EPO 17.6% on the identical sample — but the two are
complementary, not substitutes.** Replacing EPO with Lens would *gain* 18
publications and *lose* 3. Running both gives **70.6%**.

**Nothing in production changed.** Discovery is still Lens.org, evidence is still
EPO OPS, `SOURCES` is untouched, and no graph, API, migration or frontend was
modified. The one module added (`backend/evidence/lens_coverage.py`) is imported
by no runtime module, and `tests/test_lens_coverage_secrets.py` asserts that by
walking `graph/`, `workflow/`, `api/`, `search/`, `verification/`, `server.py`,
`run_mock.py` and `llm.py`.

Raw tool output: `docs/measurements/lens-fulltext-probe-2026-08-31.md` and its
JSON sidecar. This file is the analysis and the decision.

---

## 1. Headline numbers

| Source | Sample | Coverage | US | KR | CN | DK | EP | CA | WO |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| EPO OPS (2026-08-28) | 34 | 17.6% | **0%** | 0% | 0% | 0% | **75%** | **100%** | 100% |
| Lens.org (2026-08-31) | 34 | **61.8%** | **100%** | 0% | 0% | 0% | 50% | **0%** | 100% |
| **Union (both)** | 34 | **70.6%** | 100% | 0% | 0% | 0% | 75% | 100% | 100% |

Set breakdown over the 34:

| Set | Count | Publications |
|---|---:|---|
| **Lens only** | **18** | every US publication in the sample |
| **EPO only** | **3** | `CA2919371`, `CA3113249`, `EP3010333` |
| Both | 3 | `EP2987654`, `EP3071523`, `WO2020115432` |
| Neither | 10 | all 6 KR, both CN, `DK2323952`, `EP3218319` |

**This is the finding that decides the question.** The EPO artifact's option 2
("add a second enrichment source") is measurably the right one, and option 3
(narrow/replace) is measurably lossy: a straight swap drops `CA2919371`,
`CA3113249` and `EP3010333` — 8.8% of the sample — for no reason.

The complementarity is structural, not coincidental. EPO's full-text service
covers EP/WO-family documents; Lens's index is strongest on US grants. They fail
in different places by design, which is exactly what makes them worth having
together.

**Ten publications neither source can serve** (KR x6, CN x2, `DK2323952`,
`EP3218319`). Those remain `EXCLUDED_NO_FULL_TEXT` under any combination
measured here, and the report's coverage-disclosure obligation is unchanged for
them.

---

## 2. Evidence-contract quality

Every quality property the evidence layer needs held, on all 21 publications
Lens returned text for:

| Property | Result |
|---|---|
| Served identity matched the request | **34 / 34** — zero borrowed family text |
| Text version date present and parseable | **34 / 34** (`date_published` maps to `PROVIDER_PUBLICATION_DATE`, the *approved* policy) |
| Claim numbering | **21 / 21 `STABLE`** — zero `ClaimNumberingError` |
| `SOURCE_RANGE` locators reproducible | **21 / 21** — every segment slices back out of the exact retrieved text byte-for-byte |
| Provider paragraph identifiers | **0 / 21** — Lens serves the description as one blob, so every description locator is `SOURCE_RANGE` |
| Retries needed | **0** at the paced rate |

Description sizes ran 11,957 to 232,362 characters (median about 46,000),
producing 8 to 122 `SOURCE_RANGE` locators each. Claim counts ran 1 to 98,
median 18.

### Two quality caveats, both real

1. **`WO2020115432` is French, and Lens returned it as a single claim** — one
   `claim 1` covering the claim set of a 43,319-character document. A locator
   reading "claim 1" for an entire claim block is a weak citation, and it is the
   one record in the sample where the claim structure looks unusable rather than
   absent. `_parse_claims` cannot detect this: one claim numbered 1 is
   structurally valid. **A minimum-plausibility check on claim structure is
   engineering this measurement newly justifies.**
2. **Language is not always English.** 20 of 21 were `en`; one was `fr`. The
   evidence layer records `language` per passage and the verifier prompt is
   English. Nothing today translates, flags, or refuses a non-English passage.
   That is a pre-existing gap this measurement makes concrete, not one Lens
   introduces.

---

## 3. Latency and operational profile

Paced, sequential, concurrency 1:

- **p50 0.41s, p95 1.28s, max 2.53s.**
- 34 publications in 3m55s wall clock, of which about 3m35s was deliberate
  pacing.
- **Zero retries were needed.** Zero timeouts, zero truncated responses.

Against EPO, which needs **three** calls per publication (claims, description,
biblio) where Lens needs **one**, this is a 3x call reduction and it removes the
separate bibliographic-request failure mode entirely.

The 30s-700s responses and mid-transfer connection cuts recorded in
`search/lens.py`'s constants are a **discovery**-path artefact — many results
per response, each carrying full text. A single-publication enrichment lookup
does not exhibit it. That is a useful negative finding: the reliability concern
documented in that module does not transfer to the enrichment role.

### Rate limiting, and a measurement that was nearly wrong

The trial tier allows **10 requests/minute** and **1000 requests/month**
(`x-rate-limit-remaining-request-per-minute` / `-per-month`; the monthly window
resets 2026-09-17).

The **first, unpaced** run of this tool spent 34 requests and got 23 back as
HTTP 429. It would have reported *"coverage 72.7% of 11 answered"* — a number
computed over the throttle rather than over the index. It did not report 23
publications as `NO_TEXT`, because `FAILED` is excluded from the denominator by
construction; but 11 of 34 is not the authorised sample, and publishing it would
have been a measurement of the rate limiter.

Three things were added in response, all pinned by tests:
`REQUESTS_PER_MINUTE = 10` with `DEFAULT_DELAY_SECONDS` **derived** from it;
bounded retry (`MAX_ATTEMPTS = 3`) honouring `Retry-After`, retrying only
`{429, 500, 502, 503, 504}` and never a deterministic 4xx; and a JSON sidecar
with `--merge-json`, so an interrupted run resumes by **reusing answered
publications and retrying only failed ones**.

---

## 4. Contract gaps that remain

| Field | Why | Status |
|---|---|---|
| `source` | `evidence/passages.py::_passage` hardcodes EPO OPS | **Blocker.** A Lens passage today would stamp EPO provenance on text EPO never served |
| `source_document_id` | `evidence/normalize.py` hardcodes an `epo:` prefix | **Blocker.** Same |
| `paragraph_ids` | Lens serves description as one blob | **Confirmed by measurement** (0/21). Not a blocker — `SOURCE_RANGE` is the honest locator and all 21 were reproducible |
| claim char offsets | `_parse_claims` returns `PatentClaim` (no offsets); `ProviderClaim` needs `char_start`/`char_end` into one canonical claims-section text | **Engineering.** Lens returns claims as fragments, so the section text has to be assembled and the offsets taken from it |

**No migration is required.** `migrations/0012.create-evidence-passages.sql`
types `source` and `source_document_id` as non-empty `TEXT` — free text, not an
enum — so a Lens source name and a non-EPO document id are already storable.
The `locator_type` enum's `EPO_PARAGRAPH` label is EPO-specific vocabulary, but
Lens produces no paragraph locators, so no enum change is needed either.

### The independence question is now sharper, not softer

Lens is already the only enabled discovery provider (`search/search.py`).
`search/enrichment.py` states the rule — "a discovery source and an enrichment
source are different roles" — and `evidence/passages.py` enforces it
structurally: `build_passages` takes **no `PatentRecord`**, so discovery
metadata can never become evidence.

The measurement makes this concrete. This probe queried Lens's patent search
endpoint — **the same endpoint discovery uses**. Routing its output into a
passage is precisely what that signature forbids, unless the enrichment fetch is
a distinct, per-publication, recorded act against the approved candidate set.
That is implementable — this probe *is* that shape, one request per publication
by publication number — but it must be built as such and recorded as such, not
achieved by reading the discovery response twice.

**Keeping EPO removes the single-point-of-failure objection entirely**, which is
a second reason the additive design beats replacement.

---

## 5. Commercial and trial implications

- The token is a **non-commercial 14-day trial**; continued or commercial use
  needs a paid subscription (`subscriptions@lens.org`).
- Observed allowance: **1000 requests/month**, **100,000 records/month**,
  **10 requests/minute**. After all Phase 3A work, **904 requests remained**.
- At one request per publication, a 15-candidate verified run costs **15
  requests** — roughly **60 verified runs/month** on the trial tier before the
  monthly cap binds. The per-minute limit makes a 15-candidate enrichment pass
  take about 95s sequentially, which is material for a workflow that already
  pauses for human review but must be designed for, not discovered in
  production.
- **This is measured on a trial key and tier limits are tier-dependent.**
  Coverage should carry over to a paid tier; the 10/minute ceiling and the
  monthly cap almost certainly will not.
- **EPO OPS is currently free under existing credentials.** Adding Lens is a
  recurring cost; *replacing* EPO with it would be a recurring cost that also
  loses coverage. Price before engineering.

---

## 6. Methodology

Command: `python -m evidence.lens_coverage` with optional `--dry-run`,
`--max-calls N`, `--delay-seconds S`, `--merge-json PATH`.

* **The sample is TAKEN, never GENERATED.** No code path runs discovery to build
  one.
* **`--dry-run` performs zero HTTP calls.** Run first, every time.
* **One request per distinct publication, strictly sequential.** Concurrency is
  1 *by construction* — no flag can raise it — respecting `search/lens.py`'s
  module-level request lock.
* **Paced** at `DEFAULT_DELAY_SECONDS` (6.5s), derived from the measured
  `REQUESTS_PER_MINUTE = 10`.
* **`--max-calls` (default 40) is checked before each request**, not after.
* **Bounded retry**, `Retry-After` honoured, injectable sleeper so no test
  sleeps. Deterministic 4xx never retried. Exhaustion is `FAILED`.
* **Reuses `search/lens.py`'s own parsers** — a second parser would measure
  itself rather than the production adapter.
* **`FAILED` is never counted as `NO_TEXT`**, and coverage is `None`, never
  `0.0`, when nothing was answered.
* **No credential means exit 3 and no artifact**, so a 0% file can never be
  confused with a measured absence.

### Sample

`backend/evidence/samples/epo-coverage-2026-08-28.publications.txt` — the
**identical 34 publications** from `docs/measurements/epo-coverage-2026-08-28.md`,
pinned verbatim so the comparison is like-for-like. 34 distinct, 0 duplicates
collapsed. US=18, KR=6, EP=4, CA=2, CN=2, DK=1, WO=1. A test asserts the count
and the jurisdiction split so the sample cannot drift away from the artifact it
is meant to be compared with.

### Credential and body safety

The token is interpolated in **exactly one** place (the Authorization header).
A response body is read in **exactly one** place, which returns a category and
discards it; stored detail is rebuilt from the status alone. Quota readings come
from response *headers*, never the body. `tests/test_lens_coverage_secrets.py` —
**40 tests, all passing** — drives the probe with bodies that echo the bearer
token and transport exceptions carrying it, and asserts the token appears in
neither the rendered artifact, the row repr, nor the JSON sidecar.

---

## 7. Limitations

1. **34 publications from one historical run, 18 of them US.** The right sample
   for comparability with the EPO artifact; not a representative sample of
   global discovery output. The US result (18/18) is strong but is 18 documents.
2. **Trial tier.** Coverage should generalise; the rate and quota profile will
   not. See section 5.
3. **Coverage is not evidence quality.** "Lens returned a description and
   claims" is not "the text is complete, correct, and the same text the office
   published". Nothing here compares Lens's text against EPO's for the three
   publications both served. **That comparison is the obvious next test.**
4. **The `WO2020115432` single-claim result is unexplained** (section 2). It may
   be a provider artefact for French-language WO documents, or specific to that
   record. Not diagnosed.
5. **The query form was validated only in the affirmative.** The
   jurisdiction-plus-document-number query matched all 34 with a correct served
   identity, so it works; no genuinely absent publication was exercised, and
   `NOT_MATCHED` was never observed. Note what that implies: **zero
   `NOT_MATCHED` on a sample where 13 came back `NO_TEXT` means Lens's index
   held all 34** — the 13 are documents Lens indexes without full text.
6. **`RETRIEVAL_FLOOR` remains `None`.** Nothing here measures or unblocks it,
   and no threshold should be invented from this report.
7. **No enrichment implementation was written or tested.** Section 4's blockers
   are analysed, not addressed.

---

## 8. Decision recommendation

### B — Lens is promising but requires engineering and one further test.

**A is refuted by the data**, and that is worth stating plainly: replacing EPO
with Lens is *lossy*. It would drop `CA2919371`, `CA3113249` and `EP3010333` —
three publications EPO serves and Lens does not — to gain eighteen. The correct
move is not a swap.

**C is too pessimistic.** The measurement is strong, positive and clean: 34/34
answered, zero failures, 61.8% against EPO's 17.6%, and every evidence-contract
quality property held on all 21 documents with text.

### The recommended design

**Add Lens as a second enrichment source alongside EPO. Do not remove EPO.**
That is option 2 in the EPO artifact's own list, and this measurement is what
selects it: 70.6% union coverage, no coverage lost, no single point of failure,
and EPO stays free.

Routing is a decision to record rather than assume. Two candidates: prefer by
jurisdiction (EPO for EP/WO/CA, Lens for US), or try both and take whichever
returns eligible text. Either way, which provider served each passage is
recorded — `source` already exists to carry it.

### Engineering required before any of this ships

1. **Parameterise provenance by provider** — remove the two hardcodes in
   `evidence/passages.py` (`source`) and `evidence/normalize.py`
   (`source_document_id`). No migration.
2. **Build a Lens enrichment function** returning a real `EnrichmentResult`,
   *alongside* `search/epo_fulltext.py`, routed through
   `search/enrichment.py`'s `ENRICHMENT_PROVIDER` seam — which becomes a list.
   It must be a distinct per-publication fetch against the approved candidate
   set, not a re-read of the discovery response (section 4).
3. **Assemble a canonical claims section text and take real `ProviderClaim`
   offsets from it**, so claim passages are traceable into a hashed section.
4. **Add a claim-structure plausibility check** — the `WO2020115432` case.
5. **Pace and budget the enrichment pass** — the 10/minute ceiling and the
   1000/month cap must be designed in, not discovered in production.
6. **Record two owner decisions**: whether one vendor may serve both the
   discovery and evidence roles, and the routing rule between the two sources.

### One further test, before or alongside that engineering

**Compare Lens's text against EPO's for the three publications both served**
(`EP2987654`, `EP3071523`, `WO2020115432`). Coverage says a provider *has*
something; it does not say the something is the same document. **Cost: 3 Lens
requests plus 9 EPO calls.** This is the cheapest high-value test remaining and
it should be run before any passage is built from Lens text.

**No implementation has been started, and none should be until this
recommendation is approved.**

---

## 9. What this is not

A dated measurement, not a cache. Nothing in the pipeline reads this file and it
never short-circuits a provider call. No credential, no provider response body,
and no fabricated per-publication outcome appears anywhere in it.
