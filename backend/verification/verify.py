"""One verification call: one approved feature x one publication x 1-5 passages.

This is the component the specification is about. Everything else in the package
is an input contract, an output contract, or a check; this assembles them in the
one order that keeps the guarantees:

1. **Every no-LLM-call short circuit happens first.** Zero eligible passages,
   nothing over the retrieval floor, an ineligible passage set - each is decided
   before a request is built, so an exhausted quota is never spent discovering
   something the application already knew.
2. **The capability gate is next** (spec section 14.1). An ineligible model fails
   the stage rather than degrading to a weaker output mode.
3. **Only then is the model asked**, once, with one re-ask permitted for a
   malformed response and none for a content violation.
4. **Every response is validated in full** before it is anything but a diagnostic
   artifact.

What this module deliberately does not do:

- It does not decide dates (spec section 1.3), full-text eligibility (section
  1.2.3), or which publications enter the matrix. Those are decided upstream, and
  the verifier is given no cutoff, no publication date, and no way to reason about
  timing.
- It does not orchestrate a matrix, persist anything, or emit SSE. The four human
  gates in spec section 11.1 are a workflow this repository does not have yet, and
  spec section 16 lists several of its prerequisites as still needing approval -
  per-user identity, an SSE paused state, a LangGraph checkpointer. Wiring a gate
  into the live pipeline before those decisions is out of scope here.
- It does not retry a content failure. The same prompt and passages will most
  likely produce the same violation, and a retry loop against a metered gateway is
  how quota disappears.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from llm import LLMTruncatedError, RETRY_TEMPERATURE, _llm_chat
from config import settings
from tracing import trace

from .checks import validate_response
from .decomposition import ApprovedDecomposition, validate_decomposition
from .passages import (
    MAX_PASSAGES,
    Passage,
    PassageSelection,
    canonicalize_passage,
    select_passages,
    validate_passage_set,
)
from .prompt import PROMPT_VERSION, SYSTEM_PROMPT
from .render import build_user_prompt
from .schema import FeatureDisclosureVerification, provider_response_format
from .states import CellState, ResponseValidationError, VerifierCapabilityError


logger = logging.getLogger(__name__)

TEMPERATURE = 0.0
"""Spec section 14. **Best effort, not a determinism guarantee** - batching,
kernel non-determinism and provider-side routing all break bit-exact
reproducibility. Do not build an evaluation suite on exact-match reproducibility;
measure verdict stability across repeated runs and treat instability as a
finding."""

BASE_MAX_TOKENS = 900
TOKENS_PER_REQUIREMENT = 260
"""Spec section 14's ``900 + 260 x (n_elements + n_relationships)``.

A **starting point**, explicitly provisional and unbenchmarked. Each status
carries a quote plus a note, and ``minimaxm3`` bills reasoning tokens against the
response, so ``LLMTruncatedError`` is a live risk on a large decomposition. Per
CLAUDE.md truncation is never retried at the same ceiling: raise the coefficient
or reduce the requirement count."""

TIMEOUT_SECONDS = 60
"""Above ``_llm_chat``'s 30s default because verbatim reproduction is slow.
Provisional, per spec section 14."""

TRANSPORT_RETRIES = 2
"""Transient errors only (spec section 14). Content-validation failures are not
retried - see spec section 9.1."""

PARSE_ATTEMPTS = 2
"""One re-ask for a malformed response, and only for check 1.

