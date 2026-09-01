import { Component, computed, input, output } from '@angular/core';
import { Icon } from '../shared/icon';
import { RunApiError } from '../services/runs';

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
 * - `paused` — parked at a gate, shown from the running screen.
 * - `resuming` — a decision committed; the executor has not picked it up yet.
 * - `blocked` — the gate refused, and **every** blocking condition is listed.
 * - `stale` — the artifact moved under the reviewer. Never resubmitted.
 * - `forbidden` — `RUN_NOT_FOUND`, identical for "no such run" and "not yours".
 * - `reconnecting` — the stream dropped. **A client problem, never a run
 *   failure**, so the UI says reconnecting rather than failed (Gate F item 3).
 * - `failed` — the run itself failed, with the backend's sanitized detail shown
 *   verbatim.
 * - `completed` — terminal success.
 */
export type GateState =
  | 'loading'
  | 'empty'
  | 'review'
  | 'validating'
  | 'waiting'
  | 'paused'
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
      <header class="gate-head">
        <div>
          <h1>{{ title() }}</h1>
          <p class="gate-sub">
            {{ stageLabel() }} · <span class="mono">{{ runId() }}</span>
          </p>
        </div>
        <button class="btn-outlined" type="button" (click)="back.emit()">
          <app-icon name="ChevronLeft" /> Back
        </button>
      </header>

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
              No run with this identifier was found for your account. It may not
              exist, or it may belong to someone else — the server deliberately
              does not distinguish the two, so neither does this screen.
            </p>
          </div>
        }

        @case ('reconnecting') {
          <div class="glass-card gate-card" data-state="reconnecting" aria-busy="true">
            <h2><app-icon name="RotateCcw" /> Reconnecting…</h2>
            <p>
              The live connection dropped. <strong>The run is unaffected</strong> —
              it is executing in a worker that has never heard of this browser.
              Nothing is lost; everything that happens while you are away is
              replayed when the connection returns.
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
          <div class="glass-card gate-card" data-state="resuming">
            <h2><app-icon name="Check" /> Decision recorded</h2>
            <p>
              Your decision is committed and the run is queued to continue. The
              worker picks it up on its next poll — this page does not resume it,
              and nothing is running yet.
            </p>
          </div>
        }

        @case ('failed') {
          <div class="glass-card gate-card" data-state="failed" role="alert">
            <h2><app-icon name="AlertTriangle" /> This run failed</h2>
            <pre class="error-detail">{{ detail() }}</pre>
            <p class="gate-note">
              No report was produced, and nothing partial is shown. A partial
              verification is not a shorter answer — it is a different one.
            </p>
          </div>
        }

        @case ('stale') {
          <div class="glass-card gate-card" data-state="stale" role="alert">
            <h2><app-icon name="RotateCcw" /> This changed while you were reviewing</h2>
            <pre class="error-detail">{{ detail() }}</pre>
            <p class="gate-note">
              Your decision was <strong>not</strong> applied. You reviewed specific
              evidence, and reattaching that judgement to different evidence would
              credit you with having seen something you did not.
            </p>
            <button class="btn-contained" type="button" (click)="reload.emit()">
              Reload and review again
            </button>
          </div>
        }

        @default {
          <div class="glass-card gate-card" [attr.data-state]="state()">
            @if (state() === 'blocked') {
              <div class="gate-blocked" role="alert">
                <h2><app-icon name="AlertTriangle" /> This gate cannot be completed yet</h2>
                <ul class="blocking-list">
                  @for (item of blocking(); track $index) {
                    <li>{{ describe(item) }}</li>
                  }
                </ul>
                <p class="gate-note">Every outstanding condition is listed, not just the first.</p>
              </div>
            }

            @if (state() === 'empty') {
              <p class="gate-empty">
                Nothing here yet. This is a real result, not a failed load.
              </p>
            }

            @if (rules().length > 0) {
              <section class="gate-rules">
                <h3>What this step requires</h3>
                <ul>
                  @for (rule of rules(); track rule) {
                    <li>{{ rule }}</li>
                  }
                </ul>
              </section>
            }

            <ng-content />

            <footer class="gate-actions">
              <button
                class="btn-contained"
                type="button"
                [disabled]="busy() || !canApprove()"
                (click)="approve.emit()"
              >
                @if (busy()) {
                  <span class="spinner spinner-inline" aria-hidden="true"></span> Submitting…
                } @else {
                  <app-icon name="ShieldCheck" /> {{ approveLabel() }}
                }
              </button>
              <button class="btn-outlined" type="button" [disabled]="busy()" (click)="back.emit()">
                Not yet
              </button>
            </footer>

            <p class="gate-note gate-audit">
              Recorded against your account with a timestamp, in an append-only
              audit trail. There is no automatic approval anywhere in this
              workflow. This tool reports what the supplied passages state; it is
              not a legal opinion, not an exhaustive prior-art search, and not a
              determination that anything is novel.
            </p>
          </div>
        }
      }
    </div>
  `,
})
export class GateShell {
  readonly gate = input.required<string>();
  readonly title = input.required<string>();
  readonly runId = input.required<string>();

  /**
   * The stage name an inventor sees, derived from the internal gate letter.
   *
   * **The letters are an implementation detail and never reach the screen.**
   * "Gate A" names a position in a workflow graph; an inventor is doing a piece
   * of work with a name. The mapping lives here rather than in each screen so
   * the four cannot drift into describing the same workflow differently.
   */
  readonly stageLabel = computed(() => STAGE_LABELS[this.gate()] ?? "Review");

  readonly state = input.required<GateState>();
  readonly detail = input<string>('');
  readonly blocking = input<Array<Record<string, unknown>>>([]);
  readonly rules = input<string[]>([]);
  readonly busy = input<boolean>(false);
  readonly approveLabel = input<string>('Approve');
  readonly canApprove = input<boolean>(true);

  readonly approve = output<void>();
  readonly back = output<void>();
  readonly reload = output<void>();

  describe(condition: Record<string, unknown> | string): string {
    if (typeof condition === 'string') return condition;
    const reason = condition['reason'] ?? condition['detail'] ?? condition['code'];
    const subject = condition['cell_id'] ?? condition['publication_number'] ?? '';
    return [subject, reason].filter(Boolean).join(' — ') || JSON.stringify(condition);
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
    detail.set('The request could not be completed.');
    return 'failed';
  }
  detail.set(error.failure.detail);
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
