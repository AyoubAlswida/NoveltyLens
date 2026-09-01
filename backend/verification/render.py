"""Rendering the canonical prompt's blocks (spec sections 4, 4.1, 4.2, 4.3).

Separate from :mod:`verification.prompt` so that the two verbatim prompt
constants live in a module nothing else edits.

The single most important property of this module is what it does **not**
render. Spec section 4 ends with a one-line contract - "No dates. No cutoff. No
retrieval scores. No rankings. No passage counts." - and each omission answers a
specific failure:

- **Dates and cutoffs** (section 1.3): eligibility is arithmetic decided before
  the call. A verifier that can see a date can produce a date judgment dressed as
  a textual one, and ``UNCLEAR`` would stop meaning "textual ambiguity".
- **Scores and rankings** (section 5): a relevance number beside a passage
  measurably pulls a judgment toward it, and the failure that produces is the
  most dangerous one available here - a confident ``FOUND`` on a passage that is
  merely topically close.
- **Passage counts** (section 7.2): constraint 10 tells the model to apply the
  same standard to one passage as to five, which a count would invite it to
  discount.
"""

import re
from collections.abc import Sequence

from .decomposition import ApprovedElement, ApprovedRelationship
from .passages import Passage
from .prompt import USER_PROMPT_TEMPLATE


NO_RELATIONSHIPS = "NONE"
"""Rendered for ``{{required_relationships_block}}`` when the approved
decomposition has no relationships (spec section 4.2). The response's
``relationships`` array is then empty, which spec section 9 check 6 requires
rather than merely permits."""

TRANSLATION_PREFIX = "translation (reading aid only, do not quote): "
"""Spec section 4.3's subordinate translation line.

The wording is part of the prompt, not a label for humans: it is the only thing
marking this line as a reading aid, and constraint 4 in section 3 plus check 10 in
section 9 both depend on the distinction holding."""

_SECTION_RENDER_ORDER = {"CLAIMS": 0, "DESCRIPTION": 1, "ABSTRACT": 2, "OTHER": 3}


def render_elements_block(elements: Sequence[ApprovedElement]) -> str:
    """Spec section 4.1: one ``ID: text`` line per approved element, in order.

    ``core`` is deliberately not rendered. Constraint 9 requires every requirement
    to be judged on its own evidence, and telling the model which ones matter more
    is an invitation to let importance leak into a status.
    """
    return "\n".join(f"{element.element_id}: {element.text}" for element in elements)


def render_relationships_block(relationships: Sequence[ApprovedRelationship]) -> str:
    """Spec section 4.2: one ``ID: text`` line per approved relationship, or ``NONE``."""
    if not relationships:
        return NO_RELATIONSHIPS
    return "\n".join(f"{item.relationship_id}: {item.text}" for item in relationships)


def render_passages_block(passages: Sequence[Passage]) -> str:
    """Spec section 4.3, claims first and then description.

    ``passages`` must already be canonicalized (spec section 8) and must already
    have passed section 7.3 validation; this function renders, it does not police.

    The sort is stable, so it establishes the claims-first grouping section 4.3
    requires without disturbing the ordering ``select_passages`` chose inside each
    section.
    """
    ordered = sorted(
        passages,
        key=lambda passage: _SECTION_RENDER_ORDER.get(passage.section, len(_SECTION_RENDER_ORDER)),
    )
    rendered = []
    for passage in ordered:
        lines = [
            f"[chunk_id: {passage.chunk_id}]",
            f"section: {passage.section}",
            f"locator: {passage.locator}",
            f"language: {passage.language}",
            f"text: {passage.text}",
        ]
        if passage.translation:
            lines.append(f"{TRANSLATION_PREFIX}{passage.translation}")
        rendered.append("\n".join(lines))
    return "\n\n".join(rendered)


_PLACEHOLDER = re.compile(r"\{\{\w+\}\}")


def build_user_prompt(
    *,
    feature_id: str,
    feature_version: str,
    feature_text: str,
    elements: Sequence[ApprovedElement],
    relationships: Sequence[ApprovedRelationship],
    publication_number: str,
    publication_title: str,
    jurisdiction: str,
    passages: Sequence[Passage],
) -> str:
    """Fill :data:`~verification.prompt.USER_PROMPT_TEMPLATE`.

    Substitution is ``str.replace``, not ``str.format``: the template is a
    verbatim copy of the specification, and a formatter would have to be fed an
    escaped copy of it, which is a second version of the one canonical prompt.

    Substitution is also single-pass over the template rather than iterative, so a
    value that happens to contain ``{{...}}`` is inserted as text and never
    re-expanded. The trailing scan then fails loudly on any placeholder left
    unfilled - a prompt that reached a model with a literal ``{{passages_block}}``
    in it would produce a confidently wrong verdict about nothing.
    """
    replacements = {
        "{{feature_id}}": feature_id,
        "{{feature_version}}": feature_version,
        "{{feature_text}}": feature_text,
        "{{required_elements_block}}": render_elements_block(elements),
        "{{required_relationships_block}}": render_relationships_block(relationships),
        "{{publication_number}}": publication_number,
        "{{publication_title}}": publication_title,
        "{{jurisdiction}}": jurisdiction,
        "{{passages_block}}": render_passages_block(passages),
    }

    def substitute(match: re.Match[str]) -> str:
        placeholder = match.group(0)
        if placeholder not in replacements:
            raise ValueError(f"user prompt template has an unknown placeholder: {placeholder}")
        return replacements[placeholder]

    filled = _PLACEHOLDER.sub(substitute, USER_PROMPT_TEMPLATE)

    unfilled = _PLACEHOLDER.findall(USER_PROMPT_TEMPLATE)
    missing = sorted(set(replacements) - set(unfilled))
    if missing:
        raise ValueError(f"user prompt template no longer carries placeholders: {missing}")
    return filled
