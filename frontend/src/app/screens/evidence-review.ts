import { Component, computed, inject, input, output, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Icon } from '../shared/icon';
import { classify, GateShell, type GateState } from './gate-shell';
import { RunsService } from '../services/runs';
import {
  cellStateTerm,
  outcomeTerm,
  reviewStatusTerm,
  statusTerm,
  STATUS_TERMS,
  type Term,
} from '../models/vocabulary';

/**
 * The four values this screen can show that are **not** a disclosure verdict.
 *
 * Spec Gate F item 6 and §16 decision 9: every one renders distinctly. None may
 * render as a blank, a neutral, or "not disclosed" — that would turn "we could
 * not evaluate this" into "we evaluated it and found nothing", which is a
 * different and much stronger claim.
 *
 * Two notes on what this list is and is not. It said "six" and named four, and
 * only two of them (`NOT_EVALUATED`, `VALIDATION_FAILURE`) are `cell_state`
 * members -- `PENDING` is what a comparison reads as before a state is written
 * and `UNCLEAR` is an *outcome*, so this is the set of non-verdict values a
 * reader meets here rather than one backend enum.
 *
 * **It has no reader.** The property is enforced where the values are rendered:
 * `models/vocabulary.ts` gives each one its own words, and a test refuses two
 * codes in a table sharing a label. Kept as the statement of the rule, not
 * mistaken for the mechanism that holds it.
 */
export const NON_VERDICT_STATES = [
  'PENDING',
  'UNCLEAR',
  'NOT_EVALUATED',
  'VALIDATION_FAILURE',
] as const;

interface Cell {
  cell_id: string;
  feature_id: string;
  publication_number: string;
  state: string;
  outcome?: string;
  failed_check?: string | null;
  reason?: string | null;
  result_version: number;
  source_result_version?: number;
}

interface Evidence {
  chunk_id: string;
  locator: string;
  locator_kind?: string;
  locator_source?: string;
  exact_quote: string;
}

interface RequirementResult {
  element_id?: string;
  relationship_id?: string;
  status: string;
  evidence: Evidence[];
  note?: string;
}

interface Passage {
  chunk_id: string;
  section: string;
  locator: string;
  locator_kind?: string;
  locator_source?: string;
  text: string;
}

/**
 * **Workflow Gate C — evidence and verification-cell review** (spec §12.3).
 *
 * ## Machine and human are never merged
 *
 * Constraint 7. The machine's status and the reviewer's status sit side by side
 * in the same row, labelled, with the machine's kept read-only. A validated
 * `machine_result` is immutable; a review is a separate overlay stored beside it.
 * Collapsing them would present a model guess and an expert judgement as the same
 * thing.
 *
 * ## The reviewed outcome is derived, and there is no control to type one
 *
 * Gate F item 9. `derive_outcome` is the application's, and §13.4 check 4 rejects
 * a reviewed outcome that contradicts its own reviewed statuses. The screen shows
 * the derived value read-only, so a reviewer cannot produce a contradiction the
 * backend would have to refuse.
 *
 * ## A waiver discloses a gap; it never becomes a verdict
 *
 * Gate F item 10 and cases AJ/AZ. Both a reason and an unresolved-limitation
 * statement are required before the control enables, and the cell keeps its state
 * afterwards — a waived `VALIDATION_FAILURE` still renders as one, here and in
 * the report.
 *
 * ## Evidence is shown in place
 *
 * Gate F item 8. Each cited passage is rendered with the exact quote highlighted
 * inside it, and **the locator and its kind** are shown: a reviewer needs to know
 * whether `paragraph [0031]` is a provider id or a reproducible source range.
 */
