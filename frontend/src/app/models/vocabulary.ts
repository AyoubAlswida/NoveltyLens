/**
 * The one place a machine code becomes words an inventor reads.
 *
 * ## Why this is a module and not prose in five templates
 *
 * The screens an inventor meets -- results and evidence, and the final report --
 * render values from closed backend enums: `verification/states.py`'s
 * `CellState` and `PublicationExclusion`, `verification/schema.py`'s `Status`
 * and `Outcome`, and `graph/report.py`'s rendering `class`. Written inline, the
 * same enum picked up different wording on different screens, and a reader
 * comparing the results stage against the report could not tell whether two
 * phrasings meant one thing. One table means they cannot drift.
 *
 * ## Translation, never shortening
 *
 * CLAUDE.md is explicit: `NOT_FOUND_IN_SUPPLIED_PASSAGES` and
 * `NO_DISCLOSURE_FOUND_IN_SUPPLIED_PASSAGES` **never mean "not disclosed"**, and
 * shortening either in a UI or an export is the same class of defect the
 * zero-tolerance policy forbids. So every label here carries the scope with it --
 * "in the passages examined" survives the translation, because it is the entire
 * difference between a finding this tool can support and one it cannot.
 *
 * A label is longer than the code it replaces. That is the point: the code is
 * short because it is a symbol, and the sentence is what the symbol meant.
 *
 * ## The code is never thrown away
 *
 * The screens render the label **and** the code -- the sentence for the person,
 * the code beside it for the auditor reading the same screen against
 * `verification_cells`. A report that showed only prose would be unusable as an
 * audit document, and one that shows only codes is what this module exists to
 * stop shipping.
 *
 * ## An unknown code renders as itself
 *
 * `describe()` never invents a label. A code with no entry comes back verbatim,
 * with an empty meaning, so a backend enum that gains a member surfaces as the
 * raw value rather than being silently mislabelled as the nearest thing in this
 * table. Guessing here would be the fabrication the policy forbids, arriving
 * through the vocabulary instead of through a fallback.
 */

/** One machine code, and what it says in words. */
export interface Term {
  /** The plain-English rendering. Keeps every scope qualifier the code carries. */
  readonly label: string;
  /**
   * One sentence on what the code means, for a tooltip or a legend.
   *
   * Empty for a code this table does not know, which is how an unrecognised
   * value is visible as unrecognised.
   */
  readonly meaning: string;
}

/**
 * Per-requirement statuses (`verification/schema.py::Status`).
 *
 * These are the three values a reviewer can also *choose* at the results stage,
 * so the labels are written to read correctly as an answer to "what did the
 * passages say about this requirement?".
 */
export const STATUS_TERMS: Readonly<Record<string, Term>> = {
  FOUND: {
    label: 'Found in the passages examined',
    meaning:
      'The examined passages state this requirement, and a verbatim quotation is cited for it.',
  },
  NOT_FOUND_IN_SUPPLIED_PASSAGES: {
    label: 'Not found in the passages examined',
    meaning:
      'The examined passages do not state this requirement. This is not a finding that the publication fails to disclose it, only that the text examined here does not.',
  },
  UNCLEAR: {
    label: 'Unclear from the passages examined',
    meaning:
      'The examined passages are ambiguous about this requirement. An ambiguity is never rounded up or down.',
  },
};

/**
 * Feature-level outcomes (`verification/schema.py::Outcome`).
 *
 * Derived by `verification/outcome.py::derive_outcome`, never asserted by a
 * model and never typed by a reviewer.
 */
export const OUTCOME_TERMS: Readonly<Record<string, Term>> = {
  FULLY_DISCLOSED_IN_SUPPLIED_PASSAGES: {
    label: 'Every requirement found in the passages examined',
    meaning:
      'Each requirement of this capability was found in the passages examined for this publication.',
  },
  PARTIALLY_DISCLOSED_IN_SUPPLIED_PASSAGES: {
    label: 'Some requirements found in the passages examined',
    meaning:
      'Some requirements were found and some were not, with nothing left ambiguous.',
  },
  NO_DISCLOSURE_FOUND_IN_SUPPLIED_PASSAGES: {
    label: 'No requirement found in the passages examined',
    meaning:
      'No requirement of this capability was found in the passages examined. This is not a finding that the publication discloses none of them.',
  },
  UNCLEAR: {
    label: 'Unclear from the passages examined',
    meaning:
      'At least one requirement was ambiguous, so no overall answer can be given for this capability.',
  },
};

/**
 * Cell states (`verification/states.py::CellState`).
 *
 * Two of the three are **not** verdicts, and their labels say so without
 * borrowing any disclosure wording -- that is the substitution `states.py`'s own
 * module docstring exists to forbid. `PENDING` is not a `cell_state` in the
 * database; it is what a comparison reads as before one is written, and it is
 * here so that value renders as itself rather than falling through.
 */
export const CELL_STATE_TERMS: Readonly<Record<string, Term>> = {
  VERIFIED: {
    label: 'Checked',
    meaning:
      'A model response passed every validation check, so this comparison has a result.',
  },
  NOT_EVALUATED: {
    label: 'Not evaluated',
    meaning:
      'This comparison did not complete, so no answer exists for it. It is not a finding of any kind.',
  },
  VALIDATION_FAILURE: {
    label: 'Could not be validated',
    meaning:
      'A response came back and failed a validation check, so it was kept as a diagnostic and never used as a result.',
  },
  PENDING: {
    label: 'Not checked yet',
    meaning: 'This comparison has not run.',
  },
};

