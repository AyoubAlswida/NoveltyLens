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
 * Every table, in one object, so a test can assert totality against the backend
 * enums and injectivity within each table without enumerating them by hand.
 */
export const VOCABULARY: Readonly<Record<string, Readonly<Record<string, Term>>>> = {
  status: STATUS_TERMS,
};

/*
 * `outcome`, `cell_state`, `exclusion`, `cell_class` and `review_status` were
 * REMOVED with the screens that rendered them. Every one of them named part of
 * the verification matrix -- a cell's state, a disclosure outcome, why a
 * publication was excluded from citation, a reviewer's status on a cell -- and
 * feature verification was withdrawn from the product, so nothing renders any of
 * it. The backend enums behind them are retained (spec section 10.3); a
 * translation table for words no screen shows is worse than nothing, because it
 * reads as evidence that some screen still shows them.
 *
 * `status` stays: run statuses are still rendered, and a run still moves through
 * them.
 */

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