@Component({
  selector: 'app-evidence-review',
  standalone: true,
  imports: [FormsModule, Icon, GateShell],
  template: `
    <app-gate-shell
      gate="C"
      title="Review the evidence"
      [runId]="runId()"
      [state]="state()"
      [detail]="detail()"
      [blocking]="blocking()"
      [rules]="RULES"
      [busy]="state() === 'validating'"
      approveLabel="Complete review"
      [canApprove]="blocking().length === 0"
      (approve)="complete()"
      (back)="back.emit()"
      (reload)="reload()"
    >
      <section class="gate-body">
        <h3>What each publication says about your invention ({{ cells().length }})</h3>
        <p class="gate-note">
          One row per capability, per publication. Every result is scoped to
          the passages examined: <strong>not found in the passages
          examined</strong> means the text examined here does not state the
          requirement, never that the publication fails to disclose it.
        </p>
        <p class="gate-note">
          The plain-English wording is the system's own result put into words.
          The code beside it is the recorded value, kept so this screen can be
          read against the stored record.
        </p>

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
              <th scope="col">Result version</th>
              <th scope="col"><span class="sr-only">Open</span></th>
            </tr>
          </thead>
          <tbody>
            @for (cell of cells(); track cell.cell_id) {
              <tr [class.row-open]="openCellId() === cell.cell_id">
                <th scope="row" class="mono">{{ cell.feature_id }}</th>
                <td class="mono">{{ cell.publication_number }}</td>
                <td>
                  <!-- MACHINE. Read-only, always rendered as itself: the words
                       are a translation of the code, never a softening of it,
                       and the code stays visible beside them. -->
                  <span
                    class="cell-state"
                    [attr.data-cell-state]="cell.state"
                    [attr.title]="cellWords(cell.state).meaning"
                  >
                    {{ cellWords(cell.state).label }}
                  </span>
                  <span class="machine-code">{{ cell.state }}</span>
                  @if (cell.outcome) {
                    <span class="cell-outcome" [attr.title]="outcomeWords(cell.outcome).meaning">
                      {{ outcomeWords(cell.outcome).label }}
                      <span class="machine-code">{{ cell.outcome }}</span>
                    </span>
                  }
                  @if (cell.failed_check) {
                    <span class="cell-check">failed {{ cell.failed_check }}</span>
                  }
                </td>
                <td>
                  <!-- HUMAN. A separate column, never merged with the machine's. -->
                  @if (reviewFor(cell.cell_id); as review) {
                    <span class="human-decision" [attr.data-review]="review['status']">
                      {{ reviewWords(review['status']).label }}
                    </span>
                    <span class="machine-code">{{ review['status'] }}</span>
                  } @else if (waiverFor(cell.cell_id)) {
                    <span class="human-decision" data-review="WAIVED">
                      {{ reviewWords('WAIVED').label }}
                    </span>
                    <span class="machine-code">WAIVED</span>
                  } @else {
                    <span class="human-none">no decision yet</span>
                  }
                </td>
                <td class="mono">{{ cell.result_version }}</td>
                <td>
                  <button
                    class="btn-outlined btn-small"
                    type="button"
                    [attr.aria-expanded]="openCellId() === cell.cell_id"
                    [attr.aria-label]="'Open cell ' + cell.cell_id"
                    (click)="open(cell)"
                  >
                    <app-icon name="ChevronDown" /> Open
                  </button>
                </td>
              </tr>
            }
          </tbody>
        </table>

        @if (openCellId(); as cellId) {
          <section class="cell-detail" [attr.aria-label]="'Detail for ' + cellId">
            <h3>Cell detail <span class="mono">{{ cellId }}</span></h3>

            @if (cellLoading()) {
              <div class="spinner" aria-hidden="true"></div>
            } @else {
              <h4>Each requirement, and the text it was checked against</h4>
              <table class="requirement-result-table">
                <caption class="sr-only">
                  System result and your decision, per requirement
                </caption>
                <thead>
                  <tr>
                    <th scope="col">Requirement</th>
                    <th scope="col">What the system found</th>
                    <th scope="col">Your decision</th>
                    <th scope="col">Text it cited</th>
                  </tr>
                </thead>
                <tbody>
                  @for (req of requirements(); track requirementId(req)) {
                    <tr>
                      <th scope="row" class="mono">{{ requirementId(req) }}</th>
                      <td>
                        <span
                          class="machine-status"
                          [attr.data-status]="req.status"
                          [attr.title]="statusWords(req.status).meaning"
                        >
                          {{ statusWords(req.status).label }}
                        </span>
                        <span class="machine-code">{{ req.status }}</span>
                      </td>
                      <td>
                        <label class="sr-only" [attr.for]="'ov-' + requirementId(req)">
                          Reviewed status for {{ requirementId(req) }}
                        </label>
                        <select
                          [attr.id]="'ov-' + requirementId(req)"
                          [ngModel]="overrides()[requirementId(req)] ?? req.status"
                          (ngModelChange)="setOverride(requirementId(req), $event)"
                          [disabled]="state() === 'validating' || !canReview()"
                        >
                          <!-- The VALUE stays the enum the backend validates.
                               Only the text a person reads is translated, so a
                               reviewer chooses a sentence and the request still
                               carries the code. -->
                          @for (choice of STATUS_CHOICES; track choice.code) {
                            <option [value]="choice.code">{{ choice.label }}</option>
                          }
                        </select>
                      </td>
                      <td>
                        @for (item of req.evidence; track item.chunk_id + item.exact_quote) {
                          <div class="evidence-item">
                            <span class="locator">{{ item.locator }}</span>
                            <span class="locator-kind" [attr.data-kind]="item.locator_kind">
                              {{ item.locator_kind || 'LOCATOR' }}
                            </span>
                            <span class="locator-source">{{ item.locator_source || '' }}</span>
                            <blockquote class="exact-quote">{{ item.exact_quote }}</blockquote>
                          </div>
                        }
                        @if (req.evidence.length === 0) {
                          <span class="human-none">no evidence cited</span>
                        }
                      </td>
                    </tr>
                  }
                </tbody>
              </table>

              <!-- DERIVED and read-only. There is no control to type an outcome. -->
              @if (derivedOutcome()) {
                <p class="derived-outcome">
                  Overall for this capability, from your decisions above:
                  <strong>{{ outcomeWords(derivedOutcome()).label }}</strong>
                  <span class="machine-code">{{ derivedOutcome() }}</span>
                  <span class="derived-note">
                    Worked out from the requirement decisions, not typed. There is
                    no control to enter one, because an overall answer that
                    contradicted its own requirements would be refused.
                  </span>
                </p>
              } @else {
                <p class="derived-outcome">
                  No requirements were returned for this comparison, so there is
                  nothing to work an overall answer out from.
                </p>
              }

              <h4>The passages examined</h4>
              @if (passages().length === 0) {
                <p class="gate-empty">
                  No passages were supplied to the verifier for this cell.
                </p>
              }
              @for (passage of passages(); track passage.chunk_id) {
                <article class="passage">
                  <header>
                    <span class="mono">{{ passage.chunk_id }}</span>
                    <span class="passage-section">{{ passage.section }}</span>
                    <span class="locator">{{ passage.locator }}</span>
                    <span class="locator-kind" [attr.data-kind]="passage.locator_kind">
                      {{ passage.locator_kind || 'LOCATOR' }}
                    </span>
                  </header>
                  <p class="passage-text" [innerHTML]="highlighted(passage)"></p>
                </article>
              }

              <div class="cell-actions">
                <button
                  class="btn-contained"
                  type="button"
                  [disabled]="state() === 'validating' || !canReview()"
                  (click)="saveReview()"
                >
                  <app-icon name="PenLine" /> Save decision
                </button>
                <button
                  class="btn-outlined"
                  type="button"
                  [disabled]="state() === 'validating'"
                  (click)="requestRerun()"
                >
                  <app-icon name="RotateCcw" /> Request more context and re-run
                </button>
              </div>
              @if (rerunNote()) {
                <p class="field-note" role="status">{{ rerunNote() }}</p>
              }

              <details class="waiver-block">
                <summary>Waive this cell</summary>
                <p class="gate-note">
                  A waiver <strong>discloses a gap; it never becomes a verdict</strong>.
                  The cell keeps its state here, in the draft, and in the final
                  report.
                </p>
                <label for="waiver-reason">Reason</label>
                <input id="waiver-reason" [(ngModel)]="waiverReason" />
                <label for="waiver-limitation">Unresolved limitation</label>
                <input id="waiver-limitation" [(ngModel)]="waiverLimitation" />
                <button
                  class="btn-outlined"
                  type="button"
                  [disabled]="!waiverReason.trim() || !waiverLimitation.trim() || state() === 'validating'"
                  (click)="saveWaiver()"
                >
                  Record waiver
                </button>
              </details>
            }
          </section>
        }
      </section>
    </app-gate-shell>
  `,
})
export class EvidenceReview {
  private readonly runs = inject(RunsService);

