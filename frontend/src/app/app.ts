import {
  Component,
  ChangeDetectionStrategy,
  computed,
  signal,
  effect,
  inject,
} from '@angular/core';
import { Icon } from './shared/icon';
import { SubmitScreen } from './screens/submit-screen';
import { RunningScreen } from './screens/running-screen';
import { ReportScreen } from './screens/report-screen';
import { ErrorScreen } from './screens/error-screen';
import { HistoryScreen } from './screens/history-screen';
import { LoginScreen } from './screens/login-screen';
import { ActiveRunStore, RunApiError, RunsService, StreamCursor } from './services/runs';
import { AdvancedSearchStages } from './screens/advanced-search-stages';
import type { StoredGate } from './models/report';
import { AnalysisService } from './services/analysis';
import { AuthService } from './services/auth';
import { HistoryService, reportFromHistory } from './services/history';
import { PUBLIC_ERRORS, publicRunError } from './shared/public-errors';
import {
  ADVANCED_SEARCH_PHASE_ORDER,
  ADVANCED_EXECUTION_PHASE_ORDER,
  ADVANCED_PRE_REVIEW_PHASE_ORDER,
  AnalysisReport,
  HistoryEntry,
  Phase,
  QUICK_SEARCH_PHASE_ORDER,
  SearchMode,
  StreamEvent,
  WorkflowGate,
} from './models/report';

type Screen =
  | 'submit'
  | 'running'
  | 'report'
  | 'error'
  | 'history'
  | 'review';
/**
 * `'verification-report'` is gone. A completed Advanced Search run renders
 * through the ordinary `'report'` screen -- one display path for a live run, a
 * run reopened from History, and a durable run, so none of them can drift.
 */


const RECENTS_SHOWN = 8;
/** How many past searches the sidebar lists. The History screen shows the full
 * set; this is a shortcut, and a sidebar that scrolls past the sign-out button
 * stops being one. */

const PET_EXAMPLE_DESC =
  "An automatic dry-pet-food dispenser with a rotating body containing several equal-sized compartments, each closed by a spring-loaded trap door at the bottom. A drive mechanism periodically withdraws a support bar holding a trap door shut, letting that compartment's food fall by gravity through a directional trough into the pet's bowl below, dispensing one measured portion at a time on a schedule. UNIQUE ADDITION (not in the source patent): the dispenser also has a small load cell built into the bowl itself that weighs how much food the pet actually ate versus how much was dispensed, and if a portion goes mostly uneaten for several consecutive feedings, it automatically reduces future portion sizes and sends a low-appetite alert to the owner's phone.";
const EXAMPLE_DESC =
  window.location.port === '4201'
    ? PET_EXAMPLE_DESC
    : 'A portable, refillable drinking-water bottle that automatically sterilizes its contents with an integrated UV-C emitter and filters particulates through a replaceable graphene-oxide membrane. A flow-rate sensor in the cap detects use and triggers a self-cleaning sterilization cycle without manual activation.';