Spec section 9.1 permits exactly this, matching ``_llm_json``'s existing
single-retry behaviour in ``backend/llm.py``. A response failing any of checks
2-18 is a *content* failure and fails the cell immediately."""


@dataclass(frozen=True, slots=True)
class PublicationUnderEvaluation:
    """The one publication this call is about.

    ``document_ids`` is the set of ``source_document_id`` values that belong to
    this publication's own document record. Spec section 7.3 check 3 rejects any
    passage outside it: a family member, continuation, or related filing is a
    different document with different content and a different date, and its text
    can never be evidence for this one.
    """

    publication_number: str
    title: str
    jurisdiction: str
    document_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class CellResult:
    """The outcome of one feature x publication comparison.

    ``machine_result`` is populated **only** when ``state`` is ``VERIFIED``. A
    response that failed a spec section 9 check is kept in ``raw_response`` as a
    diagnostic artifact and nothing more: it is not a machine result, it cannot
    carry a review overlay (spec section 13.4 check 0), and it can never be
    approved, overridden, or waived into a verdict (spec sections 9.1, 12.3).

    ``cell_id`` and ``result_version`` are required rather than optional, and that
    is the point of them. Spec section 13.2 gives both to every stored result, and
    section 13.4's binding checks compare a review overlay's own
    ``source_cell_id`` and ``source_result_version`` against them. A result that
    cannot say which cell and which version it is cannot be bound to a review at
    all - it would let a reviewer's approval of specific evidence land on a re-run
    that produced different evidence, which section 13.6 forbids.
    """

    state: CellState
    cell_id: str
    result_version: str
    machine_result: FeatureDisclosureVerification | None = None
    failed_check: str = ""
    reason: str = ""
    raw_response: str = ""
    passage_selection: PassageSelection | None = None
    prompt_version: str = PROMPT_VERSION
    model: str = ""
    attempts: int = 0
    passage_chunk_ids: tuple[str, ...] = field(default_factory=tuple)
    waiver: object | None = None
    """A spec section 12.3 waiver recorded against this cell, or ``None``.

    Typed loosely to keep :mod:`verification.review` importing this module rather
    than the reverse. Set only by :func:`verification.review.waive_cell`, which
    never changes ``state`` or ``machine_result`` - a waiver is a disclosed gap,
    not a verdict.
    """


NO_ELIGIBLE_PASSAGES = "zero eligible passages retrieved"
NO_PASSAGE_OVER_FLOOR = "no passage cleared the retrieval threshold"
"""The two ``NOT_EVALUATED`` reasons that must stay distinct (regression cases L
and M). Neither is ever ``NO_DISCLOSURE_FOUND_IN_SUPPLIED_PASSAGES``: converting
an empty retrieval into a disclosure verdict would manufacture a finding out of
the absence of a search result."""


def max_tokens_for(decomposition: ApprovedDecomposition) -> int:
    """Spec section 14's output budget for this decomposition."""
    return BASE_MAX_TOKENS + TOKENS_PER_REQUIREMENT * decomposition.requirement_count


def structured_output_format() -> dict:
    """The ``response_format`` this verifier is eligible to run with (spec section 14).

    Raises :class:`~verification.states.VerifierCapabilityError` naming the
    missing capability rather than falling back. Spec section 14.1 is categorical:
    no prompt-only fallback, no silent switch to a weaker output mode, and no
    loosening of validation to accommodate a weaker model. A degraded output mode
    chosen at runtime is the fabricated-or-degraded output the zero-tolerance
    policy forbids, and the fact that it degrades a *format* rather than a
    *finding* does not exempt it - looser parsing is how an unvalidated verdict
    reaches a report.

    **Eligibility is a recorded configuration decision, never a runtime probe.**
    Nothing here asks the provider what it supports; it reads what a human wrote
    down after running the spec section 14.2.1 capability test. That test has not
    been run for the currently configured model (spec section 17.4), so
    ``minimaxm3``'s eligibility is unverified - CLAUDE.md records only that
    ``json_object`` was *accepted* by the gateway, which is not evidence of
    ``json_schema`` support.
    """
    mode = settings.verifier_structured_output
    if mode == "json_schema":
        return provider_response_format()
    if mode == "json_object":
        if not settings.verifier_generic_json_decision.strip():
            raise VerifierCapabilityError(
                "VERIFIER_STRUCTURED_OUTPUT=json_object asks the verifier to run without "
                "JSON Schema-constrained generation, which spec section 14.1 makes an "
                "eligibility requirement. Section 14.2.2 point 3 allows it as an explicit "
                "MVP decision, but point 4 requires that decision to be recorded: set "
                "VERIFIER_GENERIC_JSON_DECISION to the owner, date, model and adapter "
                "revisions it covers, and the measured malformed-response rate."
            )
        logger.warning(
            "Verifier running in generic JSON mode under recorded decision: %s",
            settings.verifier_generic_json_decision,
        )
        return {"type": "json_object"}
    raise VerifierCapabilityError(
        f"VERIFIER_STRUCTURED_OUTPUT={mode!r} is not a supported output mode. The verifier "
        f"requires JSON Schema-constrained generation ('json_schema'); 'json_object' is "
        f"available only under a recorded decision. There is no prompt-only mode."
    )