  readonly runId = input.required<string>();
  readonly back = output<void>();
  readonly resumed = output<void>();

  readonly state = signal<GateState>('loading');
  readonly detail = signal('');
  readonly cells = signal<Cell[]>([]);
  readonly blocking = signal<Array<Record<string, unknown>>>([]);
  readonly reviews = signal<Record<string, Record<string, unknown>>>({});
  readonly waivers = signal<Record<string, unknown[]>>({});

  readonly openCellId = signal<string | null>(null);
  readonly cellLoading = signal(false);
  readonly requirements = signal<RequirementResult[]>([]);
  readonly passages = signal<Passage[]>([]);
  readonly overrides = signal<Record<string, string>>({});
  readonly openCellVersion = signal<number>(1);
  readonly rerunNote = signal('');

  waiverReason = '';
  waiverLimitation = '';
  private actionKey = this.runs.newKey();

  /**
   * `graph/gates.py`'s `GATE_C_RULES`, in plain words, **one-to-one and in the
   * same order**.
   *
   * The server's list is the binding one and is written in the machine
   * vocabulary; this is the same five rules said in the language the rest of the
   * screen uses. They are paired positionally on purpose, so the two can be read
   * side by side, and `tests/test_frontend_review_contract.py` asserts the counts
   * still match -- a rule added on the server with no gloss here would otherwise
   * leave a reviewer held to a condition this screen never states.
   */
  readonly RULES = [
    'every comparison has a result',
    'no requirement is left unclear',
    'no outstanding request for more context',
    'every comparison that could not be validated is re-run or waived, never overridden or converted into a result',
    'every comparison that was not evaluated is acknowledged or waived',
  ];

