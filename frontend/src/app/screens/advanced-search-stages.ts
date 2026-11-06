import {
  ChangeDetectionStrategy,
  Component,
  computed,
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
import type { WorkflowGate } from '../models/report';

/** One editable row. `key` is for `@for` tracking only and is never sent. */
export interface DraftRow {
  key: string;
  text: string;
}

/** Stage 3 exists only for a legacy run already committed to query review. */
export type StageNumber = 1 | 2 | 3;

/** The two current stages, plus the legacy query-review label. */
export const STAGE_NAMES = ['Summary', 'Invention Requirements (Claims)'];
export const LEGACY_STAGE_NAMES = [...STAGE_NAMES, 'Queries'];

export const TOTAL_STAGES = STAGE_NAMES.length;

/*
 * The upfront "What this step requires" list was REMOVED, along with the
 * `STAGE_RULES` table that fed it.
 *
 * The rules are still enforced -- `workflow/review.py` is and always was the
 * binding check -- and a reviewer who breaks one is still told which one, by
 * name, at the moment they break it: `violations()` renders a sentence per
 * broken rule and the approve control stays disabled until it is empty.
 *
 * What changed is only that the list is no longer recited before it is needed.
 * The guarantee that replaced it is narrower and worth stating: a reviewer is
 * told the rule they tripped, when they trip it, rather than being shown all of
 * them in advance.
 */
/**
 * **Advanced Search: two current stages, with legacy query-review support.**
 *
 * A standard prior-art search preceded by guided editing. The system first
 * proposes only a technical summary. After the user edits and approves that
 * summary, it generates searchable invention requirements from the approved
 * text. The user approves those independently, then the system generates and
 * validates search queries before the ordinary search pipeline executes.
 *
 * It performs **no** evidence verification, makes **no** legal conclusion, and
 * presents **no** numeric score.
 *
 * ## Why legacy stage 3 remains in this component
 *
 * Runs admitted before flow version 2 can already be parked at STRATEGY. Their
 * query editor stays here so those durable checkpoints remain resumable, while
 * new runs expose only Summary and Invention Requirements (Claims).
 *
 * ## What navigation does and does not do
 *
 * Moving backward preserves every edit and never touches the server. Each
 * forward transition approves the artifact visible on that stage. For current
 * runs, leaving stage 2 starts automatic query generation and search. Legacy
 * runs still move to stage 3 and approve their queries.
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
      [gate]="stageGate()"
      [state]="state()"
      [detail]="detail()"
      [title]="stageTitle()"
      [subtitle]="stageLede()"
      [steps]="stageNames()"
      [current]="stage()"
      [blocking]="blocking()"
      [busy]="state() === 'validating'"
      [canApprove]="violations().length === 0"
      [approveLabel]="approveLabel()"
      (approve)="next()"
      (back)="previous()"
      (reload)="load(runId(), gate())"
    >
      <!-- The stage counter moved into the toolbar's meta line, beside the
           title, where the report screen puts its own run facts. Still rendered
           text, not an aria-label: a reviewer three screens into a flow needs to
           see how far along they are. -->

      @switch (stage()) {
        @case (1) {
          <!-- The heading and the explanation live in the header now, so the
               pane goes straight to the thing being reviewed rather than saying
               the same sentence twice. -->
          <section class="stage-pane" [attr.aria-label]="stageTitle()">
            <div class="stage-side-by-side">
              <div class="comparison-panel submitted-description-panel">
                <div class="stage-field-heading">
                  <h3>Submitted description</h3>
                  <span class="read-only-label">Original</span>
                </div>
                <!-- Read-only, always. Never bound to an input. -->
                <p class="original-description">{{ originalDescription() }}</p>
              </div>
              <div class="comparison-panel editable-summary">
                <div class="stage-field-heading">
                  <h3>Technical summary</h3>
                  @if (summaryApproved()) {
                    <span class="read-only-label">Approved</span>
                  } @else {
                    <span class="editable-label">
                      <app-icon name="PenLine" [size]="14" /> Editable
                    </span>
                  }
                </div>
                <label class="sr-only" for="summary-input">Technical summary</label>
                <!-- Wrapped in .textarea-wrap so this box IS the search page's
                     textarea rather than a copy of its values: same border,
                     radius, hover fill, focus ring and dark counterparts, from
                     one rule. Copying the numbers instead would let the two
                     drift the first time either is touched. -->
                <div class="textarea-wrap">
                  <textarea
                    id="summary-input"
                    rows="8"
                    [ngModel]="summary()"
                    (ngModelChange)="summary.set($event)"
                    [readOnly]="summaryApproved()"
                    placeholder="A short technical restatement of the invention."
                  ></textarea>
                </div>
              </div>
            </div>
          </section>
        }

        @case (2) {
          <section class="stage-pane" [attr.aria-label]="stageTitle()">
            <ul class="draft-list">
              @for (row of elements(); track row.key; let i = $index) {
                <li class="draft-row">
                  <label class="sr-only" [attr.for]="'element-' + row.key">
                    Invention requirement (claim) {{ i + 1 }}
                  </label>
                  <textarea
                    class="draft-input requirement-input"
                    rows="2"
                    wrap="soft"
                    [id]="'element-' + row.key"
                    [ngModel]="row.text"
                    (ngModelChange)="editRow(elements, row.key, $event)"
                  ></textarea>
                  <button
                    class="draft-remove"
                    type="button"
                    (click)="removeRow(elements, row.key)"
                    [attr.aria-label]="'Remove invention requirement (claim) ' + (i + 1)"
                  >
                    <app-icon name="X" [size]="16" />
                  </button>
                </li>
              } @empty {
                <li class="empty-state">No invention requirements yet. Add the first one.</li>
              }
            </ul>

            <button class="secondary-button" type="button" (click)="addRow(elements, 'E')">
              <app-icon name="Plus" [size]="16" /> Add requirement
            </button>
          </section>
        }

        @case (3) {
          <section class="stage-pane" [attr.aria-label]="stageTitle()">
            <ul class="draft-list">
              @for (row of queries(); track row.key; let i = $index) {
                <li class="draft-row">
                  <label class="sr-only" [attr.for]="'query-' + row.key">
                    Search query {{ i + 1 }}
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
                    [attr.aria-label]="'Remove search query ' + (i + 1)"
                  >
                    <app-icon name="X" [size]="16" />
                  </button>
                </li>
              } @empty {
                <li class="empty-state">No search queries yet. Add the first one.</li>
              }
            </ul>

            <button class="secondary-button" type="button" (click)="addRow(queries, 'Q')">
              <app-icon name="Plus" [size]="16" /> Add query
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
  readonly resumed = output<WorkflowGate>();
  readonly searchReleased = output<void>();

  readonly TOTAL_STAGES = TOTAL_STAGES;
  readonly strategyReviewRequired = signal(false);
  readonly stageNames = computed(() =>
    this.strategyReviewRequired() ? LEGACY_STAGE_NAMES : STAGE_NAMES,
  );

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
   * already moved past. Summary approval is tracked independently because it
   * happens one durable pause earlier.
   */
  readonly understandingApproved = signal(false);
  readonly summaryApproved = signal(false);

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
        this.strategyReviewRequired.set(true);
        const payload = await this.runs.gateStrategy(runId);
        this.queries.set(((payload['queries'] as string[]) ?? []).map((q) => this.row(q)));
        // The understanding is already approved; stages 1 and 2 are still
        // walkable so a reviewer can re-read what they approved, and going
        // forward from them will not try to approve it again.
        this.understandingApproved.set(true);
        this.summaryApproved.set(true);
        this.stage.set(3);
      } else {
        const payload = await this.runs.gateA(runId);
        this.strategyReviewRequired.set(Boolean(payload['strategy_review_required']));
        this.originalDescription.set(String(payload['original_description'] ?? ''));
        this.summary.set(String(payload['technical_summary'] ?? ''));
        const technicalElements = this.technicalElements(payload);
        this.elements.set(technicalElements.map((text) => this.row(text)));
        const reviewStage = String(payload['review_stage'] ?? 'summary');
        this.summaryApproved.set(reviewStage === 'limitations');
        if (reviewStage === 'limitations' && technicalElements.length === 0) {
          // There is a durable checkpoint between summary approval and the
          // limitation-generating node. It can already carry the next stage
          // name while the generated artifact is still empty. Keep waiting;
          // presenting that checkpoint as an editable empty proposal invents
          // a result the worker has not produced yet.
          this.state.set('resuming');
          await this.waitForLimitations();
          return;
        }
        this.stage.set(reviewStage === 'limitations' ? 2 : 1);
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
    return this.stage() === 3 ? 'STRATEGY' : 'A';
  }

  approveLabel(): string {
    if (this.stage() === 2 && !this.strategyReviewRequired()) return 'Approve and search';
    if (this.stage() === 3) return 'Approve and search';
    return 'Next';
  }

  stageTitle(): string {
    switch (this.stage()) {
      case 1:
        return 'Review and edit invention summary';
      case 2:
        return 'Edit the Invention Requirements (Claims)';
      default:
        return 'Review search queries';
    }
  }

  /** The sentence under the title: what this stage is asking the reviewer for. */
  stageLede(): string {
    switch (this.stage()) {
      case 1:
        return 'This is how Novelty Lens understood your invention. Edit it if needed.';
      case 2:
        return 'Edit the invention requirements to be searched. Add anything missing and remove anything not being claimed.';
      default:
        return 'These are the search queries that will run. Approving them starts the search.';
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
      problems.push('At least one invention requirement (claim) is needed.');
    }
    if (this.stage() === 3 && !this.nonEmpty(this.queries()).length) {
      problems.push('At least one search query is needed.');
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

    if (stage === 1) {
      await this.commit('summary', () =>
        this.runs.approveGateSummary(
          this.runId(),
          this.summary().trim(),
          this.actionKey('summary'),
        ),
      );
      return;
    }

    if (stage === 2) {
      // Already approved earlier in this session: walking back and forward is
      // navigation, not a second approval.
      if (this.understandingApproved()) {
        if (this.strategyReviewRequired()) this.stage.set(3);
        return;
      }
      await this.commit('limitations', () =>
        this.runs.approveGateLimitations(
          this.runId(),
          this.nonEmpty(this.elements()),
          this.actionKey('limitations'),
        ),
      );
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
      if (action === 'summary') {
        this.summaryApproved.set(true);
        this.resumed.emit('A');
        await this.waitForLimitations();
        return;
      }
      if (action === 'limitations') {
        this.understandingApproved.set(true);
        if (this.strategyReviewRequired()) {
          this.resumed.emit('A');
        } else {
          this.searchReleased.emit();
        }
        return;
      }
      this.resumed.emit('STRATEGY');
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

  private async waitForLimitations(): Promise<void> {
    for (let attempt = 0; attempt < 90; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 500));
      try {
        const payload = await this.runs.gateA(this.runId());
        if (String(payload['review_stage'] ?? '') !== 'limitations') continue;
        const technicalElements = this.technicalElements(payload);
        if (technicalElements.length === 0) continue;
        this.originalDescription.set(String(payload['original_description'] ?? ''));
        this.summary.set(String(payload['technical_summary'] ?? ''));
        this.elements.set(technicalElements.map((text) => this.row(text)));
        this.stage.set(2);
        this.state.set('review');
        return;
      } catch {
        // The worker is between checkpoints. The durable stream remains active.
      }
    }
    this.detail.set('The approved summary was saved, but the invention requirements are not ready yet.');
    this.state.set('reconnecting');
  }

  private technicalElements(payload: Record<string, unknown>): string[] {
    const values = payload['technical_elements'];
    if (!Array.isArray(values)) return [];
    return values.filter(
      (value): value is string => typeof value === 'string' && value.trim().length > 0,
    );
  }
}
