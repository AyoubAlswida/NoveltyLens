import { Component, ChangeDetectionStrategy, signal, effect, inject } from '@angular/core';
import { Icon } from './shared/icon';
import { SubmitScreen } from './screens/submit-screen';
import { RunningScreen } from './screens/running-screen';
import { ReportScreen } from './screens/report-screen';
import { ErrorScreen } from './screens/error-screen';
import { AnalysisService } from './services/analysis';
import { AnalysisReport, Phase, PHASE_ORDER, StreamEvent } from './models/report';

type Screen = 'submit' | 'running' | 'report' | 'error';

const EXAMPLE_DESC =
  'A portable, refillable drinking-water bottle that automatically sterilizes its contents with an integrated UV-C emitter and filters particulates through a replaceable graphene-oxide membrane. A flow-rate sensor in the cap detects use and triggers a self-cleaning sterilization cycle without manual activation.';

@Component({
  selector: 'app-root',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Icon, SubmitScreen, RunningScreen, ReportScreen, ErrorScreen],
  template: `
    <div class="app-shell">
      <aside class="brand-rail" aria-label="Primary navigation">
        <button class="rail-brand" aria-label="Novelty Lens home" (click)="goHome()">
          <img src="/novelty-lens-mark-white.svg" alt="" />
        </button>

        <nav class="rail-nav">
          <button
            class="rail-item rail-item-active"
            (click)="startNewSearch()"
            aria-label="Start a new search, clearing the current description and report"
          >
            <app-icon name="Plus" [size]="21" [strokeWidth]="2" />
            <span>New Search</span>
          </button>
        </nav>

        <div class="rail-spacer"></div>
        <button
          class="rail-mode"
          (click)="toggleTheme()"
          [attr.aria-label]="'Switch to ' + (theme() === 'dark' ? 'light' : 'dark') + ' appearance'"
        >
          <app-icon [name]="theme() === 'dark' ? 'Moon' : 'Sun'" [size]="20" [strokeWidth]="1.7" />
          <span>{{ theme() === 'dark' ? 'Dark' : 'Light' }}</span>
        </button>
      </aside>

      <main class="workspace">
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
              <app-report-screen [data]="report" (back)="goHome()" />
            }
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
      </main>
    </div>
  `,
})
export class App {
  private readonly analysis = inject(AnalysisService);

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

  constructor() {
    // The theme lives on <html> rather than a wrapper so the dark background
    // covers the full viewport, including any overscroll area.
    effect(() => {
      const value = this.theme();
      document.documentElement.dataset['theme'] = value;
      localStorage.setItem('novelty-lens-theme', value);
    });
  }

  toggleTheme() {
    this.theme.update((previous) => (previous === 'dark' ? 'light' : 'dark'));
  }

  // The rail's two buttons deliberately differ. The logo is plain navigation
  // and preserves whatever you had typed; "New Search" is the destructive one
  // that empties the form and drops the previous run.
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
    this.screen.set('submit');
  }

  async startRunning() {
    if (!this.description().trim()) return;

    this.screen.set('running');
    this.phaseIdx.set(-1);
    this.phaseMessages.set({});
    this.reportData.set(null);
    this.errorDetail.set('');
    this.failedPhase.set(null);

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
                source: patent.source || 'Patent database',
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
    } catch (error) {
      console.error('Backend connection error:', error);
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
