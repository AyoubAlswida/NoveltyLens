# EPO full-text coverage — 2026-08-28

Measured by `python -m evidence.coverage`. **No SerpAPI, Lens, embedding
or LLM quota was spent**: the sample was taken from data that already
existed and only EPO was called.

## Sample

- Source: **stored `patents` rows (most recent 34)**
- Raw entries: 34
- Distinct publications: 34 (0 duplicate(s) collapsed)
- Jurisdictions: CA=2, CN=2, DK=1, EP=4, KR=6, US=18, WO=1

## EPO response categories

| Category | Count |
|---|---:|
| `TEXT` | 6 |
| — claims available | 6 |
| — description available | 6 |
| — both | 6 |
| `NO_TEXT` | 28 |
| `FAILED` | 0 |

**Coverage: 17.6%** of the 34 publication(s) EPO
answered for.

`FAILED` is reported separately and is **not** counted as `NO_TEXT`.
Counting a provider outage as absent text would understate coverage
using the provider's own downtime.

## Coverage by jurisdiction

This is the cut that matters. EPO's full-text service holds text only
for EP/WO-family documents, so coverage is not evenly distributed --
an aggregate percentage hides which offices are excluded entirely.

| Office | `TEXT` | `NO_TEXT` | `FAILED` | Coverage |
|---|---:|---:|---:|---:|
| CA | 2 | 0 | 0 | 100% |
| CN | 0 | 2 | 0 | 0% |
| DK | 0 | 1 | 0 | 0% |
| EP | 3 | 1 | 0 | 75% |
| KR | 0 | 6 | 0 | 0% |
| US | 0 | 18 | 0 | 0% |
| WO | 1 | 0 | 0 | 100% |

## What this means for the approved workflow

On this sample the approved workflow would **exclude 28 of 34** publications (82.4%) as `EXCLUDED_NO_FULL_TEXT` before any verification call is made.

**No publication from CN, DK, KR, US had EPO full text at all.** Those offices are not under-served, they are excluded entirely: EPO's full-text service covers EP/WO-family documents, and a filing that never entered that family has nothing for EPO to serve.

This is a **product decision, not a defect**, and it is the decision this measurement exists to inform. The options are:

1. **Accept it.** The workflow verifies a smaller, EP/WO-weighted set of prior art, and every exclusion is visible and reasoned at Workflow Gate B. Nothing is fabricated; the coverage is simply narrower than discovery's.
2. **Add a second enrichment source** for the excluded offices. That is a new provider integration with its own recorded decision -- not a change this gate makes.
3. **Narrow discovery** to the jurisdictions EPO can serve, so the candidate set and the evidence layer agree. This changes what the product searches, which is a larger decision than it looks.

Doing nothing selects option 1 by default, which is why it is written down here rather than left implicit.

## What this is not

A dated measurement, not a cache. Nothing in the pipeline reads this
file and it never short-circuits a provider call.

## Per publication

| Publication | Status | Claims | Description |
|---|---|---|---|
| `US11002233` | NO_TEXT | no | no |
| `US7654321` | NO_TEXT | no | no |
| `EP3218319` | NO_TEXT | no | no |
| `WO2020115432` | TEXT | yes | yes |
| `US9789456` | NO_TEXT | no | no |
| `US10442712` | NO_TEXT | no | no |
| `EP2987654` | TEXT | yes | yes |
| `US8123999` | NO_TEXT | no | no |
| `US6401657` | NO_TEXT | no | no |
| `CN104221898` | NO_TEXT | no | no |
| `KR200429492` | NO_TEXT | no | no |
| `KR20110066580` | NO_TEXT | no | no |
| `KR20110067001` | NO_TEXT | no | no |
| `US10743517` | NO_TEXT | no | no |
| `EP3010333` | TEXT | yes | yes |
| `US6988465` | NO_TEXT | no | no |
| `US12376562` | NO_TEXT | no | no |
| `CA3113249` | TEXT | yes | yes |
| `CN113615590` | NO_TEXT | no | no |
| `US9358508` | NO_TEXT | no | no |
| `US9346687` | NO_TEXT | no | no |
| `CA2919371` | TEXT | yes | yes |
| `EP3071523` | TEXT | yes | yes |
| `US10954151` | NO_TEXT | no | no |
| `US10000304` | NO_TEXT | no | no |
| `DK2323952` | NO_TEXT | no | no |
| `US9387268` | NO_TEXT | no | no |
| `US20210308305` | NO_TEXT | no | no |
| `KR101046185` | NO_TEXT | no | no |
| `KR20090027908` | NO_TEXT | no | no |
| `US10058842` | NO_TEXT | no | no |
| `KR101199558` | NO_TEXT | no | no |
| `US10988389` | NO_TEXT | no | no |
| `US10736978` | NO_TEXT | no | no |
