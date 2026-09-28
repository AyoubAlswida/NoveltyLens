import { Component, computed, input, output } from '@angular/core';
import { Icon } from '../shared/icon';
import { RunApiError } from '../services/runs';
import { PUBLIC_ERRORS, publicRunError } from '../shared/public-errors';

/**
 * Every state a workflow-gate screen can be in.
 *
 * **Thirteen, and collapsing any two would report one situation as another** — the
 * same substitution the backend's zero-tolerance policy forbids, moved into the
 * UI (spec Gate F item 4).
 *
 * - `loading` — we have not asked yet. **Not** `empty`.
 * - `empty` — the gate genuinely has nothing here. **Not** a failed fetch.
 * - `review` — parked, and it is this person's turn.
 * - `validating` — a decision is in flight; controls lock.
 * - `waiting` — the run is not at this gate yet.
 * - `resuming` — a decision committed; the executor has not picked it up yet.
 * - `blocked` — the gate refused, and **every** blocking condition is listed.
 * - `stale` — the artifact moved under the reviewer. Never resubmitted.
 * - `forbidden` — `RUN_NOT_FOUND`; the API deliberately does not distinguish
 *   "no such run" from "not yours".
 * - `reconnecting` — the stream dropped. **A client problem, never a run
 *   failure**, so the UI says reconnecting rather than failed (Gate F item 3).
 * - `failed` — the run itself failed, with the backend's sanitized detail shown
 *   verbatim.
 * - `completed` — terminal success.
 *
 * `paused` was REMOVED, by proof rather than by guess: it meant "parked at a
 * gate, shown from the running screen", and no surviving screen ever sets it --
 * `grep` finds the SSE `paused` EVENT type, which is a different thing that the
 * running screen handles itself. The other twelve all survive and **none were
 * collapsed into another**: each distinction exists because reporting one
 * situation as another is the defect this project forbids, so `loading` still
 * is not `empty`, and `reconnecting` still is not `failed`.
 */
export type GateState =
  | 'loading'
  | 'empty'
  | 'review'
  | 'validating'
  | 'waiting'
  | 'resuming'
  | 'blocked'
  | 'stale'
  | 'forbidden'
  | 'reconnecting'
  | 'failed'
  | 'completed';

/**
 * The frame every gate screen shares: heading, state rendering, completion rules,
 * the approve/decline footer, and the audit note.
 *
 * Shared so the four gates cannot drift on what a `stale` or a `forbidden` looks
 * like, while each supplies its own body through `<ng-content>`. The content is
 * rendered **only** in the states where a decision is possible, so a screen
 * cannot accidentally show an editor over a run the caller does not own.
 */
/**
 * Internal gate letter to the stage name an inventor sees.
 *
 * Kept as data next to the shell that renders it, so a screen cannot invent its
 * own wording for a stage the rest of the workflow names differently.
 */
export const STAGE_LABELS: Record<string, string> = {
  A: 'Invention understanding',
  STRATEGY: 'Search strategy',
  B: 'Results and evidence',
  C: 'Results and evidence',
  D: 'Final report',
};

