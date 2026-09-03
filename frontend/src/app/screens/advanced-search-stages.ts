import {
  ChangeDetectionStrategy,
  Component,
  effect,
  inject,
  input,
  output,
  signal,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Icon } from '../shared/icon';
import { GateShell, GateState, classify } from './gate-shell';
import { RunApiError, RunsService } from '../services/runs';

/** One editable row. `key` is for `@for` tracking only and is never sent. */
export interface DraftRow {
  key: string;
  text: string;
}

/** Which of the three stages is on screen. */
export type StageNumber = 1 | 2 | 3;

export const TOTAL_STAGES = 3;

/**
 * What each stage requires, in the reviewer's words.
 *
 * **Pairs one-to-one and in the same order with `graph/gates.py`'s
 * `GATE_A_RULES` and `GATE_STRATEGY_RULES`**, which are the binding lists. A
 * reviewer held to a condition has to have been told about it, so the two sides
 * shrink together; `tests/test_frontend_review_contract.py` compares the counts,
 * which is deliberately weak and the strongest check available when the two
 * lists say the same thing in different words.
 */
export const STAGE_RULES: Record<string, string[]> = {
  A: [
    'a non-empty technical summary',
    'at least one technical element with text in it',
  ],
  STRATEGY: [
    'at least one search with terms in it',
  ],
};

/**
 * **Advanced Search: three stages, two approvals, one screen.**
 *
 * A standard prior-art search preceded by guided editing. The system proposes a
 * technical summary and a flat list of technical elements; the user edits and
 * approves them; the system proposes search queries; the user edits and approves
 * those; the approved queries are then executed by the ordinary search pipeline
 * and the ordinary report is displayed.
 *
 * It performs **no** evidence verification, makes **no** legal conclusion, and
 * presents **no** numeric score.
 *
 * ## Why one component for three stages
 *
 * Stages 1 and 2 are a single backend approval (the understanding); stage 3 is a
 * second one (the strategy). Two approvals, three screens — and the three
 * screens are one flow a person walks forward and back through, so the drafts
 * live in one place. Splitting them across components would put the drafts
 * behind a router or a shared store purely so that pressing Back could find them
 * again.
 *
 * ## What navigation does and does not do
 *
 * Moving between stages **preserves every edit and never touches the server**.
 * Only two transitions mutate anything: leaving stage 2 the first time (the
 * understanding approval) and leaving stage 3 (the strategy approval). In
 * particular 1 → 2 calls no approval endpoint, which is asserted rather than
 * assumed — a stage that quietly approved on its way past would make the
 * "Next" button an approval the reviewer never recognised as one.
 *
 * Once the understanding is approved the run moves to the strategy gate, so
 * going back to stage 2 and forward again must **not** try to approve it a
 * second time: that would be a `WRONG_GATE` conflict for a reviewer who did
 * nothing wrong. `understandingApproved` records that it is done; after it is
 * set, 2 → 3 is plain navigation like 1 → 2.
 *
 * ## The original description is never rewritten
 *
 * Not by a proposal, not by an edit, not by an approval. It is shown read-only
 * beside the summary, and that side-by-side is the point of stage 1: the
 * inventor is being asked whether the system understood them, which they cannot
 * answer if what they wrote has been replaced by the system's version of it.
 */
