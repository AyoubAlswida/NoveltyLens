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
import { ActiveRunStore, RunsService, RunSummary } from './services/runs';
import { UnderstandingReview } from './screens/understanding-review';
import { StrategyReview } from './screens/strategy-review';
import { CandidateReview } from './screens/candidate-review';
import { EvidenceReview } from './screens/evidence-review';
import { DraftReview } from './screens/draft-review';
import { VerificationReport } from './screens/verification-report';
import type { WorkflowGate } from './models/report';
import { AnalysisService } from './services/analysis';
import { AuthService } from './services/auth';
import { HistoryService, reportFromHistory } from './services/history';
import { AnalysisReport, HistoryEntry, Phase, PHASE_ORDER, StreamEvent } from './models/report';

type Screen =
  | 'submit'
  | 'running'
  | 'report'
  | 'error'
  | 'history'
  | 'review'
  | 'verification-report';


const RECENTS_SHOWN = 8;
/** How many past searches the sidebar lists. The History screen shows the full
 * set; this is a shortcut, and a sidebar that scrolls past the sign-out button
 * stops being one. */

const EXAMPLE_DESC =
  'A portable, refillable drinking-water bottle that automatically sterilizes its contents with an integrated UV-C emitter and filters particulates through a replaceable graphene-oxide membrane. A flow-rate sensor in the cap detects use and triggers a self-cleaning sterilization cycle without manual activation.';

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
    LoginScreen, UnderstandingReview, StrategyReview, CandidateReview, EvidenceReview, DraftReview, VerificationReport],
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
                [problemSolved]="problemSolved()"
                (problemSolvedChange)="problemSolved.set($event)"
                [noveltyStatement]="noveltyStatement()"
                (noveltyStatementChange)="noveltyStatement.set($event)"
                (run)="startRunning()"
                (verify)="startVerifiedRun()"
              />
            }
            @case ('running') {
              <app-running-screen [phaseIdx]="phaseIdx()" [phaseMessages]="phaseMessages()" />
            }
            @case ('report') {
              @if (reportData(); as report) {
                <app-report-screen [data]="report" (back)="goBackFromReport()" />
              }
            }
            @case ('review') {
              @if (reviewRunId(); as runId) {
                <!-- One screen per workflow gate. They ask different questions
                     and enforce different completion rules, so a single generic
                     editor could not implement any of them faithfully. -->
                @switch (reviewGate()) {
                  @case ('A') {
                    <app-understanding-review
                      [runId]="runId"
                      (back)="closeReview()"
                      (resumed)="afterDecision()"
                    />
                  }
                  @case ('STRATEGY') {
                    <app-strategy-review
                      [runId]="runId"
                      (back)="closeReview()"
                      (resumed)="afterDecision()"
                    />
                  }
                  @case ('B') {
                    <app-candidate-review
                      [runId]="runId"
                      (back)="closeReview()"
                      (resumed)="afterDecision()"
                    />
                  }
                  @case ('C') {
                    <app-evidence-review
                      [runId]="runId"
                      (back)="closeReview()"
                      (resumed)="afterDecision()"
                    />
                  }
                  @case ('D') {
                    <app-draft-review
                      [runId]="runId"
                      (back)="closeReview()"
                      (resumed)="afterDecision()"
                    />
                  }
                }
              }
            }
            @case ('verification-report') {
              @if (finalReport(); as report) {
                <div class="screen screen-scroll">
                  <header class="gate-head">
                    <h1>Final report</h1>
                    <button class="btn-outlined" type="button" (click)="closeReview()">
                      Back
                    </button>
                  </header>
                  <app-verification-report [report]="report" status="APPROVED" />
                </div>
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
                (retry)="startRunning()"
                (back)="goHome()"
              />
            }
          }
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


  readonly screen = signal<Screen>('submit');

  /** The verified run currently under review, and which gate it is parked at.
   *
   * Both are null on a search-only run, which never pauses. They are set
   * together by `openReview` and cleared together, because a gate without a run
   * names nothing and a run without a gate cannot be reviewed. */
  readonly reviewRunId = signal<string | null>(null);
  readonly reviewGate = signal<WorkflowGate | null>(null);

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
    this.errorDetail.set('');
    try {
      const created = await this.runs.createRun(description, {
        problemSolved: this.problemSolved().trim(),
        noveltyStatement: this.noveltyStatement().trim(),
      });
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
      void this.followToGate(runId);
    } catch (error) {
      this.errorDetail.set(
        error instanceof Error ? error.message : 'The run could not be started.',
      );
      this.screen.set('error');
    }
  }

  /** Watch an admitted run until it parks at a gate, then open that gate.
   *
   * Polling rather than streaming: this asks the one question the screen needs
   * -- "is there a decision to make yet?" -- and the answer lives in the run
   * row, which any API process can answer. The SSE stream carries progress and
   * is the better source for a phase checklist; wiring it here is worth doing
   * and is not what fixes the empty gate.
   *
   * A terminal run is handled as itself: `COMPLETED` opens the report,
   * `FAILED` opens the error screen with the sanitized detail. Neither is a
   * gate, and neither should leave the reviewer on a spinner.
   */
  private async followToGate(runId: string): Promise<void> {
    for (let attempt = 0; attempt < 600; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 1000));
      // The reviewer navigated away -- to History, to a new search. Their
      // choice wins, and the run is unaffected either way.
      if (this.reviewRunId() !== runId) return;
      let run: RunSummary;
      try {
        run = await this.runs.getRun(runId);
      } catch {
        // A dropped request is not a dropped run. Keep waiting.
        continue;
      }
      if (run.status === 'WAITING_FOR_REVIEW' && run.current_gate) {
        this.openReview(runId, run.current_gate);
        return;
      }
      if (run.status === 'COMPLETED') {
        this.finalReport.set(await this.runs.report(runId));
        this.screen.set('verification-report');
        return;
      }
      if (run.status === 'FAILED') {
        this.errorDetail.set(run.failure_detail || 'The run failed.');
        this.screen.set('error');
        return;
      }
    }
  }

  /** Show the review screen for one gate of one run. */
  openReview(runId: string, gate: WorkflowGate): void {
    this.reviewRunId.set(runId);
    this.reviewGate.set(gate);
    this.screen.set('review');
  }

  /** Leave the review screen without deciding. The run is untouched: closing a
   * tab, navigating away and refusing to decide are all the same thing to a
   * worker that has never heard of this browser. */
  closeReview(): void {
    this.reviewRunId.set(null);
    this.reviewGate.set(null);
    this.screen.set('submit');
  }

  /** The approved final report, when one has been fetched. */
  readonly finalReport = signal<Record<string, unknown> | null>(null);

  /**
   * After a decision commits, go back to watching the run rather than to submit.
   *
   * The run is `RESUMING`, not running: the executor picks it up on its next
   * poll. The running screen says so, and reconnects to the stream from the
   * cursor it already holds, so the next `review_required` arrives without the
   * user doing anything.
   */
  afterDecision(): void {
    this.reviewGate.set(null);
    this.screen.set('running');
  }

  /**
   * Remember which run this account is on, so a reload lands back on it.
   *
   * **Namespaced by email** (spec Gate F item 1): two accounts on one browser
   * must not inherit each other's run. Cleared on sign-out, on any 401, and
   * whenever the signed-in email changes (item 2).
   */
  rememberActiveRun(runId: string): void {
    const email = this.auth.email();
    if (email) this.activeRuns.remember(email, runId);
  }

  /**
   * Restore the remembered run **after asking the backend whether it exists**.
   *
   * Never renders a run from localStorage alone: a 404 clears the key silently
   * and returns to submit, because the backend answers 404 identically for "no
   * such run" and "not yours" and this must not try to tell them apart.
   */
  async restoreActiveRun(): Promise<void> {
    const email = this.auth.email();
    if (!email) return;
    try {
      const run = await this.activeRuns.restore(email);
      if (!run) return;
      this.reviewRunId.set(run.run_id);
      if (run.status === 'WAITING_FOR_REVIEW' && run.current_gate) {
        this.reviewGate.set(run.current_gate);
        this.screen.set('review');
      } else if (run.status === 'COMPLETED') {
        // **A finished run reopens its report, not a progress bar.** Sending
        // every non-parked status to 'running' meant that after a restart -- or
        // any reload once the run was over -- a reviewer was shown "Analyzing
        // your invention..." with a spinner, for work that had finished and been
        // approved. That is the same substitution the backend forbids, moved
        // into the UI: a completed run reported as one still in progress.
        const report = await this.runs.report(run.run_id);
        this.finalReport.set(report);
        this.screen.set('verification-report');
      } else if (run.status === 'FAILED') {
        // Its own branch for the same reason: a failed run is not a running one,
        // and the error screen names the failure instead of spinning forever.
        this.errorDetail.set(run.failure_detail || 'The run failed.');
        this.screen.set('error');
      } else {
        this.screen.set('running');
      }
    } catch {
      // A transport failure is not proof the run is gone. Stay where we are; the
      // running screen shows a reconnecting state rather than dropping the run.
    }
  }
  readonly description = signal(EXAMPLE_DESC);
  /** Optional Stage 1 context for a verified run. Empty is a real answer -- the
   * inventor left it blank -- and is sent as an empty string, not omitted. */
  readonly problemSolved = signal('');
  readonly noveltyStatement = signal('');
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
        // And a verified run that is parked at a gate is reopened at that gate.
        //
        // Without this call the app signed in, saw a remembered run id in
        // localStorage, and rendered the submit screen anyway -- so a reviewer
        // who closed the tab during a review had no way back to the gate, and
        // the four gate screens were unreachable from the running product even
        // though every one of them was built and tested.
        //
        // `restoreActiveRun` re-fetches the run before rendering anything and
        // clears the key on a 404, so a stale or foreign id can never put a gate
        // on screen.
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
      this.historyError.set(
        failure instanceof Error && failure.message
          ? failure.message
          : 'Clearing the history failed without a readable error.',
      );
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
      this.historyError.set(
        failure instanceof Error && failure.message
          ? failure.message
          : 'The history request failed without a readable error.',
      );
    } finally {
      this.historyLoading.set(false);
    }
  }

  async startRunning() {
    if (!this.description().trim()) return;

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
            const idx = (PHASE_ORDER as readonly string[]).indexOf(event.phase);
            if (idx >= 0) {
              lastPhase = PHASE_ORDER[idx];
              this.phaseIdx.update((previous) => Math.max(previous, idx));
              this.phaseMessages.update((previous) => ({
                ...previous,
                [event.phase]: event.message,
              }));
            }
          } else if (event.type === 'error') {
            throw new Error(event.detail || 'Backend query failed');
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
                pct: Math.round(patent.pct),
                risk: patent.risk,
                rationale:
                  patent.rationale || 'No detailed rationale was returned for this record.',
                key_overlap: patent.key_overlap ?? [],
                source: patent.source || 'Patent database',
                url: patent.url || '',
              })),
              top_score: Math.round(backend.top_score ?? 0),
              verdict: backend.verdict ?? 'Strong',
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
      // The backend's own `error` detail is shown verbatim -- the zero-tolerance
      // policy means a failed run reports what actually failed, never a guess.
      this.errorDetail.set(
        error instanceof Error && error.message
          ? error.message
          : 'The analysis stream ended without a readable error.',
      );
      this.failedPhase.set(lastPhase);
      this.screen.set('error');
    }
  }
}