@Component({
  selector: 'app-gate-shell',
  standalone: true,
  imports: [Icon],
  template: `
    <!-- screen-scroll is what makes this box scroll. The workspace is
         height:100vh with overflow hidden, so every screen owns its own
         scrolling, and the six original screens all carry this class. The four
         gate screens shipped without it, so a decomposition with more than a
         few requirements could not be reached at all -- approve button
         included. The browser checks missed it because clicking through the
         DOM does not require an element to be reachable by a person. -->
    <div class="screen screen-scroll gate-screen">
      @if (state() !== 'resuming') {
      <header class="stage-header glass-card">
        <!-- No back control here. It lives in the footer, labelled, beside the
             action it is an alternative to. A chevron up here as well meant two
             controls doing the identical thing on one screen, and the icon-only
             one said nothing about what it did. -->
        <div class="stage-header-body">
          <h1>{{ displayTitle() }}</h1>
          @if (state() !== 'resuming' && subtitle()) {
            <p class="stage-lede">{{ subtitle() }}</p>
          }
        </div>

        <!-- Anchors the right end of the header. Rendered text rather than an
             aria-label: a reviewer three screens into a flow needs to see how
             far along they are. -->
        @if (steps().length > 0) {
          <span class="stage-step-count">
            Step {{ current() }} of {{ steps().length }}
          </span>
        }
      </header>
      }

      @switch (state()) {
        @case ('loading') {
          <div class="glass-card gate-card" data-state="loading" aria-busy="true">
            <div class="spinner" aria-hidden="true"></div>
            <p>Loading what needs your review…</p>
          </div>
        }

        @case ('forbidden') {
          <div class="glass-card gate-card" data-state="forbidden" role="alert">
            <h2><app-icon name="LockKeyhole" /> Not available</h2>
            <p>
              {{ unavailableMessage }}
            </p>
          </div>
        }

        @case ('reconnecting') {
          <div class="glass-card gate-card" data-state="reconnecting" aria-busy="true">
            <h2><app-icon name="RotateCcw" /> Reconnecting…</h2>
            <p>
              We lost the connection. The run is unaffected, and we’re trying again now.
            </p>
          </div>
        }

        @case ('waiting') {
          <div class="glass-card gate-card" data-state="waiting" aria-busy="true">
            <h2>Not at this step yet</h2>
            <p>The run has not reached {{ stageLabel() }}. Nothing to review here.</p>
          </div>
        }

        @case ('resuming') {
          <div
            class="glass-card gate-card gate-transition"
            data-state="resuming"
            aria-busy="true"
          >
            <div class="gate-transition-icon" aria-hidden="true">
              <app-icon name="Check" [size]="24" />
            </div>
            <div class="gate-transition-copy">
              <span class="gate-transition-status">Changes saved</span>
              <h1>{{ displayTitle() }}</h1>
              <p>{{ transitionMessage() }}</p>
            </div>
            <div class="gate-transition-progress" aria-hidden="true">
              <span></span>
            </div>
            <p class="gate-transition-note">
              No action is needed. The next step will open automatically.
            </p>
          </div>
        }

        @case ('failed') {
          <div class="glass-card gate-card" data-state="failed" role="alert">
            <h2><app-icon name="AlertTriangle" /> This run failed</h2>
            <p class="gate-note">{{ detail() }}</p>
            <p class="gate-note">
              No partial results are shown.
            </p>
          </div>
        }

        @case ('stale') {
          <div class="glass-card gate-card" data-state="stale" role="alert">
            <h2><app-icon name="RotateCcw" /> This changed while you were reviewing</h2>
            <p class="gate-note">{{ detail() }}</p>
            <button class="primary-action compact" type="button" (click)="reload.emit()">
              Reload and review again
            </button>
          </div>
        }

        @default {
          <div class="glass-card gate-card" [attr.data-state]="state()">
            @if (state() === 'blocked') {
              <div class="gate-blocked" role="alert">
                <h2><app-icon name="AlertTriangle" /> Check your changes</h2>
                <ul class="blocking-list">
                  @for (item of blocking(); track $index) {
                    <li>{{ describe(item) }}</li>
                  }
                </ul>
              </div>
            }

            @if (state() === 'empty') {
              <p class="gate-empty">
                Nothing here yet. This is a real result, not a failed load.
              </p>
            }

            <ng-content />

            <footer class="gate-actions">
              <button
                class="primary-action compact"
                type="button"
                [disabled]="busy() || !canApprove()"
                (click)="approve.emit()"
              >
                @if (busy()) {
                  <span class="spinner spinner-inline" aria-hidden="true"></span> Submitting…
                } @else {
                  <!-- No icon. A ShieldCheck was left here from when this button
                       said "Approve" on a verification gate; on "Next" it means
                       nothing, and an icon that decorates rather than tells you
                       something is noise on the control you press most. -->
                  {{ approveLabel() }}
                }
              </button>
              <!-- "Back", not "Not yet". The old label answered a question this
                   screen stopped asking: it belonged to a gate where declining
                   meant "I am not approving this yet". Here it steps back a
                   stage, and on the first stage it leaves the review. -->
              <button class="secondary-button" type="button" [disabled]="busy()" (click)="back.emit()">
                Back
              </button>
            </footer>

          </div>
        }
      }
    </div>
  `,
})
export class GateShell {
  readonly unavailableMessage = PUBLIC_ERRORS.unavailable;
  readonly gate = input.required<string>();
  readonly title = input.required<string>();
  /** The sentence under the title, saying what this stage is asking for. */
  readonly subtitle = input<string>('');