@Component({
  selector: 'app-root',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    Icon,
    SubmitScreen,
    RunningScreen,
    ReportScreen,
    ErrorScreen,
    HistoryScreen,
    LoginScreen,
    AdvancedSearchStages,
  ],
  template: `
    <div class="app-shell">
      <!-- Signed out there is nothing to navigate: no screens, no recents, no
           account to sign out of. The rail is removed rather than emptied, and
           the one control that still applies moves to a corner button. -->
      @if (auth.signedIn()) {
      <aside
        class="brand-rail"
        [class.rail-open]="sidebarOpen()"
        aria-label="Primary navigation"
      >
        <div class="rail-head">
          <button class="rail-brand" aria-label="Novelty Lens home" (click)="goHome()">
            <!-- Two cuts, picked by theme, because the tile behind the mark differs.
                 Light keeps the blue/green gradient tile, which needs the reversed
                 (white) cut. Dark has no tile at all any more, so the mark sits on
                 flat black and shows its real colours. Do NOT collapse these into
                 one src with a CSS filter -- see globals.css's .rail-brand note. -->
            <img [src]="theme() === 'dark' ? MARK_COLOUR : MARK_WHITE" alt="" />
          </button>

          @if (sidebarOpen()) {
            <span class="rail-wordmark">Novelty<span>Lens</span></span>
          }

          <button
            class="rail-toggle"
            (click)="toggleSidebar()"
            [attr.aria-expanded]="sidebarOpen()"
            [attr.aria-label]="sidebarOpen() ? 'Collapse sidebar' : 'Expand sidebar'"
            [title]="sidebarOpen() ? 'Collapse sidebar' : 'Expand sidebar'"
          >
            <app-icon name="PanelLeft" [size]="18" [strokeWidth]="1.8" />
          </button>
        </div>

        <nav class="rail-nav">
          <button
            class="rail-item"
            [class.rail-item-active]="screen() !== 'history'"
            (click)="startNewSearch()"
            aria-label="Start a new search, clearing the current description and report"
            title="New Search"
          >
            <app-icon name="Plus" [size]="20" [strokeWidth]="2" />
            <span>New Search</span>
          </button>
          <button
            class="rail-item"
            [class.rail-item-active]="screen() === 'history'"
            (click)="openHistory()"
            aria-label="View past searches"
            title="History"
          >
            <app-icon name="History" [size]="20" [strokeWidth]="2" />
            <span>History</span>
          </button>
        </nav>

        <!-- Recents is expanded-only: the titles are the whole point of it, and
             there is nothing meaningful to show for a search description in a
             72px column. Collapsing hides the section rather than shrinking it. -->
        @if (sidebarOpen()) {
          <div class="rail-recents">
            <div class="rail-section-row">
              <p class="rail-section">Recents</p>
              @if (recents().length && !clearing()) {
                <button
                  class="rail-clear"
                  (click)="askClearHistory()"
                  title="Delete every stored search"
                >
                  Clear all
                </button>
              }
            </div>

            <!-- Two-step rather than a native confirm(): deleting every stored
                 report is irreversible on both sides, and the app replaced its
                 last native dialog on purpose (see ErrorScreen). -->
            @if (confirmingClear()) {
              <div class="rail-confirm" role="alertdialog" aria-label="Confirm clearing history">
                <p>Delete all {{ history().length }} stored searches? This cannot be undone.</p>
                <div>
                  <button class="rail-confirm-yes" (click)="clearHistory()" [disabled]="clearing()">
                    {{ clearing() ? 'Clearing…' : 'Delete all' }}
                  </button>
                  <button class="rail-confirm-no" (click)="confirmingClear.set(false)">
                    Cancel
                  </button>
                </div>
              </div>
            }

            @if (historyLoading() && !recents().length) {
              <p class="rail-recents-note">Loading…</p>
            } @else if (historyError()) {
              <!-- Distinct from "no searches yet" for the same reason the History
                   screen keeps them apart: a failed fetch is not an empty list. -->
              <p class="rail-recents-note">Could not load recents.</p>
            } @else {
              <ul>
                @for (entry of recents(); track entry.id) {
                  <li>
                    <button
                      class="rail-recent"
                      [class.rail-recent-active]="entry.id === openEntryId()"
                      [title]="entry.description"
                      (click)="openHistoryEntry(entry)"
                    >
                      {{ entry.description }}
                    </button>
                  </li>
                } @empty {
                  <li class="rail-recents-note">No searches yet.</li>
                }
              </ul>
            }
          </div>
        }

        <div class="rail-spacer"></div>

        <button
          class="rail-item rail-signout"
          (click)="signOut()"
          [attr.aria-label]="'Sign out of ' + (auth.email() ?? 'Novelty Lens')"
          [title]="auth.email() ?? 'Sign out'"
        >
          <app-icon name="LogOut" [size]="19" [strokeWidth]="1.9" />
          <span>Sign out</span>
        </button>

        <button
          class="rail-item rail-mode"
          (click)="toggleTheme()"
          [attr.aria-label]="'Switch to ' + (theme() === 'dark' ? 'light' : 'dark') + ' appearance'"
          [title]="'Switch to ' + (theme() === 'dark' ? 'light' : 'dark') + ' appearance'"
        >
          <app-icon [name]="theme() === 'dark' ? 'Moon' : 'Sun'" [size]="19" [strokeWidth]="1.7" />
          <span>{{ theme() === 'dark' ? 'Dark' : 'Light' }}</span>
        </button>
      </aside>
      }

      <main class="workspace">
        @if (!auth.signedIn()) {
          <!-- The rail's theme toggle, relocated: signed out it is the only
               control that still means anything, so it sits in the page corner
               instead of dragging a whole navigation column back on screen. -->
          <button
            class="theme-fab"
            (click)="toggleTheme()"
            [attr.aria-label]="'Switch to ' + (theme() === 'dark' ? 'light' : 'dark') + ' appearance'"
            [title]="'Switch to ' + (theme() === 'dark' ? 'light' : 'dark') + ' appearance'"
          >
            <app-icon [name]="theme() === 'dark' ? 'Moon' : 'Sun'" [size]="18" [strokeWidth]="1.7" />
            <span>{{ theme() === 'dark' ? 'Dark' : 'Light' }}</span>
          </button>

          <app-login-screen (signedIn)="loadHistory()" />
        } @else {
          @switch (screen()) {
            @case ('submit') {
              <app-submit-screen
                [description]="description()"
                (descriptionChange)="description.set($event)"
                (run)="startRunning()"
                (verify)="startVerifiedRun()"
              />
            }
            @case ('running') {
              <app-running-screen
                [phaseIdx]="phaseIdx()"
                [phaseMessages]="phaseMessages()"
                [phaseOrder]="phaseOrder()"
                [mode]="runMode()"
              />
            }
            @case ('report') {
              @if (reportData(); as report) {
                <app-report-screen [data]="report" (back)="goBackFromReport()" />
              }
            }
            @case ('review') {
              @if (reviewRunId(); as runId) {
                @if (reviewGate() === 'A' || reviewGate() === 'STRATEGY') {
                  <!-- One mounted editor owns all three visible stages. Changing
                       gates updates its input without discarding the drafts the
                       reviewer just approved. -->
                  <app-advanced-search-stages
                    [runId]="runId"
                    [gate]="activeReviewGate()"
                    (back)="requestReviewExit()"
                    (resumed)="afterDecision($event)"
                    (searchReleased)="afterRequirementsApproved()"
                  />
                } @else {
                    <!--
                      A run parked at a gate this app no longer knows: B, C or D
                      from before feature verification was withdrawn. It must
                      fail HONESTLY -- not a spinner, not an empty gate screen,
                      not a 500. The run's data is retained; there is simply no
                      way to continue it. Spec section 6.3.
                    -->
                    <div class="screen screen-scroll">
                      <header class="report-toolbar glass-card gate-toolbar">
                        <button
                          class="secondary-button icon-button"
                          type="button"
                          (click)="requestReviewExit()"
                          aria-label="Back"
                        >
                          <app-icon name="ChevronLeft" [size]="20" />
                        </button>
                        <div class="report-title-block">
                          <h1>This run cannot be continued</h1>
                        </div>
                      </header>
                      <p class="gate-note">
                        It is paused at a review step that has been removed from the
                        product. Its results are still stored and nothing has been
                        deleted, but there is no way to carry it forward. Start a new
                        search to analyse the same invention again.
                      </p>
                    </div>
                }
              }
            }
            @case ('history') {
              <app-history-screen
                [entries]="history()"
                [loading]="historyLoading()"
                [error]="historyError()"
                (open)="openHistoryEntry($event)"
                (refresh)="loadHistory()"
              />
            }
            @case ('error') {
              <app-error-screen
                [detail]="errorDetail()"
                [failedPhase]="failedPhase()"
                [phaseOrder]="errorPhaseOrder()"
                (retry)="retryFailedRun()"
                (back)="goHome()"
              />
            }
          }
        }

        @if (confirmingRunExit()) {
          <div class="run-exit-backdrop" (click)="keepReviewing()">
            <section
              class="run-exit-dialog"
              role="alertdialog"
              aria-modal="true"
              aria-labelledby="run-exit-title"
              (click)="$event.stopPropagation()"
            >
              <h2 id="run-exit-title">Cancel this Advanced Search?</h2>
              <p>
                Leaving now stops the current run. The approved drafts and any
                unfinished search work will not be kept as a result.
              </p>
              @if (runExitError()) {
                <p class="run-exit-error" role="alert">{{ runExitError() }}</p>
              }
              <div class="run-exit-actions">
                <button
                  class="primary-action compact"
                  type="button"
                  [disabled]="cancellingRun()"
                  (click)="cancelCurrentRun()"
                >
                  {{ cancellingRun() ? 'Cancelling…' : 'Cancel search' }}
                </button>
                <button
                  class="secondary-button"
                  type="button"
                  [disabled]="cancellingRun()"
                  (click)="keepReviewing()"
                >
                  Keep reviewing
                </button>
              </div>
            </section>
          </div>
        }
      </main>
    </div>
  `,
})
export class App {
  /** The colour cut. Used on the rail in dark mode, where there is no tile. */
  readonly MARK_COLOUR = '/novelty-lens-mark.svg';
  /** The drawn reversed cut, for the gradient tile light mode still has. */
  readonly MARK_WHITE = '/novelty-lens-mark-white.svg';

