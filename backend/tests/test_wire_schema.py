"""The canonical/wire schema split (owner amendment, 2026-08-29).

**The amendment's safety argument is one sentence: every keyword removed from the
provider-facing schema is enforced locally instead.** That sentence is worth
nothing as prose, so this file proves it — for each omitted keyword there is a
response that violates it and an assertion that the canonical validator rejects
that response.

The rest of the file guards the two ways this split can rot:

1. **Drift.** Two hand-maintained schemas diverge silently, and the divergence is
   invisible until a response that should have been rejected is accepted. The wire
   schema is *generated*, and these tests assert it is generated, is a pure
   function of the canonical schema, and mutates nothing.
2. **A new incompatibility.** OpenAI rejects a schema on the *first* problem it
   finds, so discovering incompatibilities live costs one paid round trip each.
   `openai_strict_violations` finds all of them offline, and
   `test_the_wire_schema_has_none_of_the_28_measured_incompatibilities` is the
   offline proof the 2026-08-28 blocker is gone.

No test here makes a network call.
"""

import copy
import json

import pytest

from verification.checks import parse_response
from verification.schema import (
    RESPONSE_JSON_SCHEMA,
    canonical_validator_version,
    provider_response_format,
)
from verification.states import ResponseValidationError
from verification.wire import (
    OMITTED_METADATA,
    WIRE_KEYWORDS,
    WIRE_OMITTED_KEYWORDS,
    WIRE_TRANSFORM_VERSION,
    WireSchemaError,
    openai_strict_violations,
    to_wire_schema,
    wire_schema_fingerprint,
)

MEASURED_INCOMPATIBILITY_COUNT = 28
"""What `verification.wire.openai_strict_violations` found in the section 10
schema on 2026-08-28, when every verifier call was failing HTTP 400. Pinned as a
number so that a canonical schema change which *adds* an incompatibility shows up
here rather than as a paid 400 later."""


def wire() -> dict:
    return to_wire_schema(RESPONSE_JSON_SCHEMA)


# --- the canonical schema is untouched, and remains the authority ----------


def test_the_transformation_does_not_mutate_the_canonical_schema():
    """`RESPONSE_JSON_SCHEMA` is a module-level dict every other caller shares. A
    transformation that edited it in place would silently rewrite the authority."""
    before = copy.deepcopy(RESPONSE_JSON_SCHEMA)
    to_wire_schema(RESPONSE_JSON_SCHEMA)
    to_wire_schema(RESPONSE_JSON_SCHEMA)
    assert RESPONSE_JSON_SCHEMA == before


def test_the_canonical_schema_still_carries_every_constraint():
    """The amendment removes keywords from the *rendering*, never from the
    contract. Section 10 is unchanged and `test_verification_spec_consistency`
    still compares it to the markdown character for character."""
    properties = RESPONSE_JSON_SCHEMA["properties"]
    assert properties["feature_id"]["minLength"] == 1
    assert properties["elements"]["minItems"] == 1
    assert properties["missing_element_ids"]["uniqueItems"] is True
    assert properties["verification_scope"]["const"] == "SUPPLIED_PASSAGES_ONLY"
    assert RESPONSE_JSON_SCHEMA["$schema"]
    assert RESPONSE_JSON_SCHEMA["title"] == "FeatureDisclosureVerification"


def test_the_transformation_is_deterministic():
    assert json.dumps(wire(), sort_keys=True) == json.dumps(wire(), sort_keys=True)
    assert wire_schema_fingerprint(wire()) == wire_schema_fingerprint(wire())


def test_the_fingerprint_moves_when_the_transformation_version_moves():
    """The rendering can change without the contract changing; the identity has to
    notice either."""
    first = wire_schema_fingerprint(wire())
    import verification.wire as module

    original = module.WIRE_TRANSFORM_VERSION
    try:
        module.WIRE_TRANSFORM_VERSION = original + "-changed"
        assert wire_schema_fingerprint(wire()) != first
    finally:
        module.WIRE_TRANSFORM_VERSION = original


# --- provider compatibility, established offline ---------------------------