  /**
   * The names of every stage, in order, and which one is showing.
   *
   * Names rather than numbers: "Elements" tells a reviewer what is coming,
   * where a third dot tells them only that something is. The count beside it
   * ("Step 2 of 3") is the plain rendered text the spec requires.
   */
  readonly steps = input<string[]>([]);
  readonly current = input<number>(0);


  /**
   * The stage name an inventor sees, derived from the internal gate letter.
   *
   * **The letters are an implementation detail and never reach the screen.**
   * "Gate A" names a position in a workflow graph; an inventor is doing a piece
   * of work with a name. The mapping lives here rather than in each screen so
   * the four cannot drift into describing the same workflow differently.
   */
  readonly stageLabel = computed(() => STAGE_LABELS[this.gate()] ?? "Review");

  readonly displayTitle = computed(() => {
    if (this.state() !== 'resuming') return this.title();
    if (this.current() === 1) return 'Preparing your invention requirements';
    if (this.current() === 2) return 'Preparing your search strategy';
    return 'Starting your prior-art search';
  });

  readonly transitionMessage = computed(() => {
    if (this.current() === 1) {
      return 'We’re turning your approved invention summary into focused invention requirements for you to review.';
    }
    if (this.current() === 2) {
      return 'We’re creating focused search queries from the invention requirements you approved.';
    }
    return 'We’re starting the searches you approved. Results will appear here when they’re ready.';
  });

  readonly state = input.required<GateState>();
  readonly detail = input<string>('');
  readonly blocking = input<Array<Record<string, unknown>>>([]);
  readonly busy = input<boolean>(false);
  readonly approveLabel = input<string>('Approve');
  readonly canApprove = input<boolean>(true);

  readonly approve = output<void>();
  readonly back = output<void>();
  readonly reload = output<void>();

  describe(condition: Record<string, unknown> | string): string {
    const code = typeof condition === 'string' ? condition : condition['code'];
    if (code === 'NO_SUMMARY') return 'Add a technical summary.';
    if (code === 'NO_ELEMENTS') return 'Add at least one technical limitation.';
    if (code === 'NO_QUERIES') return 'Add at least one search query.';
    return PUBLIC_ERRORS.blocked;
  }
}


/**
 * The one place an API failure becomes a screen state.
 *
 * Shared by every gate screen so they cannot disagree about what a
 * `RUN_NOT_FOUND` or a `STALE_RESULT_VERSION` means. Every branch is a state the
 * user can act on; nothing collapses into a generic error.
 */
export function classify(error: unknown, detail: { set(value: string): void }): GateState {
  if (!(error instanceof RunApiError)) {
    detail.set(PUBLIC_ERRORS.analysis);
    return 'failed';
  }
  detail.set(publicRunError(error.failure.code));
  switch (error.failure.code) {
    case 'RUN_NOT_FOUND':
      return 'forbidden';
    case 'GATE_BLOCKED':
      return 'blocked';
    case 'STALE_RESULT_VERSION':
    case 'IDEMPOTENCY_CONFLICT':
    case 'WRONG_GATE':
    case 'RUN_NOT_RESUMABLE':
      return 'stale';
    case 'STREAM_UNAVAILABLE':
      return 'reconnecting';
    default:
      return 'failed';
  }
}
