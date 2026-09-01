"""The authoritative output contract (spec sections 10 and 10.1).

Three representations of one contract, serving three different ends:

- :data:`RESPONSE_JSON_SCHEMA` is section 10 verbatim. It is the **canonical**
  contract and the authority whenever any two of these disagree. It is no longer
  what is sent to a provider.
- The pydantic models are section 10.1 verbatim. They are the **canonical
  validator**: what checks a response on the way back in, and - since the
  2026-08-29 amendment - the only thing enforcing the constraints the provider
  can no longer be asked to enforce.
- :func:`provider_response_format` renders the canonical schema through
  :mod:`verification.wire` into the **wire** schema: the same contract expressed
  in the subset of JSON Schema OpenAI's strict Structured Outputs mode accepts.

**The wire layer removes keywords, never requirements.** Owner amendment
2026-08-29: sending section 10 as written was rejected with a deterministic HTTP
400 before the model was reached, so four keywords are omitted from the
provider-facing copy and every one of them is enforced locally instead. See
``verification.wire.WIRE_OMITTED_KEYWORDS`` for the mapping and
``tests/test_wire_schema.py`` for the proof. Spec section 14.1 still holds
exactly as written: validation strictness does not vary by provider.

Schema validation covers section 9's checks 1-2 and part of 5-6. The cross-field
checks live in :mod:`verification.checks`, because "this quote appears in that
passage" cannot be expressed in a schema.

The models deliberately contain **no human-review fields** (spec section 13.7).
Asking a model to emit a reviewer identity or a review status invites it to
fabricate a human decision, which is the most consequential fabrication available
in this system.
"""

import hashlib
import json
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


RESPONSE_JSON_SCHEMA_SOURCE = """{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "FeatureDisclosureVerification",
  "type": "object",
  "additionalProperties": false,
  "required": [
    "feature_id", "feature_version", "publication_number", "verification_scope",
    "outcome", "elements", "relationships", "missing_element_ids",
    "missing_relationship_ids", "ambiguities", "requests_for_more_context"
  ],
  "properties": {
    "feature_id": { "type": "string", "minLength": 1 },
    "feature_version": { "type": "string", "minLength": 1 },
    "publication_number": { "type": "string", "minLength": 1 },
    "verification_scope": { "const": "SUPPLIED_PASSAGES_ONLY" },
    "outcome": {
      "enum": [
        "FULLY_DISCLOSED_IN_SUPPLIED_PASSAGES",
        "PARTIALLY_DISCLOSED_IN_SUPPLIED_PASSAGES",
        "NO_DISCLOSURE_FOUND_IN_SUPPLIED_PASSAGES",
        "UNCLEAR"
      ]
    },
    "elements": {
      "type": "array", "minItems": 1,
      "items": { "$ref": "#/$defs/elementResult" }
    },
    "relationships": {
      "type": "array", "minItems": 0,
      "items": { "$ref": "#/$defs/relationshipResult" }
    },
    "missing_element_ids": { "type": "array", "items": { "type": "string" }, "uniqueItems": true },
    "missing_relationship_ids": { "type": "array", "items": { "type": "string" }, "uniqueItems": true },
    "ambiguities": { "type": "array", "items": { "$ref": "#/$defs/ambiguity" } },
    "requests_for_more_context": { "type": "array", "items": { "$ref": "#/$defs/contextRequest" } }
  },
  "$defs": {
    "status": {
      "enum": ["FOUND", "NOT_FOUND_IN_SUPPLIED_PASSAGES", "UNCLEAR"]
    },
    "evidenceItem": {
      "type": "object", "additionalProperties": false,
      "required": ["chunk_id", "locator", "exact_quote"],
      "properties": {
        "chunk_id": { "type": "string", "minLength": 1 },
        "locator": { "type": "string", "minLength": 1 },
        "exact_quote": { "type": "string", "minLength": 1 }
      }
    },
    "combinationBasis": {
      "type": "object", "additionalProperties": false,
      "required": ["chunk_id", "locator", "exact_quote", "explanation"],
      "properties": {
        "chunk_id": { "type": "string", "minLength": 1 },
        "locator": { "type": "string", "minLength": 1 },
        "exact_quote": { "type": "string", "minLength": 1 },
        "explanation": { "type": "string", "minLength": 1, "maxLength": 300 }
      }
    },
    "elementResult": {
      "type": "object", "additionalProperties": false,
      "required": ["element_id", "status", "evidence", "combination_basis", "note"],
      "properties": {
        "element_id": { "type": "string", "minLength": 1 },
        "status": { "$ref": "#/$defs/status" },
        "evidence": { "type": "array", "items": { "$ref": "#/$defs/evidenceItem" } },
        "combination_basis": { "type": "array", "items": { "$ref": "#/$defs/combinationBasis" } },
        "note": { "type": "string", "maxLength": 300 }
      }
    },
    "relationshipResult": {
      "type": "object", "additionalProperties": false,
      "required": ["relationship_id", "status", "evidence", "combination_basis", "note"],
      "properties": {
        "relationship_id": { "type": "string", "minLength": 1 },
        "status": { "$ref": "#/$defs/status" },
        "evidence": { "type": "array", "items": { "$ref": "#/$defs/evidenceItem" } },
        "combination_basis": { "type": "array", "items": { "$ref": "#/$defs/combinationBasis" } },
        "note": { "type": "string", "maxLength": 300 }
      }
    },
    "ambiguity": {
      "type": "object", "additionalProperties": false,
      "required": ["requirement_type", "requirement_id", "chunk_ids", "reason"],
      "properties": {
        "requirement_type": { "enum": ["ELEMENT", "RELATIONSHIP"] },
        "requirement_id": { "type": "string", "minLength": 1 },
        "chunk_ids": { "type": "array", "minItems": 1, "items": { "type": "string" } },
        "reason": { "type": "string", "minLength": 1, "maxLength": 300 }
      }
    },
    "contextRequest": {
      "type": "object", "additionalProperties": false,
      "required": ["chunk_id", "requested_context", "reason"],
      "properties": {
        "chunk_id": { "type": "string", "minLength": 1 },
        "requested_context": {
          "enum": ["PRECEDING", "FOLLOWING", "PRECEDING_AND_FOLLOWING",
                   "PARENT_CLAIM", "REFERENCED_DEFINITION", "REFERENCED_FIGURE"]
        },
        "reason": { "type": "string", "minLength": 1, "maxLength": 300 }
      }
    }
  }
}"""
"""Spec section 10, verbatim. Kept as source text rather than a dict literal so
``tests/test_verification_spec_consistency.py`` can compare it to the markdown
character for character."""

