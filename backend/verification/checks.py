"""Application-side response validation (spec section 9).

An unvalidated model response is not a result. Every check runs on every
response, failures are never repaired and never partially accepted, and a
response that fails any check is rejected wholesale: it is retained as a
diagnostic artifact but it does **not** become the cell's machine result, and it
therefore cannot carry a human review overlay (spec sections 9.1, 12.3, 13.4
check 0).

Three of the eighteen carry most of the weight, and the specification names them:

- **Check 10** is the anti-fabrication core. Every ``exact_quote`` must be an
  exact substring of the stored canonical passage text - a plain ``in`` test,
  nothing more. Nothing here normalizes, case-folds, re-punctuates, repairs, or
  fuzzy-matches a quotation. If no exact supporting substring can be produced,
  the requirement cannot be ``FOUND``; that is the intended consequence, not an
  edge case to work around.
- **Check 15** is the anti-hedging core. The outcome is recomputed from the
  returned statuses and the response is rejected on mismatch, so the model's own
  ``outcome`` is a consistency check on its reasoning rather than the answer.
- **Checks 5 and 6** are what make the approved decomposition binding rather than
  advisory: the model cannot add, omit, merge, split, rename, or reorder a
  requirement.
"""

import json
from collections.abc import Mapping, Sequence

from pydantic import ValidationError

from .decomposition import ApprovedDecomposition
from .outcome import derive_outcome
from .passages import Passage
from .schema import (
    EvidenceItem,
    FeatureDisclosureVerification,
    Status,
    UNIQUE_ITEM_FIELDS,
)
from .states import ResponseValidationError


def parse_response(payload: str | Mapping) -> FeatureDisclosureVerification:
    """Spec section 9 check 1: one complete JSON object matching the section 10 schema.

    "One complete JSON object" is literal. Spec section 3's output contract says
    "ONE JSON object and nothing else: no markdown fences, no commentary before or
    after", so ``json.loads`` is applied to the response **as it arrived**:

    - a ```` ```json ```` fence fails, because a fence is not JSON;
    - leading or trailing prose fails, because ``json.loads`` rejects both;
    - nothing here extracts a JSON substring, strips a fence, or repairs a
      response into something parseable.

    That strictness is deliberate on two counts. Repairing model output is what
    the zero-tolerance policy forbids, and stripping a fence is a repair however
    small. And a provider that ignores structured output announces itself with
    fences - stripping them for it would hide the exact evidence spec section
    14.2.1's capability test is trying to collect.

    Raises :class:`~verification.states.ResponseValidationError` with check 1.
    This is the **only** check spec section 9.1 permits a single re-ask for -
    matching ``_llm_json``'s existing behaviour in ``backend/llm.py`` - so it is
    separated from the rest, and the caller decides whether to re-ask. The
    re-asked response is held to this same contract; there is no second, looser
    pass.

    ``uniqueItems`` on the two missing-id arrays is enforced here rather than on
    the models: spec section 10.1's pydantic classes are reproduced verbatim and
    pydantic has no ``uniqueItems``, while section 10's JSON Schema - the
    authority when the two disagree - marks both arrays unique.
    """
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as error:
            raise ResponseValidationError("check 1", f"response is not valid JSON: {error}") from error
    else:
        data = payload

    if not isinstance(data, dict):
        raise ResponseValidationError("check 1", f"response is a {type(data).__name__}, not a JSON object")

    try:
        result = FeatureDisclosureVerification.model_validate(data)
    except ValidationError as error:
        raise ResponseValidationError("check 1", f"response fails the section 10 schema: {error}") from error

    for field in UNIQUE_ITEM_FIELDS:
        values = getattr(result, field)
        if len(set(values)) != len(values):
            raise ResponseValidationError("check 1", f"{field} contains duplicates: {values}")

    return result


