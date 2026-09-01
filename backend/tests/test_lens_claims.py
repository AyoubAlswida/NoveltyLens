"""Lens.org claim parsing: the evidence layer's locators depend on it.

``_parse_claims`` exists so an evidence cell can say "claim 4". These tests pin
the two properties that makes possible - claims stay separate, and each keeps
its own number - plus the multi-language bug that flattening hid.
"""

import pytest

from models import PatentClaim
from search.lens import ClaimNumberingError, _parse_claims, _to_patent_record


def _group(lang: str, *claim_texts: str) -> dict:
    """One per-language claims group in Lens's response shape."""
    return {
        "lang": lang,
        "claims": [{"claim_text": [text]} for text in claim_texts],
    }


def test_claims_stay_separate_and_keep_their_numbers():
    claims = _parse_claims([_group("en", "1. A method of cooling.", "2. The method of claim 1.")])

    assert claims == [
        PatentClaim(number=1, text="1. A method of cooling."),
        PatentClaim(number=2, text="2. The method of claim 1."),
    ]


def test_claim_number_comes_from_the_document_not_position():
    """A provider that omits or reorders a claim must not silently renumber it:
    claim 7 stays claim 7 even when it arrives first."""
    claims = _parse_claims([_group("en", "7. A controller.", "8. The controller of claim 7.")])

    assert [claim.number for claim in claims] == [7, 8]


def test_position_is_the_fallback_when_text_carries_no_number():
    claims = _parse_claims([_group("en", "A method of cooling.", "The method above.")])

    assert [claim.number for claim in claims] == [1, 2]


def test_only_one_language_group_is_read():
    """The groups are translations of each other, not extra claims. Reading all
    of them triplicated an EP patent's claims into full_text and made the
    numbering meaningless."""
    claims = _parse_claims(
        [
            _group("de", "1. Ein Verfahren zur Kuehlung."),
            _group("en", "1. A method of cooling."),
        ]
    )

    assert len(claims) == 1
    assert claims[0].text == "1. A method of cooling."


def test_first_group_is_used_when_no_english_group_exists():
    claims = _parse_claims([_group("de", "1. Ein Verfahren zur Kuehlung.")])

    assert [claim.text for claim in claims] == ["1. Ein Verfahren zur Kuehlung."]


def test_claim_text_fragments_join_within_one_claim_only():
    """``claim_text`` is a list of line fragments of a single claim."""
    claims = _parse_claims(
        [{"lang": "en", "claims": [{"claim_text": ["1. A method", "comprising cooling."]}]}]
    )

    assert claims == [PatentClaim(number=1, text="1. A method comprising cooling.")]


def test_malformed_claims_yield_no_claims_rather_than_raising():
    assert _parse_claims(None) == []
    assert _parse_claims([]) == []
    assert _parse_claims(["not a dict"]) == []
    assert _parse_claims([{"lang": "en", "claims": [{"claim_text": []}]}]) == []


def test_record_carries_structured_claims_without_changing_full_text():
    """`full_text` is what compare.py, vector_store.py and the drop-if-no-text
    rule all read. The structured view is additive, not a replacement."""
    record = _to_patent_record(
        {
            "lens_id": "123-456",
            "jurisdiction": "US",
            "doc_number": "1234567",
            "kind": "B2",
            "date_published": "2020-01-01",
            "biblio": {"invention_title": [{"lang": "en", "text": "Cooling system"}]},
            "abstract": [{"lang": "en", "text": "An abstract."}],
            "description": [{"lang": "en", "text": "A description."}],
            "claims": [_group("en", "1. A method of cooling.")],
        }
    )

    assert record.id == "US1234567B2"
    assert record.full_text == "A description. 1. A method of cooling."
    assert record.description == "A description."
    assert record.claims == [PatentClaim(number=1, text="1. A method of cooling.")]


def test_claims_are_empty_not_absent_for_a_patent_lens_returned_none_for():
    record = _to_patent_record(
        {
            "lens_id": "123-456",
            "jurisdiction": "US",
            "doc_number": "1234567",
            "kind": "B2",
            "description": [{"lang": "en", "text": "A description."}],
        }
    )

    assert record.claims == []
    assert record.full_text == "A description."


# --------------------------------------------------------------------------
# Numbering: the positional fallback must never collide with a stated number
# --------------------------------------------------------------------------

def test_a_fully_numbered_set_is_unaffected():
    """The common case, and the one the two-pass resolution must not change."""
    claims = _parse_claims(
        [_group("en", "1. A method.", "2. The method of claim 1.", "3. The method of claim 2.")]
    )

    assert [claim.number for claim in claims] == [1, 2, 3]


def test_a_fully_unnumbered_set_falls_back_to_position():
    """Nothing states a number, so nothing can collide with a position."""
    claims = _parse_claims([_group("en", "A method.", "A further method.", "A third method.")])

    assert [claim.number for claim in claims] == [1, 2, 3]


