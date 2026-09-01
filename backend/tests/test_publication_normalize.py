"""Publication-number normalization, identity, and the refusal to guess.

**Why this is correctness rather than tidiness.** EPO's full-text endpoints
return 404 for a kind-suffixed identifier and 200 for the bare epodoc form --
`EP4778509`, not `EP4778509A1`. Discovery returns the suffixed form. Without
normalization every enrichment call 404s and every candidate falls to
`EXCLUDED_NO_FULL_TEXT`: a total failure indistinguishable from a legitimate
coverage result.

**And why it refuses rather than guesses.** A publication number quietly
"corrected" into a different real document would attach that document's text to
this candidate -- fabricated evidence with a correct-looking citation.
"""

import pytest

from evidence.normalize import (
    PublicationNumber,
    PublicationNumberError,
    deduplicate,
    normalize,
    same_publication,
    try_normalize,
)


@pytest.mark.parametrize(
    "raw,expected,kind",
    [
        ("US1234567B2", "US1234567", "B2"),
        ("EP4778509A1", "EP4778509", "A1"),
        ("EP4778509", "EP4778509", ""),
        ("CN110000000A", "CN110000000", "A"),
        ("JP2020123456A", "JP2020123456", "A"),
        ("WO2020123456", "WO2020123456", ""),
        # Shapes discovery actually emits.
        ("US 11,234,567 B2", "US11234567", "B2"),
        ("WO2020/123456", "WO2020123456", ""),
        ("us1234567b2", "US1234567", "B2"),
        ("  EP4778509A1  ", "EP4778509", "A1"),
        ("EP-4778509-A1", "EP4778509", "A1"),
    ],
)
def test_normalization_strips_the_kind_code_and_separators(raw, expected, kind):
    publication = normalize(raw)
    assert publication.normalized == expected
    assert publication.kind == kind
    assert publication.requested == raw.strip(), "the requested form is kept verbatim"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("USD123456S", "USD123456"),
        ("USRE45678E", "USRE45678"),
        ("USPP12345P2", "USPP12345"),
    ],
)
def test_a_leading_document_class_is_not_mistaken_for_a_kind_code(raw, expected):
    """`D` (design), `RE` (reissue) and `PP` (plant) lead the document number.

    Stripping them as kind codes would produce a *different real publication* --
    `US123456` is not `USD123456` -- and query the wrong document. The rule that
    prevents it is that a kind code only ever follows a digit.
    """
    assert normalize(raw).normalized == expected


@pytest.mark.parametrize(
    "bad", ["", "   ", "1234567", "XYZ", "US", "notapatent", "12US3456", "-"]
)
def test_an_unparseable_number_is_refused_not_guessed(bad):
    with pytest.raises(PublicationNumberError):
        normalize(bad)
    assert try_normalize(bad) is None


def test_the_source_document_id_binds_to_the_normalized_publication():
    """`verification/passages.py`'s check 3 resolves this back to the evaluated
    publication, which is what refuses borrowed family text."""
    assert normalize("US1234567B2").source_document_id == "epo:US1234567"


# --- identity: collisions and non-collisions -------------------------------


def test_kind_code_variants_are_the_same_publication():
    """`US1234567B2` and `US1234567A1` are the same document at two publication
    stages, and EPO serves one full text for the pair. Merging them is
    deliberate."""
    assert same_publication("US1234567B2", "US1234567A1")
    assert same_publication("US1234567B2", "US1234567")


@pytest.mark.parametrize(
    "left,right,why",
    [
        ("US1234567", "US1234568", "adjacent numbers are unrelated documents"),
        ("US1234567", "EP1234567", "the same number under two offices is two documents"),
        ("US1234567", "US12345678", "a longer number is a different document"),
        ("USD123456", "US123456", "a design patent is not its utility namesake"),
    ],
)
def test_different_publications_never_collide(left, right, why):
    assert not same_publication(left, right), why


def test_an_unparseable_number_is_equal_to_nothing():
    """Returning True on a parse failure would be the borrowed-family-text bug
    wearing a different hat: it would let a served identity nobody could read
    pass the served-vs-requested check."""
    assert not same_publication("???", "???")
    assert not same_publication("US1234567", "garbage")


# --- deduplication ---------------------------------------------------------


def test_duplicates_collapse_and_the_count_is_reported():
    """A sample of 50 rows that is 30 distinct patents is a sample of 30.
    Reporting 50 would overstate a coverage measurement by two thirds."""
    distinct, collapsed = deduplicate(
        ["US1234567B2", "US1234567A1", "us 1,234,567", "EP4778509", "EP4778509A1"]
    )
    assert [p.normalized for p in distinct] == ["US1234567", "EP4778509"]
    assert collapsed == 3


def test_deduplication_preserves_first_seen_order():
    """Order matters because the coverage tool prints the query order in its
    `--dry-run`, and a run whose order shifts between the dry run and the real
    one is not the run that was previewed."""
    distinct, _ = deduplicate(
        ["EP1000000", "US2000000", "EP1000000A1", "CN3000000"]
    )
    assert [p.normalized for p in distinct] == ["EP1000000", "US2000000", "CN3000000"]


def test_deduplication_keeps_the_first_requested_form():
    """The first spelling is what a user recognises and what discovery gave us."""
    distinct, _ = deduplicate(["US 1,234,567 B2", "US1234567A1"])
    assert distinct[0].requested == "US 1,234,567 B2"
    assert distinct[0].normalized == "US1234567"


def test_deduplication_refuses_an_unparseable_entry():
    with pytest.raises(PublicationNumberError):
        deduplicate(["US1234567B2", "not-a-number"])


def test_a_publication_number_is_hashable_and_comparable():
    """Frozen and slotted, so it can key a dict of enrichment results."""
    assert normalize("US1234567B2") == normalize("US1234567B2")
    assert isinstance(hash(normalize("US1234567B2")), int)