def test_the_canonical_schema_is_incompatible_and_that_is_expected():
    """The measured blocker. Section 10 is correct JSON Schema and OpenAI's strict
    mode accepts a subset of it — which is why a rendering exists at all."""
    violations = openai_strict_violations(RESPONSE_JSON_SCHEMA)
    assert len(violations) == MEASURED_INCOMPATIBILITY_COUNT
    assert ("properties.verification_scope", "const/enum without 'type'") in violations


def test_the_wire_schema_has_none_of_the_28_measured_incompatibilities():
    """**The offline proof that the 2026-08-28 blocker is fixed**, for every
    violation rather than for the first one the provider happens to report."""
    assert openai_strict_violations(wire()) == []


def test_the_response_format_production_sends_is_the_wire_schema():
    """A gate or a test that probed a different schema would certify something the
    application never does."""
    sent = provider_response_format()
    assert sent["type"] == "json_schema"
    assert sent["json_schema"]["strict"] is True
    assert sent["json_schema"]["schema"] == wire()
    assert openai_strict_violations(sent["json_schema"]["schema"]) == []


def test_the_wire_schema_carries_only_allow_listed_keywords():
    def keywords(node, found):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("properties", "$defs"):
                    for sub in value.values():
                        keywords(sub, found)
                    continue
                found.add(key)
                keywords(value, found)
        elif isinstance(node, list):
            for item in node:
                keywords(item, found)
        return found

    assert keywords(wire(), set()) <= WIRE_KEYWORDS


def test_every_const_and_enum_gained_an_explicit_type():
    """The single keyword that produced the deterministic 400."""
    schema = wire()
    assert schema["properties"]["verification_scope"]["type"] == "string"
    assert schema["properties"]["outcome"]["type"] == "string"
    assert schema["$defs"]["status"]["type"] == "string"
    assert schema["$defs"]["ambiguity"]["properties"]["requirement_type"]["type"] == "string"
    assert schema["$defs"]["contextRequest"]["properties"]["requested_context"]["type"] == "string"


def test_closed_shapes_and_required_properties_survive_the_rendering():
    """These are the constraints OpenAI *does* enforce, so they must not be among
    the casualties."""
    schema = wire()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    for name, definition in schema["$defs"].items():
        if definition.get("type") != "object":
            continue
        assert definition["additionalProperties"] is False, name
        assert set(definition["required"]) == set(definition["properties"]), name


def test_root_metadata_is_dropped_from_the_wire_copy_only():
    schema = wire()
    for keyword in OMITTED_METADATA:
        assert keyword not in schema
        assert keyword in RESPONSE_JSON_SCHEMA


# --- every omitted keyword is enforced locally instead ---------------------


def valid_response() -> dict:
    """A response that satisfies the full canonical contract.

    Every negative test below is this object with exactly one thing wrong, so a
    rejection can only be attributable to that one thing.
    """
    return {
        "feature_id": "F2",
        "feature_version": "3",
        "publication_number": "US1234567B2",
        "verification_scope": "SUPPLIED_PASSAGES_ONLY",
        "outcome": "NO_DISCLOSURE_FOUND_IN_SUPPLIED_PASSAGES",
        "elements": [
            {
                "element_id": "E1",
                "status": "NOT_FOUND_IN_SUPPLIED_PASSAGES",
                "evidence": [],
                "combination_basis": [],
                "note": "",
            }
        ],
        "relationships": [],
        "missing_element_ids": ["E1"],
        "missing_relationship_ids": [],
        "ambiguities": [],
        "requests_for_more_context": [],
    }


def test_the_fixture_itself_is_accepted():
    """Otherwise every negative test below would pass for the wrong reason."""
    assert parse_response(valid_response()).feature_id == "F2"


def test_the_omitted_keyword_list_is_exactly_what_the_transformation_removes():
    """A keyword may be listed only if it is genuinely omitted, and omitted only if
    it is listed — otherwise the mapping from keyword to enforcing mechanism stops
    describing reality."""
    assert set(WIRE_OMITTED_KEYWORDS) == {"minLength", "minItems", "maxItems", "uniqueItems"}
    rendered = json.dumps(wire())
    for keyword in WIRE_OMITTED_KEYWORDS:
        assert f'"{keyword}"' not in rendered