@Component({
  selector: 'app-advanced-search-stages',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FormsModule, Icon, GateShell],
  template: `
    <app-gate-shell
      [runId]="runId()"
      [gate]="stageGate()"
      [state]="state()"
      [detail]="detail()"
      [title]="stageTitle()"
      [blocking]="blocking()"
      [rules]="rules()"
      [busy]="state() === 'validating'"
      [canApprove]="violations().length === 0"
      [approveLabel]="stage() === TOTAL_STAGES ? 'Approve and search' : 'Next'"
      (approve)="next()"
      (back)="previous()"
      (reload)="load(runId(), gate())"
    >
      <!-- Rendered text, not an aria-label: a reviewer three screens into a
           flow needs to see how far along they are. -->
      <p class="stage-counter">{{ stage() }} / {{ TOTAL_STAGES }}</p>

      @switch (stage()) {
        @case (1) {
          <section class="stage-pane" aria-labelledby="stage1-heading">
            <h2 id="stage1-heading">Does this describe your invention?</h2>

            <div class="stage-side-by-side">
              <div>
                <h3>What you wrote</h3>
                <!-- Read-only, always. Never bound to an input. -->
                <p class="original-description">{{ originalDescription() }}</p>
              </div>
              <div>
                <h3>How the system understood it</h3>
                <label class="sr-only" for="summary-input">Technical summary</label>
                <textarea
                  id="summary-input"
                  class="summary-input"
                  rows="8"
                  [ngModel]="summary()"
                  (ngModelChange)="summary.set($event)"
                  placeholder="A short technical restatement of the invention."
                ></textarea>
              </div>
            </div>
          </section>
        }

        @case (2) {
          <section class="stage-pane" aria-labelledby="stage2-heading">
            <h2 id="stage2-heading">The technical elements to search for</h2>
            <p class="pane-note">
              Each line is one technical element of the invention. Edit them, add
              what is missing, and remove anything that is not part of what you
              are claiming.
            </p>

            <ul class="draft-list">
              @for (row of elements(); track row.key; let i = $index) {
                <li class="draft-row">
                  <label class="sr-only" [attr.for]="'element-' + row.key">
                    Technical element {{ i + 1 }}
                  </label>
                  <input
                    class="draft-input"
                    [id]="'element-' + row.key"
                    [ngModel]="row.text"
                    (ngModelChange)="editRow(elements, row.key, $event)"
                  />
                  <button
                    class="draft-remove"
                    type="button"
                    (click)="removeRow(elements, row.key)"
                    [attr.aria-label]="'Remove technical element ' + (i + 1)"
                  >
                    <app-icon name="Trash2" [size]="16" />
                  </button>
                </li>
              } @empty {
                <li class="empty-state">No technical elements yet. Add the first one.</li>
              }
            </ul>

            <button class="btn-outlined" type="button" (click)="addRow(elements, 'E')">
              <app-icon name="Plus" [size]="16" /> Add element
            </button>
          </section>
        }

        @case (3) {
          <section class="stage-pane" aria-labelledby="stage3-heading">
            <h2 id="stage3-heading">The searches that will run</h2>
            <p class="pane-note">
              These are the searches this run will perform. Edit them, add your
              own, and remove any that are not worth running. Approving this step
              starts the search.
            </p>

            <ul class="draft-list">
              @for (row of queries(); track row.key; let i = $index) {
                <li class="draft-row">
                  <label class="sr-only" [attr.for]="'query-' + row.key">
                    Search {{ i + 1 }}
                  </label>
                  <input
                    class="draft-input"
                    [id]="'query-' + row.key"
                    [ngModel]="row.text"
                    (ngModelChange)="editRow(queries, row.key, $event)"
                  />
                  <button
                    class="draft-remove"
                    type="button"
                    (click)="removeRow(queries, row.key)"
                    [attr.aria-label]="'Remove search ' + (i + 1)"
                  >
                    <app-icon name="Trash2" [size]="16" />
                  </button>
                </li>
              } @empty {
                <li class="empty-state">No searches yet. Add the first one.</li>
              }
            </ul>

            <button class="btn-outlined" type="button" (click)="addRow(queries, 'Q')">
              <app-icon name="Plus" [size]="16" /> Add search
            </button>
          </section>
        }
      }

      @if (violations().length > 0) {
        <ul class="stage-violations" role="alert">
          @for (violation of violations(); track violation) {
            <li>{{ violation }}</li>
          }
        </ul>
      }
    </app-gate-shell>
  `,
})
export class AdvancedSearchStages {
  private readonly runs = inject(RunsService);

  readonly runId = input.required<string>();
  /** Which gate the run is actually parked at, which decides where we land. */
  readonly gate = input.required<string>();
  readonly back = output<void>();
  readonly resumed = output<void>();

  /**
   * How long to wait between polls for the proposed queries.
   *
   * An input purely so a test can set it to zero. The backend's retry policies
   * take an injectable sleeper for the same reason: a suite that genuinely
   * sleeps is a suite people stop running.
   */
  readonly pollIntervalMs = input<number>(1000);

  readonly TOTAL_STAGES = TOTAL_STAGES;

  readonly state = signal<GateState>('loading');
  readonly detail = signal('');
  readonly blocking = signal<Array<Record<string, unknown>>>([]);

  readonly stage = signal<StageNumber>(1);
  readonly originalDescription = signal('');
  readonly summary = signal('');
  readonly elements = signal<DraftRow[]>([]);
  readonly queries = signal<DraftRow[]>([]);

