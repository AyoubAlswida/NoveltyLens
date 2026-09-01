"""Passage identity, canonicalization, selection, and pre-call validation.

Covers spec sections 7 (identity, count, eligibility) and 8 (the one
canonicalization rule).

Two properties here are load-bearing and easy to erode:

- **Canonicalization happens once, before the call, on the passage.** Never on a
  quote afterwards. Contradictory normalization rules are how quote-checking
  dies, so there is exactly one rule and everything downstream - including a
  human reviewer's own quotation (spec section 13.4 check 5) - is held to a plain
  substring test against the stored canonical text.
- **Retrieval scores never reach the model** (spec section 5). They select and
  order passages here and they are recorded for audit, but they are absent from
  the rendered prompt entirely - not redacted, not labelled non-evidential.
  Instructing a model to disregard a number it can see is weaker than not showing
  it, and the failure that anchoring produces is the most dangerous one available
  here: a confident FOUND on a passage that is merely topically close.
- **Eligibility is enforced on the section, not on a flag.** An abstract cannot
  become evidence by arriving with ``evidence_eligible=True`` - see
  :data:`EVIDENCE_SECTIONS`. The classifier that is meant to set that flag
  honestly does not exist yet (spec sections 17.2, 17.3), and it stays an
  integration prerequisite; this module simply does not depend on it being right.
"""

import re
import unicodedata
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace

from .states import PassageSetIneligible


SECTIONS = ("ABSTRACT", "CLAIMS", "DESCRIPTION", "OTHER")
"""The section vocabulary of spec section 7.1.

This is what a passage object may *say* it is. It is not what may be sent to the
verifier - see :data:`EVIDENCE_SECTIONS`.
"""

EVIDENCE_SECTIONS = ("CLAIMS", "DESCRIPTION")
"""The only sections that can ever be evidence (spec section 1.2.3).

Section 1.2.3's table is exhaustive about what makes a publication eligible:
claims, a substantive description, or both. An abstract alone, a title,
bibliographic metadata, and a search-result snippet are each ruled out by name,
and the snippet is ruled out absolutely - "never eligible evidence under any
circumstances".

**This is enforced structurally rather than by trusting ``evidence_eligible``.**
That flag is set by the upstream full-text eligibility classifier, which spec
sections 17.2 and 17.3 record as *not built yet*: today's enrichment returns one
flat string in which claims-only, description-only and abstract-only outcomes are
indistinguishable. A boolean nobody computes is not a safety boundary, so the
section a passage declares is checked directly and an ``ABSTRACT`` cannot become
evidence by being flagged ``evidence_eligible=True``.

``OTHER`` is excluded for the same reason, and deliberately rather than by
oversight. It is the bucket a provider's leftovers land in - a title, a
bibliographic block, an abstract under a different name, a search snippet - and
nothing about the label establishes the provenance section 1.2.3 requires.
Treating arbitrary text as full-text evidence because it was not called an
abstract is precisely the substitution this specification exists to prevent. If a
provider ever returns a genuinely eligible section that does not fit ``CLAIMS`` or
``DESCRIPTION``, name it and add it here, with the provenance written down.
"""

MAX_PASSAGES = 5
"""Hard ceiling on passages per call (spec section 7.2). A larger set is reduced
before the call, never sent."""

PREFERRED_MIN_PASSAGES = 3
"""Retrieval *target*, not a floor (spec section 7.2).

One or two strong passages are a valid verification - a claim that states every
requirement is complete evidence on its own. Padding to reach three is strictly
worse than sending two: it spends attention budget and adds another surface for a
spurious FOUND on topical similarity. Nothing in this module pads.
"""

_SECTION_RENDER_ORDER = {"CLAIMS": 0, "DESCRIPTION": 1, "ABSTRACT": 2, "OTHER": 3}
"""Claims first, then description (spec section 4.3).

ABSTRACT and OTHER are ordered last for completeness only; neither can reach a
prompt, because spec section 7.3 check 2 rejects any passage that is not evidence
eligible and spec section 1.2.3 rules an abstract out of eligibility entirely.
"""

DUPLICATE_CHUNK = "duplicate chunk_id"
BELOW_FLOOR = "below the retrieval floor"
OVER_CEILING = f"reduced to the {MAX_PASSAGES}-passage ceiling"


