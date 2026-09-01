"""Two schema layers, and the deterministic transformation between them.

**The canonical schema is the authority; the wire schema is an adapter-specific
rendering of it.** Spec section 10 (reproduced verbatim in
:mod:`verification.schema` as ``RESPONSE_JSON_SCHEMA``) states the full contract.
OpenAI's strict Structured Outputs mode accepts a *subset* of JSON Schema, and
the section 10 schema uses four keywords outside that subset. Sending section 10
as written produces, deterministically::

    400 invalid_request_error, param=response_format:
    Invalid schema for response_format 'feature_disclosure_verification':
    In context=('properties','verification_scope'), schema must have a 'type' key.

Measured 2026-08-28 against ``gpt-4o-2024-08-06`` through ``openai-python``, and
it is not transient: under ``VERIFIER_STRUCTURED_OUTPUT=json_schema`` *every*
verifier call failed before reaching the model.

**This is a compatibility adaptation, not a weakening.** Owner-approved amendment
2026-08-29 (``docs/decisions/2026-08-29-canonical-and-wire-schemas.md``). Every
keyword this module removes from the provider-facing copy is enforced
**locally**, after parsing, by the canonical validator in
:mod:`verification.checks` - see :data:`WIRE_OMITTED_KEYWORDS`, which names the
enforcing mechanism for each one, and ``tests/test_wire_schema.py``, which proves
each one by constructing a response that violates it and asserting the canonical
layer rejects it.

**Generated, never hand-maintained.** Two schemas written by hand drift, and the
drift is invisible until a response that should have been rejected is accepted.
:func:`to_wire_schema` is a pure function of the canonical schema, the canonical
object is deep-copied rather than mutated, and ``tests/test_wire_schema.py``
asserts both.

**Fail closed on anything unrecognised.** A canonical construct this
transformation has not been taught to render raises :class:`WireSchemaError`
rather than being passed through and hoped for. A schema keyword nobody
considered reaching a provider unexamined is exactly how a constraint silently
stops being enforced.
"""

import copy
import hashlib
import json
from typing import Any

WIRE_TRANSFORM_VERSION = "1"
"""Bumped whenever :func:`to_wire_schema` changes what it emits.

Part of the capability identity through :func:`wire_schema_fingerprint`, so a
change to the transformation invalidates every prior capability artifact - the
provider-facing schema is half of what the section 14.2.1 gate measures, and a
record made against a different rendering says nothing about this one.
"""


class WireSchemaError(RuntimeError):
    """The canonical schema contains something this transformation cannot render.

    Raised rather than passed through. See the module docstring.
    """


OMITTED_METADATA = ("$schema", "title")
"""Document metadata, not constraints.

Removed from the wire copy because OpenAI rejects unknown top-level keywords, and
kept in the canonical schema because that is where the contract is documented.
Neither enforces anything, so neither needs local enforcement.
"""

WIRE_OMITTED_KEYWORDS: dict[str, str] = {
    "minLength": (
        "pydantic Field(min_length=...) on every affected field of the section "
        "10.1 models, applied by verification.checks.parse_response"
    ),
    "minItems": (
        "pydantic Field(min_length=...) on FeatureDisclosureVerification.elements "
        "and Ambiguity.chunk_ids, applied by verification.checks.parse_response"
    ),
    "maxItems": (
        "no section 10 field uses it today; listed so a future canonical maxItems "
        "cannot reach the wire schema unnoticed, and asserted unused by "
        "tests/test_wire_schema.py"
    ),
    "uniqueItems": (
        "verification.schema.UNIQUE_ITEM_FIELDS, enforced explicitly by "
        "verification.checks.parse_response - pydantic has no uniqueItems"
    ),
}
"""Every validation keyword removed from the provider-facing copy, mapped to the
mechanism that enforces it locally instead.

This mapping is the whole safety argument for the amendment, and it is tested
rather than asserted: a keyword may be listed here only if a negative test shows
the canonical layer rejecting a response that violates it.
"""

WIRE_KEYWORDS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "$ref",
        "$defs",
        "anyOf",
        "description",
        "maxLength",
    }
)
"""Every keyword permitted to appear in the generated wire schema.

An allow-list rather than a deny-list, so a keyword added to the canonical schema
later cannot reach the provider merely because nobody thought to exclude it -
:func:`to_wire_schema` raises on anything not listed here.

``maxLength`` is on the list because OpenAI **accepts** it (measured 2026-08-29;
a schema carrying an unsupported keyword is rejected outright, so acceptance is
evidence). Whether it is *honoured* is not something this module relies on:
``maxLength`` is enforced canonically by pydantic either way, and its presence
here can only help. That is an adapter-specific boundary, re-established by the
section 14.2.1 gate whenever the adapter changes.
"""

_PASS_THROUGH = ("required", "enum", "const", "$ref", "description",
                 "additionalProperties", "type", "maxLength")
_RECURSE = ("items", "anyOf")
_NAME_MAPS = ("properties", "$defs")


def _type_for_values(values: list[Any], path: str) -> str:
    """The JSON Schema ``type`` implied by a ``const``/``enum`` value set.

    Derived from the values rather than assumed, and it **raises on a mixed or
    non-string set** instead of guessing. OpenAI requires an explicit ``type``
    beside every ``const`` and ``enum``; supplying the wrong one would let the
    provider reject a value the canonical contract permits, which is a silent
    narrowing of the contract rather than a rendering of it.
    """
    kinds = {type(value) for value in values}
    if kinds == {str}:
        return "string"
    raise WireSchemaError(
        f"{path}: cannot derive a wire 'type' for a const/enum whose values are "
        f"{sorted(kind.__name__ for kind in kinds)}. Teach to_wire_schema this "
        f"case explicitly rather than guessing - a wrong type narrows the "
        f"contract instead of rendering it."
    )


