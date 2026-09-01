"""The single canonical fixture for feature-disclosure verification.

Spec section 15.3 point 4: "The section 2 approved decomposition and the section
4.3 passage set are the single canonical fixture for this document. New examples
reuse them rather than inventing parallel data, so one substring check covers
every quotation."

**The data now lives in :mod:`verification.regression_cases`, and this module is a
re-export.** That direction is forced rather than stylistic. The section 14.2.1
capability gate runs these same cases against a live model, the gate ships *inside*
the application package, and nothing in that package may import the test tree
(``tests/test_capability_runner.py`` asserts it). Two copies would drift, and the
drift would be invisible: the gate would measure a model against passages the
offline suite never pins.

Every name this module exported before still resolves, so
``test_feature_disclosure_verification.py`` and
``test_verification_spec_consistency.py`` are unchanged -- and the latter still
re-extracts the decomposition and the passage set from the specification markdown
and asserts these *are* that data, which is what makes them canonical rather than
merely shared.
"""

from verification.regression_cases import (  # noqa: F401 - re-exported fixture data
    ALTERNATIVE_EMBODIMENT,
    CANONICAL_PASSAGES,
    CASE_IDS,
    CLAIM_1,
    CLAIM_1_ELEMENT_ONLY,
    CLAIM_1_TEXT,
    CLAIM_4_DEPENDENT,
    CLAIM_6_DEPENDENT,
    COMPLETE_CLAIM,
    DECOMPOSITION,
    DECOMPOSITION_JSON,
    DOCUMENT_ID,
    DOCUMENT_IDS,
    FAN_SPEED_CLAIM,
    FIGURE_REFERENCE_PARAGRAPH,
    FIRST_EMBODIMENT,
    INJECTED_PARAGRAPH,
    JURISDICTION,
    PARAGRAPH_31,
    PARAGRAPH_31_TEXT,
    PUBLICATION_NUMBER,
    PUBLICATION_TITLE,
    THERMAL_MANAGEMENT_PARAGRAPH,
    TRANSLATED_CLAIM,
    UNRELATED_PARAGRAPH,
    VERIFIER_CASES,
    RegressionCase,
    case_by_id,
    passage,
)