  private readonly analysis = inject(AnalysisService);
  private readonly historyService = inject(HistoryService);
  readonly auth = inject(AuthService);

  /** Account-namespaced memory of which verified run this browser is on. */

  private readonly activeRuns = inject(ActiveRunStore);
  private readonly runs = inject(RunsService);
  private readonly streamCursor = inject(StreamCursor);
  private advancedStreamAbort: AbortController | null = null;


  readonly screen = signal<Screen>('submit');

  /** The verified run currently under review, and which gate it is parked at.
   *
   * Both are null on a search-only run, which never pauses. They are set
   * together by `openReview` and cleared together, because a gate without a run
   * names nothing and a run without a gate cannot be reviewed. */
  readonly reviewRunId = signal<string | null>(null);
  /**
   * `StoredGate`, not `WorkflowGate`: a legacy run can be parked at `B`, `C` or
   * `D`, and the review `@switch` has a `@default` arm that says so plainly
   * rather than rendering nothing. Section 6.3.
   */
  readonly reviewGate = signal<StoredGate | null>(null);
  readonly confirmingRunExit = signal(false);
  readonly cancellingRun = signal(false);
  readonly runExitError = signal('');

  /** The branch currently shown. It controls both progress order and retry. */
  readonly runMode = signal<SearchMode>('quick');
  /** False while Advanced Search is preparing either review; true after the
   * reviewer approves the requirements and automatic query planning begins. */
  readonly advancedSearchReleased = signal(false);
  readonly phaseOrder = computed(() =>
    this.runMode() === 'advanced'
      ? this.advancedSearchReleased()
        ? ADVANCED_EXECUTION_PHASE_ORDER
        : ADVANCED_PRE_REVIEW_PHASE_ORDER
      : QUICK_SEARCH_PHASE_ORDER,
  );
  readonly errorPhaseOrder = computed(() =>
    this.runMode() === 'advanced'
      ? ADVANCED_SEARCH_PHASE_ORDER
      : QUICK_SEARCH_PHASE_ORDER,
  );