  /**
   * Whether the understanding approval has already been committed this session.
   *
   * Set when leaving stage 2 succeeds, and when the component mounts on a run
   * that is already at the strategy gate. It is what stops a reviewer who walks
   * back to stage 2 and forward again from re-approving an artifact the run has
   * already moved past.
   */
  readonly understandingApproved = signal(false);

  private key = 0;
  private readonly actionKeys = new Map<string, string>();

  constructor() {
    effect(() => {
      const runId = this.runId();
      const gate = this.gate();
      if (runId) void this.load(runId, gate);
    });
  }

  private row(text: string): DraftRow {
    this.key += 1;
    return { key: `r${this.key}`, text };
  }

  async load(runId: string, gate: string): Promise<void> {
    this.state.set('loading');
    try {
      if (gate === 'STRATEGY') {
        const payload = await this.runs.gateStrategy(runId);
        this.queries.set(((payload['queries'] as string[]) ?? []).map((q) => this.row(q)));
        // The understanding is already approved; stages 1 and 2 are still
        // walkable so a reviewer can re-read what they approved, and going
        // forward from them will not try to approve it again.
        this.understandingApproved.set(true);
        this.stage.set(3);
      } else {
        const payload = await this.runs.gateA(runId);
        this.originalDescription.set(String(payload['original_description'] ?? ''));
        this.summary.set(String(payload['technical_summary'] ?? ''));
        this.elements.set(
          ((payload['technical_elements'] as string[]) ?? []).map((text) => this.row(text)),
        );
        this.stage.set(1);
      }
      this.state.set('review');
    } catch (error) {
      this.state.set(classify(error, this.detail));
    }
  }

  /**
   * Which approval the CURRENT stage belongs to, for the shell's stage label.
   *
   * Not `gate()`: that is the gate the run is parked at when this component
   * mounts, and it does not move as the reviewer walks the stages. Reading it
   * directly left stage 3 sub-titled "Invention understanding" -- naming the
   * wrong stage on the one screen whose job is to say where you are.
   */
  stageGate(): string {
    return this.stage() === TOTAL_STAGES ? 'STRATEGY' : 'A';
  }

  /** The rules for the approval this stage belongs to. */
  rules(): string[] {
    return this.stage() === TOTAL_STAGES ? STAGE_RULES['STRATEGY'] : STAGE_RULES['A'];
  }

  stageTitle(): string {
    switch (this.stage()) {
      case 1:
        return 'Review the understanding';
      case 2:
        return 'Edit the technical elements';
      default:
        return 'Edit and approve the searches';
    }
  }

  // --- editing -------------------------------------------------------------

  /**
   * A new row, keyed above every key already present.
   *
   * Deleting the first row and adding one produces a fresh key rather than
   * reusing the deleted one, so `@for` cannot match a new row to the DOM node of
   * a removed one and carry its input state across.
   */
  addRow(list: { update(fn: (rows: DraftRow[]) => DraftRow[]): void }, _prefix: string): void {
    list.update((rows) => [...rows, this.row('')]);
  }

  editRow(
    list: { update(fn: (rows: DraftRow[]) => DraftRow[]): void },
    key: string,
    text: string,
  ): void {
    list.update((rows) => rows.map((row) => (row.key === key ? { ...row, text } : row)));
  }

  removeRow(
    list: { update(fn: (rows: DraftRow[]) => DraftRow[]): void },
    key: string,
  ): void {
    list.update((rows) => rows.filter((row) => row.key !== key));
  }

  // --- the three validation rules -----------------------------------------

  /**
   * **Exactly three rules, and nothing else blocks** (spec section 3.5).
   *
   * A method rather than a `computed()`: it reads several signals and the rows
   * inside them, and every edit republishes the list, so a computed would be
   * correct here but the method form matches the rest of the workflow screens
   * and cannot go stale if a row ever becomes a plain nested object again.
   */
  violations(): string[] {
    const problems: string[] = [];
    if (this.stage() === 1 && !this.summary().trim()) {
      problems.push('The technical summary cannot be empty.');
    }
    if (this.stage() === 2 && !this.nonEmpty(this.elements()).length) {
      problems.push('At least one technical element is needed.');
    }
    if (this.stage() === 3 && !this.nonEmpty(this.queries()).length) {
      problems.push('At least one search is needed.');
    }
    return problems;
  }