@dataclass(frozen=True, slots=True)
class Passage:
    """One validated passage, in the shape spec section 7.1 requires.

    ``source`` names the provider that supplied the text and is metadata only -
    it is deliberately **not** part of ``chunk_id``, so a publication re-enriched
    from a different provider keeps stable chunk IDs (spec section 4.3).

    ``text_version_date`` records when this text was retrieved, so a stored
    verdict can be re-audited against the exact text it was made from.
    """

    chunk_id: str
    publication_number: str
    source_document_id: str
    source: str
    section: str
    locator: str
    language: str
    text_version_date: str
    evidence_eligible: bool
    text: str
    translation: str | None = None


@dataclass(frozen=True, slots=True)
class PassageSelection:
    """What was sent, what was dropped, and why (spec section 7.2).

    The reduction and what it dropped are recorded, because silently dropping a
    passage changes the question being asked without recording that it changed.
    """

    selected: tuple[Passage, ...]
    dropped: tuple[tuple[str, str], ...]
    """``(chunk_id, reason)`` pairs, in the order they were dropped."""

    @property
    def dropped_below_floor(self) -> tuple[str, ...]:
        """Chunks removed for failing the retrieval floor.

        Kept separable because it decides a ``NOT_EVALUATED`` cell's *reason*:
        "no passage cleared the retrieval threshold" (regression case M) is a
        different statement from "zero eligible passages retrieved" (case L), and
        neither is ever ``NO_DISCLOSURE_FOUND_IN_SUPPLIED_PASSAGES``.
        """
        return tuple(chunk_id for chunk_id, reason in self.dropped if reason == BELOW_FLOOR)


def canonicalize(text: str) -> str:
    r"""The one canonicalization rule (spec section 8).

    Normalize to NFC, convert every line terminator to ``\n``, collapse every run
    of whitespace to a single ``U+0020``, strip the ends. Applied once, here,
    before the prompt is rendered. The result is what the model receives and what
    is stored as the passage of record, and ``exact_quote`` is later checked
    against it with a plain ``in`` test - nothing case-folds, re-punctuates,
    repairs, or fuzzy-matches a quotation at validation time.
    """
    normalized = unicodedata.normalize("NFC", text)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\s+", " ", normalized).strip()


def canonicalize_passage(passage: Passage) -> Passage:
    """Return ``passage`` with its text in canonical form.

    ``translation`` is canonicalized too, but only so a multi-line translation
    cannot break the line-oriented passage block format. It is never a quotation
    source: spec section 7.4 and check 10 both require ``exact_quote`` to come
    from ``text``, since quoting a translation would produce an evidence string
    that does not exist in the source document.
    """
    return replace(
        passage,
        text=canonicalize(passage.text),
        translation=canonicalize(passage.translation) if passage.translation else passage.translation,
    )


def select_passages(
    passages: Sequence[Passage],
    *,
    retrieval_scores: Mapping[str, float] | None = None,
    retrieval_floor: float | None = None,
    max_passages: int = MAX_PASSAGES,
) -> PassageSelection:
    """Reduce a retrieved set to at most ``max_passages``, recording what it dropped.

    Deduplicates by ``chunk_id``, applies the retrieval floor when one is
    supplied, orders claims first and then by descending retrieval score, and
    takes the head. It **never pads**: a set of two comes back as a set of two.

    ``retrieval_floor`` defaults to ``None`` - no floor - on purpose. Spec section
    16 decision 6 records that the floor cannot be set yet: it depends on the
    Qdrant / Qwen3-Embedding-4B recalibration CLAUDE.md already flags as
    outstanding, and the 0.6 constant in ``search/vector_store.py`` was measured
    against bge-large. Inventing a number here would be exactly the fabricated
    constant that decision is holding open, so the caller supplies one or there
    is none.

    A floor supplied with no scores drops everything, and that is the intended
    direction: a passage whose score is unknown has not been shown to clear the
    threshold, so the cell becomes ``NOT_EVALUATED`` rather than a verdict built on
    unmeasured evidence.
    """
    dropped: list[tuple[str, str]] = []
    seen: set[str] = set()
    kept: list[Passage] = []

    for passage in passages:
        if passage.chunk_id in seen:
            dropped.append((passage.chunk_id, DUPLICATE_CHUNK))
            continue
        seen.add(passage.chunk_id)
        if retrieval_floor is not None:
            score = (retrieval_scores or {}).get(passage.chunk_id)
            if score is None or score < retrieval_floor:
                dropped.append((passage.chunk_id, BELOW_FLOOR))
                continue
        kept.append(passage)

    def sort_key(item: tuple[int, Passage]) -> tuple[int, float, int]:
        index, passage = item
        rank = _SECTION_RENDER_ORDER.get(passage.section, len(_SECTION_RENDER_ORDER))
        score = (retrieval_scores or {}).get(passage.chunk_id, 0.0)
        return (rank, -score, index)

    ordered = [passage for _, passage in sorted(enumerate(kept), key=sort_key)]
    for passage in ordered[max_passages:]:
        dropped.append((passage.chunk_id, OVER_CEILING))

    return PassageSelection(selected=tuple(ordered[:max_passages]), dropped=tuple(dropped))