def validate_evidence(
    items: Sequence[EvidenceItem],
    *,
    passages: Mapping[str, Passage],
    publication_number: str,
    where: str,
) -> None:
    """Spec section 9 checks 7-10, over one list of evidence or combination-basis items.

    Extracted because spec section 13.4 check 5 reuses these four **unchanged**
    for a human reviewer's own quotations. That reuse is the point of the whole
    design: a reviewer cannot paraphrase, cannot cite an unsupplied passage, and
    cannot introduce text from elsewhere in the patent, because that text was
    never validated as belonging to this publication.
    """
    for item in items:
        passage = passages.get(item.chunk_id)
        if passage is None:
            raise ResponseValidationError(
                "check 7", f"{where} cites chunk {item.chunk_id!r}, which was not supplied"
            )
        if passage.publication_number != publication_number:
            raise ResponseValidationError(
                "check 8",
                f"{where} cites chunk {item.chunk_id!r}, which belongs to "
                f"{passage.publication_number!r}, not {publication_number!r}",
            )
        if item.locator != passage.locator:
            raise ResponseValidationError(
                "check 9",
                f"{where} reports locator {item.locator!r} for chunk {item.chunk_id!r}, "
                f"which was supplied as {passage.locator!r}",
            )
        if item.exact_quote not in passage.text:
            # Never a translation: spec section 7.4 makes the translation a
            # reading aid, and a quote drawn from it would be an evidence string
            # that does not exist in the source document.
            raise ResponseValidationError(
                "check 10",
                f"{where} quotes {item.exact_quote!r}, which is not a substring of "
                f"chunk {item.chunk_id!r}",
            )