/**
 * Publication-scoped exclusions (`verification/states.py::PublicationExclusion`).
 *
 * Every label says *not examined* and none of them says anything about the
 * publication's content, because an exclusion is a statement about this run's
 * coverage and never a finding about a patent.
 */
export const EXCLUSION_TERMS: Readonly<Record<string, Term>> = {
  EXCLUDED_DATE: {
    label: 'Not examined, outside the date range',
    meaning:
      'Its publication date is not before the cutoff for this run. This is arithmetic against the recorded date policy, not a judgement, and it cannot be overridden here.',
  },
  EXCLUDED_NO_FULL_TEXT: {
    label: 'Not examined, no full text available',
    meaning:
      'The full-text provider answered and had no eligible text for this exact publication. It cannot be overridden here.',
  },
  EXCLUDED_INELIGIBLE: {
    label: 'Not examined, no citable passage could be built',
    meaning:
      'Text exists, but nothing citable could be built from it. Re-adding the publication sends it through the same checks again.',
  },
};

/**
 * The report's rendering class (`graph/report.py::_cell_class`, spec section 9.8).
 *
 * A validation failure and an unevaluated comparison keep their own class
 * whether or not they were waived, so the labels never say "resolved".
 */
export const CELL_CLASS_TERMS: Readonly<Record<string, Term>> = {
  'machine-generated': {
    label: 'System result, not reviewed',
    meaning: 'A person has not recorded a decision on this comparison.',
  },
  'human-reviewed': {
    label: 'Reviewed by a person',
    meaning: 'A person reviewed this comparison and agreed with the system.',
  },
  'human-overridden': {
    label: 'Changed by a reviewer',
    meaning:
      'A person reviewed this comparison and recorded a different answer. The system result is kept beside it, unchanged.',
  },
  unresolved: {
    label: 'Left unresolved',
    meaning: 'This comparison was left without a resolution.',
  },
  'validation-failure': {
    label: 'Could not be validated',
    meaning:
      'The response failed a validation check and was never used as a result.',
  },
  unevaluated: {
    label: 'Not evaluated',
    meaning: 'This comparison did not complete, so no answer exists for it.',
  },
};

/** Review overlay statuses (`review_status` in migration 0006). */
export const REVIEW_STATUS_TERMS: Readonly<Record<string, Term>> = {
  PENDING: { label: 'Awaiting your decision', meaning: 'No decision recorded yet.' },
  APPROVED: {
    label: 'Agreed with the system',
    meaning: 'A person reviewed this and recorded the same answer.',
  },
  REJECTED: {
    label: 'Rejected by a reviewer',
    meaning: 'A person reviewed this and rejected the system result.',
  },
  OVERRIDDEN: {
    label: 'Changed by a reviewer',
    meaning:
      'A person recorded a different answer. The system result is kept beside it.',
  },
  NEEDS_EXPERT_REVIEW: {
    label: 'Referred for expert review',
    meaning: 'A person referred this comparison to someone else.',
  },
  WAIVED: {
    label: 'Waived, with the gap disclosed',
    meaning:
      'A person recorded that this is being accepted unresolved. A waiver discloses a gap; it never becomes a result.',
  },
};

/**
 * Every table, in one object, so a test can assert totality against the backend
 * enums and injectivity within each table without enumerating them by hand.
 */
export const VOCABULARY: Readonly<Record<string, Readonly<Record<string, Term>>>> = {
  status: STATUS_TERMS,
  outcome: OUTCOME_TERMS,
  cell_state: CELL_STATE_TERMS,
  exclusion: EXCLUSION_TERMS,
  cell_class: CELL_CLASS_TERMS,
  review_status: REVIEW_STATUS_TERMS,
};

/**
 * The words for one code, or the code itself.
 *
 * **Never guesses.** An unrecognised value comes back as its own label with an
 * empty meaning, so a backend enum that grows a member is visible on the screen
 * as the raw code rather than being quietly rendered as the nearest neighbour in
 * one of the tables above.
 */
export function describe(
  table: Readonly<Record<string, Term>>,
  code: string | null | undefined,
): Term {
  const key = (code ?? '').trim();
  if (!key) return { label: '', meaning: '' };
  return table[key] ?? { label: key, meaning: '' };
}

/** Convenience readers, so a template does not have to name a table. */
export const statusTerm = (code: string | null | undefined) => describe(STATUS_TERMS, code);
export const outcomeTerm = (code: string | null | undefined) => describe(OUTCOME_TERMS, code);
export const cellStateTerm = (code: string | null | undefined) =>
  describe(CELL_STATE_TERMS, code);
export const exclusionTerm = (code: string | null | undefined) =>
  describe(EXCLUSION_TERMS, code);
export const cellClassTerm = (code: string | null | undefined) =>
  describe(CELL_CLASS_TERMS, code);
export const reviewStatusTerm = (code: string | null | undefined) =>
  describe(REVIEW_STATUS_TERMS, code);
