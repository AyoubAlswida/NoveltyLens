"""Passages, locators, segmentation and derived eligibility.

**A locator is a citation into a legal document.** Most of these tests are about
what is *never* emitted: a paragraph number the provider did not supply, a claim
number nobody stated, a passage from an abstract or a snippet, a chunk id that
collides with another, an "eligible" publication with nothing to cite.

**Evidence comes from EPO and nowhere else.** An earlier version built claim
passages from `PatentRecord.claims` -- *Lens* discovery metadata -- and stamped
them `source="EPO OPS"` with an `epo:` document id. That is a citation to an EPO
document attached to text EPO never served, and it fired even when EPO returned
no claims section at all. `build_passages` now takes no `PatentRecord`, which is
the strongest form that rule can take.

**No outbound call is made.** Every `EnrichmentResult` is constructed directly.
"""

import hashlib

import pytest

from evidence.eligibility import (
    ExclusionState,
    confirm_eligibility,
    is_substantive_description,
)
from evidence.passages import (
    LocatorSource,
    PassageBuildError,
    build_claim_passages,
    build_description_passages,
    build_passages,
    chunk_id_for_claim,
)
from evidence.segmenter import (
    MAX_SEGMENT_CHARS,
    MIN_SEGMENT_CHARS,
    SEGMENTER_VERSION,
    segment_description,
)
from models import (
    EnrichmentResult,
    EnrichmentStatus,
    LocatorType,
    PatentClaim,
    PatentRecord,
    ProviderClaim,
    SectionText,
    TextVersionSource,
)
from verification.passages import canonicalize, validate_passage_set

