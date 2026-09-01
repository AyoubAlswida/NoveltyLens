"""Admission-time policy validation: the run refuses to start without a recorded
human decision.

**§3.6's mechanism, and its ordering matters.** Validation happens **before
anything is written** -- no run row, no lease, no checkpoint, no workflow event,
no successful idempotency record. There is nothing to clean up because nothing
was created. `QUEUED → FAILED` is reserved for failures discovered *after*
admission (an executor that cannot start, an adapter revision that changed);
using it for policy validation would create a row whose only purpose is to record
that it should never have been created.

**Two things are checked per policy, not one.** The policy must be *set* to a
supported value, and its **decision must be recorded** -- owner, date, rationale.
A policy set with no recorded decision is a rule nobody approved, and this
codebase already has one precedent for exactly that shape:
`VERIFIER_GENERIC_JSON_DECISION`, which permits generic-JSON mode only as a
written-down decision. One pattern, used twice.

**No defaults, deliberately.** Which date screens prior art, whether the cutoff
is inclusive, and what a text version may be are legal questions. A default here
would answer them on the user's behalf, in code review, silently.
"""

from dataclasses import dataclass, field

import config
from config import settings
from evidence.dates import SUPPORTED_DATE_POLICIES
from verification.capability import CapabilityNotQualified, require_capability

PROVIDER_PUBLICATION_DATE = "provider_publication_date"
"""The approved `TEXT_VERSION_POLICY`: the served document's publication date
from provider bibliographic data."""

EXCLUDE_WITHOUT_VERSION = "exclude"
"""The strict variant: no trustworthy version means `EXCLUDED_INELIGIBLE`.
Implemented and tested; not the approved policy."""

SUPPORTED_TEXT_VERSION_POLICIES = (PROVIDER_PUBLICATION_DATE, EXCLUDE_WITHOUT_VERSION)

POLICY_NOT_APPROVED = "POLICY_NOT_APPROVED"
"""The `422` error code an admission endpoint returns. Gate E builds that
endpoint; the code is fixed here so both halves agree."""


class PolicyNotApproved(RuntimeError):
    """A required policy or its recorded decision is unset, unknown, or
    unapproved.

    Carries the missing items as **structured, safe fields** rather than only a
    sentence, because the API response quotes them and a free-text blob would be
    one more thing to sanitize.
    """

    code = POLICY_NOT_APPROVED

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__(
            "the run cannot be admitted until these decisions are recorded: "
            + "; ".join(missing)
        )


CAPABILITY_REQUIRED = "required"
CAPABILITY_DEFERRED = "deferred"
"""The two readings of `VERIFIER_CAPABILITY_ENFORCEMENT`. A closed vocabulary,
checked at admission, so an unknown value refuses rather than defaulting."""


@dataclass(frozen=True, slots=True)
class ApprovedPolicies:
    """The frozen policy pair a run is admitted under.

    Copied onto `verification_runs.date_policy` and `.text_version_policy` at
    admission and never re-read from settings afterwards, so changing a setting
    cannot retroactively reinterpret a finished run's screening. A stored run's
    exclusions can always be explained by the rules it was actually run under.
    """

    date_screening_policy: str
    text_version_policy: str
    date_screening_decision: str = field(default="", repr=False)
    text_version_decision: str = field(default="", repr=False)

    capability_qualified: bool = True
    """Whether an eligible §14.2.1 artifact matched this runtime.

    `False` means the run was admitted under a recorded deferral. It is carried
    rather than dropped so the report can state it: a verified run produced
    without a qualified verifier is a real limitation of that report, and the
    reader is the person who needs to know."""

    capability_deferral_decision: str = ""
    """The recorded deferral, when `capability_qualified` is False. Empty
    otherwise."""


