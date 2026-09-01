# Lens.org full-text probe - 2026-08-31

Measured by `python -m evidence.lens_coverage`. **Phase 3A measurement
only** - production provider routing is unchanged: discovery is Lens.org
and evidence is EPO OPS. No SerpAPI, embedding or LLM quota was spent.

## Sample

- Source: **pinned sample file `epo-coverage-2026-08-28.publications.txt`**
- Raw entries: 34
- Distinct publications: 34 (0 duplicate(s) collapsed)
- Live Lens requests made: **34**

## Outcome categories

| Category | Count |
|---|---:|
| `TEXT` | 21 |
| `NO_TEXT` | 13 |
| `NOT_MATCHED` | 0 |
| `FAILED` | 0 |

**Coverage: 61.8%** of the 34 publication(s) Lens
answered for. `FAILED` is excluded from the denominator, never counted
as absent text.

## Coverage by jurisdiction

| Office | `TEXT` | `NO_TEXT` | `NOT_MATCHED` | `FAILED` | Coverage |
|---|---:|---:|---:|---:|---:|
| CA | 0 | 2 | 0 | 0 | 0% |
| CN | 0 | 2 | 0 | 0 | 0% |
| DK | 0 | 1 | 0 | 0 | 0% |
| EP | 2 | 2 | 0 | 0 | 50% |
| KR | 0 | 6 | 0 | 0 | 0% |
| US | 18 | 0 | 0 | 0 | 100% |
| WO | 1 | 0 | 0 | 0 | 100% |

## Latency (sequential, concurrency 1)

- p50: 0.41s
- p95: 1.28s
- max: 2.53s

## Evidence-contract findings

| Publication | Outcome | Desc chars | Claims | Numbering | Lang | Text version | SOURCE_RANGE | Reproducible | Served identity |
|---|---|---:|---:|---|---|---|---:|---|---|
| `US11002233` | TEXT | 24913 | 19 | STABLE | en | 2021-05-11 | 14 | yes | US11002233B1 |
| `US7654321` | TEXT | 46198 | 26 | STABLE | en | 2010-02-02 | 26 | yes | US7654321B2 |
| `EP3218319` | NO_TEXT | 0 | 0 | NONE | - | 2017-09-20 | 0 | n/a | EP3218319A1 |
| `WO2020115432` | TEXT | 43319 | 1 | STABLE | fr | 2020-06-11 | 25 | yes | WO2020115432A1 |
| `US9789456` | TEXT | 95022 | 18 | STABLE | en | 2017-10-17 | 51 | yes | US9789456B2 |
| `US10442712` | TEXT | 15747 | 7 | STABLE | en | 2019-10-15 | 9 | yes | US10442712B2 |
| `EP2987654` | TEXT | 49082 | 10 | STABLE | en | 2016-02-24 | 27 | yes | EP2987654A1 |
| `US8123999` | TEXT | 21764 | 18 | STABLE | en | 2012-02-28 | 12 | yes | US8123999B2 |
| `US6401657` | TEXT | 11957 | 17 | STABLE | en | 2002-06-11 | 22 | yes | US6401657B1 |
| `CN104221898` | NO_TEXT | 0 | 0 | NONE | - | 2014-12-24 | 0 | n/a | CN104221898A |
| `KR200429492` | NO_TEXT | 0 | 0 | NONE | - | 2006-10-23 | 0 | n/a | KR200429492Y1 |
| `KR20110066580` | NO_TEXT | 0 | 0 | NONE | - | 2011-06-17 | 0 | n/a | KR20110066580A |
| `KR20110067001` | NO_TEXT | 0 | 0 | NONE | - | 2011-06-20 | 0 | n/a | KR20110067001A |
| `US10743517` | TEXT | 90650 | 35 | STABLE | en | 2020-08-18 | 48 | yes | US10743517B2 |
| `EP3010333` | NO_TEXT | 0 | 0 | NONE | - | 2016-04-27 | 0 | n/a | EP3010333A2 |
| `US6988465` | TEXT | 13767 | 18 | STABLE | en | 2006-01-24 | 8 | yes | US6988465B2 |
| `US12376562` | TEXT | 176096 | 15 | STABLE | en | 2025-08-05 | 94 | yes | US12376562B2 |
| `CA3113249` | NO_TEXT | 0 | 0 | NONE | - | 2020-03-26 | 0 | n/a | CA3113249A1 |
| `CN113615590` | NO_TEXT | 0 | 0 | NONE | - | 2021-11-09 | 0 | n/a | CN113615590A |
| `US9358508` | TEXT | 39291 | 14 | STABLE | en | 2016-06-07 | 21 | yes | US9358508B2 |
| `US9346687` | TEXT | 171087 | 24 | STABLE | en | 2016-05-24 | 91 | yes | US9346687B1 |
| `CA2919371` | NO_TEXT | 0 | 0 | NONE | - | 2016-07-27 | 0 | n/a | CA2919371A1 |
| `EP3071523` | TEXT | 75486 | 10 | STABLE | en | 2019-09-11 | 40 | yes | EP3071523B1 |
| `US10954151` | TEXT | 44771 | 33 | STABLE | en | 2021-03-23 | 24 | yes | US10954151B1 |
| `US10000304` | TEXT | 60598 | 14 | STABLE | en | 2018-06-19 | 33 | yes | US10000304B2 |
| `DK2323952` | NO_TEXT | 0 | 0 | NONE | - | 2016-02-08 | 0 | n/a | DK2323952T3 |
| `US9387268` | TEXT | 232362 | 98 | STABLE | en | 2016-07-12 | 122 | yes | US9387268B2 |
| `US20210308305` | TEXT | 58326 | 15 | STABLE | en | 2021-10-07 | 31 | yes | US20210308305A1 |
| `KR101046185` | NO_TEXT | 0 | 0 | NONE | - | 2011-07-04 | 0 | n/a | KR101046185B1 |
| `KR20090027908` | NO_TEXT | 0 | 0 | NONE | - | 2009-03-18 | 0 | n/a | KR20090027908A |
| `US10058842` | TEXT | 103893 | 19 | STABLE | en | 2018-08-28 | 56 | yes | US10058842B1 |
| `KR101199558` | NO_TEXT | 0 | 0 | NONE | - | 2012-11-12 | 0 | n/a | KR101199558B1 |
| `US10988389` | TEXT | 46602 | 20 | STABLE | en | 2021-04-27 | 25 | yes | US10988389B1 |
| `US10736978` | TEXT | 50014 | 44 | STABLE | en | 2020-08-11 | 27 | yes | US10736978B2 |

