# Verifier model selection: `gpt-4o-2024-08-06` is ineligible; `gpt-5-mini-2025-08-07` is selected

- **Status:** APPROVED
- **Approved by:** the repository owner
- **Date:** 2026-08-29
- **Scope:** the `local` deployment profile only
- **Authority:** spec §14.1, §14.2.2 point 5 — selecting another model is an *explicit,
  recorded configuration decision*, never an automatic runtime fallback
- **Related:** `docs/decisions/2026-08-29-canonical-and-wire-schemas.md`,
  `docs/measurements/structured-output-capability-STATUS.md`

## 1. `gpt-4o-2024-08-06` is measured and ineligible

Measured on 2026-08-29 by `python -m verification.capability_test`, runner v3, against the real
OpenAI API. Artifact: `docs/measurements/structured-output-capability-local-473db61ad9fb-2026-08-29.json`.

**Structured output was never the problem.** Probes 1–6 all passed: the adapter exposes strict
Structured Outputs, the generated wire schema was accepted, required fields, closed enums and
`additionalProperties: false` were all honoured, and the full verifier output was accepted by
the provider *and* passed canonical validation.

**Probe 7 failed.** Three of twelve stability calls were rejected by §9 **check 15**, all on
§15.1 case C, identically across all three repeats:

```
statuses:        E1 = FOUND
                 E2 = NOT_FOUND_IN_SUPPLIED_PASSAGES
                 R1 = NOT_FOUND_IN_SUPPLIED_PASSAGES
declared:        NO_DISCLOSURE_FOUND_IN_SUPPLIED_PASSAGES
§6 rule 5 gives: PARTIALLY_DISCLOSED_IN_SUPPLIED_PASSAGES   (F > 0 and N > 0)
```

Measured malformed-response rate **0.25**.

### The owner decisions on that result

1. **The 0.25 rate is not accepted.** No threshold is approved, in this task or by implication.
2. **Check 15 is not weakened.** It is the anti-hedging core: the model's declared outcome is a
   consistency check on its own reasoning, and a model that contradicts itself has produced an
   unusable verdict, not a slightly-wrong one.
3. **The prompt is not taught the derivation.** §6 is explicit that the model's `outcome` is
   "a consistency check on the model's own reasoning, never the source of truth". Disclosing the
   derived outcome to the model would make the check tautological — it would then agree by
   construction and measure nothing.
4. **The canonical/wire schema architecture is preserved**, and the §3 system prompt stays
   byte-pinned to the specification.
5. **`gpt-4o-2024-08-06` is marked measured and ineligible** for the current local verifier
   identity.

### What happens to its artifacts

Both stay on disk, byte-unchanged, and **neither can qualify anything**:

| Artifact | Records | Why it qualifies nothing |
|---|---|---|
| `…-local-d7d2541857da-2026-08-28.json` | the deterministic schema rejection, runner v2 | has no `wire_schema_version`/`validator_version` key, so no identity can be built from it |
| `…-local-473db61ad9fb-2026-08-29.json` | probes 1–6 pass, probe 7 fails, runner v3 | `model`, `adapter_revision` and `runner_version` all differ from the active runtime |

Both remain **historically accurate about what they measured**. Neither is edited or
reinterpreted, and a matching-but-ineligible artifact is the strongest possible refusal.

## 2. `gpt-5-mini-2025-08-07` is the selected local verifier model

- **Explicitly selected**, pinned to a snapshot, not inferred and not defaulted-into.
- **Scoped to `local`.** It says nothing about the `aramco` profile's model, which remains
  deferred, unprobed and unqualified. `deployment_profile` is an identity field, so this cannot
  leak across even by accident.
- **No automatic fallback.** A configured model that fails the gate fails. Choosing another is
  another decision like this one.
- **Not qualified by this document.** Selection is a configuration decision; eligibility is
  decided only by running the gate and recording the result.

Configuration: `OPENAI_MODEL=gpt-5-mini-2025-08-07`, and it is now the pinned default in
`deployment.OPENAI_DEFAULT_MODEL`.

## 3. Adapter compatibility this required

`gpt-5-mini` is a reasoning model and does not accept the classic Chat Completions parameters.
`deployment.ChatDialect` / `MODEL_DIALECTS` declares the difference; `llm.build_chat_payload`
applies it, and the capability gate builds its requests through the same function so it cannot
certify a request shape production never sends.

| Adaptation | Why | What is *not* done |
|---|---|---|
| `max_completion_tokens` replaces `max_tokens` | the reasoning families reject the old name outright; it is not an alias | no runtime retry-and-adapt: an unlisted model gets the classic shape and fails loudly |
| `temperature` **omitted** | those models accept only their own default and reject an explicit `0.0` with HTTP 400 | not rewritten to `1` — that would be this code choosing a sampling setting and recording nothing |
| `+8000` token allowance added to the ceiling | reasoning tokens never appear in `content` and still consume the completion budget, so a budget sized for the answer alone truncates every call | it only ever *adds* to what the caller asked for |
| `reasoning_effort` not set | it trades answer quality against cost, which is a decision with an owner | defaulting it in code would be that decision made silently |

**Stated consequence, not hidden:** on a model whose dialect omits `temperature`, the verifier's
`TEMPERATURE = 0.0` is **not in force**. Spec §14 already records that temperature 0 is
best-effort rather than a determinism guarantee; probe 7's repeats are what measure the
consequence, and the capability artifact records `temperature_sent: false` so a reader can see
it. Nothing about the output contract changed: strict `json_schema`, the same canonical schema,
the same generated wire schema, the same eighteen §9 checks.

