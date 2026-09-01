"""Spec section 15.3: the specification's own examples are fixtures, not illustrations.

"Drift between them and the schema is how a specification stops being
authoritative", and the section names the exact failure it is guarding against:
an earlier revision of section 13.2 carried a ``machine_result`` with empty
``elements`` and ``relationships`` arrays while reporting
``missing_element_ids: ["E3"]`` - structurally impossible under sections 9 and
10, and invisible to review because nothing validated it.

So this module reads ``docs/prompts/feature-disclosure-verification.md`` and:

1. validates every fenced JSON example against the same validator production
   uses, routed by shape - a verification response through the section 10 model
   and the section 9 cross-field checks, a decomposition through the section 2
   loader, a passage through the section 7.1 dataclass, the review overlay
   through the section 13.4 checks;
2. checks the cross-field rules on those examples too - every ``exact_quote`` a
   substring of the section 4.3 passage it cites, both missing-id arrays agreeing
   with their statuses, and both ``outcome`` and ``reviewed_outcome`` equal to
   what ``derive_outcome`` returns;
3. asserts the shipped prompt constants and JSON Schema are **byte-identical** to
   the markdown. There is one canonical prompt, and the way that stays true is a
   test rather than a promise.

Nothing here is skipped for being "deliberately partial": section 15.3 point 3 is
explicit that silence is not a label, so an unlabelled example is treated as a
fixture and must pass. The one block this module reads as prose rather than
validating is section 6.2's reference implementation, which is Python.
"""

import json
import re
from pathlib import Path
from typing import get_args, get_origin

from tests import fixtures_verification as fx
from verification import schema
from verification import (
    ApprovedDecomposition,
    CellState,
    EvidenceAction,
    EvidenceItem,
    Outcome,
    Passage,
    RequirementReview,
    ReviewOverlay,
    ReviewStatus,
    Status,
    derive_outcome,
    render_elements_block,
    render_passages_block,
    render_relationships_block,
    reviewed_outcome,
    validate_response,
    validate_review,
)
from verification.prompt import PROMPT_MARKER, SYSTEM_PROMPT, USER_PROMPT_TEMPLATE
from verification.schema import RESPONSE_JSON_SCHEMA, RESPONSE_JSON_SCHEMA_SOURCE


SPEC_PATH = Path(__file__).resolve().parents[2] / "docs" / "prompts" / "feature-disclosure-verification.md"

_FENCE_OPEN = re.compile(r"^```(\w*)\s*$")
_FENCE_CLOSE = re.compile(r"^```\s*$")


def _blocks() -> list[tuple[str, str, str]]:
    """Every fenced block in the specification, as ``(section, language, body)``."""
    lines = SPEC_PATH.read_text(encoding="utf-8").split("\n")
    found: list[tuple[str, str, str]] = []
    section = ""
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("#"):
            section = line.strip("# ").strip()
        opening = _FENCE_OPEN.match(line)
        if opening:
            language = opening.group(1)
            body: list[str] = []
            index += 1
            while index < len(lines) and not _FENCE_CLOSE.match(lines[index]):
                body.append(lines[index])
                index += 1
            found.append((section, language, "\n".join(body)))
        index += 1
    return found


BLOCKS = _blocks()


def block(section_prefix: str, language: str, nth: int = 0) -> str:
    matches = [
        body for section, lang, body in BLOCKS
        if section.startswith(section_prefix) and lang == language
    ]
    assert matches, f"no {language} block under a section starting {section_prefix!r}"
    return matches[nth]


def json_blocks() -> list[tuple[str, dict]]:
    return [
        (section, json.loads(body))
        for section, language, body in BLOCKS
        if language == "json"
    ]


# --------------------------------------------------------------------------
# The prompt and the schema are copies, and the test says so
# --------------------------------------------------------------------------

def test_the_shipped_system_prompt_is_the_specifications_system_prompt():
    assert SYSTEM_PROMPT == block("3. The canonical system prompt", "text")


def test_the_shipped_user_template_is_the_specifications_user_template():
    assert USER_PROMPT_TEMPLATE == block("4. The canonical user prompt template", "text")


def test_the_mock_marker_is_a_phrase_the_canonical_prompt_actually_contains():
    """Spec section 17.8's marker, without mutating the canonical prompt.

    The section asks for a phrase to be *added*; this registers one already
    present, so the byte-identity above survives and the doubles still cannot let
    a verification call fall through into a metered request.
    """
    assert PROMPT_MARKER in SYSTEM_PROMPT
    assert PROMPT_MARKER in block("3. The canonical system prompt", "text")


