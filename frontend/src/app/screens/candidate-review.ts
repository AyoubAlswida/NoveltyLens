import { Component, computed, inject, input, output, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Icon } from '../shared/icon';
import { classify, GateShell, type GateState } from './gate-shell';
import { RunsService } from '../services/runs';
import { exclusionTerm, type Term } from '../models/vocabulary';

interface Candidate {
  publication_number: string;
  normalized_number?: string;
  passages_built?: number;
  sections_retrieved?: string[];
  publication_date?: string;
  source?: string;
}

interface Exclusion {
  requested_number: string;
  normalized_number?: string;
  state: string;
  reason: string;
  sections_retrieved?: string[];
  passages_built?: number;
}

/**
 * **Workflow Gate B — candidate and eligibility review** (spec §12.2).
 *
 * ## Exclusions are a separate table, never matrix cells
 *
 * Spec Gate F item 6 and case AH. `EXCLUDED_DATE`, `EXCLUDED_NO_FULL_TEXT` and
 * `EXCLUDED_INELIGIBLE` each render as themselves in their own table, with the
 * reason and what was retrieved. They are **not** greyed rows in the candidate
 * list and never become cells: a cell implies a comparison was attempted and
 * answered, and for an excluded publication none was.
 *
 * ## Two exclusions are not overridable, and the UI says why
 *
 * §12.2: `EXCLUDED_DATE` and `EXCLUDED_NO_FULL_TEXT` cannot be ticked. Date
 * eligibility is arithmetic against the cutoff, not a judgement (case AL), and
 * there is no manual eligibility flag (case AK). The only path is correcting the
 * input, which creates a new version — so the control is absent rather than
 * disabled-with-a-tooltip, because a disabled tick still suggests the door
 * exists.
 *
 * ## Adding a publication by hand
 *
 * Case T. It is queued for the **same** normalization, date screening,
 * enrichment and eligibility checks a discovered candidate goes through, and the
 * screen says so. Adding is not approving, and the worker may still exclude it.
 */
@Component({
  selector: 'app-candidate-review',
  standalone: true,
  imports: [FormsModule, Icon, GateShell],
  template: `
    <app-gate-shell
      gate="B"
      title="Review the publications that will be examined"
      [runId]="runId()"
      [state]="state()"
      [detail]="detail()"
      [blocking]="[]"
      [rules]="RULES"
      [busy]="state() === 'validating'"
      approveLabel="Approve these publications"
      [canApprove]="selected().length > 0"
      (approve)="approve()"
      (back)="back.emit()"
      (reload)="reload()"
    >
      <section class="gate-body">
        <h3>Publications that can be examined ({{ candidates().length }})</h3>
        <p class="gate-note">
          The search found these, and full text was retrieved for each one, so
          each can be compared against your invention. Untick anything you do not
          want examined.
        </p>
        @if (candidates().length === 0) {
          <p class="gate-empty">
            Nothing the search found could be examined. That is a real result
            about this run's coverage, not a failure — the reasons are listed
            below.
          </p>
        }
        <table class="candidate-table">
          <caption class="sr-only">Candidates eligible for verification</caption>
          <thead>
            <tr>
              <th scope="col"><span class="sr-only">Include</span></th>
              <th scope="col">Publication</th>
              <th scope="col">Passages available to cite</th>
              <th scope="col">Parts of the document retrieved</th>
              <th scope="col">How it was found</th>
            </tr>
          </thead>
          <tbody>
            @for (candidate of candidates(); track candidate.publication_number) {
              <tr>
                <td>
                  <input
                    type="checkbox"
                    [attr.id]="'cand-' + candidate.publication_number"
                    [attr.aria-label]="'Include ' + candidate.publication_number"
                    [checked]="selected().includes(candidate.publication_number)"
                    [disabled]="state() === 'validating'"
                    (change)="toggle(candidate.publication_number)"
                  />
                </td>
                <th scope="row" class="mono">{{ candidate.publication_number }}</th>
                <td>{{ candidate.passages_built ?? 0 }}</td>
                <td>{{ (candidate.sections_retrieved ?? []).join(', ') || '—' }}</td>
                <td>{{ candidate.source || 'discovered' }}</td>
              </tr>
            }
          </tbody>
        </table>

        <!-- A SEPARATE table. Never matrix cells, never greyed candidate rows. -->
        <h3 class="exclusion-heading">
          Publications that will not be examined ({{ exclusions().length }})
        </h3>
        <p class="gate-note">
          These were <strong>not evaluated</strong> against your invention, and
          the final report says so. Leaving a publication out is a statement
          about how far this run could reach — it is never a finding about the
          publication itself.
        </p>
        @if (exclusions().length === 0) {
          <p class="gate-empty">None. Every publication found can be examined.</p>
        }
        <table class="exclusion-table">
          <caption class="sr-only">Publications excluded before verification</caption>
          <thead>
            <tr>
              <th scope="col">Publication</th>
              <th scope="col">Why it will not be examined</th>
              <th scope="col">What the system reported</th>
              <th scope="col">Can this be changed here?</th>
            </tr>
          </thead>
          <tbody>
            @for (exclusion of exclusions(); track exclusion.requested_number) {
              <tr>
                <th scope="row" class="mono">{{ exclusion.requested_number }}</th>
                <td>
                  <span
                    class="exclusion-state"
                    [attr.data-exclusion]="exclusion.state"
                    [attr.title]="exclusionWords(exclusion.state).meaning"
                  >
                    {{ exclusionWords(exclusion.state).label }}
                  </span>
                  <span class="machine-code">{{ exclusion.state }}</span>
                </td>
                <td>{{ exclusion.reason }}</td>
                <td>
                  @if (overridable(exclusion.state)) {
                    <span class="exclusion-overridable">
                      Yes — add it again below to send it through the checks
                    </span>
                  } @else {
                    <span class="exclusion-fixed">
                      No — this is not a judgement call, so correct the input
                      instead
                    </span>
                  }
                </td>
              </tr>
            }
          </tbody>
        </table>

        <div class="add-candidate">
          <h3>Add a publication yourself</h3>
          <div class="add-row">
            <label class="sr-only" for="add-pub">Publication number</label>
            <input
              id="add-pub"
              class="mono"
              [(ngModel)]="newPublication"
              [disabled]="state() === 'validating'"
              placeholder="EP4778509"
            />
            <button
              class="btn-outlined"
              type="button"
              [disabled]="state() === 'validating' || !newPublication.trim()"
              (click)="addCandidate()"
            >
              <app-icon name="Plus" /> Add
            </button>
          </div>
          <p class="gate-note">
            It goes through exactly the same date screening, full-text retrieval
            and eligibility checks as a publication the search found.
            <strong>Adding it does not approve it</strong>, and it may still end
            up in the list above.
          </p>
          @if (addedNote()) {
            <p class="field-note" role="status">{{ addedNote() }}</p>
          }
        </div>
      </section>
    </app-gate-shell>
  `,
})
export class CandidateReview {
  private readonly runs = inject(RunsService);

