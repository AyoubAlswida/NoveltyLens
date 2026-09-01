# Verifier model selection: `gpt-5.1-2025-11-13` at `reasoning_effort=high`

- **Status:** APPROVED
- **Approved by:** the repository owner
- **Date:** 2026-08-29
- **Scope:** the `local` deployment profile only
- **Authority:** spec §14.1, §14.2.2 point 5 — selecting another model is an *explicit,
  recorded configuration decision*, never an automatic runtime fallback
- **Supersedes (as a selection, not as a record):**
  `docs/decisions/2026-08-29-verifier-model-selection.md`. That document's measurements
  stand unchanged and its artifacts stay on disk.
- **Related:** `docs/decisions/2026-08-29-canonical-and-wire-schemas.md`,
  `docs/measurements/structured-output-capability-STATUS.md`

## 1. §15.1 case E is correctly specified and is not changed

The owner reviewed the case that has now blocked three of the four measured runtimes
and confirmed it.

The byte-pinned §3 system prompt states that a passage depending on an **unsupplied
referenced figure** is an `UNCLEAR` case. §15.1's own Expected column for E says
`E3 UNCLEAR` with an `ambiguities` entry and a `REFERENCED_FIGURE` context request, and
`F>0, N=0, U=1` → §6 rule 4 → `UNCLEAR`. The fixture supplies:

> "The sensor arrangement of Fig. 3 provides the input signal."

and does **not** supply Fig. 3.

Therefore, and this is the decision:

- **E3 is not changed** to `NOT_FOUND_IN_SUPPLIED_PASSAGES`.
- **No §15 expected result is changed.**
- **Check 14 is not weakened**, nor is any other of checks 1–15.
- The byte-pinned system prompt, the canonical schema and the wire-schema semantics are
  untouched.
- The zero-failure qualification policy stands. No threshold and no bounded deviation is
  approved, in this document or by implication.

A model that answers E differently is wrong. The expectation is not adjusted to fit it.

## 2. `gpt-5-mini-2025-08-07` at `reasoning_effort=high` is measured and INELIGIBLE

Recorded for this verifier identity. Evidence:
`docs/measurements/diagnostic-case-E-2026-08-29-101631.json` — case E, five runs, **0 of 5**.

| Run | E1 | E2 | E3 | R1 | Result | Reasoning tokens |
|---|---|---|---|---|---|---|
| 1 | FOUND | FOUND | NOT_FOUND | NOT_FOUND | `PARTIALLY` — wrong outcome | 4352 |
| 2 | FOUND | FOUND | UNCLEAR | UNCLEAR | rejected at §9 check 14 | 6912 |
| 3 | FOUND | FOUND | NOT_FOUND | UNCLEAR | `PARTIALLY` — wrong outcome | 7872 |
| 4 | FOUND | FOUND | NOT_FOUND | NOT_FOUND | `PARTIALLY` — wrong outcome | 4096 |
| 5 | FOUND | FOUND | NOT_FOUND | UNCLEAR | `PARTIALLY` — wrong outcome | 5696 |

Zero operational failures; zero consistency failures. The full 39-call qualification was
**not** run, per the rule that a failing case-E diagnostic stops before it.

### The running record for `local`

| Model | Configuration | Measured | Verdict |
|---|---|---|---|
| `gpt-4o-2024-08-06` | classic dialect | 2026-08-29, runner v3 | **INELIGIBLE** — probe 7, §9 check 15 on §15.1 case C, malformed rate 0.25 |
| `gpt-5-mini-2025-08-07` | effort unset | 2026-08-29, runner v4 | **INELIGIBLE** — probe 7, §15.1 case E unstable (`UNCLEAR`, `PARTIALLY`, `PARTIALLY`) |
| `gpt-5-mini-2025-08-07` | `reasoning_effort=high` | 2026-08-29, diagnostic | **INELIGIBLE** — case E, 0 of 5 |
| `gpt-5.1-2025-11-13` | `reasoning_effort=high` | 2026-08-29, targeted diagnostic | **INELIGIBLE** — case E 0/5, case I 0/3, case G 3/3 (see the STATUS document) |

Every historical artifact and diagnostic record stays on disk, byte-unchanged, and
**qualifies nothing**. `backend/tests/test_historical_artifacts_do_not_qualify.py` asserts
that against the real directory rather than in the abstract, including that completing a
record's missing identity key and relabelling its model still does not qualify.

## 3. The selection

```
OPENAI_MODEL=gpt-5.1-2025-11-13
OPENAI_REASONING_EFFORT=high
```

- **Explicit, and scoped to `local`.** It says nothing about the `aramco` profile, which
  remains deferred, unprobed and unqualified. `deployment_profile` is one of the ten
  identity fields compared at admission, so this cannot leak across even by accident.
- **Pinned to a snapshot**, not a floating alias — an alias can be moved by the provider
  with no configuration change, leaving an artifact matching on every field while
  describing a model that no longer exists.
- **A direct configuration replacement.** There is no automatic model fallback and no
  runtime model-selection chain. Three selections in a row is three owner decisions, not a
  search loop, and nothing in this codebase may pick the next one.
- **Not qualified by this document.** Selection is a configuration decision; eligibility is
  decided only by running the gate and recording the result.

## 4. The `gpt-5.1` dialect is declared, not inherited

