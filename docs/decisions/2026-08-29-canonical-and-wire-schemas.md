# Canonical and wire schemas, and the capability runner's outcome vocabulary

- **Status:** APPROVED
- **Approved by:** the repository owner
- **Date:** 2026-08-29
- **Amends:** `docs/prompts/feature-disclosure-verification.md` §10 (new §10.2) and §14.2 (new
  §14.2.3, §14.2.4)
- **Supersedes nothing.** §10, §10.1, §14.1 and §14.2.1–§14.2.2 are unchanged and still binding.

## The finding that prompted it

The §14.2.1 capability gate was run for the first time against a real provider on 2026-08-28
(`DEPLOYMENT_PROFILE=local`, official OpenAI API, `gpt-4o-2024-08-06`, `openai-python/3.5.0`).
It exited 1. The cause was deterministic and had nothing to do with the model:

    400 invalid_request_error, param=response_format:
    Invalid schema for response_format 'feature_disclosure_verification':
    In context=('properties','verification_scope'), schema must have a 'type' key.

OpenAI's strict Structured Outputs mode accepts a subset of JSON Schema. An offline inspection
found **28** places where the §10 schema falls outside it: five `const`/`enum` nodes with no
explicit `type`, fourteen `minLength`, three `minItems`, two `uniqueItems`, and the root
`$schema` and `title`. Under `VERIFIER_STRUCTURED_OUTPUT=json_schema` this meant **every**
verifier call failed before the model was reached.

Two defects in the gate itself were found in the same run:

- an HTTP 429 was classified as a provider `REJECTED`, and a `REJECTED` enforcement probe was
  scored as a **pass** — the third instance of "an operational failure read as evidence about
  the model", after a DNS failure and a missing credential;
- the stability probe counted twelve HTTP 400s and still reported
  `malformed_response_rate: 0.0` and `all_cases_stable: true`, both of which are passing terms
  in the eligibility expression.

And one disclosure defect: an HTTP 429 body printed the OpenAI **organization id** to stdout
through `redaction.safe_log`, whose body-cut pattern recognised double-quoted JSON but not the
Python dict repr the SDK produces.

## The decision

**Keep OpenAI strict Structured Outputs using `json_schema`. Do not downgrade to generic
`json_object`.** §14.1 stands unchanged.

**Split the verifier's output contract into two explicit layers:**

1. a **canonical/domain contract** — §10 and §10.1 verbatim, unchanged, the authority, enforced
   after parsing on every response;
2. a **provider-facing wire schema** — the canonical schema rendered by a deterministic, pure,
   versioned transformation into the subset one adapter accepts.

The wire schema may omit **only** keywords the provider does not support, and only where the
canonical validator enforces them. **This is a compatibility adaptation, not a weakening of
verification requirements.** Validation strictness is identical before and after; which layer
enforces a given constraint is an implementation fact, not a contract fact.

**Fix the capability runner's classification and scoring**, so that no operational failure can
satisfy a probe or contribute a passing term, and **fix the redaction gap** so no account
identifier can reach any surface.

## What is omitted from the wire schema, and what enforces it instead

| Keyword | Sites | Enforced instead by |
|---|---|---|
| `minLength` | 14 | `pydantic Field(min_length=…)` on the §10.1 models, via `checks.parse_response` |
| `minItems` | 3 | `pydantic Field(min_length=…)` on `elements` and `Ambiguity.chunk_ids` |
| `uniqueItems` | 2 | `schema.UNIQUE_ITEM_FIELDS`, enforced explicitly in `checks.parse_response` — pydantic has no `uniqueItems` |
| `maxItems` | 0 | unused by §10; listed so a future canonical `maxItems` cannot reach the wire schema unnoticed |
| `$schema`, `title` | 2 | nothing — document metadata, constrains nothing |

`maxLength` is **retained** in the wire schema: OpenAI accepts it (a schema carrying an
unsupported keyword is rejected outright, so acceptance is evidence). Whether it is *honoured*
is not relied on — the canonical validator enforces it either way.

Each row is proved rather than asserted: `backend/tests/test_wire_schema.py` constructs a
response violating each omitted keyword and asserts the canonical validator rejects it.

## Consequences accepted

- **Every prior capability artifact is invalidated.** `wire_schema_version` and
  `validator_version` join the identity, `adapter_revision` gains a behaviour version, and the
  runner version moves to 3. An artifact written before this amendment has no
  `wire_schema_version` key, so no identity can be built from it and it qualifies nothing. The
  2026-08-28 artifact **stays on disk, unmodified, and stays accurate about what it measured.**
- **A qualification is now a statement about three layers plus the adapter.** A change to the
  canonical schema, the transformation, the validator or the adapter's behaviour invalidates it
  structurally rather than by anyone remembering to re-run something.
- **`safe_log` no longer passes a provider message through, redacted or otherwise.** It is
  rebuilt from allow-listed structured fields — exception type, HTTP status, provider `code` and
  `type`, PostgreSQL `sqlstate`. Redaction is pattern-matching and patterns are never complete;
  this removes the reliance on completeness. The cost is the provider's prose, and the
  SQLSTATE/status/code fields are a sharper diagnostic than the sentence they replace.
- **`tracing.trace` redacts unconditionally.** The call site that leaked was already calling a
  sanitizer. The last point before stdout no longer assumes the caller got it right.
- **The SSE `error` detail and the `POST /api/analyze` failure detail are now sanitized.** They
  were `str(error)`, rendered verbatim by the frontend's ErrorScreen. The actionable half — the
  provider and the HTTP status — survives.

## What this decision does **not** do

- It does not change §10, §10.1, or any constraint in them.
- It does not permit `json_object`. §14.2.2's generic-JSON decision remains open and unrecorded.
- It does not qualify any model. Eligibility is decided by running the gate.
- It does not transfer to the `aramco` profile, which remains deferred and blocked, or to `test`.
- It does not touch discovery sources, EPO's enrichment-only role, Hugging Face embeddings, or
  Qdrant's disabled status.