RESPONSE_JSON_SCHEMA: dict = json.loads(RESPONSE_JSON_SCHEMA_SOURCE)


def provider_response_format(name: str = "feature_disclosure_verification") -> dict:
    """The ``response_format`` value for a JSON Schema-constrained request.

    **The provider is sent the wire schema, never the canonical one.** Owner
    amendment 2026-08-29 (spec section 10.2): OpenAI's strict Structured Outputs
    mode accepts a subset of JSON Schema, and sending section 10 as written is
    rejected with HTTP 400 before the model is ever reached - measured, and
    deterministic. :func:`verification.wire.to_wire_schema` renders section 10
    into that subset; ``$schema`` and ``title`` go because they are document
    metadata, and ``minLength``/``minItems``/``maxItems``/``uniqueItems`` go
    because the provider rejects them.

    **Nothing is relaxed by this.** Every removed keyword is enforced *locally*
    after parsing, by the models below and by
    :func:`verification.checks.parse_response`, and
    ``verification.wire.WIRE_OMITTED_KEYWORDS`` names the enforcing mechanism for
    each one. Validation strictness still does not vary by provider (spec section
    14.1): a response that satisfies the wire schema and violates the canonical
    contract is rejected here exactly as it always was.
    """
    from .wire import to_wire_schema

    schema = to_wire_schema(RESPONSE_JSON_SCHEMA)
    return {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}}