  /**
   * The three statuses a reviewer may choose, as `{code, label}`.
   *
   * Built from the vocabulary rather than written out in the template, so the
   * wording a reviewer picks from is the same wording the same status is given
   * everywhere else on the screen. The **code** is what the request carries.
   */
  readonly STATUS_CHOICES = (
    ['FOUND', 'NOT_FOUND_IN_SUPPLIED_PASSAGES', 'UNCLEAR'] as const
  ).map((code) => ({ code, label: STATUS_TERMS[code].label }));

  /** Words for a code, keeping the code. See `models/vocabulary.ts`. */
  statusWords(code: unknown): Term {
    return statusTerm(code == null ? '' : String(code));
  }

  outcomeWords(code: unknown): Term {
    return outcomeTerm(code == null ? '' : String(code));
  }

  cellWords(code: unknown): Term {
    return cellStateTerm(code == null ? '' : String(code));
  }

  reviewWords(code: unknown): Term {
    return reviewStatusTerm(code == null ? '' : String(code));
  }

  /**
   * §13.4 check 0: a `VALIDATION_FAILURE` has no valid machine result, so no
   * overlay can attach to it. The only paths are a rerun or a waiver — which is
   * what keeps invalid model output from being laundered into a verdict.
   */
  readonly canReview = computed(() => {
    const cell = this.cells().find((item) => item.cell_id === this.openCellId());
    return cell ? cell.state !== 'VALIDATION_FAILURE' && cell.state !== 'NOT_EVALUATED' : false;
  });

