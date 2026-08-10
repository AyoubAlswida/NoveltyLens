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
import { AnalysisService } from './services/analysis';
import { AuthService } from './services/auth';
import { HistoryService, reportFromHistory } from './services/history';
import { AnalysisReport, HistoryEntry, Phase, PHASE_ORDER, StreamEvent } from './models/report';

type Screen = 'submit' | 'running' | 'report' | 'error' | 'history';

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
    LoginScreen,
  ],
  template: `
    <div class="app-shell">
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

        @if (auth.signedIn()) {
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
              <p class="rail-section">Recents</p>

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
        }

        <div class="rail-spacer"></div>

        @if (auth.signedIn()) {
          <button
            class="rail-item rail-signout"
            (click)="signOut()"
            [attr.aria-label]="'Sign out of ' + (auth.email() ?? 'Novelty Lens')"
            [title]="auth.email() ?? 'Sign out'"
          >
            <app-icon name="LogOut" [size]="19" [strokeWidth]="1.9" />
            <span>Sign out</span>
          </button>
        }

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

      <main class="workspace">
        @if (!auth.signedIn()) {
          <app-login-screen (signedIn)="loadHistory()" />
        } @else {
          @switch (screen()) {
            @case ('submit') {
              <app-submit-screen
                [description]="description()"
                (descriptionChange)="description.set($event)"
                (run)="startRunning()"
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

  readonly screen = signal<Screen>('submit');
  readonly description = signal(EXAMPLE_DESC);
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