def validate_response(
    payload: str | Mapping | FeatureDisclosureVerification,
    *,
    decomposition: ApprovedDecomposition,
    publication_number: str,
    passages: Sequence[Passage],
) -> FeatureDisclosureVerification:
    """Run every spec section 9 check and return the validated machine result.

    Raises :class:`~verification.states.ResponseValidationError` naming the first
    failing check. There is no partial acceptance and no repair path: a response
    that fails is a ``VALIDATION_FAILURE`` recorded with its failing check, which
    the user resolves at Gate C and which can never be converted into a verdict.
    """
    result = payload if isinstance(payload, FeatureDisclosureVerification) else parse_response(payload)
    by_chunk = {passage.chunk_id: passage for passage in passages}

    # Check 2. Also a Literal on the model, so this is belt and braces - but the
    # constant is what the report renderer reads to prove a cell's scope, and a
    # matrix cell that cannot prove its scope is not displayed as a verified
    # result. It is worth naming its own check number when it fails.
    if result.verification_scope != "SUPPLIED_PASSAGES_ONLY":
        raise ResponseValidationError(
            "check 2", f"verification_scope is {result.verification_scope!r}"
        )

    # Check 3.
    if result.feature_id != decomposition.feature_id:
        raise ResponseValidationError(
            "check 3", f"feature_id {result.feature_id!r} != approved {decomposition.feature_id!r}"
        )
    if result.feature_version != decomposition.feature_version:
        raise ResponseValidationError(
            "check 3",
            f"feature_version {result.feature_version!r} != approved "
            f"{decomposition.feature_version!r}",
        )

    # Check 4.
    if result.publication_number != publication_number:
        raise ResponseValidationError(
            "check 4",
            f"publication_number {result.publication_number!r} != evaluated {publication_number!r}",
        )

    # Checks 5 and 6: same set, same order, no omissions, no inventions, no
    # duplicates. Comparing the tuples covers all four at once, and comparing
    # them *in order* is what preserves the approved display order.
    returned_elements = tuple(item.element_id for item in result.elements)
    if returned_elements != decomposition.element_ids:
        raise ResponseValidationError(
            "check 5",
            f"element_id values {list(returned_elements)} != approved "
            f"{list(decomposition.element_ids)}",
        )
    returned_relationships = tuple(item.relationship_id for item in result.relationships)
    if returned_relationships != decomposition.relationship_ids:
        raise ResponseValidationError(
            "check 6",
            f"relationship_id values {list(returned_relationships)} != approved "
            f"{list(decomposition.relationship_ids)}",
        )

    requirements = [
        ("ELEMENT", item.element_id, item) for item in result.elements
    ] + [
        ("RELATIONSHIP", item.relationship_id, item) for item in result.relationships
    ]

    ambiguity_keys = {(entry.requirement_type, entry.requirement_id) for entry in result.ambiguities}

    for kind, requirement_id, item in requirements:
        where = f"{kind.lower()} {requirement_id}"

        # Checks 7-10, over both evidence and combination basis.
        validate_evidence(
            item.evidence, passages=by_chunk, publication_number=publication_number, where=where
        )
        validate_evidence(
            item.combination_basis,
            passages=by_chunk,
            publication_number=publication_number,
            where=f"{where} combination_basis",
        )

        # Check 11.
        if item.status == Status.FOUND and not item.evidence:
            raise ResponseValidationError("check 11", f"{where} is FOUND with no evidence")

        # Check 12.
        if item.status == Status.NOT_FOUND_IN_SUPPLIED_PASSAGES and item.evidence:
            raise ResponseValidationError(
                "check 12", f"{where} is NOT_FOUND_IN_SUPPLIED_PASSAGES with evidence attached"
            )

        # Check 13.
        if item.status == Status.UNCLEAR and (kind, requirement_id) not in ambiguity_keys:
            raise ResponseValidationError(
                "check 13", f"{where} is UNCLEAR with no matching ambiguities entry"
            )

        # Check 14. "Citing more than one chunk" is counted over the
        # requirement's evidence: combination_basis is the *justification* for a
        # multi-passage citation, so counting it too would let a response satisfy
        # the check by widening the thing being checked.
        if len({evidence.chunk_id for evidence in item.evidence}) > 1 and not item.combination_basis:
            raise ResponseValidationError(
                "check 14",
                f"{where} cites more than one chunk with no combination_basis establishing the link",
            )

    # Check 15.
    statuses = [item.status for _, _, item in requirements]
    expected_outcome = derive_outcome(statuses)
    if result.outcome != expected_outcome:
        raise ResponseValidationError(
            "check 15",
            f"outcome {result.outcome!r} != {expected_outcome!r} derived from the returned statuses",
        )

    # Check 16. UNCLEAR items belong in neither list, which set equality against
    # the NOT_FOUND ids enforces without a separate scan.
    expected_missing_elements = {
        item.element_id
        for item in result.elements
        if item.status == Status.NOT_FOUND_IN_SUPPLIED_PASSAGES
    }
    if set(result.missing_element_ids) != expected_missing_elements:
        raise ResponseValidationError(
            "check 16",
            f"missing_element_ids {sorted(result.missing_element_ids)} != "
            f"{sorted(expected_missing_elements)}",
        )
    expected_missing_relationships = {
        item.relationship_id
        for item in result.relationships
        if item.status == Status.NOT_FOUND_IN_SUPPLIED_PASSAGES
    }
    if set(result.missing_relationship_ids) != expected_missing_relationships:
        raise ResponseValidationError(
            "check 16",
            f"missing_relationship_ids {sorted(result.missing_relationship_ids)} != "
            f"{sorted(expected_missing_relationships)}",
        )

    # Check 17.
    approved = {
        "ELEMENT": set(decomposition.element_ids),
        "RELATIONSHIP": set(decomposition.relationship_ids),
    }
    for entry in result.ambiguities:
        if entry.requirement_id not in approved[entry.requirement_type]:
            raise ResponseValidationError(
                "check 17",
                f"ambiguity names {entry.requirement_id!r}, which is not an approved "
                f"{entry.requirement_type.lower()} ID",
            )
        for chunk_id in entry.chunk_ids:
            if chunk_id not in by_chunk:
                raise ResponseValidationError(
                    "check 17",
                    f"ambiguity for {entry.requirement_id} names chunk {chunk_id!r}, "
                    f"which was not supplied",
                )

    # Check 18. `requested_context` is a closed Literal on the model, so only the
    # chunk needs checking here.
    for request in result.requests_for_more_context:
        if request.chunk_id not in by_chunk:
            raise ResponseValidationError(
                "check 18",
                f"context request names chunk {request.chunk_id!r}, which was not supplied",
            )

    return result