  /** Admit a verified run, then wait at its first gate.
   *
   * **This posts and stops.** It does not stream, does not drive the graph and
   * does not approve anything: `POST /api/runs` records the run `QUEUED`, and
   * the worker picks it up on its next poll. The browser's only job afterwards
   * is to remember which run this account is looking at and to show the gate
   * when the run reaches one.
   *
   * Until this existed the four gate screens could only be reached by writing a
   * run id into localStorage by hand -- the workflow was built, tested and
   * unreachable from the product.
   */
  async startVerifiedRun(): Promise<void> {
    const description = this.description().trim();
    if (!description) return;
    const email = this.auth.email();
    this.runMode.set('advanced');
    this.advancedSearchReleased.set(false);
    this.errorDetail.set('');
    this.failedPhase.set(null);
    try {
      const created = await this.runs.createRun(description);
      const runId = String(created['run_id']);
      if (email) this.activeRuns.remember(email, runId);
      this.reviewRunId.set(runId);
      // **The running screen, not the gate.** The run is `QUEUED`: no worker has
      // claimed it and no decomposition exists yet.
      //
      // Opening Gate A here showed "Nothing here yet. This is a real result, not
      // a failed load." -- the screen fetched its payload before the node that
      // fills it had run, and `reload()` fetches once. So the reviewer was told
      // the machine had proposed nothing, about a run that had not started, and
      // nothing ever corrected it. That is exactly the substitution the state
      // vocabulary exists to prevent, committed by the one path that skipped it.
      this.phaseIdx.set(0);
      this.phaseMessages.set({});
      this.screen.set('running');
      void this.loadHistory();
      void this.followAdvancedRun(runId);
    } catch (error) {
      this.errorDetail.set(
        error instanceof RunApiError
          ? publicRunError(error.failure.code)
          : PUBLIC_ERRORS.analysis,
      );
      this.screen.set('error');
    }
  }