def test_mixed_numbered_and_unnumbered_claims_are_allowed_when_they_do_not_collide():
    """The fallback survives where it is unambiguous - that is the whole point of
    resolving numbering rather than banning the mix."""
    claims = _parse_claims([_group("en", "5. A method.", "A further method.")])

    # Position 2 is free: no claim states claim 2.
    assert [claim.number for claim in claims] == [5, 2]


def test_a_positional_fallback_that_collides_with_a_stated_number_raises():
    """The defect this suite section exists for.

    ``["2. A method.", "A further method."]`` used to yield two claims both
    numbered 2 - two locators reading "claim 2" and two identical chunk IDs. A
    locator that names the wrong claim is fabricated evidence with a citation
    attached, so the provider call fails instead.
    """
    with pytest.raises(ClaimNumberingError) as excinfo:
        _parse_claims([_group("en", "2. A method.", "A further method.")])

    assert "position 2" in str(excinfo.value)


def test_duplicate_explicit_claim_numbers_raise():
    with pytest.raises(ClaimNumberingError) as excinfo:
        _parse_claims([_group("en", "1. A method.", "1. A different method.")])

    assert "both numbered 1" in str(excinfo.value)


def test_no_number_is_invented_to_dodge_a_collision():
    """Allocating an unused number would make the data pass and the locators lie.

    Here the unnumbered claim sits at position 3 while another claim states claim
    3, so the fallback collides - and claim 1 is unused, which is exactly the
    number a "helpful" implementation would hand it. Nothing in the resulting set
    may carry a number the document does not state, so the only correct outcome is
    a raise.
    """
    with pytest.raises(ClaimNumberingError):
        _parse_claims([_group("en", "2. A method.", "3. Another.", "A third method.")])


def test_an_unnumbered_claim_after_a_numbered_run_resolves_when_its_position_is_free():
    """The mirror of the case above, and the reason the check is a collision test
    rather than a ban on mixing: positions 1 and 2 are stated, position 3 is not."""
    claims = _parse_claims([_group("en", "1. A method.", "2. Another.", "A third method.")])

    assert [claim.number for claim in claims] == [1, 2, 3]


def test_a_reordered_claim_set_keeps_the_documents_numbering():
    """Claim 7 arriving first is still claim 7 - and position 1 being free of any
    stated number is what makes the unnumbered case below resolvable too."""
    claims = _parse_claims([_group("en", "7. A controller.", "8. The controller of claim 7.")])

    assert [claim.number for claim in claims] == [7, 8]


def test_a_partial_claim_set_keeps_its_gaps():
    claims = _parse_claims([_group("en", "4. A method.", "9. Another method.")])

    assert [claim.number for claim in claims] == [4, 9]


def test_numbering_is_resolved_within_one_language_group_only():
    """The groups are translations of each other. Reading both would see claim 1
    twice and raise on a set that is perfectly well numbered."""
    claims = _parse_claims([_group("de", "1. Ein Verfahren."), _group("en", "1. A method.")])

    assert [claim.number for claim in claims] == [1]


def test_an_ambiguous_claim_set_fails_the_record_rather_than_being_silently_dropped():
    """Zero-tolerance: the error propagates out of _to_patent_record.

    Returning a record with no claims would leave `full_text` short of its claims
    and let the drop-if-no-full-text rule discard the patent for the wrong
    reason - a silent degradation dressed as a coverage result.
    """
    with pytest.raises(ClaimNumberingError):
        _to_patent_record(
            {
                "lens_id": "123-456",
                "jurisdiction": "US",
                "doc_number": "1234567",
                "kind": "B2",
                "description": [{"lang": "en", "text": "A description."}],
                "claims": [_group("en", "2. A method.", "A further method.")],
            }
        )


def test_a_successfully_parsed_claim_set_never_carries_a_duplicate_number():
    """The property the raises above exist to guarantee.

    Spec section 17.1 derives a passage's identity from the claim number -
    ``chunk_id: PAT-{pub}-CLM-{number:03d}``, ``locator: claim {number}`` - so a
    duplicate number is a duplicate chunk ID and two passages that cannot be told
    apart. This asserts the invariant directly rather than trusting the cases
    above to be exhaustive.
    """
    resolvable = [
        ("1. A method.", "2. Another.", "3. A third."),
        ("A method.", "A further method."),
        ("5. A method.", "A further method."),
        ("7. A controller.", "8. The controller of claim 7."),
        ("4. A method.", "9. Another method."),
        ("1. A method.", "2. Another.", "A third method."),
    ]
    for texts in resolvable:
        claims = _parse_claims([_group("en", *texts)])
        numbers = [claim.number for claim in claims]
        assert len(set(numbers)) == len(numbers), texts
        assert all(number >= 1 for number in numbers), texts

        chunk_ids = [f"PAT-US1234567B2-CLM-{number:03d}" for number in numbers]
        assert len(set(chunk_ids)) == len(chunk_ids), texts
