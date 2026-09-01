import { Component, computed, input } from '@angular/core';
import {
  cellClassTerm,
  cellStateTerm,
  exclusionTerm,
  outcomeTerm,
  reviewStatusTerm,
  type Term,
} from '../models/vocabulary';

/**
 * The final report of a reviewed run.
 *
 * Used by the draft review and by the approved-report screen, with only the
 * status banner differing -- one renderer, so a draft and an approved report
 * cannot drift into showing different things.
 *
 * ## What this component refuses to do
 *
 * - **It never collapses a non-verdict into a verdict.** A comparison that was
 *   not evaluated, or whose response could not be validated, renders as itself,
 *   with its failing check or its reason.
 * - **Publications that were not examined are a separate table**, never matrix
 *   rows (case AH), and are labelled *not evaluated*, because leaving one out is
 *   a statement about coverage and never a finding about a patent.
 * - **The system's result and a person's decision stay in separate columns**
 *   (constraint 7).
 * - **A waiver appears with its unresolved limitation**, and the comparison it
 *   waives keeps its state.
 * - **No legal-opinion, exhaustive-search, or guaranteed-novelty wording.** The
 *   scope note is fixed text and is not derived from the data, so no run can
 *   produce a report without it.
 *
 * ## It reads what `graph/report.py` actually writes
 *
 * Four sections of this report rendered blank against a draft that carried the
 * data, because the accessors were written against a shape the draft builder
 * does not produce. Each is called out at its accessor, but the class of defect
 * is worth naming once: **every one of them failed silently and looked like a
 * true negative** -- "Waivers (0), None" on a report with waivers reads exactly
 * like a report with none, and the waiver section is the one place this product
 * discloses a gap it did not close.
 */