  /** Follow one durable run through progress, both pauses, and its terminal
   * outcome. The event log is the authority; `GET /runs/{id}` is used only
   * after a connection closes without a terminal frame so reconnecting never
   * turns a dropped HTTP connection into a fabricated run failure. */
  private async followAdvancedRun(runId: string): Promise<void> {
    this.advancedStreamAbort?.abort();
    const controller = new AbortController();
    this.advancedStreamAbort = controller;

    while (this.reviewRunId() === runId && !controller.signal.aborted) {
      let stoppedByEvent = false;
      try {
        await this.runs.stream(
          runId,
          this.streamCursor.get(runId),
          (event) => {
            if (controller.signal.aborted || this.reviewRunId() !== runId) return;

            if (event.type === 'progress') {
              if (ADVANCED_EXECUTION_PHASE_ORDER.includes(event.phase)) {
                this.advancedSearchReleased.set(true);
              }
              const visibleOrder = this.phaseOrder();
              const idx = visibleOrder.indexOf(event.phase);
              if (idx >= 0) {
                this.phaseIdx.update((previous) => Math.max(previous, idx));
                this.phaseMessages.update((previous) => ({
                  ...previous,
                  [event.phase]: event.message,
                }));
              }
              return;
            }

            if (event.type === 'paused') {
              stoppedByEvent = true;
              this.openReview(runId, event.gate);
              return;
            }

            if (event.type === 'snapshot' && event.status === 'WAITING_FOR_REVIEW' && event.current_gate) {
              stoppedByEvent = true;
              this.openReview(runId, event.current_gate);
              return;
            }

            if (event.type === 'result') {
              stoppedByEvent = true;
              this.reportData.set(event.data);
              this.screen.set('report');
              this.finishAdvancedRun(runId);
              void this.loadHistory();
              return;
            }

            if (event.type === 'error') {
              stoppedByEvent = true;
              this.errorDetail.set(PUBLIC_ERRORS.analysis);
              const phase = event.phase as Phase | undefined;
              this.failedPhase.set(
                phase && ADVANCED_SEARCH_PHASE_ORDER.includes(phase)
                  ? phase
                  : this.currentAdvancedPhase(),
              );
              this.screen.set('error');
              this.finishAdvancedRun(runId);
            }
          },
          (id) => this.streamCursor.advance(runId, id),
          controller.signal,
        );
      } catch (error) {
        if (controller.signal.aborted || stoppedByEvent || this.reviewRunId() !== runId) return;
        if (error instanceof RunApiError && error.status === 404) {
          this.errorDetail.set('This search is no longer available.');
          this.failedPhase.set(this.currentAdvancedPhase());
          this.screen.set('error');
          this.finishAdvancedRun(runId);
          return;
        }
        // A dropped stream says nothing about the run. Reconnect from the last
        // durable event id instead of presenting it as a backend failure.
        await new Promise((resolve) => setTimeout(resolve, 750));
        continue;
      }

      if (stoppedByEvent || this.reviewRunId() !== runId || controller.signal.aborted) return;

      try {
        const run = await this.runs.getRun(runId);
        if (run.status === 'WAITING_FOR_REVIEW' && run.current_gate) {
          this.openReview(runId, run.current_gate);
          return;
        }
        if (run.status === 'COMPLETED') {
          this.reportData.set(await this.runs.report(runId));
          this.screen.set('report');
          this.finishAdvancedRun(runId);
          void this.loadHistory();
          return;
        }
        if (run.status === 'FAILED') {
          this.errorDetail.set(PUBLIC_ERRORS.analysis);
          this.failedPhase.set(this.currentAdvancedPhase());
          this.screen.set('error');
          this.finishAdvancedRun(runId);
          return;
        }
      } catch (error) {
        if (error instanceof RunApiError && error.status === 404) {
          this.errorDetail.set('This search is no longer available.');
          this.failedPhase.set(this.currentAdvancedPhase());
          this.screen.set('error');
          this.finishAdvancedRun(runId);
          return;
        }
        // The run row is a recovery check, not the authority for declaring a
        // failure. The durable stream reconnect below remains the source.
      }
      await new Promise((resolve) => setTimeout(resolve, 750));
    }
  }

  /** Infer the active Advanced Search phase from the checklist currently shown.
   *
   * Durable terminal errors intentionally carry only sanitized failure facts and
   * older stored events do not name a phase. The UI still knows which workflow
   * segment it released and the latest progress index, so preserve that honest
   * context instead of claiming that the run failed before any phase reported.
   */
  private currentAdvancedPhase(): Phase | null {
    const order = this.phaseOrder();
    const index = this.phaseIdx();
    return index >= 0 && index < order.length ? order[index] : null;
  }