  private nonEmpty(rows: DraftRow[]): string[] {
    return rows.map((row) => row.text.trim()).filter((text) => text.length > 0);
  }

  // --- navigation ----------------------------------------------------------

  previous(): void {
    const stage = this.stage();
    if (stage === 1) {
      this.back.emit();
      return;
    }
    // No server call, and no re-proposal: the drafts are already here.
    this.stage.set((stage - 1) as StageNumber);
  }

  async next(): Promise<void> {
    if (this.violations().length > 0) return;
    const stage = this.stage();

    // 1 -> 2 never mutates anything.
    if (stage === 1) {
      this.stage.set(2);
      return;
    }

    if (stage === 2) {
      // Already approved earlier in this session: walking back and forward is
      // navigation, not a second approval.
      if (this.understandingApproved()) {
        this.stage.set(3);
        return;
      }
      await this.commit('understanding', () =>
        this.runs.approveGateA(
          this.runId(),
          this.summary().trim(),
          this.nonEmpty(this.elements()),
          this.actionKey('understanding'),
        ),
      );
      // The approval committed; the worker now proposes the queries. This
      // component waits for them and walks the reviewer on to stage 3 itself,
      // rather than handing back to the shell and being remounted -- a remount
      // would drop the stage 1 and 2 drafts, and walking back to re-read what
      // you just approved is the ordinary thing to want to do.
      if (this.state() === 'resuming') await this.awaitStrategy();
      return;
    }

    await this.commit('strategy', () =>
      this.runs.approveGateStrategy(
        this.runId(),
        this.nonEmpty(this.queries()),
        this.actionKey('strategy'),
      ),
    );
  }

  /**
   * Wait for the worker to propose the search queries, then show stage 3.
   *
   * Polling, because the strategy is proposed by a worker in another process:
   * there is nothing to await locally. Bounded rather than indefinite, and a
   * timeout leaves the run alone -- it is executing perfectly well, and the
   * reviewer can reopen it. `resuming` stays on screen throughout, which is
   * what it means: the decision is committed and the run is continuing.
   */
  private async awaitStrategy(): Promise<void> {
    for (let attempt = 0; attempt < 300; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, this.pollIntervalMs()));
      try {
        const run = await this.runs.getRun(this.runId());
        if (run.current_gate === 'STRATEGY') {
          const payload = await this.runs.gateStrategy(this.runId());
          this.queries.set(
            ((payload['queries'] as string[]) ?? []).map((q) => this.row(q)),
          );
          this.stage.set(3);
          this.state.set('review');
          return;
        }
        if (run.status === 'COMPLETED' || run.status === 'FAILED') {
          // The run finished without pausing again. Hand back to the shell,
          // which renders the report or the failure.
          this.resumed.emit();
          return;
        }
      } catch {
        // A dropped request is not a dropped run. Keep waiting.
      }
    }
  }

  /**
   * One idempotency key per user action, not per request.
   *
   * A retry of the same approval reuses its key so the backend replays the
   * stored response; a key minted per request would make every retry a new
   * mutation.
   */
  private actionKey(action: string): string {
    const existing = this.actionKeys.get(action);
    if (existing) return existing;
    const minted = this.runs.newKey();
    this.actionKeys.set(action, minted);
    return minted;
  }

  private async commit(action: string, send: () => Promise<unknown>): Promise<void> {
    this.state.set('validating');
    this.blocking.set([]);
    try {
      await send();
      this.state.set('resuming');
      if (action === 'understanding') {
        // **Deliberately silent.** Emitting `resumed` here would hand back to
        // the shell, which would remount this component at the strategy gate and
        // drop the stage 1 and 2 drafts with it. The component carries the
        // reviewer through to stage 3 itself and hands back only at the end.
        this.understandingApproved.set(true);
        return;
      }
      this.resumed.emit();
    } catch (error) {
      const next = classify(error, this.detail);
      if (next === 'blocked' && error instanceof RunApiError) {
        // Every outstanding condition, not just the first: the shell lists them
        // all, and a reviewer told about one at a time cannot fix the set.
        this.blocking.set(
          (error.failure['blocking'] as Array<Record<string, unknown>>) ?? [],
        );
      }
      this.state.set(next);
    }
  }
}
