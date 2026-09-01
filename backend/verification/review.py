"""The human review overlay (spec sections 13.1 - 13.5).

A validated machine result is **never edited**. Human review is recorded as an
overlay beside it, and the two are stored separately so a report can keep the
machine/human distinction the final document is required to show.

Two rules shape everything here:

- **A reviewer overrides individual element and relationship statuses, never an
  outcome.** The reviewed outcome is *derived* from the reviewed statuses, by the
  same function that derives the machine outcome (spec section 6.2). One
  function, two callers - so a reviewed outcome that contradicts its own reviewed
  statuses cannot be represented, and a supplied one that differs is a rejection
  rather than a correction.
- **A human's quotation is held to exactly the standard the machine's is.** Check
  5 reuses spec section 9's checks 7-10 unchanged: a reviewer cannot paraphrase,
  cannot cite an unsupplied passage, and cannot introduce text from elsewhere in
  the patent, because that text was never validated as belonging to this
  publication.

And one asymmetry, which is deliberate: a cell whose response failed validation
has **no** machine result, so no overlay can attach to it at all (check 0).
Permitting a requirement-level override there would let a human launder
unvalidated model output into a verdict - the statuses may cite chunks that do
not exist, quote text that was never supplied, or contradict their own outcome.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING

from .checks import validate_evidence
from .decomposition import ApprovedDecomposition
from .outcome import derive_outcome
from .passages import Passage
from .schema import EvidenceItem, FeatureDisclosureVerification, Outcome, Status
from .states import CellState, ResponseValidationError, ReviewValidationError

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, verify.py imports checks.py
    from .verify import CellResult


class EvidenceAction(StrEnum):
    """What the reviewer did with the machine's evidence for one requirement."""

    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    REPLACED = "REPLACED"


class ReviewStatus(StrEnum):
    """Spec section 13.2.

    ``PENDING`` is the initial state. ``APPROVED`` and ``OVERRIDDEN`` are
    **derived** from the overlay - no override anywhere gives ``APPROVED``, any
    override gives ``OVERRIDDEN`` - which is why :func:`derive_review_status`
    exists and why check 13 rejects a status that disagrees with the data.
    ``REJECTED`` and ``NEEDS_EXPERT_REVIEW`` are explicit reviewer actions.
    """

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    OVERRIDDEN = "OVERRIDDEN"
    NEEDS_EXPERT_REVIEW = "NEEDS_EXPERT_REVIEW"


@dataclass(frozen=True, slots=True)
class RequirementReview:
    """One reviewer decision about one approved element or relationship.

    ``human_evidence`` holds evidence the reviewer selected **from the supplied
    passages**. A reviewer may cite a passage the model did not cite; a reviewer
    may not introduce text that was not supplied.
    """

    requirement_id: str
    machine_status: Status
    reviewed_status: Status
    evidence_action: EvidenceAction
    human_evidence: tuple[EvidenceItem, ...] = ()
    reviewer_comment: str = ""
    overridden: bool = False