def test_maxitems_is_listed_but_unused_by_the_canonical_schema():
    """Listed defensively so a future canonical `maxItems` cannot reach the wire
    schema unnoticed. If this fails, `maxItems` has been added to section 10 and
    needs a canonical enforcement mechanism and a negative test before it ships."""
    assert '"maxItems"' not in json.dumps(RESPONSE_JSON_SCHEMA)


@pytest.mark.parametrize(
    "field",
    ["feature_id", "feature_version", "publication_number"],
)
def test_minlength_is_enforced_canonically_on_top_level_strings(field):
    """The wire schema no longer carries `minLength`, so a provider may legitimately
    return an empty string here."""
    payload = valid_response()
    payload[field] = ""
    with pytest.raises(ResponseValidationError) as raised:
        parse_response(payload)
    assert raised.value.check == "check 1"


def test_minlength_is_enforced_canonically_on_a_nested_string():
    payload = valid_response()
    payload["elements"][0]["element_id"] = ""
    with pytest.raises(ResponseValidationError):
        parse_response(payload)


def test_minlength_is_enforced_canonically_on_an_evidence_quotation():
    """An empty `exact_quote` is the most consequential empty string in the schema:
    it is a citation to nothing."""
    payload = valid_response()
    payload["elements"][0]["status"] = "FOUND"
    payload["elements"][0]["evidence"] = [
        {"chunk_id": "c1", "locator": "claim 1", "exact_quote": ""}
    ]
    with pytest.raises(ResponseValidationError):
        parse_response(payload)


def test_minitems_is_enforced_canonically_on_elements():
    """`elements` has `minItems: 1`. Without local enforcement a model could return
    a verification result that verified nothing and it would parse."""
    payload = valid_response()
    payload["elements"] = []
    with pytest.raises(ResponseValidationError):
        parse_response(payload)


def test_minitems_is_enforced_canonically_on_ambiguity_chunk_ids():
    payload = valid_response()
    payload["ambiguities"] = [
        {"requirement_type": "ELEMENT", "requirement_id": "E1",
         "chunk_ids": [], "reason": "unclear"}
    ]
    with pytest.raises(ResponseValidationError):
        parse_response(payload)


@pytest.mark.parametrize("field", ["missing_element_ids", "missing_relationship_ids"])
def test_uniqueitems_is_enforced_canonically(field):
    """pydantic has no `uniqueItems`, so this one is enforced by explicit code in
    `parse_response` rather than by the models — which is exactly why it needs its
    own test."""
    payload = valid_response()
    payload[field] = ["E1", "E1"]
    with pytest.raises(ResponseValidationError) as raised:
        parse_response(payload)
    assert "duplicates" in raised.value.detail


# --- constraints the wire schema keeps are still enforced locally too ------
#
# The provider is asked to enforce these, and the canonical layer enforces them
# regardless: spec section 14.1 says strictness does not vary by provider, so
# nothing here may rely on the provider having done its job.


def test_a_wrong_constant_is_rejected():
    payload = valid_response()
    payload["verification_scope"] = "WHOLE_DOCUMENT"
    with pytest.raises(ResponseValidationError):
        parse_response(payload)


def test_an_invalid_enum_member_is_rejected():
    payload = valid_response()
    payload["outcome"] = "DEFINITELY_DISCLOSED"
    with pytest.raises(ResponseValidationError):
        parse_response(payload)


def test_an_invalid_nested_enum_member_is_rejected():
    payload = valid_response()
    payload["elements"][0]["status"] = "PROBABLY_FOUND"
    with pytest.raises(ResponseValidationError):
        parse_response(payload)


def test_an_unknown_property_is_rejected():
    payload = valid_response()
    payload["confidence"] = 0.91
    with pytest.raises(ResponseValidationError):
        parse_response(payload)


def test_an_unknown_nested_property_is_rejected():
    payload = valid_response()
    payload["elements"][0]["confidence"] = 0.91
    with pytest.raises(ResponseValidationError):
        parse_response(payload)


