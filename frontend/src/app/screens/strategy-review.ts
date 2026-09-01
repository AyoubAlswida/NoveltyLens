import { Component, inject, input, output, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Icon } from '../shared/icon';
import { classify, GateShell, type GateState } from './gate-shell';
import { RunsService } from '../services/runs';

/** One approved technical element the search has to cover, feature-qualified. */
export interface CoverageElement {
  id: string;
  feature_id: string;
  feature_text: string;
  text: string;
  core: boolean;
}

/** One query being reviewed. */
export interface DraftQuery {
  id: string;
  text: string;
  rationale: string;
  enabled: boolean;
  /** The element ids this query is aimed at. Feature-qualified (`F1.E2`). */
  covers: string[];
}

/**
 * **Search strategy** — the second stage, and the last one before any provider
 * is contacted.
 *
 * The machine proposes queries for the understanding the inventor approved; a
 * person edits them, switches off the ones they do not want run, and approves.
 * **Only the queries approved here and left on are dispatched**, and nothing is
 * dispatched until they are: the graph pauses at this point, and asking for
 * fresh queries routes back to the planning rather than on to the search.
 *
 * ## Coverage is the question this screen asks
 *
 * Every query says which approved elements it is aimed at, and the panel shows
 * which elements are left uncovered. A **core** element with no enabled query
 * covering it blocks approval, because "no prior art found" for something nobody
 * searched for is not a finding. The claim is checked for being *real* — the ids
 * must exist in the approved analysis — and never for being *true*: whether a
 * query would actually retrieve prior art for an element is the judgement the
 * person makes.
 *
 * ## What this screen cannot do
 *
 * It cannot approve anything by itself and it cannot write an `approved_*`
 * field — only `workflow/approvals.py` does that, from an authenticated request.
 * The field it reads is called `candidate_search_strategy` precisely so nothing
 * downstream mistakes a proposal for a dispatched query. Regeneration is a `202`
 * request the worker carries out; this screen never calls a model and never
 * contacts a provider.
 */
