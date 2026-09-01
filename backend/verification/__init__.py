"""Feature Disclosure Verification.

Implements ``docs/prompts/feature-disclosure-verification.md``: one approved
technical feature, checked against one patent publication, using only 1-5
supplied passages from that publication.

**What a verdict from this package does and does not say.** It answers whether
*these supplied passages* state each required element and relationship of *this
one approved feature*. It does not say what the patent discloses - it saw a
handful of passages, not the document - and it says nothing about novelty,
patentability, or obviousness, which are legal conclusions a person draws from
the assembled evidence.

That scoping is carried by the vocabulary and enforced by the code:
``NOT_FOUND_IN_SUPPLIED_PASSAGES`` and
``NO_DISCLOSURE_FOUND_IN_SUPPLIED_PASSAGES`` are statements about the passages,
never about the patent. Any UI, export, or report sentence shortening either to
"not disclosed", "absent", or "the patent does not teach" reports a stronger
claim than the evidence supports, which is the same class of defect the
repository's zero-tolerance policy forbids.

Layout:

- :mod:`~verification.prompt` - the two canonical prompt constants, byte-identical
  to spec sections 3 and 4.
- :mod:`~verification.render` - filling them (sections 4.1-4.3).
- :mod:`~verification.decomposition` - the approved requirements (section 2).
- :mod:`~verification.passages` - passage shape, canonicalization, selection, and
  pre-call validation (sections 7 and 8).
- :mod:`~verification.schema` - the output contract (sections 10 and 10.1).
- :mod:`~verification.outcome` - deterministic outcome derivation (section 6).
- :mod:`~verification.checks` - response validation, checks 1-18 (section 9).
- :mod:`~verification.review` - the human review overlay (sections 13.1-13.5).
- :mod:`~verification.verify` - one verification call (sections 5, 9.1, 14).
- :mod:`~verification.states` - the states the application owns (section 1.2).

**Not implemented here**, and deliberately so: the four human gates of spec
section 11.1, the matrix that would hold these cells, and any persistence or SSE
surface for either. Spec section 16 lists their prerequisites as still needing
approval - per-user identity for a real audit trail, a fourth SSE event for a
paused run, and a LangGraph checkpointer for resumable ones - and spec section 17
lists enrichment work (17.2, 17.3, 17.6, 17.7) that the evidence layer needs
before a real passage can reach this code from a real provider.
"""

from .checks import parse_response, validate_evidence, validate_response
from .decomposition import (
    ApprovedDecomposition,
    ApprovedElement,
    ApprovedRelationship,
    validate_decomposition,
)
from .outcome import derive_outcome
from .passages import (
    MAX_PASSAGES,
    PREFERRED_MIN_PASSAGES,
    Passage,
    PassageSelection,
    canonicalize,
    canonicalize_passage,
    select_passages,
    validate_passage_set,
)
from .prompt import PROMPT_MARKER, PROMPT_VERSION, SYSTEM_PROMPT, USER_PROMPT_TEMPLATE
from .render import build_user_prompt, render_elements_block, render_passages_block, render_relationships_block
from .review import (
    AuditRecord,
    EvidenceAction,
    RequirementReview,
    ReviewOverlay,
    ReviewStatus,
    Waiver,
    derive_review_status,
    is_overridden,
    reviewed_outcome,
    validate_review,
    waive_cell,
)
from .schema import (
    RESPONSE_JSON_SCHEMA,
    Ambiguity,
    CombinationBasis,
    ContextRequest,
    ElementResult,
    EvidenceItem,
    FeatureDisclosureVerification,
    Outcome,
    RelationshipResult,
    Status,
    provider_response_format,
)
from .states import (
    CellState,
    PassageSetIneligible,
    PublicationExclusion,
    ResponseValidationError,
    ReviewValidationError,
    VerificationError,
    VerifierCapabilityError,
)
from .verify import (
    CellResult,
    PublicationUnderEvaluation,
    max_tokens_for,
    structured_output_format,
    verify_feature_disclosure,
)


__all__ = [
    "MAX_PASSAGES",
    "PREFERRED_MIN_PASSAGES",
    "PROMPT_MARKER",
    "PROMPT_VERSION",
    "RESPONSE_JSON_SCHEMA",
    "SYSTEM_PROMPT",
    "USER_PROMPT_TEMPLATE",
    "Ambiguity",
    "ApprovedDecomposition",
    "ApprovedElement",
    "ApprovedRelationship",
    "AuditRecord",
    "CellResult",
    "CellState",
    "CombinationBasis",
    "ContextRequest",
    "ElementResult",
    "EvidenceAction",
    "EvidenceItem",
    "FeatureDisclosureVerification",
    "Outcome",
    "Passage",
    "PassageSelection",
    "PassageSetIneligible",
    "PublicationExclusion",
    "PublicationUnderEvaluation",
    "RelationshipResult",
    "RequirementReview",
    "ResponseValidationError",
    "ReviewOverlay",
    "ReviewStatus",
    "ReviewValidationError",
    "Status",
    "VerificationError",
    "VerifierCapabilityError",
    "Waiver",
    "build_user_prompt",
    "canonicalize",
    "canonicalize_passage",
    "derive_outcome",
    "derive_review_status",
    "is_overridden",
    "max_tokens_for",
    "parse_response",
    "provider_response_format",
    "render_elements_block",
    "render_passages_block",
    "render_relationships_block",
    "reviewed_outcome",
    "select_passages",
    "structured_output_format",
    "validate_decomposition",
    "validate_evidence",
    "validate_passage_set",
    "validate_response",
    "validate_review",
    "verify_feature_disclosure",
    "waive_cell",
]