  /** Show the review screen for one gate of one run. */
  openReview(runId: string, gate: StoredGate): void {
    this.runMode.set('advanced');
    this.reviewRunId.set(runId);
    this.reviewGate.set(gate);
    this.screen.set('review');
  }

  activeReviewGate(): string {
    return this.reviewGate() ?? '';
  }

  /** Back at stage one is a cancellation request, not silent abandonment. */
  requestReviewExit(): void {
    this.runExitError.set('');
    this.confirmingRunExit.set(true);
  }

  keepReviewing(): void {
    if (!this.cancellingRun()) this.confirmingRunExit.set(false);
  }

  async cancelCurrentRun(): Promise<void> {
    const runId = this.reviewRunId();
    if (!runId || this.cancellingRun()) return;
    this.cancellingRun.set(true);
    this.runExitError.set('');
    try {
      await this.runs.cancelRun(runId);
      this.finishAdvancedRun(runId);
      this.advancedSearchReleased.set(false);
      this.phaseIdx.set(-1);
      this.phaseMessages.set({});
      this.screen.set('submit');
    } catch (error) {
      this.runExitError.set(PUBLIC_ERRORS.cancel);
    } finally {
      this.cancellingRun.set(false);
    }
  }

  private finishAdvancedRun(runId: string): void {
    this.advancedStreamAbort?.abort();
    this.advancedStreamAbort = null;
    this.streamCursor.clear(runId);
    const email = this.auth.email();
    if (email) this.activeRuns.forget(email);
    this.reviewRunId.set(null);
    this.reviewGate.set(null);
    this.confirmingRunExit.set(false);
  }

  /**
   * After a decision commits, **resume watching the run**.
   *
   * The run is `RESUMING`, not running: the executor picks it up on its next
   * stream. Gate A keeps the same editor mounted while the worker proposes the
   * next artifact (requirements after the summary). Current runs release after
   * those requirements; legacy STRATEGY runs release after query approval.
   */
  afterDecision(gate: WorkflowGate): void {
    const runId = this.reviewRunId();
    this.runMode.set('advanced');
    if (gate === 'STRATEGY') {
      this.advancedSearchReleased.set(true);
      this.reviewGate.set(null);
      this.phaseIdx.set(0);
      this.phaseMessages.set({});
      this.screen.set('running');
    }
    if (runId) void this.followAdvancedRun(runId);
  }

  /** Version-2 runs leave review after Stage 2; query generation is automatic. */
  afterRequirementsApproved(): void {
    const runId = this.reviewRunId();
    this.runMode.set('advanced');
    this.advancedSearchReleased.set(true);
    this.reviewGate.set(null);
    this.phaseIdx.set(0);
    this.phaseMessages.set({});
    this.screen.set('running');
    if (runId) void this.followAdvancedRun(runId);
  }

  /** Verify the tab's remembered id before reattaching to a durable run. */
  private async restoreActiveRun(): Promise<void> {
    const email = this.auth.email();
    if (!email) return;
    const runId = this.activeRuns.peek(email);
    if (!runId) return;
    try {
      const run = await this.runs.getRun(runId);
      this.description.set(run.description);
      this.runMode.set('advanced');
      this.reviewRunId.set(runId);
      if (run.status === 'WAITING_FOR_REVIEW' && run.current_gate) {
        this.openReview(runId, run.current_gate);
        return;
      }
      if (run.status === 'COMPLETED') {
        this.reportData.set(await this.runs.report(runId));
        this.screen.set('report');
        this.finishAdvancedRun(runId);
        return;
      }
      if (run.status === 'FAILED') {
        this.errorDetail.set(run.failure_detail ?? PUBLIC_ERRORS.analysis);
        this.screen.set('error');
        this.finishAdvancedRun(runId);
        return;
      }
      this.advancedSearchReleased.set(false);
      this.phaseIdx.set(0);
      this.phaseMessages.set({});
      this.streamCursor.advance(runId, Math.max(0, (run.last_event_id ?? 1) - 1));
      this.screen.set('running');
      void this.followAdvancedRun(runId);
    } catch (error) {
      if (error instanceof RunApiError && error.status === 404) {
        this.activeRuns.forget(email);
        return;
      }
      this.errorDetail.set(PUBLIC_ERRORS.analysis);
      this.screen.set('error');
    }
  }
  readonly description = signal(EXAMPLE_DESC);
  /** Optional Stage 1 context for a verified run. Empty is a real answer -- the
   * inventor left it blank -- and is sent as an empty string, not omitted. */
  readonly phaseIdx = signal(-1);
  readonly phaseMessages = signal<Partial<Record<Phase, string>>>({});
  readonly reportData = signal<AnalysisReport | null>(null);
  readonly errorDetail = signal('');
  readonly failedPhase = signal<Phase | null>(null);
  readonly theme = signal<'light' | 'dark'>(
    (localStorage.getItem('novelty-lens-theme') as 'light' | 'dark') ?? 'light',
  );