@Component({
  selector: 'app-verification-report',
  standalone: true,
  template: `
    <article class="verification-report" [attr.data-report-status]="status()">
      <header class="report-banner" [attr.data-report-status]="status()">
        @if (status() !== 'APPROVED') {
          <strong>DRAFT — NOT APPROVED</strong>
        } @else {
          <strong>APPROVED</strong>
          <span class="report-approved-by">{{ approvedBy() }}</span>
        }
      </header>

      <!-- The draft's own coverage statement, printed verbatim. It is written
           by COVERAGE_STATEMENT in graph/report.py and stored with the draft, so
           an approved report carries the sentence that was approved rather than
           whatever this component would say today. -->
      @if (value('coverage_statement')) {
        <p class="report-coverage">{{ value('coverage_statement') }}</p>
      }

      <section class="report-versions">
        <h3>What this report was built from</h3>
        <dl class="version-grid">
          <dt>Report version</dt><dd class="mono">{{ value('report_version') || '—' }}</dd>
          <dt>Invention understanding</dt>
          <dd class="mono">v{{ value('decomposition_version') || '—' }}</dd>
          <dt>Approved publications</dt>
          <dd class="mono">v{{ value('candidate_set_version') || '—' }}</dd>
          <dt>Date rule</dt><dd class="mono">{{ value('date_policy') || '—' }}</dd>
          <dt>Text-version rule</dt><dd class="mono">{{ value('text_version_policy') || '—' }}</dd>
        </dl>
      </section>

      <section class="report-matrix">
        <h3>What each publication says about your invention ({{ matrix().length }})</h3>
        @if (matrix().length === 0) {
          <p class="gate-empty">No comparison was produced.</p>
        }
        <table class="matrix-table">
          <caption class="sr-only">
            Each capability compared against each publication examined
          </caption>
          <thead>
            <tr>
              <th scope="col">Capability</th>
              <th scope="col">Publication</th>
              <th scope="col">What the system found</th>
              <th scope="col">Your decision</th>
              <th scope="col">How this line was produced</th>
            </tr>
          </thead>
          <tbody>
            @for (cell of matrix(); track cell['cell_id']) {
              <tr>
                <th scope="row" class="mono">{{ cell['feature_id'] }}</th>
                <td class="mono">{{ cell['publication_number'] }}</td>
                <td>
                  <span
                    class="cell-state"
                    [attr.data-cell-state]="machineState(cell)"
                    [attr.title]="cellWords(machineState(cell)).meaning"
                  >
                    {{ cellWords(machineState(cell)).label }}
                  </span>
                  <span class="machine-code">{{ machineState(cell) }}</span>
                  @if (machineOutcome(cell)) {
                    <span class="cell-outcome">
                      {{ outcomeWords(machineOutcome(cell)).label }}
                      <span class="machine-code">{{ machineOutcome(cell) }}</span>
                    </span>
                  }
                  <!-- A comparison that could not be validated prints its failing
                       check, and one that was not evaluated prints its reason.
                       Blanking either would leave the report saying a comparison
                       has no answer without saying why. -->
                  @if (cell['failed_check']) {
                    <span class="cell-check">failed check {{ cell['failed_check'] }}</span>
                  }
                  @if (cell['not_evaluated_reason']) {
                    <span class="cell-check">{{ cell['not_evaluated_reason'] }}</span>
                  }
                </td>
                <td>
                  @if (humanStatus(cell); as human) {
                    <span class="human-decision" [attr.data-review]="human">
                      {{ reviewWords(human).label }}
                    </span>
                    <span class="machine-code">{{ human }}</span>
                  } @else {
                    <span class="human-none">not reviewed by a person</span>
                  }
                  @if (reviewedOutcome(cell)) {
                    <span class="cell-outcome">
                      {{ outcomeWords(reviewedOutcome(cell)).label }}
                      <span class="machine-code">{{ reviewedOutcome(cell) }}</span>
                    </span>
                  }
                </td>
                <td>
                  <span class="cell-class" [attr.data-cell-class]="cellClass(cell)">
                    {{ classWords(cellClass(cell)).label }}
                  </span>
                  <span class="machine-code">{{ cellClass(cell) }}</span>
                </td>
              </tr>
            }
          </tbody>
        </table>
      </section>

      <!-- A SEPARATE table. A publication that was not examined is never a
           matrix row. -->
      <section class="report-exclusions">
        <h3>Publications that were not examined ({{ exclusions().length }})</h3>
        <p class="report-note">
          These were <strong>not evaluated</strong> against your invention. Their
          absence from the table above is a limit on how far this run could
          reach, and is <strong>not</strong> a finding that they disclose
          nothing.
        </p>
        @if (exclusions().length === 0) {
          <p class="gate-empty">None.</p>
        }
        <table class="exclusion-table">
          <caption class="sr-only">Publications excluded before any comparison</caption>
          <thead>
            <tr>
              <th scope="col">Publication</th>
              <th scope="col">Why it was not examined</th>
              <th scope="col">What the system reported</th>
            </tr>
          </thead>
          <tbody>
            @for (item of exclusions(); track excludedNumber(item)) {
              <tr>
                <th scope="row" class="mono">{{ excludedNumber(item) }}</th>
                <td>
                  <span
                    class="exclusion-state"
                    [attr.data-exclusion]="item['state']"
                    [attr.title]="exclusionWords(item['state']).meaning"
                  >
                    {{ exclusionWords(item['state']).label }}
                  </span>
                  <span class="machine-code">{{ item['state'] }}</span>
                </td>
                <td>{{ item['reason'] }}</td>
              </tr>
            }
          </tbody>
        </table>
      </section>

      <section class="report-waivers">
        <h3>Gaps accepted without being resolved ({{ waivers().length }})</h3>
        <p class="report-note">
          A waiver records that a reviewer accepted an unresolved comparison and
          says what remains unresolved. It is a disclosed gap and
          <strong>never becomes a result</strong> — the comparison it covers
          keeps its own state in the table above.
        </p>
        @if (waivers().length === 0) {
          <p class="gate-empty">None.</p>
        }
        <ul class="waiver-list">
          @for (waiver of waivers(); track $index) {
            <li>
              <span class="mono">{{ waiver['cell_id'] }}</span>
              <span class="waiver-reason">{{ waiver['reason'] }}</span>
              <!-- The limitation travels WITH the waiver. A waiver without one
                   would read as a resolution. -->
              <strong class="waiver-limitation">
                {{ waiver['unresolved_limitation'] }}
              </strong>
            </li>
          }
        </ul>
      </section>

      <section class="report-unresolved">
        <h3>Comparisons left without an answer ({{ unresolved().length }})</h3>
        @if (unresolved().length === 0) {
          <p class="gate-empty">None.</p>
        } @else {
          <p class="report-note">
            Each of these produced no result. Waived or not, every one is listed
            here as what it is.
          </p>
        }
        <ul class="unresolved-list">
          @for (item of unresolved(); track $index) {
            <li>
              <span class="mono">{{ item['cell_id'] }}</span>
              <span
                class="cell-state"
                [attr.data-cell-state]="item['state']"
                [attr.title]="cellWords(item['state']).meaning"
              >
                {{ cellWords(item['state']).label }}
              </span>
              <span class="machine-code">{{ item['state'] }}</span>
              @if (item['failed_check']) {
                <span class="cell-check">failed check {{ item['failed_check'] }}</span>
              }
              @if (item['reason']) {
                <span class="cell-check">{{ item['reason'] }}</span>
              }
              @if (item['waived']) {
                <span class="human-decision" data-review="WAIVED">
                  {{ reviewWords('WAIVED').label }}
                </span>
                @for (limitation of limitationsOf(item); track $index) {
                  <strong class="waiver-limitation">{{ limitation }}</strong>
                }
              }
            </li>
          }
        </ul>
      </section>

      <!-- What the reviewer wrote, on the report they approved. A caveat typed
           at the review and then dropped from the rendered report is a caveat
           nobody reading the report ever sees. -->
      @if (value('caveats')) {
        <section class="report-caveats">
          <h3>Caveats recorded by the reviewer</h3>
          <p>{{ value('caveats') }}</p>
        </section>
      }
      @if (value('comments')) {
        <section class="report-comments">
          <h3>Reviewer comments</h3>
          <p>{{ value('comments') }}</p>
        </section>
      }

      <footer class="report-limits">
        <h3>Scope and limitations</h3>
        <p>
          Every result is scoped to the passages examined. A negative result
          means the passages examined did not state the requirement — it is not a
          statement that the publication does not disclose it, and it is not a
          statement about any text that was not retrieved.
        </p>
        <p>
          Publications listed as not examined were <strong>not evaluated</strong>.
          This report does not cover them.
        </p>
        <p class="report-disclaimer">
          This is a technical comparison of specified requirements against
          retrieved passages. It is <strong>not a legal opinion</strong>, it is
          <strong>not an exhaustive prior-art search</strong>, and it does
          <strong>not determine that anything is novel or patentable</strong>.
        </p>
        @if (auditNote()) {
          <p class="report-audit">{{ auditNote() }}</p>
        }
      </footer>
    </article>
  `,
})
export class VerificationReport {
  readonly report = input<Record<string, unknown> | null>(null);
  readonly status = input<string>('DRAFT');