def _require_closed_object(node: dict[str, Any], path: str) -> None:
    """OpenAI strict mode requires every property required and every object closed.

    **Verified, never repaired.** Section 10 already satisfies both at every
    object, so this is a drift guard: if a future canonical revision makes a
    property optional, that is a real contract change with a real provider
    consequence, and silently adding it to ``required`` here would render a schema
    the canonical contract does not state.
    """
    if node.get("additionalProperties") is not False:
        raise WireSchemaError(
            f"{path}: OpenAI strict mode requires additionalProperties:false on "
            f"every object. The canonical schema does not close this one."
        )
    properties = set(node.get("properties", {}))
    required = set(node.get("required", []))
    if properties != required:
        raise WireSchemaError(
            f"{path}: OpenAI strict mode requires every property to be listed in "
            f"'required'. Optional here: {sorted(properties - required)}; required "
            f"but absent from properties: {sorted(required - properties)}. That is "
            f"a canonical contract change, not something to paper over in the wire "
            f"rendering."
        )


def _transform(node: Any, path: str, *, root: bool) -> Any:
    if isinstance(node, list):
        return [_transform(item, f"{path}[{i}]", root=False) for i, item in enumerate(node)]
    if not isinstance(node, dict):
        return node

    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in WIRE_OMITTED_KEYWORDS:
            continue
        if root and key in OMITTED_METADATA:
            continue
        if key in _NAME_MAPS:
            # Maps of NAMES to schemas: the keys are arbitrary field names, never
            # keywords, so they are recursed into without allow-list checking.
            out[key] = {
                name: _transform(sub, f"{path}.{key}.{name}", root=False)
                for name, sub in value.items()
            }
        elif key in _PASS_THROUGH:
            out[key] = copy.deepcopy(value)
        elif key in _RECURSE:
            out[key] = _transform(value, f"{path}.{key}", root=False)
        else:
            raise WireSchemaError(
                f"{path}: keyword {key!r} is neither in the wire allow-list nor a "
                f"known canonical-only keyword. Decide explicitly whether OpenAI "
                f"strict mode supports it (add it to WIRE_KEYWORDS and to the "
                f"pass-through set) or whether it is enforced locally instead (add "
                f"it to WIRE_OMITTED_KEYWORDS naming the enforcing mechanism, with "
                f"a test that proves it)."
            )

    if ("const" in out or "enum" in out) and "type" not in out:
        values = [out["const"]] if "const" in out else list(out["enum"])
        out["type"] = _type_for_values(values, path)

    if out.get("type") == "object":
        _require_closed_object(out, path)

    unknown = set(out) - WIRE_KEYWORDS
    if unknown:
        raise WireSchemaError(f"{path}: wire schema would carry unsupported {sorted(unknown)}")
    return out


def to_wire_schema(canonical: dict) -> dict:
    """The OpenAI strict-mode rendering of ``canonical``.

    Pure: ``canonical`` is deep-copied and never mutated. Deterministic: the same
    input always produces the same output, key order included, so
    :func:`wire_schema_fingerprint` is stable across processes.
    """
    return _transform(copy.deepcopy(canonical), "<root>", root=True)


# --- offline provider-compatibility inspection ------------------------------

_UNSUPPORTED_KEYWORDS = (
    "minLength", "minItems", "maxItems", "uniqueItems",
    "pattern", "format", "minimum", "maximum", "default", "$schema", "title",
)


def openai_strict_violations(schema: Any, path: str = "") -> list[tuple[str, str]]:
    """Every way ``schema`` violates OpenAI's strict Structured Outputs subset.

    Offline, exhaustive, and the reason a deterministic schema rejection never
    again has to be diagnosed one paid HTTP 400 at a time - the provider reports
    only the first problem it finds, so a live loop costs one round trip per
    violation and this costs none. Returns ``(json_path, problem)`` pairs; an
    empty list means the schema carries none of the incompatibilities this
    project has measured.
    """
    found: list[tuple[str, str]] = []
    if isinstance(schema, dict):
        for keyword in _UNSUPPORTED_KEYWORDS:
            if keyword in schema:
                found.append((path or "<root>", keyword))
        if ("const" in schema or "enum" in schema) and "type" not in schema:
            found.append((path or "<root>", "const/enum without 'type'"))
        if schema.get("type") == "object":
            if schema.get("additionalProperties") is not False:
                found.append((path or "<root>", "object without additionalProperties:false"))
            if set(schema.get("properties", {})) != set(schema.get("required", [])):
                found.append((path or "<root>", "object with a property missing from 'required'"))
        for key, value in schema.items():
            found.extend(openai_strict_violations(value, f"{path}.{key}" if path else key))
    elif isinstance(schema, list):
        for index, value in enumerate(schema):
            found.extend(openai_strict_violations(value, f"{path}[{index}]"))
    return found


def _fingerprint(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def wire_schema_fingerprint(wire: dict) -> str:
    """A content hash of the wire schema **and** the transformation version.

    Both, because those are the two ways the bytes a provider is asked to honour
    can differ: the canonical schema changed, or the rendering of it did.
    """
    return _fingerprint({"transform": WIRE_TRANSFORM_VERSION, "schema": wire})


__all__ = [
    "OMITTED_METADATA",
    "WIRE_KEYWORDS",
    "WIRE_OMITTED_KEYWORDS",
    "WIRE_TRANSFORM_VERSION",
    "WireSchemaError",
    "openai_strict_violations",
    "to_wire_schema",
    "wire_schema_fingerprint",
]