def test_the_shipped_json_schema_is_the_specifications_json_schema():
    assert json.loads(RESPONSE_JSON_SCHEMA_SOURCE) == json.loads(block("10. Formal output schema", "json"))
    assert RESPONSE_JSON_SCHEMA["title"] == "FeatureDisclosureVerification"


def _render(annotation) -> str:
    """A structural, name-based rendering of a type annotation.

    ``tuple[EvidenceItem, ...]`` renders the same whether ``EvidenceItem`` came
    from the shipped module or from executing the specification's own block.
    """
    origin = get_origin(annotation)
    if origin is None:
        return getattr(annotation, "__name__", str(annotation))
    args = ", ".join("..." if arg is Ellipsis else _render(arg) for arg in get_args(annotation))
    return f"{getattr(origin, '__name__', str(origin))}[{args}]"


def test_the_shipped_models_have_the_field_types_section_10_1_declares():
    """Section 10.1 is a fixture too, and its field *types* are load-bearing.

    The block is executed and its classes compared field by field against the
    shipped ones. That is stronger than eyeballing, and it is what catches the
    specific drift this test was added for: section 10.1 declaring ``list`` while
    the implementation uses ``tuple``, or the reverse. ``frozen=True`` blocks
    field reassignment only, so a ``list`` here would mean a validated machine
    result whose evidence could be appended to in place - which section 13.1
    forbids and :mod:`tests.test_feature_disclosure_verification` proves is
    impossible.

    Executing the block is safe and deliberate: it is the specification's own
    pydantic definitions, with no imports beyond ``enum``, ``typing`` and
    ``pydantic`` and no side effects.
    """
    namespace: dict = {}
    exec(compile(block("10.1 Pydantic equivalent", "python"), "<spec 10.1>", "exec"), namespace)

    for name in (
        "EvidenceItem", "CombinationBasis", "ElementResult", "RelationshipResult",
        "Ambiguity", "ContextRequest", "FeatureDisclosureVerification",
    ):
        specified = namespace[name]
        shipped = getattr(schema, name)
        assert set(specified.model_fields) == set(shipped.model_fields), name
        for field, info in specified.model_fields.items():
            # By rendered name, not by identity: executing the block builds its
            # own Status, Outcome and EvidenceItem classes, so `==` on the
            # annotation objects would compare two structurally identical types
            # and always fail.
            assert _render(info.annotation) == _render(shipped.model_fields[field].annotation), (
                f"{name}.{field}"
            )

        # extra="forbid" and frozen=True are the other half of the contract.
        assert specified.model_config == shipped.model_config == {"extra": "forbid", "frozen": True}

    # Every collection field on the top-level model is a tuple, not a list.
    for field, info in namespace["FeatureDisclosureVerification"].model_fields.items():
        origin = getattr(info.annotation, "__origin__", None)
        assert origin is not list, f"{field} is a list; section 13.1 requires immutability"


def test_section_10_arrays_stay_arrays_on_the_wire():
    """The tuple change must not have leaked into the authoritative JSON Schema."""
    properties = RESPONSE_JSON_SCHEMA["properties"]
    for field in ("elements", "relationships", "missing_element_ids",
                  "missing_relationship_ids", "ambiguities", "requests_for_more_context"):
        assert properties[field]["type"] == "array", field


def test_every_fenced_json_block_parses():
    """Section 15.3 point 1, at its weakest and most basic. Nothing is skipped."""
    parsed = json_blocks()
    assert len(parsed) == 5, [section for section, _ in parsed]
    for section, data in parsed:
        assert isinstance(data, dict), section


# --------------------------------------------------------------------------
# The section 2 decomposition and the section 4.3 passages are the one fixture
# --------------------------------------------------------------------------

def test_the_fixture_decomposition_is_the_specifications_decomposition():
    assert fx.DECOMPOSITION_JSON == json.loads(
        block("2. Prerequisite: an approved feature decomposition", "json")
    )
    # And it loads: unique IDs across both kinds, non-empty text, at least one
    # element, approved order preserved.
    loaded = ApprovedDecomposition.from_json(fx.DECOMPOSITION_JSON)
    assert loaded.element_ids == ("E1", "E2", "E3")
    assert loaded.relationship_ids == ("R1",)
    assert loaded.requirement_count == 4


