import { Component, inject, input, output, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Icon } from '../shared/icon';
import { classify, GateShell, type GateState } from './gate-shell';
import { RunsService } from '../services/runs';

/** One technical element being edited, inside one capability. */
export interface DraftElement {
  id: string;
  text: string;
  core: boolean;
}

/** One technical capability: a group of elements the inventor reviews together. */
export interface DraftCapability {
  id: string;
  text: string;
  elements: DraftElement[];
  /**
   * The relationships the machine proposed for this capability, carried through
   * untouched and never shown.
   *
   * They are verifier requirements, not something an inventor should have to
   * author — but they are *not* dropped, because dropping an approved
   * relationship silently removes a requirement the verifier would otherwise
   * check. When the elements change they are regenerated server-side rather than
   * patched here; see `llm.regenerate_relationships`.
   */
  relationships: Array<{ relationship_id: string; text: string; order?: number }>;
  /** Set when the elements were edited, so stale relationships are not approved. */
  elementsChanged: boolean;
}

/**
 * **Invention Understanding** — the first stage an inventor sees.
 *
 * The machine proposes an interpretation; a person corrects and confirms it.
 * What is approved here is what every publication is later checked against, and
 * **nothing is searched for until it is approved**: the graph pauses at this
 * point, and asking for a re-analysis routes back to the analysis rather than
 * on to the search.
 *
 * ## Three regions, and the first one is never editable
 *
 * The inventor's own description is shown read-only and is never rewritten —
 * not by a proposal, not by an edit, not by an approval. The approved summary is
 * stored *beside* it. That side-by-side is the point of the screen: the
 * inventor is being asked whether the system understood them, which they cannot
 * answer if the thing they wrote has been replaced by the system's version of it.
 *
 * ## What this screen cannot do
 *
 * It cannot approve anything by itself and it cannot write an `approved_*`
 * field — only `workflow/approvals.py` does that, from an authenticated request.
 * The field it reads is called `candidate_decomposition` precisely so nothing
 * downstream mistakes a proposal for a decision. Regeneration is a `202`
 * request the worker carries out; this screen never calls a model.
 *
 * ## Limits are shown, enforced here, and enforced again on the server
 *
 * The server's check is the binding one (`workflow/review.py`). This one exists
 * so a reviewer meets a limit while editing rather than after submitting, and it
 * states the limit without explaining where the number came from — see
 * `backend/graph/limits.py` on why the derivation stays out of the UI.
 */