_REQUIRED_NON_EMPTY = (
    "chunk_id", "publication_number", "source_document_id", "source",
    "section", "locator", "language", "text_version_date", "text",
)
"""Spec section 7.3 check 5's field list, minus ``evidence_eligible`` (a bool,
checked separately) and ``translation`` (which may be null)."""


def validate_passage_set(
    passages: Sequence[Passage],
    *,
    publication_number: str,
    publication_document_ids: Collection[str],
    max_passages: int = MAX_PASSAGES,
) -> None:
    """Spec section 7.3. The call is not made unless every check here holds.

    Raises :class:`~verification.states.PassageSetIneligible` naming the failed
    check. A failing set is **never** degraded into a call with a reduced passage
    set - dropping the offending passage would change the question being asked
    without recording that it changed.

    ``publication_document_ids`` is required rather than optional, and an empty
    collection therefore fails every passage. Check 3 is the family-member guard:
    a family member is a different document with different content and a
    different date, and its text can never make the evaluated publication
    eligible. A guard that passes when the caller supplies nothing to check
    against is not a guard, so the safe direction is the default.
    """
    if not passages:
        # Distinguished from a violation on purpose: an empty set is a
        # NOT_EVALUATED cell (spec section 7.2), not an EXCLUDED_INELIGIBLE
        # publication. verify.py checks for it before reaching here.
        raise PassageSetIneligible("check 7", "the passage set is empty")
    if len(passages) > max_passages:
        raise PassageSetIneligible(
            "check 7", f"{len(passages)} passages exceeds the {max_passages}-passage ceiling"
        )

    seen: set[str] = set()
    for passage in passages:
        if passage.publication_number != publication_number:
            raise PassageSetIneligible(
                "check 1",
                f"{passage.chunk_id} carries publication {passage.publication_number!r}, "
                f"not the evaluated {publication_number!r}",
            )
        # Check 2, structural half first: the section decides, and no flag
        # overrides it. See EVIDENCE_SECTIONS for why this cannot wait for the
        # upstream classifier that is supposed to set `evidence_eligible`.
        if passage.section not in EVIDENCE_SECTIONS:
            raise PassageSetIneligible(
                "check 2",
                f"{passage.chunk_id} is section {passage.section!r}, which is never eligible "
                f"evidence under spec section 1.2.3 - only {list(EVIDENCE_SECTIONS)} are, "
                f"whatever evidence_eligible says",
            )
        if passage.evidence_eligible is not True:
            raise PassageSetIneligible(
                "check 2", f"{passage.chunk_id} is not marked evidence eligible"
            )
        if passage.source_document_id not in publication_document_ids:
            raise PassageSetIneligible(
                "check 3",
                f"{passage.chunk_id} resolves to document {passage.source_document_id!r}, which is "
                f"not a document record of {publication_number!r}",
            )
        if passage.chunk_id in seen:
            raise PassageSetIneligible("check 4", f"duplicate chunk_id {passage.chunk_id!r}")
        seen.add(passage.chunk_id)

        for field in _REQUIRED_NON_EMPTY:
            if not str(getattr(passage, field)).strip():
                raise PassageSetIneligible("check 5", f"{passage.chunk_id} has an empty {field!r}")
        if passage.section not in SECTIONS:
            raise PassageSetIneligible(
                "check 5", f"{passage.chunk_id} has section {passage.section!r}, not one of {SECTIONS}"
            )
        if not canonicalize(passage.text):
            raise PassageSetIneligible(
                "check 6", f"{passage.chunk_id} has empty text after canonicalization"
            )