@dataclass(frozen=True, slots=True)
class Waiver:
    """A disclosed gap, never a way to make one disappear (spec section 12.3).

    Waiving a ``VALIDATION_FAILURE`` means "proceed with this gap disclosed", not
    "treat it as answered". The cell continues to display as a validation failure
    with its failing check - in the matrix, in the draft, and in the final report -
    beside this waiver's ``unresolved_limitation``. A waived ``NOT_EVALUATED``
    stays ``NOT_EVALUATED``.

    Every field except ``requirement_id`` and ``source_result_version`` is
    required and non-empty, and all of them are *bound* rather than merely
    recorded (spec section 12.3, enforced by section 13.4 check 18). Recording a
    cell id is not the same as checking it: an unchecked waiver is a transferable
    one, attachable to a different publication, a different feature, or a re-run
    of the same cell that no longer has the problem it excuses.

    ``source_result_version`` may be omitted, in which case the waiver is bound
    through the overlay's own ``source_result_version`` - which check 16 has
    already pinned to the expected version. The binding is never absent, only
    sometimes implicit.
    """

    reviewer_id: str
    at: str
    cell_id: str
    reason: str
    unresolved_limitation: str
    requirement_id: str | None = None
    source_result_version: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewOverlay:
    """Spec section 13.2's ``review`` object.

    ``source_cell_id`` and ``source_result_version`` are the overlay's statement
    of **what it was composed against**, and spec section 13.4 checks 15 and 16
    compare both to the cell and version the review is actually being saved
    against. A review attached to a result that later goes stale stays attached to
    it and does **not** transfer forward: the reviewer approved a specific piece of
    evidence, not a conclusion in perpetuity (spec section 13.6).

    Both live here rather than being read off the enclosing record, and that is
    the whole mechanism. The enclosing record is the *destination*; a binding
    check that takes its expectation from the destination checks nothing.
    ``source_result_version`` was on this class from the start and was compared to
    nothing, which is the exact shape of an unenforced invariant - a field
    documenting an intention the code held nobody to.
    """

    status: ReviewStatus
    element_reviews: tuple[RequirementReview, ...]
    relationship_reviews: tuple[RequirementReview, ...]
    reviewed_outcome: Outcome
    reviewer_id: str
    reviewed_at: str
    source_result_version: str
    source_cell_id: str
    requests_for_more_context: tuple[dict, ...] = ()
    waiver: Waiver | None = None
    approved_via_bulk_action: bool = False


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """Spec section 13.5. Appended, never updated.

    Requirement-level changes carry ``requirement_type`` and ``requirement_id`` so
    an audit reader can reconstruct exactly *which* status changed, rather than
    only that the cell changed.

    **This requires real user identity.** The repository has one shared seeded
    account and no ownership column, and an audit trail attributing every decision
    to the same demo user is not an audit trail. Spec section 16 decision 1 makes
    per-user accounts a prerequisite for Gate C, not a follow-up.
    """

    audit_id: str
    cell_id: str
    workflow_stage: str
    reviewer_id: str
    at: str
    field: str
    previous_value: str
    new_value: str
    reason: str
    source_result_version: str
    requirement_type: str | None = None
    requirement_id: str | None = None


def is_overridden(review: RequirementReview) -> bool:
    """Spec section 13.2: overridden when the status changed **or** the evidence did.

    The second half is easy to lose. A reviewer who keeps a ``FOUND`` but replaces
    the citation behind it has changed the record, and the report has to show that
    as human-overridden rather than machine-generated.
    """
    return (
        review.reviewed_status != review.machine_status
        or review.evidence_action != EvidenceAction.ACCEPTED
    )


def derive_review_status(overlay: ReviewOverlay) -> ReviewStatus:
    """``APPROVED`` or ``OVERRIDDEN``, derived from the overlay.

    ``PENDING``, ``REJECTED`` and ``NEEDS_EXPERT_REVIEW`` are explicit reviewer
    actions and are returned unchanged - there is nothing to derive about them.
    """
    if overlay.status in (ReviewStatus.PENDING, ReviewStatus.REJECTED, ReviewStatus.NEEDS_EXPERT_REVIEW):
        return overlay.status
    reviews = (*overlay.element_reviews, *overlay.relationship_reviews)
    return ReviewStatus.OVERRIDDEN if any(is_overridden(item) for item in reviews) else ReviewStatus.APPROVED


def reviewed_outcome(
    machine_result: FeatureDisclosureVerification,
    overlay: ReviewOverlay,
) -> Outcome:
    """Spec section 13.3. A human cannot type, select, or otherwise supply this.

    A requirement the reviewer did not touch keeps its machine status as its
    effective reviewed status. The UI pre-populates ``reviewed_status`` with
    ``machine_status``, so an untouched requirement is explicit in the record
    rather than absent from it - which is also why the lookup below is a plain
    indexing and not a ``.get`` with a default. A requirement missing from the
    overlay is a bug in the caller, not a value to guess at.
    """
    overrides = {item.requirement_id: item for item in overlay.element_reviews}
    overrides |= {item.requirement_id: item for item in overlay.relationship_reviews}

    effective = [
        overrides[item.element_id].reviewed_status for item in machine_result.elements
    ] + [
        overrides[item.relationship_id].reviewed_status for item in machine_result.relationships
    ]
    return derive_outcome(effective)