@Component({
  selector: 'app-understanding-review',
  standalone: true,
  imports: [FormsModule, Icon, GateShell],
  template: `
    <app-gate-shell
      gate="A"
      title="Review the invention understanding"
      [runId]="runId()"
      [state]="state()"
      [detail]="detail()"
      [blocking]="[]"
      [rules]="RULES"
      [busy]="state() === 'validating'"
      approveLabel="Approve understanding and continue"
      [canApprove]="violations().length === 0"
      (approve)="approve()"
      (back)="back.emit()"
      (reload)="reload()"
    >
      <section class="gate-body understanding-body">
        <!-- The inventor's own words. Read-only, always, everywhere. -->
        <section class="understanding-pane" aria-labelledby="orig-heading">
          <h3 id="orig-heading">Original invention description</h3>
          <p class="pane-note">What you submitted. This is never changed.</p>
          <blockquote class="original-text">{{ originalDescription() }}</blockquote>

          @if (problemSolved()) {
            <h4>Problem solved</h4>
            <blockquote class="original-text">{{ problemSolved() }}</blockquote>
          }
          @if (noveltyStatement()) {
            <h4>What you believe is new</h4>
            <blockquote class="original-text">{{ noveltyStatement() }}</blockquote>
          }
        </section>

        <!-- The machine's interpretation. Editable, and labelled as an
             interpretation rather than as a finding. -->
        <section class="understanding-pane" aria-labelledby="ai-heading">
          <h3 id="ai-heading">How the system understood it</h3>
          <p class="pane-note">
            This is the system's interpretation, not your description. Correct
            anything it got wrong.
          </p>
          <label class="sr-only" for="ai-summary">Technical summary</label>
          <textarea
            id="ai-summary"
            class="summary-input"
            rows="5"
            [ngModel]="summary()"
            (ngModelChange)="summary.set($event)"
            [disabled]="state() === 'validating'"
          ></textarea>
        </section>

        <section aria-labelledby="elements-heading">
          <h3 id="elements-heading">Technical elements</h3>
          <p class="pane-note">
            Grouped by capability. Mark an element <strong>core</strong> when the
            invention is not itself without it — at least one must be core.
          </p>

          @for (capability of capabilities(); track capability.id; let c = $index) {
            <article class="capability-card">
              <header class="capability-head">
                <label class="sr-only" [attr.for]="'cap-text-' + c">Capability</label>
                <input
                  [attr.id]="'cap-text-' + c"
                  class="capability-title"
                  [(ngModel)]="capability.text"
                  [disabled]="state() === 'validating'"
                  placeholder="What this capability does"
                />
                <button
                  class="btn-outlined btn-small"
                  type="button"
                  [attr.aria-label]="'Remove capability ' + (c + 1)"
                  [disabled]="state() === 'validating' || capabilities().length < 2"
                  (click)="removeCapability(c)"
                >
                  <app-icon name="X" /> Remove
                </button>
              </header>

              <ul class="element-list">
                @for (element of capability.elements; track element.id; let e = $index) {
                  <li class="element-row" [class.row-invalid]="!element.text.trim()">
                    <label class="sr-only" [attr.for]="'el-' + c + '-' + e">Element text</label>
                    <input
                      [attr.id]="'el-' + c + '-' + e"
                      class="element-text"
                      [(ngModel)]="element.text"
                      (ngModelChange)="markChanged(capability)"
                      [disabled]="state() === 'validating'"
                    />
                    <label class="element-core">
                      <input
                        type="checkbox"
                        [(ngModel)]="element.core"
                        (ngModelChange)="touch()"
                        [disabled]="state() === 'validating'"
                      />
                      Core
                    </label>
                    <button
                      class="btn-outlined btn-small"
                      type="button"
                      [attr.aria-label]="'Delete element ' + (e + 1)"
                      [disabled]="state() === 'validating'"
                      (click)="removeElement(capability, e)"
                    >
                      <app-icon name="X" />
                    </button>
                  </li>
                }
              </ul>

              <button
                class="btn-outlined btn-small"
                type="button"
                [disabled]="state() === 'validating' || capability.elements.length >= maxPerCapability()"
                (click)="addElement(capability)"
              >
                <app-icon name="Plus" /> Add element
              </button>
            </article>
          }

          <button
            class="btn-outlined"
            type="button"
            [disabled]="state() === 'validating' || capabilities().length >= maxCapabilities()"
            (click)="addCapability()"
          >
            <app-icon name="Plus" /> Add capability
          </button>
          <p class="pane-note">
            {{ totalElements() }} of {{ maxElementsTotal() }} elements,
            {{ capabilities().length }} of {{ maxCapabilities() }} capabilities.
          </p>
        </section>

        <!-- Asking for a re-analysis. A 202 request, never a model call here. -->
        <section class="regenerate-pane" aria-labelledby="regen-heading">
          <h3 id="regen-heading">Not what you meant?</h3>
          <label class="sr-only" for="regen-feedback">What the system misunderstood</label>
          <textarea
            id="regen-feedback"
            class="summary-input"
            rows="2"
            [(ngModel)]="feedback"
            [disabled]="state() === 'validating'"
            placeholder="Optional: what did it get wrong?"
          ></textarea>
          <button
            class="btn-outlined"
            type="button"
            [disabled]="state() === 'validating'"
            (click)="regenerate()"
          >
            <app-icon name="RefreshCw" /> Regenerate analysis
          </button>
          <p class="pane-note">
            This starts a fresh analysis as a new version. Nothing you see now is
            deleted.
          </p>
        </section>

        @if (violations().length > 0) {
          <div class="gate-validation" data-state="validation" role="alert">
            <h3>Not ready to approve</h3>
            <ul>
              @for (problem of violations(); track problem) {
                <li>{{ problem }}</li>
              }
            </ul>
          </div>
        }
      </section>
    </app-gate-shell>
  `,
})
export class UnderstandingReview {
  private readonly runs = inject(RunsService);

  readonly runId = input.required<string>();
  readonly back = output<void>();
  readonly resumed = output<void>();

  readonly state = signal<GateState>('loading');
  readonly detail = signal('');
  readonly capabilities = signal<DraftCapability[]>([]);
  readonly originalDescription = signal('');
  readonly problemSolved = signal('');
  readonly noveltyStatement = signal('');
  readonly approvedVersion = signal<number | null>(null);