@Component({
  selector: 'app-strategy-review',
  standalone: true,
  imports: [FormsModule, Icon, GateShell],
  template: `
    <app-gate-shell
      gate="STRATEGY"
      title="Review the search strategy"
      [runId]="runId()"
      [state]="state()"
      [detail]="detail()"
      [blocking]="blocking()"
      [rules]="RULES"
      [busy]="state() === 'validating'"
      approveLabel="Approve strategy and search"
      [canApprove]="violations().length === 0"
      (approve)="approve()"
      (back)="back.emit()"
      (reload)="reload()"
    >
      <section class="gate-body strategy-body">
        <!-- What was approved at the previous stage. Read-only here: this stage
             approves queries, and editing the analysis would change an artifact
             that was already approved at its own version. -->
        <section class="understanding-pane" aria-labelledby="approved-heading">
          <h3 id="approved-heading">What we understood, and approved</h3>
          <p class="pane-note">Approved earlier. Not editable here.</p>
          <blockquote class="original-text">{{ approvedSummary() }}</blockquote>
        </section>

        <section aria-labelledby="queries-heading">
          <h3 id="queries-heading">AI-proposed query coverage</h3>
          <p class="pane-note">
            These are the searches that will run. Switch off anything you do not
            want searched, and say which technical elements each search is looking
            for. Nothing is searched until you approve this.
          </p>

          @for (query of queries(); track query.id; let q = $index) {
            <article class="query-card" [class.query-off]="!query.enabled">
              <header class="query-head">
                <label class="query-enabled">
                  <input
                    type="checkbox"
                    [(ngModel)]="query.enabled"
                    (ngModelChange)="touch()"
                    [disabled]="state() === 'validating'"
                    [attr.aria-label]="'Run query ' + (q + 1)"
                  />
                  Run this search
                </label>
                <button
                  class="btn-outlined btn-small"
                  type="button"
                  [attr.aria-label]="'Remove query ' + (q + 1)"
                  [disabled]="state() === 'validating' || queries().length < 2"
                  (click)="removeQuery(q)"
                >
                  <app-icon name="X" /> Remove
                </button>
              </header>

              <label class="sr-only" [attr.for]="'query-text-' + q">Search terms</label>
              <input
                [attr.id]="'query-text-' + q"
                class="query-text"
                [class.row-invalid]="!query.text.trim()"
                [(ngModel)]="query.text"
                (ngModelChange)="touch()"
                [disabled]="state() === 'validating'"
                placeholder="Search terms"
              />
              @if (query.rationale) {
                <p class="query-rationale">{{ query.rationale }}</p>
              }

              <fieldset class="query-coverage">
                <legend>What this search is looking for</legend>
                @for (element of elements(); track element.id) {
                  <label class="coverage-chip" [class.chip-core]="element.core">
                    <input
                      type="checkbox"
                      [checked]="query.covers.includes(element.id)"
                      (change)="toggleCoverage(query, element.id)"
                      [disabled]="state() === 'validating'"
                    />
                    <span class="mono">{{ element.id }}</span>
                    {{ element.text }}
                    @if (element.core) {
                      <span class="chip-core-tag">core</span>
                    }
                  </label>
                }
              </fieldset>
            </article>
          }

          <button
            class="btn-outlined"
            type="button"
            [disabled]="state() === 'validating' || queries().length >= maxQueries()"
            (click)="addQuery()"
          >
            <app-icon name="Plus" /> Add a search
          </button>
          <p class="pane-note">
            {{ enabledCount() }} of {{ queries().length }} searches will run.
            At most {{ maxQueries() }} searches.
          </p>
        </section>

        <!-- Coverage, stated as a fact about this strategy rather than as a
             score. An uncovered core element is the one thing that blocks
             approval here, so it is named, not counted. -->
        <section class="coverage-summary" aria-labelledby="coverage-heading">
          <h3 id="coverage-heading">Coverage of your approved elements</h3>
          <ul class="coverage-list">
            @for (element of elements(); track element.id) {
              <li
                class="coverage-row"
                [class.coverage-missing]="!covered().has(element.id) && element.core"
                [class.coverage-partial]="!covered().has(element.id) && !element.core"
              >
                <span class="mono">{{ element.id }}</span>
                {{ element.text }}
                @if (covered().has(element.id)) {
                  <span class="coverage-tag">searched for</span>
                } @else if (element.core) {
                  <span class="coverage-tag">not searched for — core</span>
                } @else {
                  <span class="coverage-tag">not searched for</span>
                }
              </li>
            }
          </ul>
          <p class="pane-note">
            A core element with no search will not be reported on. Finding no
            prior art for something nobody searched for is not a finding.
          </p>
        </section>

        <!-- Asking for different queries. A 202 request, never a model call
             here, and never a provider call. -->
        <section class="regenerate-pane" aria-labelledby="regen-strategy-heading">
          <h3 id="regen-strategy-heading">Not the right searches?</h3>
          <label class="sr-only" for="regen-strategy-feedback">What is wrong with these searches</label>
          <textarea
            id="regen-strategy-feedback"
            class="summary-input"
            rows="2"
            [(ngModel)]="feedback"
            [disabled]="state() === 'validating'"
            placeholder="Optional: what should it look for instead?"
          ></textarea>
          <button
            class="btn-outlined"
            type="button"
            [disabled]="state() === 'validating'"
            (click)="regenerate()"
          >
            <app-icon name="RefreshCw" /> Propose different searches
          </button>
          <p class="pane-note">
            This asks for a fresh set as a new version. Nothing you see now is
            deleted, and nothing is searched.
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
export class StrategyReview {
  private readonly runs = inject(RunsService);

  readonly runId = input.required<string>();
  readonly back = output<void>();
  readonly resumed = output<void>();

  readonly state = signal<GateState>('loading');
  readonly detail = signal('');
  readonly blocking = signal<Array<Record<string, unknown>>>([]);
  readonly queries = signal<DraftQuery[]>([]);
  readonly elements = signal<CoverageElement[]>([]);
  readonly approvedSummary = signal('');
  readonly approvedVersion = signal<number | null>(null);

  /** From the server rather than restated here, so the two cannot disagree. */
  readonly maxQueries = signal(10);

  feedback = '';
  version = 1;
  understandingVersion = 0;

  private actionKey = this.runs.newKey();

  readonly RULES = [
    'at least one search, and at least one of them switched on',
    'every search has terms',
    'every core technical element is covered by a search that will run',
  ];

  /**
   * The element ids at least one **enabled** query claims to cover.
   *
   * A method rather than a `computed()`, for the same reason `violations()` is
   * one on the understanding screen: it reads a signal *and* plain nested
   * objects, and a computed tracks only signals — so toggling a query off would
   * never invalidate it and the coverage panel would show a search that is no
   * longer running as still covering an element.
   */
  covered(): Set<string> {
    const ids = new Set<string>();
    for (const query of this.queries()) {
      if (!query.enabled) continue;
      for (const id of query.covers) ids.add(id);
    }
    return ids;
  }

  enabledCount(): number {
    return this.queries().filter((query) => query.enabled).length;
  }

  /**
   * The completion rules, evaluated live, so the reviewer sees *which* rule
   * blocks them rather than sending a request to be told. The server enforces
   * the same set and is the binding check.
   */
  violations(): string[] {
    const queries = this.queries();
    const problems: string[] = [];

    if (queries.length === 0) {
      problems.push('At least one search is required.');
    }
    if (queries.length > 0 && this.enabledCount() === 0) {
      problems.push('At least one search must be switched on, or nothing is searched.');
    }
    if (queries.some((query) => !query.text.trim())) {
      problems.push('Every search needs terms.');
    }
    if (queries.length > this.maxQueries()) {
      problems.push(`At most ${this.maxQueries()} searches can be approved.`);
    }
    const ids = queries.map((query) => query.id);
    if (new Set(ids).size !== ids.length) {
      problems.push('Two searches share an identifier.');
    }
    const covered = this.covered();
    const uncovered = this.elements()
      .filter((element) => element.core && !covered.has(element.id))
      .map((element) => element.id);
    if (uncovered.length > 0) {
      problems.push(
        `These core elements have no search that will run: ${uncovered.join(', ')}.`,
      );
    }
    if (!Number.isFinite(this.version) || this.version < 1) {
      problems.push('The approval must name a version.');
    }
    if (!Number.isFinite(this.understandingVersion) || this.understandingVersion < 1) {
      problems.push('The strategy must name the approved analysis it covers.');
    }
    return problems;
  }

  constructor() {
    queueMicrotask(() => this.reload());
  }

  async reload(): Promise<void> {
    this.state.set('loading');
    this.detail.set('');
    this.blocking.set([]);
    this.actionKey = this.runs.newKey();
    try {
      const payload = await this.runs.gateStrategy(this.runId());

      const approved = payload['approved_version'];
      this.approvedVersion.set(typeof approved === 'number' ? approved : null);
      this.approvedSummary.set(String(payload['approved_summary'] ?? ''));

      const limits = (payload['limits'] ?? {}) as Record<string, number>;
      if (typeof limits['max_queries'] === 'number') {
        this.maxQueries.set(limits['max_queries']);
      }

      this.elements.set(
        ((payload['elements'] ?? []) as CoverageElement[]).map((element) => ({
          id: String(element.id),
          feature_id: String(element.feature_id ?? ''),
          feature_text: String(element.feature_text ?? ''),
          text: String(element.text ?? ''),
          core: element.core === true,
        })),
      );

      const understanding = payload['understanding_version'];
      this.understandingVersion = typeof understanding === 'number' ? understanding : 0;

      const proposal = (payload['candidate_search_strategy'] ?? null) as {
        version?: number;
        understanding_version?: number;
        queries?: Array<{
          query_id?: string;
          query_text?: string;
          rationale?: string;
          enabled?: boolean;
          covered_element_ids?: string[];
        }>;
      } | null;

      this.version = proposal?.version ?? (this.approvedVersion() ?? 0) + 1;
      if (typeof proposal?.understanding_version === 'number') {
        this.understandingVersion = proposal.understanding_version;
      }
      this.feedback = '';

      const drafted: DraftQuery[] = (proposal?.queries ?? []).map((query, index) => ({
        id: query.query_id ?? `Q${index + 1}`,
        text: query.query_text ?? '',
        rationale: query.rationale ?? '',
        // A proposed query arrives on. Switching one off is the reviewer's
        // decision, and defaulting to off would be this screen making it.
        enabled: query.enabled !== false,
        covers: (query.covered_element_ids ?? []).map(String),
      }));
      this.queries.set(drafted);

      // An empty proposal is a real state, not a failure: the machine may have
      // produced nothing, and the reviewer writes the searches themselves.
      this.state.set(drafted.length === 0 ? 'empty' : 'review');
    } catch (error) {
      this.state.set(classify(error, this.detail));
    }
  }

  /**
   * Re-publish the query list so the screen re-renders after a nested edit.
   *
   * The queries are plain objects inside a signal's array, so mutating one does
   * not notify anything. Every edit path goes through here.
   */
  touch(): void {
    this.queries.set([...this.queries()]);
  }

  toggleCoverage(query: DraftQuery, elementId: string): void {
    query.covers = query.covers.includes(elementId)
      ? query.covers.filter((id) => id !== elementId)
      : [...query.covers, elementId];
    this.touch();
  }

  addQuery(): void {
    if (this.queries().length >= this.maxQueries()) return;
    // Ids are minted above the highest already present rather than from the
    // count, so removing Q2 and adding one does not produce a second Q3.
    const used = this.queries().map((query) => Number(query.id.replace(/^Q/, '')) || 0);
    const next = Math.max(0, ...used) + 1;
    this.queries.set([
      ...this.queries(),
      { id: `Q${next}`, text: '', rationale: '', enabled: true, covers: [] },
    ]);
  }

  removeQuery(index: number): void {
    this.queries.set(this.queries().filter((_, i) => i !== index));
  }

  /**
   * Ask for a different set of searches. **A request, not a re-planning.**
   *
   * The worker carries it out under the run's lease; this returns `202` and the
   * run resumes. Nothing here is an approval, the rejected version is kept, and
   * no provider is contacted.
   */
  async regenerate(): Promise<void> {
    this.state.set('validating');
    try {
      await this.runs.regenerateStrategy(
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
      await this.runs.approveGateStrategy(
        this.runId(),
        {
          version: this.version,
          understanding_version: this.understandingVersion,
          // **Every query, enabled or not.** A switched-off search is a decision
          // about the search, and the record has to be able to say what was
          // deliberately not searched for. Filtering here would leave the report
          // unable to distinguish that from a query nobody ever proposed.
          queries: this.queries().map((query, index) => ({
            query_id: query.id.trim(),
            query_text: query.text.trim(),
            rationale: query.rationale,
            enabled: query.enabled,
            covered_element_ids: query.covers,
            order: index + 1,
          })),
        },
        this.actionKey,
      );
      this.state.set('resuming');
      this.resumed.emit();
    } catch (error) {
      const failure = this.runs.lastFailure();
      this.blocking.set(failure?.blocking ?? []);
      this.state.set(classify(error, this.detail));
    }
  }
}