def validate_admission_policies(
    source=settings,
    *,
    target=None,
    capability_directory=None,
) -> ApprovedPolicies:
    """Check both policies and both recorded decisions, or refuse the run.

    Every missing item is collected before raising, rather than failing on the
    first: an operator setting this up deserves the whole list in one response,
    not four rounds of trial and error.

    Raises:
        PolicyNotApproved: naming exactly what is missing.
    """
    missing: list[str] = []

    date_policy = (getattr(source, "date_screening_policy", "") or "").strip()
    if not date_policy:
        missing.append(
            "DATE_SCREENING_POLICY is unset (expected one of: "
            + ", ".join(SUPPORTED_DATE_POLICIES)
            + ")"
        )
    elif date_policy not in SUPPORTED_DATE_POLICIES:
        missing.append(
            f"DATE_SCREENING_POLICY={date_policy!r} is not supported (expected one of: "
            + ", ".join(SUPPORTED_DATE_POLICIES)
            + ")"
        )
    elif not (getattr(source, "date_screening_decision", "") or "").strip():
        missing.append(
            "DATE_SCREENING_DECISION is unset: the policy is chosen but nobody has "
            "recorded who approved it, when, and why"
        )

    text_policy = (getattr(source, "text_version_policy", "") or "").strip()
    if not text_policy:
        missing.append(
            "TEXT_VERSION_POLICY is unset (expected one of: "
            + ", ".join(SUPPORTED_TEXT_VERSION_POLICIES)
            + ")"
        )
    elif text_policy not in SUPPORTED_TEXT_VERSION_POLICIES:
        missing.append(
            f"TEXT_VERSION_POLICY={text_policy!r} is not supported (expected one of: "
            + ", ".join(SUPPORTED_TEXT_VERSION_POLICIES)
            + ")"
        )
    elif not (getattr(source, "text_version_decision", "") or "").strip():
        missing.append(
            "TEXT_VERSION_DECISION is unset: the policy is chosen but nobody has "
            "recorded who approved it, when, and why"
        )

    if missing:
        raise PolicyNotApproved(missing)

    # --- Structured-output capability (amendment 2026-08-28) ---------------
    #
    # A verified run asks a model to make statements about a patent. Spec §14.1
    # makes JSON Schema-constrained generation an ELIGIBILITY REQUIREMENT rather
    # than a preference, and §14.2.1's gate is what establishes whether THIS
    # runtime meets it. Admitting a run without that establishes nothing and
    # spends quota to produce verdicts nobody can stand behind.
    #
    # This is checked at admission, beside the policy decisions, and for the same
    # reason: nothing is written yet, so there is nothing to clean up. It fails
    # closed -- no artifact, no match, or a matching-but-ineligible artifact all
    # refuse. There is no bypass flag.
    #
    # SEARCH-ONLY RUNS NEVER REACH HERE. `validate_admission_policies` is called
    # for verified runs only; POST /api/analyze and /api/analyze/stream are
    # untouched by the amendment, because changing which provider development
    # talks to is not a reason to change what a search does.
    enforcement = (
        getattr(source, "verifier_capability_enforcement", "required") or "required"
    ).strip().lower()

    if enforcement == CAPABILITY_REQUIRED:
        require_capability(target or config.llm_target, directory=capability_directory)
        qualified = True
        deferral = ""
    elif enforcement == CAPABILITY_DEFERRED:
        # **Admitted, and recorded as unqualified.** Not a bypass: nothing
        # downstream is told this runtime passed §14.2.1, the flag travels into
        # the run and the report as a stated limitation, and all eighteen §9
        # checks still run on every response. A model whose output does not
        # conform fails the cell with the failing check named and the run keeps
        # its state -- which is exactly why deferring is survivable.
        deferral = (
            getattr(source, "verifier_capability_deferral_decision", "") or ""
        ).strip()
        if not deferral:
            raise PolicyNotApproved(
                [
                    "VERIFIER_CAPABILITY_DEFERRAL_DECISION is unset while "
                    "VERIFIER_CAPABILITY_ENFORCEMENT=deferred: running without a "
                    "capability qualification is a decision, and a decision with "
                    "nobody recorded as having taken it is indistinguishable from "
                    "a default nobody noticed"
                ]
            )
        qualified = False
    else:
        raise PolicyNotApproved(
            [
                f"VERIFIER_CAPABILITY_ENFORCEMENT={enforcement!r} is not supported "
                f"(expected {CAPABILITY_REQUIRED!r} or {CAPABILITY_DEFERRED!r}). It "
                "is refused rather than read as either one: a typo must not become "
                "the permissive reading."
            ]
        )

    return ApprovedPolicies(
        date_screening_policy=date_policy,
        text_version_policy=text_policy,
        date_screening_decision=source.date_screening_decision,
        text_version_decision=source.text_version_decision,
        capability_qualified=qualified,
        capability_deferral_decision=deferral,
    )


__all__ = [
    "ApprovedPolicies",
    "CAPABILITY_DEFERRED",
    "CAPABILITY_REQUIRED",
    "CapabilityNotQualified",
    "EXCLUDE_WITHOUT_VERSION",
    "POLICY_NOT_APPROVED",
    "PROVIDER_PUBLICATION_DATE",
    "PolicyNotApproved",
    "SUPPORTED_TEXT_VERSION_POLICIES",
    "validate_admission_policies",
]