`OPENAI_ADAPTER_BEHAVIOUR_VERSION` moves to **3**, so `adapter_revision` changes and every prior
artifact is invalidated — the shape of the request is half of what §14.2.1 measures.

## 4. The stability measurement is widened

Probe 7 previously ran four cases (A, C, F, G). It now runs **every executable §15.1 verifier
case, A through K — eleven cases at three independent runs each, 33 calls.**

The four-case subset happened to contain case C, which is the only reason gpt-4o's inconsistency
was seen at all, and happened to omit seven cases about which nothing was learned while the
record said "stability across the §15 regression suite". Nobody chose that; it was simply what
had been written down. §15.2's cases are *application* behaviour — most explicitly "no LLM
call" — and are covered offline, not by a model qualification.

**Coverage is checked, not assumed.** The required identifiers are derived from the specification
markdown in `tests/test_stability_coverage.py`, and a missing or unknown case fails the probe
closed. There is now **one** copy of the §15.1 data
(`verification/regression_cases.py`); `tests/fixtures_verification.py` re-exports it.

Eligibility requires **all** of: every probe passed, complete case coverage, every scheduled call
returning a valid response, zero malformed responses, zero canonical-validation failures, zero
outcome-consistency failures, zero operational failures contributing to the measurement, and
every case stable across its repetitions. `CAPABILITY_RUNNER_VERSION` moves to **4**, because a
four-case measurement and an eleven-case measurement are different measurements.

## 4a. AMENDMENT — `OPENAI_REASONING_EFFORT=high`

**Approved by the repository owner on 2026-08-29**, after the first qualification run of
`gpt-5-mini-2025-08-07` exited 1.

### What that run measured

Probes 1-6 passed. Probe 7 ran the full §15.1 suite — eleven cases, three runs each, 33 calls —
with **zero** operational failures, **zero** canonical-validation failures and **zero**
outcome-consistency failures. Ten of eleven cases were stable and matched their §15.1
expectation, including case C, the case that disqualified `gpt-4o-2024-08-06`.

**Case E did not.** Three repeats returned `UNCLEAR`, `PARTIALLY`, `PARTIALLY`. Case E is the
conservative-ambiguity case: E3's only passage refers to a figure that was not supplied, and §6
rule 4 makes that `UNCLEAR` rather than "partially". On two of three runs the model called E3
`NOT_FOUND` — a substantive judgement difference on exactly the case a human must see at Gate C.

`gpt-5-mini` rejects an explicit `temperature`, so those calls ran at the provider's default
sampling; the artifact records `temperature_sent: false`.

### The decision

Set `OPENAI_REASONING_EFFORT=high`. This is an **explicit quality-over-cost decision for verifier
qualification**, taken by the owner rather than defaulted in code — the reason the setting was
left unset in the first place is that it trades answer quality against cost, and that is a
decision with an owner.

Nothing else changes: not the model, not the byte-pinned §3 system prompt, not the canonical
schema, not the wire schema, not the expected outcomes, not any §9 consistency check, and not the
zero-failure acceptance policy.

### It is bound to the capability identity

`reasoning_effort` changes what the model does, so a qualification made at one effort says
nothing about another. `deployment.InferenceConfig` captures every behaviour-affecting inference
setting — reasoning effort, whether and what temperature is sent, the token parameter, the
effective ceiling, the dialect, the timeout floor, and `top_p`/`seed` should either ever be used —
and its `inference_config_fingerprint` is now one of the identity fields admission compares.

Consequences, all asserted in `tests/test_inference_identity.py`:

- an artifact measured with the effort **unset** cannot qualify a `high` runtime;
- an artifact measured at **`high`** cannot qualify an unset or lower-effort runtime;
- each field moves the fingerprint **independently**;
- `reasoning_effort=""` and `temperature_sent=false` are **values**, not gaps — a provider
  default that affects behaviour is represented explicitly, never inherited silently;
- a value outside `minimal|low|medium|high` raises `ConfigError` before any paid call;
- a family that cannot honour the setting **refuses** rather than dropping it;
- `OPENAI_ADAPTER_BEHAVIOUR_VERSION` moves to 4, because the request changed.

Both existing `gpt-5-mini` and `gpt-4o` artifacts predate `inference_config_fingerprint`, carry
no such key, and therefore cannot be read as an identity at all. They stay on disk and stay
accurate about what they measured.

### The cost-bounded diagnostic

Re-running 39 calls to learn whether one case changed spends 38 of them learning nothing.
`python -m verification.diagnose E` runs case E alone, five times, through the exact production
path. It is **structurally non-qualifying**: its record carries no `runtime_identity` and no
`eligible` key, its filename is outside the namespace `capability.load_artifacts` reads, and it
does not import the artifact writer. `tests/test_diagnose.py` asserts each property, including
that a copy renamed into the capability namespace still qualifies nothing. A pass there is
permission to spend the full gate; its calls are never counted toward a qualification.

---

## 5. What this decision does not do

- It does not weaken check 15, any other §9 check, the canonical schema, or the wire schema.
- It does not disclose the derived outcome to the model.
- It does not permit `json_object`, a prompt-only fallback, or any provider fallback.
- It does not approve a non-zero failure threshold.
- It does not qualify `gpt-5-mini-2025-08-07`; only the gate can.
- It does not touch discovery sources, EPO's enrichment-only role, Hugging Face embeddings,
  Qdrant's disabled status, or the search-only pipeline.