  /**
   * §6.2's derivation, mirrored read-only. Never an input.
   *
   * **The rule ORDER is the rule**, and an earlier version of this mirror got it
   * wrong in the one direction that matters. It asked "any unclear and nothing
   * not-found?" first and fell through to `PARTIALLY_DISCLOSED` otherwise, so a
   * capability with nothing found, something not found and something unclear --
   * `found = 0`, `notFound > 0`, `unclear > 0` -- rendered as *partially
   * disclosed*. `verification/outcome.py` derives `UNCLEAR` for that case, and
   * says why in as many words: the ambiguity might have resolved in favour of
   * disclosure, so it must not be rounded. The screen was showing a reviewer a
   * disclosure claim the application does not make, on the one screen whose job
   * is to let them check exactly that.
   *
   * Written here in `derive_outcome`'s own order so the two can be compared line
   * by line rather than reasoned about:
   *
   * 1. nothing not-found and nothing unclear -> FULLY
   * 2. nothing found and nothing unclear     -> NONE_FOUND
   * 3. nothing found, or nothing not-found   -> UNCLEAR
   * 4. otherwise                             -> PARTIALLY
   *
   * A `computed()` is correct here, unlike `violations()` on the understanding
   * screen: every value it reads (`requirements`, `overrides`) is a signal.
   */
  readonly derivedOutcome = computed(() => {
    const statuses = this.requirements().map(
      (req) => this.overrides()[this.requirementId(req)] ?? req.status,
    );
    if (statuses.length === 0) return '';
    const found = statuses.filter((s) => s === 'FOUND').length;
    const notFound = statuses.filter((s) => s === 'NOT_FOUND_IN_SUPPLIED_PASSAGES').length;
    const unclear = statuses.filter((s) => s === 'UNCLEAR').length;
    if (notFound === 0 && unclear === 0) return 'FULLY_DISCLOSED_IN_SUPPLIED_PASSAGES';
    if (found === 0 && unclear === 0) return 'NO_DISCLOSURE_FOUND_IN_SUPPLIED_PASSAGES';
    if (found === 0 || notFound === 0) return 'UNCLEAR';
    return 'PARTIALLY_DISCLOSED_IN_SUPPLIED_PASSAGES';
  });

  constructor() {
    queueMicrotask(() => this.reload());
  }

  requirementId(req: RequirementResult): string {
    return req.element_id ?? req.relationship_id ?? '';
  }

  reviewFor(cellId: string): Record<string, unknown> | null {
    return this.reviews()[cellId] ?? null;
  }

  waiverFor(cellId: string): boolean {
    return (this.waivers()[cellId] ?? []).length > 0;
  }

  async reload(): Promise<void> {
    this.state.set('loading');
    this.detail.set('');
    this.actionKey = this.runs.newKey();
    try {
      const payload = await this.runs.gateC(this.runId());
      const matrix = (payload['matrix'] ?? []) as Cell[];
      this.cells.set(matrix);
      const blocking = (payload['blocking'] ?? []) as Array<Record<string, unknown>>;
      this.blocking.set(blocking);

      // `blocked` still renders the matrix -- the shell shows the blocking list
      // ABOVE the body rather than instead of it. A reviewer who cannot see the
      // cells cannot clear the conditions that are blocking them.
      if (blocking.length > 0) {
        this.state.set('blocked');
        return;
      }
      this.state.set(matrix.length === 0 ? 'empty' : 'review');
    } catch (error) {
      this.state.set(classify(error, this.detail));
    }
  }