def test_a_missing_top_level_required_field_is_rejected():
    payload = valid_response()
    del payload["relationships"]
    with pytest.raises(ResponseValidationError):
        parse_response(payload)


def test_a_missing_nested_required_field_is_rejected():
    payload = valid_response()
    del payload["elements"][0]["combination_basis"]
    with pytest.raises(ResponseValidationError):
        parse_response(payload)


def test_maxlength_is_still_enforced_canonically():
    """`maxLength` survives into the wire schema because OpenAI accepts it, but
    acceptance is not evidence it is honoured — so the canonical layer enforces it
    either way."""
    payload = valid_response()
    payload["elements"][0]["note"] = "x" * 301
    with pytest.raises(ResponseValidationError):
        parse_response(payload)


# --- the transformation fails closed ---------------------------------------


def test_an_unrecognised_keyword_raises_rather_than_reaching_the_provider():
    """A keyword nobody considered must not be passed through and hoped for: that
    is how a constraint silently stops being enforced."""
    schema = copy.deepcopy(RESPONSE_JSON_SCHEMA)
    schema["properties"]["feature_id"]["pattern"] = "^F[0-9]+$"
    with pytest.raises(WireSchemaError) as raised:
        to_wire_schema(schema)
    assert "pattern" in str(raised.value)


def test_a_non_string_enum_raises_rather_than_being_guessed_at():
    schema = copy.deepcopy(RESPONSE_JSON_SCHEMA)
    schema["properties"]["outcome"] = {"enum": ["UNCLEAR", 7]}
    with pytest.raises(WireSchemaError) as raised:
        to_wire_schema(schema)
    assert "cannot derive a wire 'type'" in str(raised.value)


def test_an_optional_property_raises_rather_than_being_added_to_required():
    """OpenAI strict mode requires every property to be required. Quietly adding
    one would render a schema the canonical contract does not state."""
    schema = copy.deepcopy(RESPONSE_JSON_SCHEMA)
    schema["required"].remove("ambiguities")
    with pytest.raises(WireSchemaError) as raised:
        to_wire_schema(schema)
    assert "ambiguities" in str(raised.value)


def test_an_open_object_raises():
    schema = copy.deepcopy(RESPONSE_JSON_SCHEMA)
    schema["$defs"]["evidenceItem"]["additionalProperties"] = True
    with pytest.raises(WireSchemaError) as raised:
        to_wire_schema(schema)
    assert "additionalProperties" in str(raised.value)


# --- the identity moves when any layer moves -------------------------------


def test_the_capability_identity_covers_all_three_layers():
    from verification.capability import IDENTITY_FIELDS

    for name in ("schema_version", "wire_schema_version", "validator_version"):
        assert name in IDENTITY_FIELDS, (
            f"{name} must be compared at admission, or a capability result could "
            f"outlive the thing it measured"
        )


def test_the_validator_version_is_derived_from_what_the_validator_enforces():
    """Derived, not hand-maintained: a constant is a thing somebody forgets to
    bump, and the failure mode is a capability record outliving the validator it
    was measured against."""
    assert canonical_validator_version().startswith("sha256:")
    assert canonical_validator_version() == canonical_validator_version()


def test_the_pre_amendment_artifact_shape_cannot_qualify_anything():
    """Artifacts written before 2026-08-29 have no `wire_schema_version`. They stay
    on disk and stay accurate about what they measured, and they qualify nothing —
    a missing key is unreadable, never a match against an empty string."""
    from verification.capability import _identity_from_record

    old = {
        "runtime_identity": {
            "deployment_profile": "local", "provider": "openai",
            "endpoint": {"scheme": "https", "host": "api.openai.com", "port": "", "path": "/v1"},
            "model": "gpt-4o-2024-08-06", "model_revision": "gpt-4o-2024-08-06",
            "adapter_revision": "openai-python/3.5.0",
            "schema_version": "sha256:cee4a3a1caa7df7e", "runner_version": "2",
        }
    }
    assert _identity_from_record(old) is None
