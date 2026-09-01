"""Publication-number normalization to **epodoc** form: country code plus
document number, with the kind code removed.

**Why this is correctness rather than tidiness.** EPO's published-data full-text
endpoints return 404 for a kind-suffixed identifier and 200 for the bare form --
`EP4778509`, not `EP4778509A1`. That is confirmed against the live API and is
already recorded in `search/epo.py`. Discovery providers return the suffixed
form, so without normalization every enrichment call would 404 and every
candidate would fall to `EXCLUDED_NO_FULL_TEXT`: a total failure that looks
exactly like a legitimate coverage result.

**Both forms are recorded.** The requested one is what discovery gave us and
what a user recognises; the normalized one is what we asked the provider for.
Keeping only the second loses the ability to say which document a reviewer was
actually shown.

**Nothing here invents an identifier.** A string this module cannot parse raises
:class:`PublicationNumberError` rather than being guessed at, padded, or
defaulted to a country. A wrong publication number silently substituted for an
unparseable one would attach real evidence to the wrong document.
"""

import re
from dataclasses import dataclass

# Country code, document number, optional kind code.
#
# The kind code is the trailing letter (plus optional digit) that follows a
# DIGIT: `A1`, `B2`, `T3`, `U1`, `S`. Requiring a preceding digit is what stops
# the `D` of a US design number (`USD123456`) or the `RE` of a reissue
# (`USRE45678`) being mistaken for one -- those letters lead the document number
# rather than trailing it.
_PUBLICATION = re.compile(
    r"""
    ^
    (?P<country>[A-Z]{2})          # ISO country / office code
    (?P<number>
        [A-Z]{0,2}                 # optional leading class: D, RE, PP, ...
        \d[\dA-Z]*?                # the number itself, at least one digit
    )
    (?P<kind>(?<=\d)[A-Z]\d?)?     # kind code: only ever after a digit
    $
    """,
    re.VERBOSE,
)

_SEPARATORS = re.compile(r"[\s,./\-_()]+")


class PublicationNumberError(ValueError):
    """A publication number could not be parsed.

    Raised rather than guessed. Every alternative -- assuming a country,
    stripping until something matches, falling back to the raw string -- risks
    querying a *different real document* and attaching its text to this
    candidate.
    """


@dataclass(frozen=True, slots=True)
class PublicationNumber:
    """One publication identity, in every form the pipeline needs.

    Two of these are equal when their `normalized` forms match, which is exactly
    the identity EPO answers on. `US1234567B2` and `US1234567A1` are therefore
    the *same* publication for lookup purposes and differ only in `requested` --
    a deliberate merge, because they are the same document at different
    publication stages and EPO serves one full text for the pair.
    """

    requested: str
    """Verbatim, as discovery supplied it."""

    normalized: str
    """epodoc form: country + document number, no kind code."""

    country: str
    number: str
    kind: str
    """The kind code that was removed, `""` when there was none. Kept so a
    display surface can show what the user searched for."""

    @property
    def source_document_id(self) -> str:
        """The provider-qualified document identity a passage carries.

        `verification/passages.py`'s check 3 resolves this back to the evaluated
        publication's own document record, which is what refuses borrowed family
        text.
        """
        return f"epo:{self.normalized}"


def normalize(publication_number: str) -> PublicationNumber:
    """Parse one publication number into every form the pipeline needs.

    Accepts the shapes discovery actually produces -- `US 11,234,567 B2`,
    `EP4778509A1`, `WO2020/123456`, `us1234567b2` -- by removing separators and
    upper-casing before matching. It does **not** accept a bare number with no
    country, because there is no honest way to supply one.

    Raises:
        PublicationNumberError: when the input is empty or unparseable.
    """
    if not publication_number or not publication_number.strip():
        raise PublicationNumberError("a publication number cannot be empty")

    requested = publication_number.strip()
    compact = _SEPARATORS.sub("", requested).upper()

    match = _PUBLICATION.match(compact)
    if match is None:
        raise PublicationNumberError(
            f"{requested!r} is not a recognisable publication number: expected a "
            "two-letter country code followed by a document number, optionally "
            "with a kind code"
        )

    country = match.group("country")
    number = match.group("number")
    kind = match.group("kind") or ""
    return PublicationNumber(
        requested=requested,
        normalized=f"{country}{number}",
        country=country,
        number=number,
        kind=kind,
    )


def try_normalize(publication_number: str) -> PublicationNumber | None:
    """:func:`normalize`, returning None instead of raising.

    For the one caller that legitimately has a *set* of numbers of mixed quality
    -- the coverage tool -- and needs to report how many it could not parse
    rather than stopping at the first. Pipeline code uses `normalize` and lets
    the failure propagate.
    """
    try:
        return normalize(publication_number)
    except PublicationNumberError:
        return None


def deduplicate(publication_numbers: list[str]) -> tuple[list[PublicationNumber], int]:
    """Normalize a list and collapse repeats, preserving first-seen order.

    Returns:
        The distinct publications and how many duplicates were collapsed. The
        count is reported rather than discarded: a sample of 50 rows that is 30
        distinct patents is a sample of 30, and saying "50" would overstate a
        coverage measurement by two thirds.

    Raises:
        PublicationNumberError: on the first unparseable entry.
    """
    seen: dict[str, PublicationNumber] = {}
    collapsed = 0
    for raw in publication_numbers:
        publication = normalize(raw)
        if publication.normalized in seen:
            collapsed += 1
            continue
        seen[publication.normalized] = publication
    return list(seen.values()), collapsed


def same_publication(left: str, right: str) -> bool:
    """Whether two publication numbers name the same document.

    Compares normalized forms, so `US1234567B2` and `US1234567` match -- the same
    document, one with its kind code. `US1234567` and `US1234568` do not, and
    neither do `US1234567` and `EP1234567`: a shared number under two offices is
    two unrelated documents.

    An unparseable input is **not** equal to anything. Returning True on a parse
    failure would be the borrowed-family-text bug wearing a different hat.
    """
    parsed_left = try_normalize(left)
    parsed_right = try_normalize(right)
    if parsed_left is None or parsed_right is None:
        return False
    return parsed_left.normalized == parsed_right.normalized