def verify_feature_disclosure(
    *,
    decomposition: ApprovedDecomposition,
    publication: PublicationUnderEvaluation,
    passages: Sequence[Passage],
    cell_id: str,
    result_version: str = "1",
    retrieval_scores: Mapping[str, float] | None = None,
    retrieval_floor: float | None = None,
    max_passages: int = MAX_PASSAGES,
    transport_retries: int = TRANSPORT_RETRIES,
    parse_attempts: int = PARSE_ATTEMPTS,
) -> CellResult:
    """Verify one approved feature against one publication's supplied passages.

    Returns a :class:`CellResult`. Raises rather than returning in exactly two
    cases, both of which mean *no cell should exist*:

    - :class:`~verification.states.PassageSetIneligible`, when the passage set
      fails spec section 7.3 checks 1-6. That is a **publication**-scoped
      exclusion (``EXCLUDED_INELIGIBLE``), and spec section 1.2.1 forbids creating
      a matrix cell for an excluded publication - returning a cell would imply a
      comparison was attempted and answered negatively.
    - :class:`~verification.states.VerifierCapabilityError`, when the configured
      model is not eligible to serve this verifier. The stage fails; it does not
      degrade.

    ``cell_id`` names the cell being evaluated (spec section 13.2's
    ``run:feature:publication``) and is required, because a result that cannot say
    which cell it belongs to cannot be bound to a human review (section 13.4
    checks 15-16). ``result_version`` defaults to ``"1"`` - the first result for
    a cell - and a re-verification supplies the next one, linking back through
    ``source_result_version``.

    ``retrieval_scores`` never reach the model (spec section 5). They order the
    selection and belong in the audit record; the rendered prompt carries no score,
    rank, date, or passage count.

    ``transport_retries`` and ``parse_attempts`` default to the production
    policy.  The explicitly cost-capped diagnostic sets them to ``0`` and ``1``
    so one requested repetition can make at most one provider call; changing
    retry policy does not change the payload being measured.
    """
    if not cell_id.strip():
        raise ValueError("cell_id is required: an unidentified result cannot be reviewed")
    if not result_version.strip():
        raise ValueError("result_version is required: a review binds to a specific version")
    if transport_retries < 0:
        raise ValueError("transport_retries must be at least zero")
    if parse_attempts < 1:
        raise ValueError("parse_attempts must be at least one")
    validate_decomposition(decomposition)

    canonical = [canonicalize_passage(passage) for passage in passages]
    selection = select_passages(
        canonical,
        retrieval_scores=retrieval_scores,
        retrieval_floor=retrieval_floor,
        max_passages=max_passages,
    )

    if not selection.selected:
        # No LLM call. Regression cases L and M: an eligible publication whose
        # evaluation did not complete is NOT_EVALUATED with its reason, never a
        # disclosure verdict.
        reason = NO_PASSAGE_OVER_FLOOR if selection.dropped_below_floor else NO_ELIGIBLE_PASSAGES
        trace("verify.not_evaluated", publication=publication.publication_number,
              feature=decomposition.feature_id, reason=reason)
        return CellResult(
            state=CellState.NOT_EVALUATED,
            cell_id=cell_id,
            result_version=result_version,
            reason=reason,
            passage_selection=selection,
            model=settings.llm_model,
        )

    # Raises PassageSetIneligible on checks 1-6; a set that got here cannot fail
    # check 7, since select_passages caps it and the empty case returned above.
    validate_passage_set(
        selection.selected,
        publication_number=publication.publication_number,
        publication_document_ids=publication.document_ids,
        max_passages=max_passages,
    )

    response_format = structured_output_format()

    user_prompt = build_user_prompt(
        feature_id=decomposition.feature_id,
        feature_version=decomposition.feature_version,
        feature_text=decomposition.feature_text,
        elements=decomposition.elements,
        relationships=decomposition.relationships,
        publication_number=publication.publication_number,
        publication_title=publication.title,
        jurisdiction=publication.jurisdiction,
        passages=selection.selected,
    )
    chunk_ids = tuple(passage.chunk_id for passage in selection.selected)

    trace("verify.start", publication=publication.publication_number,
          feature=decomposition.feature_id, feature_version=decomposition.feature_version,
          requirements=decomposition.requirement_count, chunks=list(chunk_ids))

    raw = ""
    parse_failure: ResponseValidationError | None = None
    for attempt in range(parse_attempts):
        raw = _llm_chat(
            user_prompt,
            system=SYSTEM_PROMPT,
            max_tokens=max_tokens_for(decomposition),
            # A re-ask at temperature 0.0 would resample the identical broken
            # output, wasting the one attempt spec section 9.1 allows.
            temperature=TEMPERATURE if attempt == 0 else max(TEMPERATURE, RETRY_TEMPERATURE),
            timeout=TIMEOUT_SECONDS,
            retries=transport_retries,
            response_format=response_format,
        )
        try:
            # The raw response, unmodified. Spec section 3's output contract is
            # "ONE JSON object and nothing else: no markdown fences, no commentary
            # before or after" - so a fenced or prose-wrapped response is a
            # response that did not meet the contract, and check 1 has to say so.
            #
            # This used to call `_strip_fences` first. That quietly repaired the
            # most visible symptom of a provider not honouring structured output,
            # which is exactly the evidence the section 14.2.1 capability test
            # needs: a model whose fences are stripped for it looks compliant.
            # Repairing model output is also the thing the zero-tolerance policy
            # forbids, and stripping is a repair however small.
            result = validate_response(
                raw,
                decomposition=decomposition,
                publication_number=publication.publication_number,
                passages=selection.selected,
            )
        except ResponseValidationError as error:
            if error.check == "check 1" and attempt + 1 < parse_attempts:
                # The one permitted re-ask, unchanged, at a nudged temperature.
                logger.warning("Malformed verifier response (attempt %d/%d): %s",
                               attempt + 1, parse_attempts, error.detail)
                trace("verify.reask", attempt=attempt, detail=error.detail)
                parse_failure = error
                continue
            trace("verify.validation_failure", check=error.check, detail=error.detail)
            return CellResult(
                state=CellState.VALIDATION_FAILURE,
                cell_id=cell_id,
                result_version=result_version,
                failed_check=error.check,
                reason=error.detail,
                raw_response=raw,
                passage_selection=selection,
                model=settings.llm_model,
                attempts=attempt + 1,
                passage_chunk_ids=chunk_ids,
            )

        trace("verify.done", publication=publication.publication_number,
              feature=decomposition.feature_id, outcome=str(result.outcome))
        return CellResult(
            state=CellState.VERIFIED,
            cell_id=cell_id,
            result_version=result_version,
            machine_result=result,
            raw_response=raw,
            passage_selection=selection,
            model=settings.llm_model,
            attempts=attempt + 1,
            passage_chunk_ids=chunk_ids,
        )

    # Every attempt failed check 1. Not repaired, not partially accepted.
    return CellResult(
        state=CellState.VALIDATION_FAILURE,
        cell_id=cell_id,
        result_version=result_version,
        failed_check="check 1",
        reason=parse_failure.detail if parse_failure else "no response was parsed",
        raw_response=raw,
        passage_selection=selection,
        model=settings.llm_model,
        attempts=parse_attempts,
        passage_chunk_ids=chunk_ids,
    )


__all__ = [
    "BASE_MAX_TOKENS",
    "CellResult",
    "LLMTruncatedError",
    "NO_ELIGIBLE_PASSAGES",
    "NO_PASSAGE_OVER_FLOOR",
    "PARSE_ATTEMPTS",
    "PublicationUnderEvaluation",
    "TEMPERATURE",
    "TIMEOUT_SECONDS",
    "TOKENS_PER_REQUIREMENT",
    "max_tokens_for",
    "structured_output_format",
    "verify_feature_disclosure",
]