  // Limits come from the server rather than being restated here, so the two
  // cannot disagree. The fallbacks are only for a payload that predates them.
  readonly maxCapabilities = signal(5);
  readonly maxPerCapability = signal(8);
  readonly maxElementsTotal = signal(25);

  /**
   * A signal, not a plain field.
   *
   * `violations()` used to be a `computed()` reading `this.summary` as an
   * ordinary property. A computed tracks signals and nothing else, so editing
   * the summary never invalidated it: the approve control stayed enabled after
   * the summary was cleared, and the reviewer would have been refused by the
   * server instead of by the screen. The rule is the same for anything
   * `violations()` reads.
   */
  readonly summary = signal('');
  feedback = '';
  version = 1;

  private actionKey = this.runs.newKey();

  readonly RULES = [
    'a non-empty technical summary',
    'at least one technical capability, and at least one element in it',
    'at least one element marked as core',
    'every element carries non-empty text',
  ];

  totalElements(): number {
    return this.capabilities().reduce(
      (sum, capability) => sum + capability.elements.length,
      0,
    );
  }

  /**
   * The completion rules, evaluated live, so the reviewer sees *which* rule
   * blocks them rather than sending a request to be told. The server enforces
   * the same set and is the binding check.
   */
  violations(): string[] {
    const capabilities = this.capabilities();
    const problems: string[] = [];

    if (!this.summary().trim()) {
      problems.push('The technical summary cannot be empty.');
    }
    if (capabilities.length === 0) {
      problems.push('At least one technical capability is required.');
    }
    if (capabilities.some((capability) => capability.elements.length === 0)) {
      problems.push('Every capability needs at least one technical element.');
    }
    if (
      capabilities.some((capability) =>
        capability.elements.some((element) => !element.text.trim()),
      )
    ) {
      problems.push('Every technical element needs non-empty text.');
    }
    const anyCore = capabilities.some((capability) =>
      capability.elements.some((element) => element.core),
    );
    if (capabilities.length > 0 && !anyCore) {
      problems.push('At least one element must be marked as core.');
    }
    if (capabilities.length > this.maxCapabilities()) {
      problems.push(`At most ${this.maxCapabilities()} capabilities can be approved.`);
    }
    if (capabilities.some((c) => c.elements.length > this.maxPerCapability())) {
      problems.push(`At most ${this.maxPerCapability()} elements are allowed in one capability.`);
    }
    if (this.totalElements() > this.maxElementsTotal()) {
      problems.push(`At most ${this.maxElementsTotal()} elements can be approved in total.`);
    }
    if (!Number.isFinite(this.version) || this.version < 1) {
      problems.push('The approval must name a version.');
    }
    return problems;
  }

  constructor() {
    queueMicrotask(() => this.reload());
  }

  async reload(): Promise<void> {
    this.state.set('loading');
    this.detail.set('');
    this.actionKey = this.runs.newKey();
    try {
      const payload = await this.runs.gateA(this.runId());

      const approved = payload['approved_version'];
      this.approvedVersion.set(typeof approved === 'number' ? approved : null);
      this.originalDescription.set(String(payload['original_description'] ?? ''));
      this.problemSolved.set(String(payload['problem_solved'] ?? ''));
      this.noveltyStatement.set(String(payload['inventor_novelty_statement'] ?? ''));

      const limits = (payload['limits'] ?? {}) as Record<string, number>;
      if (typeof limits['max_capabilities'] === 'number') {
        this.maxCapabilities.set(limits['max_capabilities']);
      }
      if (typeof limits['max_elements_per_capability'] === 'number') {
        this.maxPerCapability.set(limits['max_elements_per_capability']);
      }
      if (typeof limits['max_elements_total'] === 'number') {
        this.maxElementsTotal.set(limits['max_elements_total']);
      }

      const proposal = (payload['candidate_decomposition'] ?? null) as {
        version?: number;
        summary?: string;
        features?: Array<{
          feature_id?: string;
          feature_text?: string;
          elements?: Array<{ element_id: string; text: string; core?: boolean }>;
          relationships?: Array<{ relationship_id: string; text: string; order?: number }>;
        }>;
      } | null;

      this.summary.set(proposal?.summary ?? '');
      this.version = proposal?.version ?? (this.approvedVersion() ?? 0) + 1;
      this.feedback = '';

      const drafted: DraftCapability[] = (proposal?.features ?? []).map((feature, index) => ({
        id: feature.feature_id ?? `F${index + 1}`,
        text: feature.feature_text ?? '',
        elements: (feature.elements ?? []).map((element) => ({
          id: element.element_id,
          text: element.text,
          core: element.core !== false,
        })),
        relationships: feature.relationships ?? [],
        elementsChanged: false,
      }));
      this.capabilities.set(drafted);

      // An empty proposal is a real state, not a failure: the machine may have
      // produced nothing, and the reviewer writes the understanding themselves.
      this.state.set(drafted.length === 0 && !this.summary() ? 'empty' : 'review');
    } catch (error) {
      this.state.set(classify(error, this.detail));
    }
  }