PARAGRAPH = (
    "In one embodiment, heat exchanger 120 is mounted inside exhaust duct 110 and "
    "receives coolant from pump 130. Controller 140 adjusts the coolant flow rate "
    "according to a stored duty schedule, which is calibrated during manufacture "
    "and stored in non-volatile memory accessible to the controller at runtime. "
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _section(name, text, *, paragraph_ids=(), language="en", claims=()) -> SectionText:
    return SectionText(
        section=name,
        text=text,
        text_sha256=_sha(text),
        language=language,
        paragraph_ids=tuple(paragraph_ids),
        claims=tuple(claims),
    )


def _epo_claims(*numbered, language="en") -> SectionText:
    """A CLAIMS section as EPO actually serves it: structured claims carrying the
    document's own numbers, with offsets into the exact section text.

    **The only permitted source of claim evidence.** A test that built claim
    passages from `PatentRecord.claims` would be asserting the defect rather than
    the fix.
    """
    texts = [text for _, text in numbered]
    section_text = "\n\n".join(texts)
    claims = []
    cursor = 0
    for number, text in numbered:
        start = section_text.index(text, cursor)
        cursor = start + len(text)
        claims.append(
            ProviderClaim(number=number, text=text, char_start=start, char_end=cursor)
        )
    return _section("CLAIMS", section_text, language=language, claims=claims)


def _enrichment(
    *,
    sections=(),
    status=EnrichmentStatus.TEXT,
    served="EP4778509",
    normalized="EP4778509",
    version="2019-06-01",
    version_source=TextVersionSource.PROVIDER_PUBLICATION_DATE,
    available=None,
) -> EnrichmentResult:
    retrieved = tuple(s.section for s in sections)
    return EnrichmentResult(
        requested_publication="EP4778509A1",
        normalized_publication=normalized,
        served_publication=served,
        source_document_id=f"epo:{normalized}",
        status=status,
        sections=tuple(sections),
        sections_available=tuple(available if available is not None else retrieved),
        sections_retrieved=retrieved,
        language_attempted="en",
        text_version_original="20190601",
        text_version_normalized=version,
        text_version_source=version_source,
        retrieved_at="2026-08-28T12:00:00+00:00",
    )


def _lens_record(claims=()) -> PatentRecord:
    """A discovery record, with Lens claim text. Present in these tests only to
    prove it is **not** used as evidence."""
    return PatentRecord(
        id="EP4778509A1",
        title="A closed-loop exhaust heat recovery system",
        source="Lens.org",
        claims=list(claims),
    )


# --- segmentation ----------------------------------------------------------


def test_segmentation_is_deterministic():
    """Same string in, same segments out. That is what makes a `SOURCE_RANGE`
    locator reproducible: re-fetch the same text version, slice the same offsets,
    get the same passage."""
    text = PARAGRAPH * 3
    assert segment_description(text) == segment_description(text)


def test_every_segment_reproduces_itself_from_its_offsets():
    """The invariant a `SOURCE_RANGE` locator rests on. A locator whose offsets
    do not reproduce its own text is a fabricated citation with a
    plausible-looking address."""
    text = "\n\n".join(PARAGRAPH for _ in range(4))
    for segment in segment_description(text):
        assert text[segment.start : segment.end] == segment.text


def test_empty_text_produces_no_segments_rather_than_one_empty_segment():
    assert segment_description("") == []
    assert segment_description("   \n\n  \t ") == []


def test_a_fragment_is_merged_forward_rather_than_cited_alone():
    """A 40-character fragment is a heading or a figure caption, not a citable
    unit -- and it would waste one of the verifier's five passage slots."""
    text = "FIELD OF THE INVENTION\n\n" + PARAGRAPH * 2
    segments = segment_description(text)
    assert all(
        segment.length >= MIN_SEGMENT_CHARS or segment is segments[-1]
        for segment in segments
    )
    assert "FIELD OF THE INVENTION" in segments[0].text


def test_an_over_long_paragraph_is_split_at_a_sentence_boundary():
    """Splitting mid-sentence would produce a passage nothing can quote."""
    segments = segment_description(PARAGRAPH * 30)
    assert len(segments) > 1
    for segment in segments[:-1]:
        assert segment.length <= MAX_SEGMENT_CHARS
        assert segment.text.rstrip().endswith((".", "!", "?"))


def test_offsets_never_point_at_whitespace():
    text = "\n\n   " + PARAGRAPH + "   \n\n"
    for segment in segment_description(text):
        assert segment.text == segment.text.strip()


# --- claim evidence comes from EPO, never from discovery -------------------


def test_lens_claims_with_an_epo_description_produce_no_claim_passages():
    """**The B2 defect, pinned.** EPO returned a description and no claims; Lens
    has claims. The old builder produced EPO-attributed claim passages out of
    Lens text. It must now produce none."""
    enrichment = _enrichment(sections=[_section("DESCRIPTION", PARAGRAPH * 2)])
    built = build_passages(enrichment)

    assert all(entry.passage.section == "DESCRIPTION" for entry in built)
    assert built, "the description passages are still built"
    assert build_claim_passages(enrichment) == []


def test_lens_claims_alone_produce_nothing_at_all():
    """EPO returned nothing. Lens claims cannot make a publication eligible."""
    enrichment = _enrichment(sections=[], status=EnrichmentStatus.NO_TEXT, available=())
    assert build_passages(enrichment) == []
    result = confirm_eligibility(enrichment, [])
    assert result.exclusion.state is ExclusionState.EXCLUDED_NO_FULL_TEXT


def test_epo_claims_produce_claim_passages_from_epo_text():
    built = build_passages(
        _enrichment(
            sections=[
                _epo_claims(
                    (1, "A system comprising a heat exchanger."),
                    (4, "The system of claim 1, wherein the duct is round."),
                )
            ]
        )
    )
    assert [entry.passage.locator for entry in built] == ["claim 1", "claim 4"]
    assert [entry.passage.chunk_id for entry in built] == [
        "PAT-EP4778509-CLM-001",
        "PAT-EP4778509-CLM-004",
    ]
    assert all(entry.locator_kind is LocatorType.CLAIM_NUMBER for entry in built)
    assert all(
        entry.locator_source == LocatorSource.PROVIDER_CLAIM_NUMBER for entry in built
    )
    assert built[0].passage.text == "A system comprising a heat exchanger."


def test_when_lens_and_epo_disagree_the_epo_text_is_used():
    """Two providers, two strings, one authoritative source. A passage carrying
    an EPO document id must carry EPO's text -- otherwise the citation says
    "EPO document X, claim 1" over words EPO never served."""
    epo_text = "A system comprising a heat exchanger disposed within an exhaust duct."
    lens_text = "A completely different sentence Lens returned for claim 1."
    record = _lens_record(claims=[PatentClaim(number=1, text=lens_text)])

    built = build_passages(_enrichment(sections=[_epo_claims((1, epo_text))]))

    assert built[0].passage.text == epo_text
    assert lens_text not in built[0].passage.text
    assert record.claims[0].text == lens_text, "the discovery record is untouched"


def test_a_claims_section_with_no_per_claim_structure_produces_no_passages():
    """A flat claims blob has no per-claim structure, so no claim can be located
    honestly. Numbering it by position would invent the numbering."""
    flat = _section("CLAIMS", "1. A system. 2. The system of claim 1.", claims=())
    assert build_claim_passages(_enrichment(sections=[flat])) == []


def test_missing_claim_numbers_are_never_invented():
    """The parser refuses upstream; this asserts the builder has no fallback that
    would number by position if it ever received an unnumbered claim."""
    from search.epo_fulltext import _parse_claims_section
    import xml.etree.ElementTree as ET

    from search.lens import ClaimNumberingError

    xml = (
        '<claims xmlns="http://www.epo.org/fulltext" lang="EN">'
        "<claim><claim-text>A system comprising a duct.</claim-text></claim>"
        "</claims>"
    )
    with pytest.raises(ClaimNumberingError) as caught:
        _parse_claims_section(ET.fromstring(xml), "en")
    assert "invent" in str(caught.value)


def test_duplicate_provider_claim_numbers_are_rejected():
    """Two passages sharing a chunk id would silently collapse at
    `validate_passage_set`'s uniqueness check, and a requirement would lose its
    evidence with no error anywhere."""
    duplicated = _epo_claims((2, "First claim text."), (2, "Second claim text."))
    with pytest.raises(PassageBuildError) as caught:
        build_passages(_enrichment(sections=[duplicated]))
    assert "duplicate claim number 2" in str(caught.value)


def test_duplicate_claim_numbers_are_rejected_at_the_parser_too():
    import xml.etree.ElementTree as ET

    from search.epo_fulltext import _parse_claims_section
    from search.lens import ClaimNumberingError

    xml = (
        '<claims xmlns="http://www.epo.org/fulltext" lang="EN">'
        '<claim num="2"><claim-text>First.</claim-text></claim>'
        '<claim num="2"><claim-text>Second.</claim-text></claim>'
        "</claims>"
    )
    with pytest.raises(ClaimNumberingError) as caught:
        _parse_claims_section(ET.fromstring(xml), "en")
    assert "both numbered 2" in str(caught.value)


def test_genuine_claim_numbers_are_preserved_not_renumbered_by_position():
    """A retrieved claim set starting at claim 7 stays claim 7. Renumbering would
    cite claim 1 for text that is claim 7 -- a wrong citation with a confident
    address."""
    built = build_passages(
        _enrichment(sections=[_epo_claims((7, "The system of claim 1."))])
    )
    assert built[0].passage.locator == "claim 7"
    assert built[0].passage.chunk_id == "PAT-EP4778509-CLM-007"


def test_source_and_document_id_match_the_actual_evidence_source():
    built = build_passages(_enrichment(sections=[_epo_claims((1, "A system."))]))
    passage = built[0].passage
    assert passage.source == "EPO OPS"
    assert passage.source_document_id == "epo:EP4778509"


def test_every_claim_passage_is_traceable_into_the_exact_epo_section():
    """Offsets index the exact retrieved claims text, whose SHA-256 the passage
    records -- so a reviewer can slice the stored range back out."""
    section = _epo_claims(
        (1, "A system comprising a heat exchanger."),
        (2, "The system of claim 1, wherein the duct is round."),
    )
    built = build_passages(_enrichment(sections=[section]))
    for entry in built:
        assert entry.section_text_sha256 == section.text_sha256
        sliced = section.text[entry.char_start : entry.char_end]
        assert canonicalize(sliced) == entry.passage.text


def test_an_empty_claim_produces_no_passage():
    section = _section(
        "CLAIMS",
        "Real text.",
        claims=[
            ProviderClaim(number=1, text="   ", char_start=0, char_end=3),
            ProviderClaim(number=2, text="Real text.", char_start=0, char_end=10),
        ],
    )
    built = build_passages(_enrichment(sections=[section]))
    assert [entry.passage.locator for entry in built] == ["claim 2"]


# --- description passages: provider numbering ------------------------------


def test_provider_paragraph_ids_produce_epo_paragraph_locators():
    text = PARAGRAPH.strip() + "\n\n" + PARAGRAPH.strip()
    built = build_description_passages(
        _enrichment(
            sections=[_section("DESCRIPTION", text, paragraph_ids=("0030", "0031"))]
        )
    )
    assert [entry.passage.locator for entry in built] == [
        "paragraph [0030]",
        "paragraph [0031]",
    ]
    assert all(entry.locator_kind is LocatorType.EPO_PARAGRAPH for entry in built)
    assert all(entry.char_start is None for entry in built)


def test_partial_provider_numbering_falls_back_to_ranges_for_all_of_them():
    """A partial mapping would number some passages and range others, reading as
    if the unnumbered ones were somehow lesser rather than simply unnumbered."""
    text = PARAGRAPH.strip() + "\n\n" + PARAGRAPH.strip()
    built = build_description_passages(
        _enrichment(sections=[_section("DESCRIPTION", text, paragraph_ids=("0030",))])
    )
    assert all(entry.locator_kind is LocatorType.SOURCE_RANGE for entry in built)


def test_no_provider_numbering_produces_source_range_locators():
    built = build_description_passages(
        _enrichment(sections=[_section("DESCRIPTION", PARAGRAPH * 2)])
    )
    assert built
    for entry in built:
        assert entry.locator_kind is LocatorType.SOURCE_RANGE
        assert entry.locator_source == LocatorSource.DERIVED_SOURCE_OFFSET
        assert entry.segmenter_version == SEGMENTER_VERSION
        assert entry.char_start is not None and entry.char_end > entry.char_start


def test_no_invented_paragraph_number_is_ever_emitted():
    """A range that says what it is beats a paragraph number that lies."""
    built = build_description_passages(
        _enrichment(sections=[_section("DESCRIPTION", PARAGRAPH * 2)])
    )
    for entry in built:
        assert "paragraph [" not in entry.passage.locator
        assert entry.passage.locator.startswith("description characters ")


def test_stored_offsets_slice_the_stored_section_text_back_out():
    text = PARAGRAPH * 3
    section = _section("DESCRIPTION", text)
    for entry in build_description_passages(_enrichment(sections=[section])):
        assert entry.section_text_sha256 == _sha(text)
        assert canonicalize(text[entry.char_start : entry.char_end]) == entry.passage.text


# --- stable identity across reruns -----------------------------------------


def test_rerunning_over_the_same_text_yields_byte_identical_chunk_ids():
    enrichment = _enrichment(
        sections=[_epo_claims((1, "A system.")), _section("DESCRIPTION", PARAGRAPH * 2)]
    )
    first = [entry.passage.chunk_id for entry in build_passages(enrichment)]
    second = [entry.passage.chunk_id for entry in build_passages(enrichment)]
    assert first == second


def test_changed_source_text_changes_the_hash():
    """Honest rather than stable: different text is a different passage."""
    original = _enrichment(sections=[_section("DESCRIPTION", PARAGRAPH * 2)])
    edited = _enrichment(
        sections=[
            _section("DESCRIPTION", "An added opening paragraph.\n\n" + PARAGRAPH * 2)
        ]
    )
    first = build_description_passages(original)
    second = build_description_passages(edited)
    assert first[0].section_text_sha256 != second[0].section_text_sha256
    assert {e.text_sha256 for e in first} != {e.text_sha256 for e in second}


def test_a_provider_numbered_id_is_stable_even_when_the_text_shifts():
    def ids(text):
        return [
            entry.passage.chunk_id
            for entry in build_description_passages(
                _enrichment(
                    sections=[
                        _section("DESCRIPTION", text, paragraph_ids=("0030", "0031"))
                    ]
                )
            )
        ]

    a = ids(PARAGRAPH.strip() + "\n\n" + PARAGRAPH.strip())
    b = ids(PARAGRAPH.strip() + " Extra sentence.\n\n" + PARAGRAPH.strip())
    assert a == b == ["PAT-EP4778509-DESC-P0030", "PAT-EP4778509-DESC-P0031"]


# --- what never becomes a passage ------------------------------------------


@pytest.mark.parametrize("section", ["ABSTRACT", "OTHER", "TITLE"])
def test_only_claims_and_description_ever_become_passages(section):
    """Refused on the *section*, not on a flag."""
    assert build_passages(_enrichment(sections=[_section(section, PARAGRAPH * 2)])) == []


def test_a_search_snippet_is_never_evidence():
    enrichment = _enrichment(
        sections=[_section("OTHER", "…heat exchanger disposed within an exhaust duct…")],
        available=("OTHER",),
    )
    assert build_passages(enrichment) == []
    assert confirm_eligibility(enrichment, []).exclusion.state is (
        ExclusionState.EXCLUDED_NO_FULL_TEXT
    )


def test_a_failed_enrichment_never_produces_passages():
    with pytest.raises(PassageBuildError) as caught:
        build_passages(_enrichment(status=EnrichmentStatus.FAILED))
    assert "FAILED" in str(caught.value)


def test_a_no_text_enrichment_produces_no_passages():
    assert build_passages(_enrichment(status=EnrichmentStatus.NO_TEXT)) == []


# --- the verifier's own contract -------------------------------------------


def test_every_built_passage_passes_the_verifier_validation_unchanged():
    """Spec §7.3, applied by `verification/passages.py` -- not weakened, not
    bypassed."""
    built = build_passages(
        _enrichment(
            sections=[
                _epo_claims((1, "A system comprising a duct.")),
                _section("DESCRIPTION", PARAGRAPH * 2),
            ]
        )
    )
    validate_passage_set(
        [entry.passage for entry in built][:5],
        publication_number="EP4778509",
        publication_document_ids={"epo:EP4778509"},
    )


def test_a_passage_from_another_document_fails_the_provenance_check():
    from verification.states import PassageSetIneligible

    built = build_passages(
        _enrichment(sections=[_epo_claims((1, "A system comprising a duct."))])
    )
    with pytest.raises(PassageSetIneligible) as caught:
        validate_passage_set(
            [entry.passage for entry in built],
            publication_number="EP4778509",
            publication_document_ids={"epo:EP9999999"},
        )
    assert caught.value.check


def test_no_llm_call_is_made_when_provenance_fails():
    from unittest.mock import patch

    from verification.states import PassageSetIneligible

    built = build_passages(
        _enrichment(sections=[_epo_claims((1, "A system comprising a duct."))])
    )
    with patch("llm._llm_chat") as chat:
        with pytest.raises(PassageSetIneligible):
            validate_passage_set(
                [entry.passage for entry in built],
                publication_number="EP4778509",
                publication_document_ids=set(),
            )
    chat.assert_not_called()


def test_every_required_field_is_non_empty():
    built = build_passages(_enrichment(sections=[_epo_claims((1, "A system."))]))
    passage = built[0].passage
    for field in (
        "chunk_id", "publication_number", "source_document_id", "source",
        "section", "locator", "language", "text_version_date", "text",
    ):
        assert getattr(passage, field), f"{field} must be non-empty (spec 7.3 check 5)"


def test_a_missing_text_version_refuses_to_build_a_passage():
    with pytest.raises(PassageBuildError) as caught:
        build_passages(
            _enrichment(
                sections=[_epo_claims((1, "A system."))],
                version="",
                version_source=TextVersionSource.NONE,
            )
        )
    assert "text_version_date" in str(caught.value)


# --- derived eligibility ---------------------------------------------------


def test_claims_only_is_eligible():
    """Claims alone are sufficient -- they are the operative technical scope."""
    enrichment = _enrichment(sections=[_epo_claims((1, "A system comprising a duct."))])
    result = confirm_eligibility(enrichment, build_passages(enrichment))
    assert result.eligible
    assert result.sections_with_passages == ("CLAIMS",)


def test_description_only_is_eligible_and_produces_citable_passages():
    """A description carries embodiment detail a claim may omit. Gate B must
    demonstrably support this case."""
    enrichment = _enrichment(sections=[_section("DESCRIPTION", PARAGRAPH * 2)])
    result = confirm_eligibility(enrichment, build_passages(enrichment))
    assert result.eligible
    assert result.sections_with_passages == ("DESCRIPTION",)
    assert all(entry.passage.locator for entry in result.passages)


def test_mixed_claims_and_description_is_eligible():
    enrichment = _enrichment(
        sections=[
            _epo_claims((1, "A system comprising a duct.")),
            _section("DESCRIPTION", PARAGRAPH * 2),
        ]
    )
    result = confirm_eligibility(enrichment, build_passages(enrichment))
    assert result.eligible
    assert set(result.sections_with_passages) == {"CLAIMS", "DESCRIPTION"}


def test_neither_claims_nor_description_is_excluded_no_full_text():
    enrichment = _enrichment(sections=[], status=EnrichmentStatus.NO_TEXT, available=())
    result = confirm_eligibility(enrichment, [])
    assert not result.eligible
    assert result.exclusion.state is ExclusionState.EXCLUDED_NO_FULL_TEXT
    assert result.exclusion.passages_built == 0


def test_abstract_only_is_excluded_no_full_text():
    enrichment = _enrichment(
        sections=[_section("ABSTRACT", "A heat recovery system is disclosed.")],
        available=("ABSTRACT",),
    )
    result = confirm_eligibility(enrichment, build_passages(enrichment))
    assert result.exclusion.state is ExclusionState.EXCLUDED_NO_FULL_TEXT
    assert "ABSTRACT" in " ".join(result.exclusion.sections_available)


def test_borrowed_family_text_is_excluded_ineligible():
    """A family member is a different document with different content and a
    different date."""
    enrichment = _enrichment(
        sections=[_epo_claims((1, "A system."))],
        served="EP9999999",
        normalized="EP4778509",
    )
    result = confirm_eligibility(enrichment, [])
    assert result.exclusion.state is ExclusionState.EXCLUDED_INELIGIBLE
    assert "EP9999999" in result.exclusion.reason
    assert result.exclusion.failing_check


def test_a_kind_code_variant_is_not_borrowed_text():
    """`EP4778509A1` served for a request for `EP4778509` is the same document at
    a different publication stage -- not a family member."""
    enrichment = _enrichment(
        sections=[_epo_claims((1, "A system."))], served="EP4778509A1"
    )
    assert confirm_eligibility(enrichment, build_passages(enrichment)).eligible


def test_a_different_family_member_number_is_not_the_same_publication():
    """The guard is about identity, not similarity: an adjacent number under the
    same office is a different document."""
    enrichment = _enrichment(
        sections=[_epo_claims((1, "A system."))],
        served="EP4778510",
        normalized="EP4778509",
    )
    assert confirm_eligibility(enrichment, []).exclusion.state is (
        ExclusionState.EXCLUDED_INELIGIBLE
    )


def test_no_trustworthy_text_version_is_excluded_ineligible():
    enrichment = _enrichment(
        sections=[_epo_claims((1, "A system."))],
        version="",
        version_source=TextVersionSource.NONE,
    )
    result = confirm_eligibility(enrichment, [])
    assert result.exclusion.state is ExclusionState.EXCLUDED_INELIGIBLE
    assert "text version" in result.exclusion.reason


def test_text_retrieved_but_no_locator_producible_is_excluded_ineligible():
    """**Never "eligible" with an empty passage set.** The row says exactly why:
    text was there, a citable locator was not."""
    enrichment = _enrichment(sections=[_section("DESCRIPTION", "   ")])
    result = confirm_eligibility(enrichment, [])
    assert result.exclusion.state is ExclusionState.EXCLUDED_INELIGIBLE
    assert result.exclusion.sections_retrieved == ("DESCRIPTION",)
    assert result.exclusion.passages_built == 0


def test_a_failed_enrichment_has_no_eligibility_answer():
    with pytest.raises(ValueError) as caught:
        confirm_eligibility(_enrichment(status=EnrichmentStatus.FAILED), [])
    assert "never an exclusion" in str(caught.value)


def test_eligible_with_no_passages_is_structurally_impossible():
    from evidence.eligibility import EligibilityResult

    with pytest.raises(ValueError):
        EligibilityResult(eligible=True, passages=())


def test_the_substantive_description_heuristic_can_only_reject():
    assert not is_substantive_description("See the cross-reference above.")
    assert not is_substantive_description("FIG. 1 is a diagram. FIG. 2 is a chart.")
    assert is_substantive_description(PARAGRAPH * 2)