def test_the_section_7_1_passage_example_loads_as_a_passage():
    data = json.loads(block("7.1 Required passage shape", "json"))
    passage = Passage(**data)
    assert passage.section in ("ABSTRACT", "CLAIMS", "DESCRIPTION", "OTHER")
    assert passage.evidence_eligible is True
    assert passage.publication_number in passage.chunk_id


def test_the_rendered_elements_block_is_the_specifications_section_4_1_block():
    assert render_elements_block(fx.DECOMPOSITION.elements) == block(
        "4.1 `{{required_elements_block}}`", "text"
    )


def test_the_relationships_block_renders_id_and_text_or_the_literal_none():
    rendered = render_relationships_block(fx.DECOMPOSITION.relationships)
    assert rendered == "R1: the controller varies coolant flow in response to sensor output"
    assert render_relationships_block(()) == "NONE"

    # Section 4.2's printed example annotates the relationship text with element
    # references ("the controller (E2) ... sensor output (E3)") that section 2's
    # approved R1 does not carry, so the two cannot be compared byte for byte the
    # way 4.1 can. What is checkable - and what the renderer owes - is the shape.
    printed = block("4.2 `{{required_relationships_block}}`", "text")
    assert printed.startswith("R1: ")
    assert rendered.startswith("R1: ")


def test_the_rendered_passages_block_is_the_specifications_section_4_3_block():
    assert render_passages_block(fx.CANONICAL_PASSAGES) == block(
        "4.3 `{{passages_block}}`", "text", 0
    )


def test_the_translated_passage_renders_its_reading_aid_subordinately():
    assert render_passages_block([fx.TRANSLATED_CLAIM]) == block(
        "4.3 `{{passages_block}}`", "text", 1
    )


# --------------------------------------------------------------------------
# Section 13.2's cell record, validated as the fixture it claims to be
# --------------------------------------------------------------------------

CELL_RECORD = json.loads(
    block("13.2 The review overlay is per requirement", "json")
)


def test_the_section_13_2_machine_result_passes_every_section_9_check():
    """The failure section 15.3 names by name: this block once could not.

    Validated against the section 2 decomposition and the section 4.3 passages,
    which is what makes every ``exact_quote`` in it checkable by hand.
    """
    result = validate_response(
        CELL_RECORD["machine_result"],
        decomposition=fx.DECOMPOSITION,
        publication_number=fx.PUBLICATION_NUMBER,
        passages=fx.CANONICAL_PASSAGES,
    )
    assert result.outcome == Outcome.PARTIALLY
    assert result.missing_element_ids == ("E3",)
    assert result.missing_relationship_ids == ("R1",)


def test_every_quote_in_the_section_13_2_record_is_a_substring_of_its_cited_passage():
    """Section 15.3 point 2, applied to the human half of the record as well."""
    by_chunk = {passage.chunk_id: passage for passage in fx.CANONICAL_PASSAGES}

    quotes: list[tuple[str, str]] = []
    for item in CELL_RECORD["machine_result"]["elements"] + CELL_RECORD["machine_result"]["relationships"]:
        for entry in item["evidence"] + item["combination_basis"]:
            quotes.append((entry["chunk_id"], entry["exact_quote"]))
    for review in CELL_RECORD["review"]["element_reviews"] + CELL_RECORD["review"]["relationship_reviews"]:
        for entry in review["human_evidence"]:
            quotes.append((entry["chunk_id"], entry["exact_quote"]))

    assert quotes, "the record is supposed to carry quotations"
    for chunk_id, quote in quotes:
        assert chunk_id in by_chunk, chunk_id
        assert quote in by_chunk[chunk_id].text, (chunk_id, quote)


def _overlay_from(record: dict) -> ReviewOverlay:
    def reviews(entries: list[dict], key: str) -> tuple[RequirementReview, ...]:
        return tuple(
            RequirementReview(
                requirement_id=entry[key],
                machine_status=Status(entry["machine_status"]),
                reviewed_status=Status(entry["reviewed_status"]),
                evidence_action=EvidenceAction(entry["evidence_action"]),
                human_evidence=tuple(EvidenceItem(**item) for item in entry["human_evidence"]),
                reviewer_comment=entry["reviewer_comment"],
                overridden=entry["overridden"],
            )
            for entry in entries
        )

    review = record["review"]
    return ReviewOverlay(
        status=ReviewStatus(review["status"]),
        element_reviews=reviews(review["element_reviews"], "element_id"),
        relationship_reviews=reviews(review["relationship_reviews"], "relationship_id"),
        reviewed_outcome=Outcome(review["reviewed_outcome"]),
        reviewer_id=review["reviewer_id"],
        reviewed_at=review["reviewed_at"],
        source_result_version=review["source_result_version"],
        source_cell_id=review["source_cell_id"],
        requests_for_more_context=tuple(review["requests_for_more_context"]),
        waiver=review["waiver"],
        approved_via_bulk_action=review["approved_via_bulk_action"],
    )


