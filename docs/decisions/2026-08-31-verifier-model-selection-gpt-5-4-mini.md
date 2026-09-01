# Verifier model selection: `gpt-5.4-mini-2026-03-17` at `reasoning_effort=medium`

- **Status:** APPROVED
- **Approved by:** the repository owner
- **Date:** 2026-08-31
- **Scope:** the `local` deployment profile only
- **Authority:** spec §14.1, §14.2.2 point 5 — selecting another model is an *explicit,
  recorded configuration decision*, never an automatic runtime fallback
- **Supersedes (as a selection, not as a record):**
  `docs/decisions/2026-08-29-verifier-model-selection-gpt-5-1.md`, which in turn
  superseded `docs/decisions/2026-08-29-verifier-model-selection.md`. **Every
  measurement in both documents stands unchanged and every artifact stays on disk.**
- **Related:** `docs/decisions/2026-08-29-canonical-and-wire-schemas.md`,
  `docs/measurements/structured-output-capability-STATUS.md`

## 1. The selection

```
DEPLOYMENT_PROFILE=local
OPENAI_MODEL=gpt-5.4-mini-2026-03-17
OPENAI_REASONING_EFFORT=medium
```

- **Official OpenAI API**, through the official SDK. The `local` profile selects the
  provider; there is deliberately no separate provider knob to drift against it.
- **Pinned to a snapshot**, never the floating `gpt-5.4-mini` alias. An alias can be
  moved to a new revision by the provider with no configuration change, leaving an
  artifact that matches on every identity field while describing a model that no longer
  exists. `model_pinned` records the answer either way.
- **Scoped to `local`.** It says nothing about `aramco`, which remains deferred, unprobed
  and unqualified. `deployment_profile` is one of the ten identity fields compared at
  admission, so this cannot leak across even by accident.
- **A direct configuration replacement.** There is no automatic model fallback and no
  runtime model-selection chain. Four selections in a row is four owner decisions, not a
  search loop, and nothing in this codebase may pick the next one.
- **Not qualified by this document.** Selection is a configuration decision; eligibility
  is decided only by running the gate and recording the result.

## 2. What is measured, and what this model's state actually is

`gpt-5.4-mini-2026-03-17` is **SELECTED AND MEASURED INELIGIBLE**. The 2026-08-31
targeted diagnostic passed 4 of 11 runs: E 0/5, I 1/3, and G 3/3. The full 39-call
qualification must not be run for this runtime, and **this state qualifies nothing**.

| Model | Configuration | Measured | Verdict |
|---|---|---|---|
| `gpt-4o-2024-08-06` | classic dialect | 2026-08-29, runner v3 | **INELIGIBLE** — probe 7, §9 check 15 on §15.1 case C, malformed rate 0.25 |
| `gpt-5-mini-2025-08-07` | effort unset | 2026-08-29, runner v4 | **INELIGIBLE** — probe 7, §15.1 case E unstable |
| `gpt-5-mini-2025-08-07` | `reasoning_effort=high` | 2026-08-29, diagnostic | **INELIGIBLE** — case E, 0 of 5 |
| `gpt-5.1-2025-11-13` | `reasoning_effort=high` | 2026-08-29, targeted diagnostic | **INELIGIBLE** — case E 0/5, case I 0/3; case G 3/3 |
| `gpt-5.4-mini-2026-03-17` | `reasoning_effort=medium` | 2026-08-31, targeted diagnostic | **INELIGIBLE** — case E 0/5, case I 1/3; case G 3/3 |

**No artifact was created, modified, copied or relabelled by this decision**, and none may
be. `backend/tests/test_historical_artifacts_do_not_qualify.py` asserts that against the
real `docs/measurements/` directory, including that supplying a record's missing identity
key and relabelling its model still does not qualify it.

**§15.1 case E is unchanged.** The 2026-08-29 review confirmed it and this decision does
not reopen it. E3 stays `UNCLEAR`, no §15 expected result moved, check 14 was not weakened,
and the zero-failure qualification policy stands. A model that answers E differently is
wrong; the expectation is not adjusted to fit it.

## 3. The `gpt-5.4-mini` dialect is declared, not inherited

| Property | `gpt-5.4-mini` | `gpt-5.1` | `gpt-5` family |
|---|---|---|---|
| API surface | Chat Completions | same | same |
| structured outputs | strict Structured Outputs | same | same |
| token ceiling parameter | `max_completion_tokens` | same | same |
| `temperature` | **omitted** | omitted | omitted |
| `reasoning_effort` vocabulary | **`none, low, medium, high, xhigh`** | `none, low, medium, high` | `minimal, low, medium, high` |
| reasoning-token allowance | 16000 | 16000 | 8000 |
| timeout floor | 300s | 300s | 180s |
| `top_p`, `seed`, `verbosity` | not sent | not sent | not sent |

