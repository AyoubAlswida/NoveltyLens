# Evidence-layer policy decisions — approved 2026-08-28

**Decision owner:** the repository owner (`ayoubalswida@gmail.com`), acting as the
product decision-maker for this MVP.

**Approval date:** 2026-08-28. These decisions were **put to the owner and answered
on that date, in the request that authorized Implementation Gate B's completion**.
They were not approved earlier, and no earlier version of this repository was run
under them. Anything in the code or in `CLAUDE.md` that predates this file was
written against proposals, not approvals.

**Scope:** the MVP. Each decision is revisitable; three of the five name the
condition that should trigger revisiting them.

These are the decisions `backend/evidence/policy.py` requires before a run is
admitted. That module refuses to admit a run whose policy is unset *or whose
decision is unrecorded* — the same shape as `VERIFIER_GENERIC_JSON_DECISION`. The
env keys carrying them are in `backend/.env.example`.

---

## 1. Which date screens prior art

**Policy:** `DATE_SCREENING_POLICY=publication_lt_cutoff` — the **evaluated
publication's own publication date**, compared **exclusively** (`publication_date
< cutoff_date`).

**Rationale.** The publication date is when the document became available to the
public, which is the question a prior-art screen actually asks. The priority date
answers a different question — when the applicant claims to have had the invention
— and using it would exclude documents that were not public at the cutoff. The
priority date is still stored and displayed beside the publication date, and it
**never screens**.

Exclusive rather than inclusive because a document published *on* the cutoff was
not available *before* it. The edge case is rare and the direction of the error is
not symmetric: including a same-day publication asserts availability that was not
established, while excluding one is a visible, reasoned exclusion a reviewer can
overturn.

**Revisit when:** a jurisdiction-specific rule makes the same-day case material,
or a user needs priority-date screening for a freedom-to-operate question rather
than a novelty question.

---

## 2. An uncertain publication date

**Policy:** a publication whose date cannot be established with certainty is
**`EXCLUDED_DATE` with certainty `UNCERTAIN`**, carrying a **visible reason**
surfaced at Workflow Gate B. It is never silently passed and never silently
dropped.

**Rationale.** The two failure directions are not equal. Passing an undated
publication into verification produces a verdict about a document that may not be
prior art at all, and nothing downstream would say so. Excluding it produces a
row a human reads, with the provider's original date string stored **verbatim**
beside the exclusion, so a reviewer challenging the exclusion sees what the
provider said rather than what we made of it.

`date_original` is stored unparsed for exactly this reason.

**Revisit when:** the Gate B reviewer surface exists and the reviewer can
adjudicate an uncertain date directly rather than only seeing that it was
excluded.

---

## 3. What a passage's text version may be

**Policy:** `TEXT_VERSION_POLICY=provider_publication_date` — the **served
publication's date, taken from EPO bibliographic data**, recorded as
`PROVIDER_PUBLICATION_DATE`. `retrieved_at` is stored **separately** and is never
substituted for it.

Where a **successful, well-formed** bibliographic response genuinely carries no
publication date, that publication is **`EXCLUDED_INELIGIBLE`** — the one honest
absence.

**Rationale.** A stored verdict cites specific text. If the text version cannot be
identified, the verdict cannot later be checked against the document it was made
about. Provider metadata identifies the version only as coarsely as the
publication does, which is a real limitation, but it is a limitation with a name.

Retrieval time is never an option: `retrieved_at` says when we fetched, not which
version we fetched, and substituting it would date a stored verdict against a
document version nobody can identify.

### 3a. A failed bibliographic request is not an absent date

**Policy:** a bibliographic **request failure** — a timeout, a transport error, a
401, a 403, a 404, a 429, a 5xx, or malformed XML — **fails the run**. It must
**never** produce an exclusion, and never `NO_TEXT`.

**Rationale.** This was a real defect, not a hypothetical. `_fetch_biblio`
returned an empty date for every non-200 and every exception, which turned every
one of those conditions into `EXCLUDED_INELIGIBLE`. Two things were wrong with
that: the run reported a publication as ineligible when nobody had ever asked the
provider successfully, and the coverage figure the product reports was silently
understated using the provider's own downtime.

The distinction is the whole point of the three-state enrichment result. *The
provider said no* and *we could not ask the provider* are different facts and must
stay different facts. `backend/search/epo_fulltext.py` now distinguishes exactly
three outcomes, and `backend/tests/test_epo_enrichment.py` pins each.

**Revisit when:** never, as a policy. The retry bounds around it are tunable; the
rule that an outage is not an absence is not.

---

## 4. EPO-only enrichment, and its measured cost

**Policy:** accept **EPO as the only enrichment source, temporarily**, for the
MVP.

**The measured limitation is accepted with the decision, not discovered after
it.** `docs/measurements/epo-coverage-2026-08-28.md` records a real measurement
against 34 stored publications on 2026-08-28:

| | |
|---|---|
| Overall coverage | **17.6%** (6 of 34) |
| Would be excluded | **82.4%** (28 of 34) as `EXCLUDED_NO_FULL_TEXT` |
| Offices with **zero** coverage | **US (0/18), KR (0/6), CN (0/2), DK (0/1)** |
| Offices with coverage | EP 75% (3/4), CA 100% (2/2), WO 100% (1/1) |

Those four offices are not under-served — they are **excluded entirely**. EPO's
full-text service holds text for EP/WO-family documents, and a filing that never
entered that family has nothing for EPO to serve.

**Rationale.** Adding a second enrichment source is a new provider integration
with its own credentials, its own failure modes and its own recorded decision. It
is not a change Gate B makes. Narrowing *discovery* to the offices EPO can serve
would make the candidate set and the evidence layer agree, but it changes what the
product searches — a larger decision than it looks, and one that would quietly
make the product worse at the thing it is for.

### What this obliges the product to say

These are requirements on the report and the UI, not aspirations:

- Every exclusion is **visible and reasoned**. A publication that was discovered
  and then not evaluated appears, with its state and its reason.
- The product and the report **state plainly that excluded publications were not
  evaluated for feature disclosure**.
- The result is **never described as a complete or exhaustive novelty search**.
- **No claim that no prior art exists** is ever made on the basis that EPO lacked
  full text. "EPO had no text for this document" is not a statement about the
  document's content.

`NOT_FOUND_IN_SUPPLIED_PASSAGES` and `NO_DISCLOSURE_FOUND_IN_SUPPLIED_PASSAGES`
already carry this discipline inside the verifier. This decision extends the same
discipline to the coverage story around it.

### Post-MVP priority

**Adding a second enrichment source is recorded here as a post-MVP priority**,
targeting the US-family documents that dominate the excluded set. Until it lands,
the coverage figure above is the honest description of what this product
evaluates.

**Do not narrow discovery as part of this work.**

---

## Where these are enforced

| Decision | Enforced by | Pinned by |
|---|---|---|
| 1, 2 | `backend/evidence/dates.py`, `backend/evidence/policy.py` | `tests/test_date_screening.py` |
| 3 | `backend/evidence/eligibility.py`, `backend/search/epo_fulltext.py` | `tests/test_evidence_passages.py` |
| 3a | `backend/search/epo_fulltext.py` `_fetch_biblio` | `tests/test_epo_enrichment.py` |
| 4 | product/report copy; `backend/evidence/coverage.py` measures it | `tests/test_evidence_policy_and_coverage.py` |

A run is admitted only when both env policies **and** both recorded decisions are
present. `backend/evidence/policy.py`'s module docstring explains why the decision
text is checked at all: a policy set with no recorded decision is a rule nobody
approved.