def test_the_section_13_2_review_overlay_passes_every_section_13_4_check():
    machine = validate_response(
        CELL_RECORD["machine_result"],
        decomposition=fx.DECOMPOSITION,
        publication_number=fx.PUBLICATION_NUMBER,
        passages=fx.CANONICAL_PASSAGES,
    )
    overlay = _overlay_from(CELL_RECORD)
    validate_review(
        overlay,
        cell_state=CellState.VERIFIED,
        # The record's own identity, not values invented for the test: section
        # 13.4 checks 15-16 compare the overlay's source_* fields to these, so
        # reading both sides out of the fixture is what makes the check real.
        cell_id=CELL_RECORD["cell_id"],
        result_version=CELL_RECORD["result_version"],
        machine_result=machine,
        decomposition=fx.DECOMPOSITION,
        publication_number=fx.PUBLICATION_NUMBER,
        passages=fx.CANONICAL_PASSAGES,
    )
    assert overlay.source_cell_id == CELL_RECORD["cell_id"]
    assert overlay.source_result_version == CELL_RECORD["result_version"]
    # Section 15.3 point 2: reviewed_outcome must equal what derive_outcome
    # returns, through the section 13.3 path.
    assert reviewed_outcome(machine, overlay) == Outcome(CELL_RECORD["review"]["reviewed_outcome"])
    # E1's citation was replaced without its status changing, which is still an
    # override - so the record's OVERRIDDEN status is the derived one.
    assert overlay.status == ReviewStatus.OVERRIDDEN
    assert overlay.element_reviews[0].overridden is True


def test_the_section_13_2_record_keeps_retrieval_scores_out_of_the_prompt_and_in_the_audit():
    """Spec section 5: scores are first-class application data and never evidence."""
    context = CELL_RECORD["machine_context"]
    assert set(context["retrieval_scores"]) == {passage.chunk_id for passage in fx.CANONICAL_PASSAGES}
    rendered = render_passages_block(fx.CANONICAL_PASSAGES)
    for score in context["retrieval_scores"].values():
        assert str(score) not in rendered


def test_the_section_13_5_audit_record_carries_everything_an_audit_needs():
    record = json.loads(block("13.5 Every human change writes an audit record", "json"))
    for required in (
        "audit_id", "cell_id", "workflow_stage", "reviewer_id", "at",
        "field", "previous_value", "new_value", "reason", "source_result_version",
    ):
        assert record[required], required
    # Requirement-level changes additionally name which requirement changed, so
    # an audit reader can reconstruct the status rather than only the cell.
    assert record["requirement_type"] in ("ELEMENT", "RELATIONSHIP")
    assert record["requirement_id"] in (*fx.DECOMPOSITION.element_ids, *fx.DECOMPOSITION.relationship_ids)
    assert Status(record["previous_value"]) and Status(record["new_value"])


# --------------------------------------------------------------------------
# Section 6.2's reference implementation is the one that shipped
# --------------------------------------------------------------------------

def test_the_section_6_1_table_and_the_shipped_derivation_agree():
    """Parsed straight out of the specification's enumeration table.

    A table row and an implementation that disagree is exactly the drift section
    15.3 exists to catch, and the table is machine-readable, so there is no reason
    to transcribe it by hand.
    """
    text = SPEC_PATH.read_text(encoding="utf-8")
    table = text.split("### 6.1 Complete enumeration", 1)[1].split("Consequences worth", 1)[0]
    rows = re.findall(
        r"^\| (> 0|0) \| (> 0|0) \| (> 0|0) \| \d \| `(\w+)` \|$", table, flags=re.MULTILINE
    )
    assert len(rows) == 7, rows

    counts = {"0": 0, "> 0": 2}
    for found, missing, unclear, expected in rows:
        statuses = (
            [Status.FOUND] * counts[found]
            + [Status.NOT_FOUND_IN_SUPPLIED_PASSAGES] * counts[missing]
            + [Status.UNCLEAR] * counts[unclear]
        )
        assert derive_outcome(statuses) == Outcome(expected), (found, missing, unclear)