  private body(): Record<string, unknown> {
    /**
     * The report content, whichever envelope it arrived in.
     *
     * Three shapes reach this component and all three are legitimate: the draft
     * itself (the draft review's payload), a nested verification block, and
     * the report *version* row from `GET /api/runs/{id}/report`, which wraps its
     * content in `draft` and carries `approved_by`, `comments` and `caveats`
     * beside it.
     *
     * The third was not unwrapped, so an approved report rendered with every
     * section empty -- "no comparison was produced" against a report that
     * carried seven. It is **merged** rather than substituted because the
     * wrapper carries fields the content does not. Replacing would have traded
     * one set of blanks for another.
     */
    const value = this.report();
    if (!value) return {};
    let merged: Record<string, unknown> = { ...value };
    for (const key of ['verification', 'draft']) {
      const nested = value[key];
      if (nested && typeof nested === 'object' && !Array.isArray(nested)) {
        merged = { ...merged, ...(nested as Record<string, unknown>) };
      }
    }
    return merged;
  }

  value(key: string): string {
    const raw = this.body()[key];
    return raw === undefined || raw === null ? '' : String(raw);
  }

  readonly matrix = computed(() => (this.body()['matrix'] ?? []) as Array<Record<string, unknown>>);

  readonly exclusions = computed(
    () => (this.body()['excluded_publications'] ?? []) as Array<Record<string, unknown>>,
  );