def _accepted_machine_evidence(
    review: RequirementReview,
    machine_evidence: Sequence[EvidenceItem],
) -> bool:
    return review.evidence_action == EvidenceAction.ACCEPTED and bool(machine_evidence)


WAIVER_REQUIRED_FIELDS = ("reviewer_id", "at", "cell_id", "reason", "unresolved_limitation")
"""Spec section 12.3: what every waiver must carry. Check 14 enforces presence;
:func:`_validate_waiver_binding` enforces that the values point at this cell."""


def _validate_waiver_completeness(waiver: Waiver) -> None:
    """Spec section 13.4 check 14: the waiver carries what section 12.3 requires."""
    missing = [name for name in WAIVER_REQUIRED_FIELDS if not str(getattr(waiver, name)).strip()]
    if missing:
        raise ReviewValidationError("check 14", f"waiver is missing {missing}")


def _validate_waiver_binding(
    waiver: Waiver,
    *,
    cell_id: str,
    result_version: str,
    reviewer_id: str,
    decomposition: ApprovedDecomposition,
) -> None:
    """Spec section 13.4 check 18: the waiver belongs to *this* cell.

    Completeness is checked first, because a binding check on an empty field
    reports the wrong problem: a waiver with no ``cell_id`` at all is incomplete
    (check 14), not mis-bound.
    """
    _validate_waiver_completeness(waiver)

    if waiver.cell_id != cell_id:
        raise ReviewValidationError(
            "check 18",
            f"waiver names cell {waiver.cell_id!r} but is attached to {cell_id!r}; a waiver "
            f"excuses one specific gap and is not transferable to another",
        )
    if waiver.reviewer_id != reviewer_id:
        raise ReviewValidationError(
            "check 18",
            f"waiver is attributed to {waiver.reviewer_id!r} but recorded by {reviewer_id!r}; "
            f"an audit trail that credits a decision to the wrong person is worse than none",
        )
    if waiver.requirement_id is not None:
        approved = (*decomposition.element_ids, *decomposition.relationship_ids)
        if waiver.requirement_id not in approved:
            raise ReviewValidationError(
                "check 18",
                f"waiver names requirement {waiver.requirement_id!r}, which is not an approved "
                f"element or relationship of {decomposition.feature_id}: {list(approved)}",
            )
    if waiver.source_result_version is not None and waiver.source_result_version != result_version:
        raise ReviewValidationError(
            "check 18",
            f"waiver was recorded against result version {waiver.source_result_version!r} but is "
            f"attached to {result_version!r}",
        )


def waive_cell(
    cell: "CellResult",
    waiver: Waiver,
    *,
    decomposition: ApprovedDecomposition,
) -> "CellResult":
    """Record a spec section 12.3 waiver against a cell, changing nothing else.

    This is the path for the cells that **cannot** carry a review overlay -
    ``VALIDATION_FAILURE`` and ``NOT_EVALUATED`` - which section 12.3 nonetheless
    permits a reviewer to carry forward under a documented waiver so Gate C can
    complete.

    The returned cell has the **same state, the same ``machine_result`` (still
    ``None`` for a failure), the same failing check and the same reason**. A
    waiver is a disclosed gap, not an answer: a waived ``VALIDATION_FAILURE``
    still renders as a validation failure with its failing check in the matrix,
    the draft and the final report, beside the waiver's unresolved limitation, and
    a waived ``NOT_EVALUATED`` stays ``NOT_EVALUATED``. Nothing here can promote a
    state into a verdict, because nothing here writes ``state`` or
    ``machine_result`` at all.

    The waiver's bindings are validated exactly as an overlay's are (check 18), so
    a waiver written for one cell cannot be attached to another.
    """
    _validate_waiver_binding(
        waiver,
        cell_id=cell.cell_id,
        result_version=cell.result_version,
        # Self-attributed, and deliberately so: a cell-level waiver has no review
        # overlay beside it, so there is no second record of who was reviewing for
        # the waiver's `reviewer_id` to disagree with. The check is meaningful in
        # `validate_review`, where the overlay names the reviewer independently.
        reviewer_id=waiver.reviewer_id,
        decomposition=decomposition,
    )
    return replace(cell, waiver=waiver)