`xhigh` is the real difference and it is new to the union: this family keeps `none` (so
`minimal` remains absent) and adds an effort **above** `high`.

The allowance and the timeout floor are **chosen, not measured** — no run has been made
against this model — and they are deliberately generous rather than sized down because it
is a "mini". The allowance only ever *adds* to the caller's own budget, so a wider one
cannot shorten a response: the cost of being generous is a ceiling that is never reached,
and the cost of being tight is a truncation classified `TOKEN_CEILING`, which is never
retried and abandons the whole capability record. The same asymmetry governs the timeout:
a read timeout on a provider that *was* answering is recorded `UNREACHABLE`, an
operational failure that measures nothing.

**Nothing is probed and nothing adapts at runtime.** These are declared properties. A model
whose dialect is wrong fails deterministically with the provider naming the parameter,
which is the signal to add a row — not to retry differently.

## 4. The version-boundary rule, which is the substantive fix

`"gpt-5.4-mini-2026-03-17".startswith("gpt-5")` is true. Under the previous bare-prefix
match this model would have taken the **`gpt-5` family's** dialect:
`minimal|low|medium|high`, an 8000-token allowance and a 180s floor.

**It would not have failed.** All three families agree on `max_completion_tokens` and on
omitting `temperature`, and `medium` is accepted by all three — so the owner-selected
runtime would have run under a dialect nobody declared for it, at the wrong allowance and
the wrong floor, with no error anywhere, and the capability artifact would have recorded
that as the identity it qualified.

`gpt-5.1` met this same trap on 2026-08-29 and was fixed by adding a row. A row alone
leaves the *next* version to rediscover it, so the rule itself is now closed:
`deployment._matches_family` requires a prefix to match at a **version boundary** — the
name must equal the prefix, or continue with a character that cannot extend the version
number. A `.` or a digit continues it; a `-` does not.

The consequence is deliberate and is the safer direction: a `gpt-5.x` version this table
has never heard of matches **nothing** and falls to `CLASSIC_DIALECT`, whose `max_tokens`
and explicit `temperature` a reasoning model rejects with a deterministic HTTP 400. The
gate classifies that `BAD_REQUEST`, writes no artifact and exits 2. Failing loudly on an
undeclared model is the point.

## 5. What this invalidates

- `llm.OPENAI_ADAPTER_BEHAVIOUR_VERSION` to **6**. The dialect table and the matching rule
  both changed, so the request changed, so `adapter_revision` changed, so **every prior
  artifact is invalidated**. They were already invalid for other reasons; this is one more.
- `deployment.REASONING_EFFORTS` gains `xhigh`. It is *derived* from `MODEL_DIALECTS`, so
  it cannot claim a value no dialect accepts.
- `dialect_version` for this model carries the new vocabulary, so it folds into
  `inference_config_fingerprint` — a family gaining or losing an accepted value is itself
  a runtime change rather than a silent one.
- `verification.CAPABILITY_RUNNER_VERSION` is **unchanged**. No scoring rule, expectation,
  check or threshold moved. Only the adapter did.

## 6. What this decision does not do

- It does not qualify `gpt-5.4-mini-2026-03-17`; only the gate can.
- It does not create, modify, copy or relabel any eligibility artifact.
- It does not set `VERIFIER_CAPABILITY_ENFORCEMENT=deferred`, and does not bypass
  admission. Admission still fails closed, and was verified doing so under this exact
  configuration.
- It does not change §15.1 case E, any §15 expected result, the byte-pinned system prompt,
  the canonical schema, the wire-schema semantics, or checks 1-18.
- It does not approve a threshold, a bounded deviation, or a majority-success rule.
- It does not permit `json_object`, a prompt-only fallback, or any provider fallback, and
  it does not implement automatic model substitution.
- It does not touch discovery (Lens.org), EPO's enrichment-only role, the embedding path,
  Qdrant's disabled status, or the search-only pipeline.

## 7. The next step, which is a separate decision to spend

The cost-bounded, structurally non-qualifying diagnostic runs first:

```
cd backend && python -m verification.diagnose E:5 I:3 G:3
```

**11 paid calls, enforced as a hard ceiling.** Each requested diagnostic repetition uses
one provider attempt: no transport retry, malformed-output re-ask, or outer retry.
Production verification retains its bounded retry policies. The 2026-08-31 diagnostic
failed, so the full 39-call §14.2.1 gate (`python -m verification.capability_test`) must
not be run.