## Provider outcomes

| Publication | HTTP | Failure category | Elapsed (s) | Detail (redacted) |
|---|---:|---|---:|---|
| `US11002233` | 200 | - | 2.47 | - |
| `US7654321` | 200 | - | 0.66 | - |
| `EP3218319` | 200 | - | 0.39 | - |
| `WO2020115432` | 200 | - | 0.34 | - |
| `US9789456` | 200 | - | 0.41 | - |
| `US10442712` | 200 | - | 0.33 | - |
| `EP2987654` | 200 | - | 0.42 | - |
| `US8123999` | 200 | - | 0.33 | - |
| `US6401657` | 200 | - | 0.36 | - |
| `CN104221898` | 200 | - | 0.3 | - |
| `KR200429492` | 200 | - | 0.31 | - |
| `KR20110066580` | 200 | - | 0.33 | - |
| `KR20110067001` | 200 | - | 0.52 | - |
| `US10743517` | 200 | - | 0.33 | - |
| `EP3010333` | 200 | - | 0.31 | - |
| `US6988465` | 200 | - | 2.53 | - |
| `US12376562` | 200 | - | 1.28 | - |
| `CA3113249` | 200 | - | 0.41 | - |
| `CN113615590` | 200 | - | 0.36 | - |
| `US9358508` | 200 | - | 0.33 | - |
| `US9346687` | 200 | - | 0.66 | - |
| `CA2919371` | 200 | - | 0.34 | - |
| `EP3071523` | 200 | - | 0.47 | - |
| `US10954151` | 200 | - | 0.44 | - |
| `US10000304` | 200 | - | 0.48 | - |
| `DK2323952` | 200 | - | 0.42 | - |
| `US9387268` | 200 | - | 1.06 | - |
| `US20210308305` | 200 | - | 0.41 | - |
| `KR101046185` | 200 | - | 0.34 | - |
| `KR20090027908` | 200 | - | 0.36 | - |
| `US10058842` | 200 | - | 0.48 | - |
| `KR101199558` | 200 | - | 0.47 | - |
| `US10988389` | 200 | - | 0.42 | - |
| `US10736978` | 200 | - | 0.33 | - |

## `EnrichmentResult` / passage fields not producible

| Field | Publications affected |
|---|---:|
| `claims_char_offsets` | 34 |
| `language` | 13 |
| `paragraph_ids` | 34 |
| `section_text` | 13 |
| `source` | 34 |
| `source_document_id` | 34 |
| `text_sha256` | 13 |

`segmenter_version` for every `SOURCE_RANGE` locator above: `desc-seg-1`.

## What this is not

A dated measurement, not a cache. Nothing in the pipeline reads this file
and it never short-circuits a provider call. No credential and no provider
response body appears anywhere in it.