`"gpt-5.1-2025-11-13".startswith("gpt-5")` is true, so without an explicit row this model
would have silently taken the older family's dialect. The request would still have been
*accepted* — the two families agree on `max_completion_tokens` and on omitting
`temperature` — so nothing would have failed loudly. What would have differed is exactly
the part that decides whether a qualification run is worth anything.

`deployment.MODEL_DIALECTS` now carries `gpt-5.1` ahead of `gpt-5` (longest prefix wins):

| Property | `gpt-5.1` | `gpt-5` family | Why it is declared separately |
|---|---|---|---|
| token ceiling parameter | `max_completion_tokens` | same | `max_tokens` is rejected outright, not aliased |
| `temperature` | **omitted** | same | the reasoning families reject an explicit value; omitting leaves the provider's default in force and the identity records `temperature_sent: false` |
| `reasoning_effort` vocabulary | `none, low, medium, high` | `minimal, low, medium, high` | **the real difference.** `gpt-5.1` removed `minimal` and added `none`; a value valid on one is a deterministic HTTP 400 on the other |
| reasoning-token allowance | **16000** | 8000 | reasoning tokens are billed against the ceiling and never appear in `content`. `gpt-5-mini` was measured at 4096–7872 on one case at `high`; a larger model at the same effort has no reason to spend fewer. A ceiling that is never reached costs nothing; a tight one turns every call into `TOKEN_CEILING`, which is never retried and abandons the record |
| timeout floor | **300s** | 180s | a read timeout on a provider that was answering is recorded `UNREACHABLE` — an operational failure that measures nothing |
| `top_p`, `seed`, `verbosity` | **not sent** | not sent | `verbosity` is a `gpt-5.x` output-length control. The §10 schema and the byte-pinned prompt already govern output length; adding a second lever would be this code making a choice the specification already made |

**Nothing is probed and nothing adapts at runtime.** These are declared properties. A
model whose dialect is wrong fails deterministically with the provider naming the
parameter, which is the signal to add a row — not to retry differently.

**The effort is checked twice, for two different questions.** `read_reasoning_effort`
asks "is this a real setting?" against the union of every declared dialect (itself
*derived* from the table, so it cannot claim a value no dialect accepts). `build_chat_payload`
then asks "does *this model* take it?" against the configured model's own vocabulary and
**raises before the request is built**. A provider 400 would arrive after the call is made
and billed, and the gate classifies it `BAD_REQUEST` — an operational outcome that measures
nothing and abandons the whole record. An owner-selected setting is never dropped and never
downgraded to a neighbouring value.

**Production and the capability runner build the same payload**, through the one
`llm.build_chat_payload`. A gate that probed a differently-shaped request would certify
something the application never sends.

## 5. What this invalidates

- `llm.OPENAI_ADAPTER_BEHAVIOUR_VERSION` → **5**. The request changed (a new dialect row,
  and a new pre-flight refusal), so `adapter_revision` changed, so **every prior artifact
  is invalidated**.
- `deployment.InferenceConfig` gained `verbosity`, and its `dialect_version` now carries the
  per-family effort vocabulary — so a family gaining or losing an accepted value is itself a
  runtime change rather than a silent one. The `inference_config_fingerprint` moves on each
  field independently; `backend/tests/test_gpt51_dialect.py` asserts that field by field.
- `verification.CAPABILITY_RUNNER_VERSION` → **5**, for the scoring change in §6 below.

## 6. Probe 7 now gates on correctness, not only stability

**The hole this closes was real and is worth stating plainly.** Under runner v4, a case
passed if its three runs *agreed with each other*. `expected_outcome` was recorded and
never gated on. A model returning the **same wrong answer three times** was perfectly
stable and therefore passed; `gpt-5-mini` failed case E only because its three wrong
answers happened to disagree.

Runner v5 compares every run against §15.1's own Expected column:

1. **The outcome** the section prints for the case.
2. **The per-requirement statuses the section *names*.** Transcribed, never reasoned to —
   only what §15.1 actually prints is encoded. Case E carries `E3 → UNCLEAR` and stops
   there, because `outcome == UNCLEAR` under §6 rule 4 already forces `N=0` and `F>0` given
   that one status. Inventing an expectation is the same defect as changing one.
3. **The combination link**, for cases A and I, where §15.1 requires the linking text
   quoted verbatim inside `combination_basis`. Containment, not equality: a longer verbatim
   span containing the link satisfies §15.1 exactly as well as the shortest one does.

This is a **strengthening**. No check was relaxed, no threshold introduced, and no
expectation moved. `backend/tests/test_stability_coverage.py` pins it with a stably-wrong
response that v4 would have passed.

## 7. What this decision does not do

- It does not change §15.1 case E, any §15 expected result, the byte-pinned system prompt,
  the canonical schema, the wire-schema semantics, or checks 1–15.
- It does not approve a threshold, a bounded deviation, or a majority-success rule.
- It does not change the provider, or permit `json_object`, a prompt-only fallback, or any
  provider fallback.
- It does not implement automatic model fallback or a runtime model-selection chain.
- It does not qualify `gpt-5.1-2025-11-13`; only the gate can — and the targeted
  diagnostic run on 2026-08-29 measured it **ineligible** before the gate was spent.
- It does not touch discovery sources, EPO's enrichment-only role, the Hugging Face
  embedding path, Qdrant's disabled status, or the search-only pipeline.
