import { Component, ChangeDetectionStrategy, input, output } from '@angular/core';
import { Icon } from '../shared/icon';
import { BrandHeader } from '../shared/chrome';
import { HistoryEntry, Verdict } from '../models/report';

/**
 * Past runs, newest first, straight from `GET /api/history`.
 *
 * Three states, kept distinct on purpose: loading, failed, and genuinely empty.
 * Collapsing a failed fetch into "no searches yet" would report a broken
 * database as an ordinary empty list -- the same substitution the backend's
 * zero-tolerance policy forbids, just moved into the UI.
 */
@Component({
  selector: 'app-history-screen',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Icon, BrandHeader],
  template: `
    <div class="screen screen-scroll">
      <app-brand-header />

      <section class="intro-copy submit-intro">
        <p>Every completed search, stored server-side and newest first.</p>
      </section>

      <section class="history-card glass-card">
        <div class="section-heading">
          <div>
            <p class="eyebrow">Search history</p>
            <h2>Past reports</h2>
          </div>
          <button
            class="secondary-button"
            (click)="refresh.emit()"
            [disabled]="loading()"
            aria-label="Reload search history"
          >
            <app-icon name="RotateCcw" [size]="16" />
            <span>Refresh</span>
          </button>
        </div>

        @if (loading()) {
          <p class="empty-state padded">Loading past searches…</p>
        } @else if (error(); as message) {
          <div class="history-error" role="alert">
            <p>
              <app-icon name="AlertTriangle" [size]="17" [strokeWidth]="2" />
              <span>Could not load your history.</span>
            </p>
            <pre class="error-detail">{{ message }}</pre>
          </div>
        } @else if (entries().length === 0) {
          <p class="empty-state padded">
            No searches yet. Runs are stored automatically once they complete.
          </p>
        } @else {
          <ul class="history-list">
            @for (entry of entries(); track entry.id) {
              <li>
                <button class="history-row" (click)="open.emit(entry)">
                  <span class="history-main">
                    <span class="history-title">{{ entry.description }}</span>
                    <span class="history-meta">
                      {{ formatDate(entry.created_at) }} &middot;
                      {{ entry.query_count }} queries &middot;
                      {{ entry.patents.length }}
                      {{ entry.patents.length === 1 ? 'match' : 'matches' }}
                    </span>
                  </span>
                  <span class="history-verdict {{ verdictClass(entry.verdict) }}">
                    {{ entry.verdict }}
                  </span>
                  <span class="history-score {{ riskClass(entry.top_score) }}">
                    {{ entry.top_score }}%
                  </span>
                </button>
              </li>
            }
          </ul>
        }
      </section>
    </div>
  `,
})
export class HistoryScreen {
  readonly entries = input.required<HistoryEntry[]>();
  readonly loading = input.required<boolean>();
  readonly error = input.required<string>();
  readonly open = output<HistoryEntry>();
  readonly refresh = output<void>();

  formatDate(isoTimestamp: string) {
    return new Date(isoTimestamp).toLocaleString('en-US', {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
    });
  }

  /** The same thresholds `ReportScreen.displayRisk` uses, so a row and the
   * report it opens can never disagree about severity. */
  riskClass(topScore: number) {
    return `risk-${topScore >= 75 ? 'high' : topScore >= 25 ? 'medium' : 'low'}`;
  }

  verdictClass(verdict: Verdict) {
    return `verdict-${verdict.toLowerCase()}`;
  }
}
