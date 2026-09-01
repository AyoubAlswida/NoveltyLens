"""Application-owned states and failure types (spec sections 1.2, 9.1, 12.3, 14.1).

The vocabulary here is the point of the whole component, so it is worth stating
once more why these are separate types rather than one "it didn't work" flag.

*Publication*-scoped exclusions and *cell*-scoped non-verdicts are different
things. An excluded publication never enters the approved candidate set, so no
feature x publication cell is ever created for it - not an empty cell, not a
greyed one (spec section 1.2.1). ``NOT_EVALUATED`` and ``VALIDATION_FAILURE``
belong to an approved, eligible publication whose comparison against one feature
did not produce a verdict; those are real cells and do appear in the matrix.

None of them is ever a disclosure verdict. Converting an empty retrieval, a
provider timeout, or a rejected response into
``NO_DISCLOSURE_FOUND_IN_SUPPLIED_PASSAGES`` would manufacture a finding out of
the absence of one, which is the substitution the repository's zero-tolerance
policy exists to prevent.
"""

from enum import StrEnum


class PublicationExclusion(StrEnum):
    """States that remove a publication from the run before any matrix exists.

    Scope is the **publication**. None of these ever becomes a matrix cell.
    """

    EXCLUDED_DATE = "EXCLUDED_DATE"
    """Not date-eligible against the run's cutoff. Decided by arithmetic before
    any LLM call, and never overridable by a reviewer (spec sections 1.3, 12.2)."""

    EXCLUDED_NO_FULL_TEXT = "EXCLUDED_NO_FULL_TEXT"
    """Enrichment *succeeded* and carried no eligible full text for this exact
    publication (spec sections 1.2.2, 1.2.3).

    A failed enrichment call - timeout, 5xx, transport error, auth failure - is
    **not** this state. The provider never said whether text exists, so recording
    an exclusion would report a finding the run does not have."""

    EXCLUDED_INELIGIBLE = "EXCLUDED_INELIGIBLE"
    """Passages exist but fail identity or eligibility validation (spec section 7.3
    checks 1-6)."""


class CellState(StrEnum):
    """States of one feature x publication comparison. Scope is the **cell**."""

    VERIFIED = "VERIFIED"
    """A model response that passed every spec section 9 check. The only state
    carrying a ``machine_result``, and therefore the only one a human review
    overlay can attach to (spec section 13.4 check 0)."""

    NOT_EVALUATED = "NOT_EVALUATED"
    """An eligible, approved publication whose evaluation did not complete: zero
    eligible passages, nothing cleared the retrieval floor, context preparation
    failed, the budget cap was reached, or execution did not finish."""

    VALIDATION_FAILURE = "VALIDATION_FAILURE"
    """A response was returned and failed a spec section 9 check. It is retained
    as a diagnostic artifact but is **not** the cell's machine result, so it can
    carry no review overlay and can never be approved into a verdict."""


class VerificationError(RuntimeError):
    """Base class for every failure this component raises."""


class VerifierCapabilityError(VerificationError):
    """The configured model or adapter cannot serve this verifier (spec section 14.1).

    Raised instead of silently downgrading to a weaker output mode. Spec section
    14.1 is explicit that there is no prompt-only fallback and no runtime
    loosening of validation: an ineligible model fails the stage, and choosing a
    different one is a recorded configuration decision.
    """


class PassageSetIneligible(VerificationError):
    """A passage set failed spec section 7.3 checks 1-6.

    Raised rather than returned, deliberately. This is a **publication**-scoped
    exclusion (``EXCLUDED_INELIGIBLE``), and spec section 1.2.1 forbids creating a
    matrix cell for an excluded publication - returning a cell result would imply
    a comparison was attempted and answered. The caller records the exclusion
    against the publication and creates no cells for it.

    Note the specification is internally loose here: section 7.3 says the state is
    recorded "for that cell", while section 1.2 (scope column), section 1.2.1 and
    regression case S all say publication scope with no cell created. The latter
    three agree, so they win.
    """

    def __init__(self, check: str, detail: str) -> None:
        super().__init__(f"{check}: {detail}")
        self.check = check
        self.detail = detail


class ResponseValidationError(VerificationError):
    """A model response failed one of the spec section 9 checks.

    Carries the failing check, because ``VALIDATION_FAILURE`` must be recorded
    *with its failing check named* (spec section 9.1) - both so a reviewer at
    Gate C can see what went wrong, and so the final report can print it beside
    the cell rather than blanking it.
    """

    def __init__(self, check: str, detail: str) -> None:
        super().__init__(f"{check}: {detail}")
        self.check = check
        self.detail = detail


class ReviewValidationError(VerificationError):
    """A human review overlay failed one of the spec section 13.4 checks.

    A review failing any check is rejected with the failing check named, and is
    never partially saved.
    """

    def __init__(self, check: str, detail: str) -> None:
        super().__init__(f"{check}: {detail}")
        self.check = check
        self.detail = detail