def canonical_validator_version() -> str:
    """A content hash of the **canonical validator**, not of its source text.

    Derived from what the validator actually enforces - the section 10.1 models'
    own generated JSON Schema, plus the constraints applied beside them - rather
    than from a hand-maintained constant nobody remembers to bump or a source
    hash that moves every time a comment is reworded.

    It is part of the capability identity because the amendment made the
    canonical layer load-bearing for constraints the provider no longer sees: a
    capability artifact says "this runtime honoured the contract", and after the
    amendment half of that contract is enforced here. A change to a field's
    ``min_length``, to ``UNIQUE_ITEM_FIELDS``, or to the model shape moves this
    value and invalidates every prior artifact.
    """
    payload = {
        "models": FeatureDisclosureVerification.model_json_schema(),
        "unique_item_fields": list(UNIQUE_ITEM_FIELDS),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


class Status(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND_IN_SUPPLIED_PASSAGES = "NOT_FOUND_IN_SUPPLIED_PASSAGES"
    UNCLEAR = "UNCLEAR"


class Outcome(StrEnum):
    FULLY = "FULLY_DISCLOSED_IN_SUPPLIED_PASSAGES"
    PARTIALLY = "PARTIALLY_DISCLOSED_IN_SUPPLIED_PASSAGES"
    NONE_FOUND = "NO_DISCLOSURE_FOUND_IN_SUPPLIED_PASSAGES"
    UNCLEAR = "UNCLEAR"


class Strict(BaseModel):
    """Frozen and closed, and the collections below are tuples for the same reason.

    ``frozen=True`` blocks field *reassignment* and nothing else. While the
    collection fields were ``list``, a validated machine result was mutable in
    every way that matters - ``result.elements.clear()``,
    ``result.missing_element_ids.append("E99")``, and worst of all
    ``result.elements[0].evidence.append(...)``, which attaches evidence to a
    requirement no model cited. Spec section 13.1 says a validated result is
    never edited, so every collection is a tuple of frozen models: deeply
    immutable, and the only way to change one is a new ``result_version``.

    This is a representation change, not a contract change. Pydantic accepts a
    JSON array into a tuple field and serializes it back as a JSON array, so the
    wire format is unchanged and :data:`RESPONSE_JSON_SCHEMA` - the authority -
    keeps ``"type": "array"`` throughout. Do **not** convert back to lists after
    validation; that reintroduces the mutability the tuples exist to remove.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceItem(Strict):
    chunk_id: str = Field(min_length=1)
    locator: str = Field(min_length=1)
    exact_quote: str = Field(min_length=1)


class CombinationBasis(EvidenceItem):
    explanation: str = Field(min_length=1, max_length=300)


class ElementResult(Strict):
    element_id: str = Field(min_length=1)
    status: Status
    evidence: tuple[EvidenceItem, ...]
    combination_basis: tuple[CombinationBasis, ...]
    note: str = Field(max_length=300)


class RelationshipResult(Strict):
    relationship_id: str = Field(min_length=1)
    status: Status
    evidence: tuple[EvidenceItem, ...]
    combination_basis: tuple[CombinationBasis, ...]
    note: str = Field(max_length=300)


class Ambiguity(Strict):
    requirement_type: Literal["ELEMENT", "RELATIONSHIP"]
    requirement_id: str = Field(min_length=1)
    chunk_ids: tuple[str, ...] = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=300)


class ContextRequest(Strict):
    chunk_id: str = Field(min_length=1)
    requested_context: Literal[
        "PRECEDING", "FOLLOWING", "PRECEDING_AND_FOLLOWING",
        "PARENT_CLAIM", "REFERENCED_DEFINITION", "REFERENCED_FIGURE",
    ]
    reason: str = Field(min_length=1, max_length=300)


class FeatureDisclosureVerification(Strict):
    feature_id: str = Field(min_length=1)
    feature_version: str = Field(min_length=1)
    publication_number: str = Field(min_length=1)
    verification_scope: Literal["SUPPLIED_PASSAGES_ONLY"]
    outcome: Outcome
    elements: tuple[ElementResult, ...] = Field(min_length=1)
    relationships: tuple[RelationshipResult, ...]
    missing_element_ids: tuple[str, ...]
    missing_relationship_ids: tuple[str, ...]
    ambiguities: tuple[Ambiguity, ...]
    requests_for_more_context: tuple[ContextRequest, ...]


UNIQUE_ITEM_FIELDS = ("missing_element_ids", "missing_relationship_ids")
"""Fields spec section 10 marks ``uniqueItems: true``.

Pydantic has no ``uniqueItems``, so section 10.1 - reproduced above verbatim -
does not enforce it, and section 10 is the authority where the two differ.
:func:`verification.checks.validate_response` enforces it as part of check 1's
schema conformance rather than by editing the section 10.1 models, which stay a
faithful copy of the specification.
"""
