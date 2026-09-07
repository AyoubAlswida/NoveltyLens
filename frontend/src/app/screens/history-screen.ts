import { Component, ChangeDetectionStrategy, input, output } from '@angular/core';
import { Icon } from '../shared/icon';
import { HistoryEntry, Risk } from '../models/report';

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
  imports: [Icon],
  template: `
    <div class="screen screen-scroll">
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
              <span>{{ message }}</span>
            </p>
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
                  <!--
                    The stored percentage and verdict are gone, so a row shows
                    the novelty risk of its closest match and nothing else. A
                    run that found nothing shows no rating: there is nothing to
                    rate, and a blank is honest where "Low" would be a finding
                    the run never made.
                  -->
                  @if (topRisk(entry); as risk) {
                    <span class="history-risk {{ riskClass(risk) }}">{{ risk }}</span>
                  }
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

  /**
   * The rating of the run's closest match, or `undefined` if it found nothing.
   *
   * Read straight off the stored match rather than recomputed from a score, so
   * a row and the report it opens cannot disagree: both render the one rating
   * the backend published, and neither has a number to re-derive it from.
   * `patents` is ordered by `search_run_matches.rank`, so index 0 is the
   * closest match.
   */
  topRisk(entry: HistoryEntry): Risk | undefined {
    return entry.patents[0]?.risk;
  }

  riskClass(risk: Risk) {
    return `risk-${risk.toLowerCase()}`;
  }
}