  async open(cell: Cell): Promise<void> {
    this.openCellId.set(cell.cell_id);
    this.openCellVersion.set(cell.result_version);
    this.cellLoading.set(true);
    this.overrides.set({});
    this.rerunNote.set('');
    try {
      const payload = await this.runs.cell(this.runId(), cell.cell_id);
      const machine = (payload['machine_result'] ?? {}) as {
        elements?: RequirementResult[];
        relationships?: RequirementResult[];
      };
      this.requirements.set([...(machine.elements ?? []), ...(machine.relationships ?? [])]);
      this.passages.set((payload['passages'] ?? []) as Passage[]);
      const review = payload['review'] as Record<string, unknown> | null;
      if (review) this.reviews.set({ ...this.reviews(), [cell.cell_id]: review });
    } catch (error) {
      this.state.set(classify(error, this.detail));
    } finally {
      this.cellLoading.set(false);
    }
  }

  setOverride(requirementId: string, status: string): void {
    this.overrides.set({ ...this.overrides(), [requirementId]: status });
  }

  /**
   * The exact quote, highlighted **inside** the passage it came from.
   *
   * Plain substring match, the same test §8 applies before the call — no
   * normalisation, no case folding, no fuzzy matching. A quote that does not
   * appear verbatim is not highlighted, and that absence is itself informative.
   */
  highlighted(passage: Passage): string {
    const quotes = this.requirements()
      .flatMap((req) => req.evidence)
      .filter((item) => item.chunk_id === passage.chunk_id)
      .map((item) => item.exact_quote)
      .filter(Boolean);

    const escape = (value: string) =>
      value.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

    let rendered = escape(passage.text);
    for (const quote of quotes) {
      const safe = escape(quote);
      if (rendered.includes(safe)) {
        rendered = rendered.split(safe).join(`<mark class="quote-hit">${safe}</mark>`);
      }
    }
    return rendered;
  }

  async saveReview(): Promise<void> {
    const cellId = this.openCellId();
    if (!cellId || !this.canReview()) return;
    this.state.set('validating');
    try {
      const elementReviews = this.requirements().map((req) => ({
        requirement_id: this.requirementId(req),
        requirement_type: req.element_id ? 'ELEMENT' : 'RELATIONSHIP',
        machine_status: req.status,
        reviewed_status: this.overrides()[this.requirementId(req)] ?? req.status,
      }));
      await this.runs.reviewCell(
        this.runId(),
        cellId,
        {
          source_cell_id: cellId,
          source_result_version: this.openCellVersion(),
          element_reviews: elementReviews,
        },
        this.actionKey,
      );
      await this.reload();
    } catch (error) {
      this.state.set(classify(error, this.detail));
    }
  }

  async saveWaiver(): Promise<void> {
    const cellId = this.openCellId();
    if (!cellId || !this.waiverReason.trim() || !this.waiverLimitation.trim()) return;
    this.state.set('validating');
    try {
      await this.runs.waiveCell(
        this.runId(),
        cellId,
        {
          source_cell_id: cellId,
          source_result_version: this.openCellVersion(),
          reason: this.waiverReason.trim(),
          unresolved_limitation: this.waiverLimitation.trim(),
        },
        this.runs.newKey(),
      );
      this.waiverReason = '';
      this.waiverLimitation = '';
      await this.reload();
    } catch (error) {
      this.state.set(classify(error, this.detail));
    }
  }

  async requestRerun(): Promise<void> {
    const cellId = this.openCellId();
    if (!cellId) return;
    this.state.set('validating');
    try {
      const response = (await this.runs.rerunCell(
        this.runId(),
        cellId,
        [],
        this.openCellVersion(),
        this.runs.newKey(),
      )) as { result_version?: number };
      this.rerunNote.set(
        `Queued. It will produce version ${response.result_version} — the current version stays retrievable.`,
      );
      this.state.set('review');
    } catch (error) {
      this.state.set(classify(error, this.detail));
    }
  }

  async complete(): Promise<void> {
    this.state.set('validating');
    try {
      await this.runs.completeGateC(this.runId(), this.actionKey);
      this.state.set('resuming');
      this.resumed.emit();
    } catch (error) {
      this.state.set(classify(error, this.detail));
    }
  }
}
