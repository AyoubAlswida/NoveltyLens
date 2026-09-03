# Feature verification withdrawn; Advanced Search replaces it; numeric scores removed

**Date:** 2026-09-01
**Approved by:** the repository owner, in the request that authorized
`docs/prompts/advanced-search-three-stage-implementation.md`
**Status:** implemented

This is a **scope decision**, not an implementer's call. The repository's convention is that
a change of this size is recorded before it is relied on, because most of what it removes was
built deliberately and is documented at length as the project's most carefully guarded work.

---

## 1. Feature-disclosure verification is withdrawn from the product

The five-gate reviewed workflow — the approved candidate set (Gate B), the
feature × publication matrix and its cells (Gate C), the drafted report and its approval
(Gate D), the evidence layer that produced citable passages, and the two human work requests
that fed them — is **removed from the product**.

**What replaces it.** **Advanced Search**: a standard prior-art search preceded by guided
editing. The system proposes a technical summary and a flat list of technical elements; a
person edits and approves them; the system proposes search queries; the person edits and
approves those; the approved queries are executed by the ordinary search pipeline and the
ordinary report is displayed. Two backend approvals, presented as three user-facing stages.

**Why.** The verifier was never qualified. Four runtimes were measured against the real
OpenAI API and none met §14.2.1's eligibility bar — `gpt-4o-2024-08-06`,
`gpt-5-mini-2025-08-07` at two effort settings, `gpt-5.1-2025-11-13` at `high`, and
`gpt-5.4-mini-2026-03-17` at `medium` (see
`docs/measurements/structured-output-capability-STATUS.md`). The blocker was a measured model
behaviour, not a credential, a network or a structured-output gap. A reviewed workflow whose
central act cannot be performed is not a feature that is nearly finished; it is a feature
that is blocked on something outside the codebase.

**Nothing is deleted to make this tidy.** Every Gate B/C/D table, migration and row stays.
`backend/verification/`, `backend/evidence/`, `graph/evidence_nodes.py`,
`graph/verification_nodes.py`, `graph/report.py`, `graph/work_requests.py`,
`graph/durable.py`, `workflow/evidence_store.py` and `search/epo_fulltext.py` are retained
with their focused unit tests, and the standalone capability-qualification runner and its
artifacts are retained as legacy tooling. What was removed is the **product surface**: the
routes, the screens, the graph nodes and the acceptance claims. No compiled graph, route or
acceptance journey reaches any of it.

---

## 2. Numeric similarity scores and the overall verdict are removed

`pct`, `top_score` and `verdict` are gone from the API payload, the database, the UI and
every export. Migration `0024` drops the three columns.

Cosine similarity is **still computed** — it is how ranking works — and is used for exactly
two things before being discarded:

1. **ordering**, strongest first;
2. **rating**, on the existing thresholds: `>= 75` High, `<= 25` Low, Medium between.

**Why.** A percentage claims a precision the embedding cosine does not have, and a
single-word verdict over a whole run reads as a legal conclusion the pipeline never makes.
The per-match High/Medium/Low **novelty risk** rating is the only judgement that ships.

**A rating is a claim, so an empty result set carries none.** Rendering "Novelty risk: Low"
over a run that found nothing would state a finding the run did not make — the substitution
this project's zero-tolerance policy exists to forbid. The previous behaviour did exactly
that: an empty run stored `top_score: 0`, which mapped to `verdict: "Strong"`, which the
report screen rendered as "Novelty risk: Low".

**`search_run_matches.rank` is now the only record of ordering**, since the score it was
derived from is gone. Nothing may re-sort those rows on read.

---

## 3. Runs parked at a withdrawn gate are not resumable

A run left at Gate B, C or D before this change **cannot be continued**. Its data stays; the
product provides no UI and no API to carry it forward.

This must fail **honestly**, and that is enforced rather than intended. `WorkflowGate` on the
frontend is narrowed to the two gates the app renders, while the wire field is typed
`StoredGate`, which is wider — so the compiler forces the unknown-gate case to be handled,
and the review screen says "This run cannot be continued" rather than rendering a spinner, an
empty gate screen or a 500. On the backend, `WorkflowGate` deliberately **keeps** `B`, `C`
and `D` so a legacy `current_gate` still parses; removing the members would turn a legacy row
into an enum error at exactly the moment the product is supposed to explain itself.

---

## 4. What this decision costs, stated plainly

Two guarantees are genuinely lost, and neither is replaced:

- **Element-to-query coverage.** The search strategy stage used to refuse an approval that
  left a *core* element uncovered by any enabled query, so "no prior art found" could not
  rest on a search nobody performed for part of the invention. Advanced Search makes no such
  guarantee. It shows the queries to a person and asks them to approve them.
- **Passage-level disclosure verification.** No publication is checked against the
  invention's requirements any more. The report says what the search found and rates the
  overlap; it makes no statement about whether any feature is disclosed.

The 164-row §6 traceability claim was **retired**, not narrowed or regenerated:
`tests/test_matrix_traceability.py`, `acceptance/matrix_traceability.py` and
`docs/acceptance/matrix-traceability.md` are deleted together. A traceability table for a
withdrawn feature is a coverage claim that quietly stops meaning anything.

---

## 5. Consequences for configuration

`POST /api/runs` no longer calls `evidence/policy.py::validate_admission_policies` or
`require_capability`, and returns no `capability_qualified` field. Advanced Search screens no
dates, selects no text version and runs no verifier, so it **cannot** refuse admission
because `VERIFIER_CAPABILITY_ENFORCEMENT`, `VERIFIER_CAPABILITY_DEFERRAL_DECISION`,
`DATE_SCREENING_POLICY` or `TEXT_VERSION_POLICY` is unset. Those four are removed from the
Advanced Search deployment path, from the mock, and from the acceptance scripts; they remain
documented in `.env.example` under an explicit legacy banner, because the retained tooling
reads them and because runs admitted before this change recorded their policies per run.

New Advanced Search rows write the explicit constant `advanced_search_not_applicable` to the
legacy `date_policy` and `text_version_policy` columns, which are `NOT NULL`. An empty string
would read as "nobody filled this in"; this says the question does not apply. Existing rows
keep the values they were admitted under.

---

## 6. Revisit conditions

Reopen this decision if any of the following becomes true:

- a runtime **passes** the §14.2.1 capability gate, making passage-level verification
  performable rather than blocked;
- a second full-text enrichment source lands, since measured EPO coverage of 17.6% was
  already the larger constraint on the evidence layer's usefulness
  (`docs/measurements/epo-coverage-2026-08-28.md`);
- users ask for coverage checking specifically — it is the guarantee this change gives up
  that has no replacement, and it could be reintroduced to the strategy stage without the
  matrix.