  /**
   * Re-publish the capability list so the screen re-renders after a nested edit.
   *
   * The elements are plain objects inside a signal's array, so mutating one does
   * not notify anything. Every edit path goes through here.
   */
  touch(): void {
    this.capabilities.set([...this.capabilities()]);
  }

  markChanged(capability: DraftCapability): void {
    capability.elementsChanged = true;
    this.touch();
  }

  addCapability(): void {
    if (this.capabilities().length >= this.maxCapabilities()) return;
    const next = this.capabilities().length + 1;
    this.capabilities.set([
      ...this.capabilities(),
      {
        id: `F${next}`,
        text: '',
        elements: [{ id: 'E1', text: '', core: true }],
        relationships: [],
        elementsChanged: true,
      },
    ]);
  }

  removeCapability(index: number): void {
    this.capabilities.set(this.capabilities().filter((_, i) => i !== index));
  }

  addElement(capability: DraftCapability): void {
    if (capability.elements.length >= this.maxPerCapability()) return;
    // Ids are minted against the highest already present rather than the count,
    // so deleting E2 and adding one does not create a second E3.
    const used = capability.elements.map((element) => Number(element.id.replace(/^E/, '')) || 0);
    const next = Math.max(0, ...used) + 1;
    capability.elements = [
      ...capability.elements,
      { id: `E${next}`, text: '', core: false },
    ];
    this.markChanged(capability);
    this.capabilities.set([...this.capabilities()]);
  }

  removeElement(capability: DraftCapability, index: number): void {
    capability.elements = capability.elements.filter((_, i) => i !== index);
    this.markChanged(capability);
    this.capabilities.set([...this.capabilities()]);
  }

  /**
   * Ask for a fresh analysis. **A request, not a re-analysis.**
   *
   * The worker carries it out under the run's lease; this returns `202` and the
   * run resumes. Nothing here is an approval, and the rejected version is kept.
   */
  async regenerate(): Promise<void> {
    this.state.set('validating');
    try {
      await this.runs.regenerateUnderstanding(
        this.runId(),
        this.version,
        this.feedback.trim(),
        this.actionKey,
      );
      this.state.set('resuming');
      this.resumed.emit();
    } catch (error) {
      this.state.set(classify(error, this.detail));
    }
  }

  async approve(): Promise<void> {
    if (this.violations().length > 0) return;
    this.state.set('validating');
    try {
      await this.runs.approveGateA(
        this.runId(),
        {
          version: this.version,
          summary: this.summary().trim(),
          features: this.capabilities().map((capability) => ({
            feature_id: capability.id,
            feature_version: String(this.version),
            feature_text: capability.text.trim() || capability.id,
            elements: capability.elements.map((element, index) => ({
              element_id: element.id.trim(),
              text: element.text.trim(),
              core: element.core,
              order: index + 1,
            })),
            // **Dropped when the elements changed, kept otherwise.** A
            // relationship describes how specific elements interact, so one that
            // survives an element edit can name an element that is no longer
            // there — a requirement the verifier would be asked to find among
            // elements that do not exist. The server refuses that
            // (`DANGLING_RELATIONSHIP_REFERENCE`) and regenerates relationships
            // against the new element set; sending the stale ones would only
            // produce a rejection the inventor cannot act on.
            relationships: capability.elementsChanged
              ? []
              : capability.relationships.map((relationship, index) => ({
                  relationship_id: relationship.relationship_id,
                  text: relationship.text,
                  order: relationship.order ?? index + 1,
                })),
          })),
        },
        this.actionKey,
      );
      this.state.set('resuming');
      this.resumed.emit();
    } catch (error) {
      this.state.set(classify(error, this.detail));
    }
  }
}