  /**
   * Every waiver in the report, collected from the comparisons that carry them.
   *
   * **`graph/report.py` writes no top-level waivers key.** It attaches each
   * waiver to its own matrix entry, and this list was read from a top-level one
   * -- always absent, so the section rendered a count of zero and the word None
   * on every report that had any. That is the worst shape this defect could
   * take: the waiver section exists precisely to disclose a gap a reviewer chose
   * not to close, and an empty one is indistinguishable from a run with nothing
   * to disclose.
   *
   * A top-level list is still read first, so a future report shape that supplies
   * one is rendered from it rather than being ignored.
   */
  readonly waivers = computed(() => {
    const top = this.body()['waivers'];
    if (Array.isArray(top) && top.length > 0) return top as Array<Record<string, unknown>>;
    const collected: Array<Record<string, unknown>> = [];
    for (const cell of this.matrix()) {
      for (const waiver of (cell['waivers'] ?? []) as Array<Record<string, unknown>>) {
        collected.push({ cell_id: cell['cell_id'], ...waiver });
      }
    }
    return collected;
  });

  /**
   * The comparisons that produced no result, as the **list** the draft holds.
   *
   * This was read as a dictionary of counts and rendered as three numbers.
   * `build_draft` builds a list of entries, each naming its comparison, its
   * state, its failing check and any unresolved limitations -- so the three
   * numbers were zero on every report, however many comparisons were
   * unresolved. Rendering the list also keeps the failing check, which a count
   * cannot carry and which `graph/report.py`'s own contract requires be printed
   * beside the comparison.
   */
  readonly unresolved = computed(() => {
    const raw = this.body()['unresolved'];
    return Array.isArray(raw) ? (raw as Array<Record<string, unknown>>) : [];
  });

  readonly auditNote = computed(() => String(this.body()['audit_note'] ?? ''));
  readonly approvedBy = computed(() => String(this.body()['approved_by'] ?? ''));

  /**
   * The publication number, under either key the two producers use.
   *
   * `build_draft` emits `publication_number`; a `publication_exclusions` row and
   * the results stage both call it `requested_number`. This read was
   * `requested_number` only, so the column was blank for every excluded
   * publication in every report. Both are read rather than one being renamed,
   * because drafts already stored carry the key they were written with and an
   * approved report is immutable.
   */
  excludedNumber(item: Record<string, unknown>): string {
    return String(item['publication_number'] ?? item['requested_number'] ?? '');
  }

  limitationsOf(item: Record<string, unknown>): string[] {
    const raw = item['unresolved_limitations'];
    return Array.isArray(raw) ? raw.map((entry) => String(entry)) : [];
  }

  machineState(cell: Record<string, unknown>): string {
    const machine = cell['machine'] as Record<string, unknown> | undefined;
    return String(cell['state'] ?? machine?.['state'] ?? 'PENDING');
  }

  /**
   * `build_draft` names this `machine_outcome`, to keep it visibly apart from the
   * reviewed one. Read under both names: a cell row calls it `outcome`.
   */
  machineOutcome(cell: Record<string, unknown>): string {
    const machine = cell['machine'] as Record<string, unknown> | undefined;
    return String(cell['machine_outcome'] ?? cell['outcome'] ?? machine?.['outcome'] ?? '');
  }

  humanStatus(cell: Record<string, unknown>): string {
    const review = cell['review'] as Record<string, unknown> | undefined;
    return review ? String(review['status'] ?? '') : '';
  }

  /** The reviewed outcome, kept beside the machine one and never merged into it. */
  reviewedOutcome(cell: Record<string, unknown>): string {
    const review = cell['review'] as Record<string, unknown> | undefined;
    return String(cell['reviewed_outcome'] ?? review?.['reviewed_outcome'] ?? '');
  }

  cellClass(cell: Record<string, unknown>): string {
    return String(cell['class'] ?? 'machine-generated');
  }

  /** Words for a code, keeping the code. See `models/vocabulary.ts`. */
  cellWords(code: unknown): Term {
    return cellStateTerm(code == null ? '' : String(code));
  }

  outcomeWords(code: unknown): Term {
    return outcomeTerm(code == null ? '' : String(code));
  }

  exclusionWords(code: unknown): Term {
    return exclusionTerm(code == null ? '' : String(code));
  }

  reviewWords(code: unknown): Term {
    return reviewStatusTerm(code == null ? '' : String(code));
  }

  classWords(code: unknown): Term {
    return cellClassTerm(code == null ? '' : String(code));
  }
}