  readonly runId = input.required<string>();
  readonly back = output<void>();
  readonly resumed = output<void>();

  readonly state = signal<GateState>('loading');
  readonly detail = signal('');
  readonly candidates = signal<Candidate[]>([]);
  readonly exclusions = signal<Exclusion[]>([]);
  readonly selected = signal<string[]>([]);
  readonly addedNote = signal('');
  readonly version = signal(1);

  newPublication = '';
  private actionKey = this.runs.newKey();

  /**
   * `graph/gates.py`'s `GATE_B_RULES`, in plain words, **one-to-one and in the
   * same order**. The server's list is the binding one; see the equivalent note
   * on the evidence screen, and the count check in
   * `tests/test_frontend_review_contract.py`.
   */
  readonly RULES = [
    'every approved publication is dated before the cutoff for this run',
    'full text was retrieved for it',
    'it has at least one passage that can be cited',
    'a publication left out for its date, or for having no full text, cannot be added back here',
  ];

  /** Words for an exclusion code, keeping the code. See `models/vocabulary.ts`. */
  exclusionWords(code: unknown): Term {
    return exclusionTerm(code == null ? '' : String(code));
  }

  /**
   * Only `EXCLUDED_INELIGIBLE` can be re-attempted, by re-adding the publication
   * so it goes through the pipeline again. The other two are decisions about the
   * document, not about this run.
   */
  overridable(state: string): boolean {
    return state === 'EXCLUDED_INELIGIBLE';
  }

  constructor() {
    queueMicrotask(() => this.reload());
  }

  async reload(): Promise<void> {
    this.state.set('loading');
    this.detail.set('');
    this.actionKey = this.runs.newKey();
    try {
      const payload = await this.runs.gateB(this.runId());
      const ranked = (payload['ranked_candidates'] ?? []) as Array<Candidate | string>;
      const normalized = ranked.map((item) =>
        typeof item === 'string' ? { publication_number: item } : item,
      );
      this.candidates.set(normalized);
      this.selected.set(normalized.map((item) => item.publication_number));
      this.exclusions.set((payload['excluded'] ?? []) as Exclusion[]);
      const approved = payload['approved_version'];
      this.version.set((typeof approved === 'number' ? approved : 0) + 1);

      this.state.set(
        normalized.length === 0 && this.exclusions().length === 0 ? 'empty' : 'review',
      );
    } catch (error) {
      this.state.set(classify(error, this.detail));
    }
  }

  toggle(publication: string): void {
    const current = this.selected();
    this.selected.set(
      current.includes(publication)
        ? current.filter((item) => item !== publication)
        : [...current, publication],
    );
  }

  async addCandidate(): Promise<void> {
    const number = this.newPublication.trim();
    if (!number) return;
    this.state.set('validating');
    try {
      const response = (await this.runs.addCandidate(
        this.runId(),
        number,
        this.runs.newKey(),
      )) as { already_requested?: boolean };
      this.addedNote.set(
        response.already_requested
          ? `${number} was already queued for the eligibility pipeline.`
          : `${number} is queued for the same checks a discovered candidate goes through. It is not approved and may still be excluded.`,
      );
      this.newPublication = '';
      this.state.set('review');
    } catch (error) {
      this.state.set(classify(error, this.detail));
    }
  }

  async approve(): Promise<void> {
    if (this.selected().length === 0) return;
    this.state.set('validating');
    try {
      await this.runs.approveGateB(
        this.runId(),
        this.selected(),
        this.version(),
        this.actionKey,
      );
      this.state.set('resuming');
      this.resumed.emit();
    } catch (error) {
      this.state.set(classify(error, this.detail));
    }
  }
}