  readonly history = signal<HistoryEntry[]>([]);
  readonly historyLoading = signal(false);
  readonly historyError = signal('');

  readonly sidebarOpen = signal(localStorage.getItem('novelty-lens-sidebar') !== 'closed');

  readonly confirmingClear = signal(false);
  readonly clearing = signal(false);

  /** Which history entry the report screen is currently showing, so the matching
   * Recents row can be marked. Null for a freshly-run report, which is not in
   * the list the sidebar is rendering. */
  readonly openEntryId = signal<string | null>(null);

  /** The Recents list. Capped because the sidebar is navigation, not the
   * History screen -- that one shows the full set the backend returns. */
  readonly recents = computed(() => this.history().slice(0, RECENTS_SHOWN));

  /** Where the report screen's back button returns to. A report opened from
   * History goes back to History; a freshly-run one goes back to the form. */
  private cameFromHistory = false;

  constructor() {
    // The theme lives on <html> rather than a wrapper so the dark background
    // covers the full viewport, including any overscroll area.
    effect(() => {
      const value = this.theme();
      document.documentElement.dataset['theme'] = value;
      localStorage.setItem('novelty-lens-theme', value);
    });

    effect(() => {
      localStorage.setItem('novelty-lens-sidebar', this.sidebarOpen() ? 'open' : 'closed');
    });

    // A token in localStorage is only a guess that the session is still good --
    // it may have expired while the tab was closed. Checking it here means an
    // expired one shows the login screen immediately, rather than surviving
    // until the first real request fails 60 seconds into a run.
    void this.auth
      .restore()
      .then((valid) => {
        // Recents lives in the sidebar now, so history is fetched on sign-in
        // rather than only when the History screen opens.
        if (valid) void this.loadHistory();
        if (valid) void this.restoreActiveRun();
      })
      .catch((failure) => {
        // A backend that is merely unreachable is NOT an ended session; keep the
        // token and let the first real request report the real problem.
        console.warn('Could not verify the stored session:', failure);
      });
  }

  toggleTheme() {
    this.theme.update((previous) => (previous === 'dark' ? 'light' : 'dark'));
  }

  toggleSidebar() {
    this.sidebarOpen.update((open) => !open);
  }

  // The rail's buttons deliberately differ. The logo is plain navigation and
  // preserves whatever you had typed; "New Search" is the destructive one that
  // empties the form and drops the previous run.
  goHome() {
    this.screen.set('submit');
  }

  startNewSearch() {
    this.description.set('');
    this.reportData.set(null);
    this.errorDetail.set('');
    this.failedPhase.set(null);
    this.phaseIdx.set(-1);
    this.phaseMessages.set({});
    this.runMode.set('quick');
    this.cameFromHistory = false;
    this.openEntryId.set(null);
    this.screen.set('submit');
  }

  goBackFromReport() {
    this.screen.set(this.cameFromHistory ? 'history' : 'submit');
  }

  signOut() {
    this.auth.signOut();
    // Everything on screen belonged to the session that just ended. Reports and
    // history are the previous user's; leaving them rendered behind a login
    // screen would be a stale-data leak the moment anyone else signs in.
    this.reportData.set(null);
    this.history.set([]);
    this.historyError.set('');
    this.cameFromHistory = false;
    this.openEntryId.set(null);
    this.confirmingClear.set(false);
    this.screen.set('submit');
  }

  openHistory() {
    this.screen.set('history');
    void this.loadHistory();
  }

  openHistoryEntry(entry: HistoryEntry) {
    this.cameFromHistory = true;
    this.openEntryId.set(entry.id);
    this.reportData.set(reportFromHistory(entry));
    this.screen.set('report');
  }

  askClearHistory() {
    this.confirmingClear.set(true);
  }