def validate_review(
    overlay: ReviewOverlay,
    *,
    cell_state: CellState,
    cell_id: str,
    result_version: str,
    machine_result: FeatureDisclosureVerification | None,
    decomposition: ApprovedDecomposition,
    publication_number: str,
    passages: Sequence[Passage],
) -> None:
    """Spec section 13.4, applied whenever a review is saved.

    Raises :class:`~verification.states.ReviewValidationError` naming the first
    failing check. A review failing any check is rejected outright; it is never
    partially saved.

    ``cell_id`` and ``result_version`` are the **expected** identity - the cell
    and version this review is being saved against. Checks 15-18 compare the
    overlay's own claims to them, and they run immediately after check 0 despite
    their higher numbers: checks 1-14 all validate the overlay against whatever
    machine result, decomposition, and passage set they are handed, and none of
    them establishes that those are the *right* ones. Without the bindings, a
    review of result version 1 attaches cleanly to version 2 of the same cell, and
    a review of feature F2 against publication A attaches to F2 against
    publication B, because the requirement IDs are identical.
    """
    # Check 0. A cell in VALIDATION_FAILURE, NOT_EVALUATED, or any EXCLUDED_*
    # state has no machine result and cannot carry an overlay at all.
    if cell_state != CellState.VERIFIED or machine_result is None:
        raise ReviewValidationError(
            "check 0",
            f"cell is {cell_state}, which has no valid machine_result; there is nothing "
            f"coherent to review and no overlay can attach",
        )

    # Check 15. The overlay names the cell it was composed against.
    if overlay.source_cell_id != cell_id:
        raise ReviewValidationError(
            "check 15",
            f"review was composed against cell {overlay.source_cell_id!r} but is being saved "
            f"against {cell_id!r}; a review of one cell is not a review of another",
        )

    # Check 16. And the result version - the stale-review guard (spec section 13.6).
    if overlay.source_result_version != result_version:
        raise ReviewValidationError(
            "check 16",
            f"review was composed against result version {overlay.source_result_version!r} but "
            f"is being saved against {result_version!r}; a reviewer approved specific evidence, "
            f"not a conclusion in perpetuity",
        )

    # Check 17. The machine result is the one this review claims to be about.
    if machine_result.feature_id != decomposition.feature_id:
        raise ReviewValidationError(
            "check 17",
            f"machine_result is for feature {machine_result.feature_id!r}, not the approved "
            f"{decomposition.feature_id!r}",
        )
    if machine_result.feature_version != decomposition.feature_version:
        raise ReviewValidationError(
            "check 17",
            f"machine_result is for feature version {machine_result.feature_version!r}, not the "
            f"approved {decomposition.feature_version!r}; a review made against one decomposition "
            f"version cannot attach to a result computed under another",
        )
    if machine_result.publication_number != publication_number:
        raise ReviewValidationError(
            "check 17",
            f"machine_result is for publication {machine_result.publication_number!r}, not the "
            f"evaluated {publication_number!r}",
        )

    # Check 18. A waiver, if present, is bound to this cell, reviewer,
    # requirement and version. Run here rather than beside check 14 so every
    # binding failure is reported before any content check spends effort on an
    # overlay that is attached to the wrong thing.
    if overlay.waiver is not None:
        _validate_waiver_binding(
            overlay.waiver,
            cell_id=cell_id,
            result_version=result_version,
            reviewer_id=overlay.reviewer_id,
            decomposition=decomposition,
        )

    # Check 1.
    element_ids = tuple(item.requirement_id for item in overlay.element_reviews)
    if element_ids != decomposition.element_ids:
        raise ReviewValidationError(
            "check 1",
            f"element_reviews cover {list(element_ids)}, not the approved "
            f"{list(decomposition.element_ids)}",
        )
    relationship_ids = tuple(item.requirement_id for item in overlay.relationship_reviews)
    if relationship_ids != decomposition.relationship_ids:
        raise ReviewValidationError(
            "check 1",
            f"relationship_reviews cover {list(relationship_ids)}, not the approved "
            f"{list(decomposition.relationship_ids)}",
        )

    machine_by_id = {item.element_id: item for item in machine_result.elements}
    machine_by_id |= {item.relationship_id: item for item in machine_result.relationships}
    by_chunk = {passage.chunk_id: passage for passage in passages}

    for review in (*overlay.element_reviews, *overlay.relationship_reviews):
        where = f"requirement {review.requirement_id}"
        machine = machine_by_id[review.requirement_id]

        # Check 2.
        if review.machine_status != machine.status:
            raise ReviewValidationError(
                "check 2",
                f"{where} records machine_status {review.machine_status!r}, but the immutable "
                f"machine_result says {machine.status!r}",
            )

        # Check 3 is structural: `reviewed_status` is typed as the closed
        # three-status enum, so an invalid member cannot be constructed.

        # Check 5 - spec section 9's checks 7-10, unchanged, over the reviewer's
        # own quotations.
        try:
            validate_evidence(
                review.human_evidence,
                passages=by_chunk,
                publication_number=publication_number,
                where=f"{where} human_evidence",
            )
        except ResponseValidationError as error:
            raise ReviewValidationError(
                "check 5", f"{error.check} failed on human evidence: {error.detail}"
            ) from error

        # Checks 6 and 7. A FOUND with no surviving evidence is rejected: the
        # machine's evidence was rejected or replaced and nothing took its place.
        if review.reviewed_status == Status.FOUND:
            if not _accepted_machine_evidence(review, machine.evidence) and not review.human_evidence:
                check = "check 7" if review.evidence_action == EvidenceAction.REJECTED else "check 6"
                raise ReviewValidationError(
                    check, f"{where} is reviewed FOUND with no surviving evidence behind it"
                )

        # Check 8.
        if review.evidence_action == EvidenceAction.REPLACED and not review.human_evidence:
            raise ReviewValidationError(
                "check 8", f"{where} has evidence_action REPLACED with no human_evidence"
            )

        # Check 9.
        if review.reviewed_status == Status.NOT_FOUND_IN_SUPPLIED_PASSAGES and review.human_evidence:
            raise ReviewValidationError(
                "check 9",
                f"{where} is reviewed NOT_FOUND_IN_SUPPLIED_PASSAGES with human_evidence attached",
            )

        # Check 10.
        if review.reviewed_status == Status.UNCLEAR and not review.reviewer_comment.strip():
            raise ReviewValidationError(
                "check 10", f"{where} is reviewed UNCLEAR with no reviewer_comment naming the ambiguity"
            )

        # Check 12 before check 11, because check 11 reads `overridden` and a
        # disagreeing flag would make its verdict meaningless.
        if review.overridden != is_overridden(review):
            raise ReviewValidationError(
                "check 12",
                f"{where} records overridden={review.overridden}, but reviewed_status "
                f"{review.reviewed_status!r} vs machine {review.machine_status!r} and "
                f"evidence_action {review.evidence_action!r} say otherwise",
            )

        # Check 11.
        if review.overridden and not review.reviewer_comment.strip():
            raise ReviewValidationError(
                "check 11", f"{where} is overridden with no reviewer_comment recording the reason"
            )

    # Check 4. Recomputed, never accepted from input.
    expected = reviewed_outcome(machine_result, overlay)
    if overlay.reviewed_outcome != expected:
        raise ReviewValidationError(
            "check 4",
            f"reviewed_outcome {overlay.reviewed_outcome!r} != {expected!r} recomputed from the "
            f"reviewed statuses",
        )

    # Check 13.
    derived = derive_review_status(overlay)
    if overlay.status in (ReviewStatus.APPROVED, ReviewStatus.OVERRIDDEN) and overlay.status != derived:
        raise ReviewValidationError(
            "check 13", f"review status {overlay.status!r} disagrees with the overlay ({derived!r})"
        )

    # Check 14. Binding was already checked (18, above); this is the section 12.3
    # completeness requirement, kept at its own number because a waiver missing a
    # field and a waiver pointing at the wrong cell are different defects.
    if overlay.waiver is not None:
        _validate_waiver_completeness(overlay.waiver)