  /**
   * Delete every stored run. Only reachable behind the two-step confirm above.
   *
   * The list is refetched rather than emptied locally: the server is the record
   * of what exists, and assuming the delete removed everything would show an
   * empty sidebar even if it had only partly succeeded.
   */
  async clearHistory() {
    this.clearing.set(true);
    try {
      await this.historyService.clear();
      this.confirmingClear.set(false);
      // A report opened from history is now showing something that no longer
      // exists, so it goes back to the form rather than lingering as a record
      // of a run the user just deleted.
      if (this.cameFromHistory || this.screen() === 'history') {
        this.reportData.set(null);
        this.openEntryId.set(null);
        this.cameFromHistory = false;
        this.screen.set('submit');
      }
      await this.loadHistory();
    } catch (failure) {
      this.confirmingClear.set(false);
      this.historyError.set(PUBLIC_ERRORS.clearHistory);
    } finally {
      this.clearing.set(false);
    }
  }

  async loadHistory() {
    this.historyLoading.set(true);
    this.historyError.set('');
    try {
      this.history.set(await this.historyService.list());
    } catch (failure) {
      // Shown as its own state, never as an empty list -- a database that
      // cannot be read is not the same answer as "you have not searched yet."
      this.historyError.set(PUBLIC_ERRORS.history);
    } finally {
      this.historyLoading.set(false);
    }
  }

  async startRunning() {
    if (!this.description().trim()) return;

    this.runMode.set('quick');
    this.screen.set('running');
    this.phaseIdx.set(-1);
    this.phaseMessages.set({});
    this.reportData.set(null);
    this.errorDetail.set('');
    this.failedPhase.set(null);
    this.cameFromHistory = false;
    this.openEntryId.set(null);

    // Tracked in a local declared outside the try so the catch can read it
    // synchronously, without waiting on a signal update.
    let lastPhase: Phase | null = null;

    try {
      let finalReport: AnalysisReport | null = null;

      await this.analysis.stream(
        { description: this.description(), claims: '' },
        (event: StreamEvent) => {
          if (event.type === 'progress') {
            const idx = QUICK_SEARCH_PHASE_ORDER.indexOf(event.phase);
            if (idx >= 0) {
              lastPhase = QUICK_SEARCH_PHASE_ORDER[idx];
              this.phaseIdx.update((previous) => Math.max(previous, idx));
              this.phaseMessages.update((previous) => ({
                ...previous,
                [event.phase]: event.message,
              }));
            }
          } else if (event.type === 'error') {
            throw new Error(PUBLIC_ERRORS.analysis);
          } else if (event.type === 'result') {
            const backend = event.data;
            finalReport = {
              title:
                backend.title ||
                (this.description().length > 50
                  ? `${this.description().slice(0, 47)}...`
                  : this.description()),
              date:
                backend.date ||
                new Date().toLocaleDateString('en-US', {
                  month: 'short',
                  day: 'numeric',
                  year: 'numeric',
                }),
              concepts: backend.concepts ?? [],
              patents: (backend.patents ?? []).map((patent) => ({
                num: patent.num || 'Unknown publication',
                title: patent.title || 'Untitled patent record',
                risk: patent.risk,
                rationale:
                  patent.rationale || 'No detailed rationale was returned for this record.',
                key_overlap: patent.key_overlap ?? [],
                source: patent.source || 'Patent database',
                url: patent.url || '',
              })),
              recommendations: backend.recommendations ?? [],
              query_count: backend.query_count ?? 0,
            };
          }
        },
      );

      if (!finalReport) throw new Error('Stream ended without a result.');
      this.reportData.set(finalReport);
      this.screen.set('report');
      // The backend stored this run before it sent the result, so the cached
      // list is stale. It is refetched rather than dropped now that Recents
      // renders it in the sidebar: emptying it would blank that list on the
      // exact action that just added an entry to it.
      void this.loadHistory();
    } catch (error) {
      console.error('Backend connection error:', error);
      // A 401 mid-run already cleared the session in authorizedFetch, so the
      // login screen takes over and this error state is never seen.
      this.errorDetail.set(PUBLIC_ERRORS.analysis);
      this.failedPhase.set(lastPhase);
      this.screen.set('error');
    }
  }

  /** Retry the branch that failed; an Advanced Search must never become Quick. */
  retryFailedRun(): void {
    if (this.runMode() === 'advanced') {
      void this.startVerifiedRun();
      return;
    }
    void this.startRunning();
  }
}
