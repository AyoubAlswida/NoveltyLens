import { Component, ChangeDetectionStrategy, input, output, computed } from '@angular/core';
import { Icon } from '../shared/icon';
import { BrandHeader } from '../shared/chrome';
import { ExportMenu } from '../shared/export-menu';
import { AnalysisReport, Risk } from '../models/report';

@Component({
  selector: 'app-report-screen',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Icon, BrandHeader, ExportMenu],
  template: `
    <div class="screen screen-scroll report-screen">
      <app-brand-header />

      <section class="report-toolbar glass-card">
        <button
          class="secondary-button icon-button"
          (click)="back.emit()"
          aria-label="Back to new search"
        >
          <app-icon name="ChevronLeft" [size]="20" />
        </button>
        <div class="report-title-block">
          <h1>{{ data().title }}</h1>
          <p>
            Submitted {{ data().date }} &middot; {{ data().query_count }} queries &middot;
            {{ data().patents.length }} patents scanned
          </p>
        </div>
        <div class="toolbar-actions">
          <app-export-menu [data]="data()" />
        </div>
      </section>

      <section class="report-grid">
        <article class="summary-card glass-card">
          <div class="score-ring {{ riskClass(displayRisk()) }}">
            <svg viewBox="0 0 120 120" aria-hidden="true">
              <circle class="score-track" cx="60" cy="60" r="48"></circle>
              <circle
                class="score-value"
                cx="60"
                cy="60"
                r="48"
                pathLength="100"
                [attr.stroke-dasharray]="clamp(data().top_score) + ' 100'"
              ></circle>
            </svg>
            <div>
              <strong>{{ data().top_score }}%</strong><span>{{ displayRisk() }}</span>
            </div>
          </div>

          <div class="summary-copy">
            <p class="eyebrow">Closest prior-art similarity</p>
            <h2>{{ topPatent()?.title ?? 'No matching prior art indexed' }}</h2>
            <p>
              {{
                topPatent()
                  ? 'Top match: ' + topPatent()!.num
                  : 'No patent record exceeded the current matching threshold.'
              }}
            </p>
            <div class="verdict-pill {{ verdictClass(data().verdict) }}">
              Novelty verdict: {{ data().verdict }}
            </div>
          </div>
        </article>

        <article class="concept-card glass-card">
          <p class="eyebrow">Detected concepts</p>
          <div class="concept-row report-concepts">
            @for (concept of data().concepts; track concept) {
              <span class="concept-chip">{{ concept }}</span>
            } @empty {
              <span class="empty-state">No concepts returned.</span>
            }
          </div>
        </article>
      </section>

      <section class="matches-card glass-card">
        <div class="section-heading">
          <div>
            <p class="eyebrow">Prior-art matches</p>
            <h2>Ranked patent results</h2>
          </div>
          <span class="result-count">{{ data().patents.length }}</span>
        </div>

        <div class="matches-list">
          @for (patent of data().patents; track patent.num + patent.title) {
            <article class="patent-row">
              <div class="patent-main">
                <div class="patent-meta">
                  <span>{{ patent.num }}</span>
                  <span>{{ patent.source }}</span>
                </div>
                <h3>{{ patent.title }}</h3>
                <p>{{ patent.rationale }}</p>
              </div>
              <div class="patent-score {{ riskClass(patent.risk) }}">
                <div>
                  <strong>{{ patent.pct }}%</strong><span>{{ patent.risk }}</span>
                </div>
                <div class="score-bar"><i [style.width.%]="clamp(patent.pct)"></i></div>
              </div>
            </article>
          } @empty {
            <div class="empty-state padded">No matching patent records were returned.</div>
          }
        </div>
      </section>

      <section class="recommendations-card glass-card">
        <div class="section-heading">
          <div>
            <p class="eyebrow">AI guidance</p>
            <h2>How to make it more unique</h2>
          </div>
          <app-icon name="Sparkles" [size]="24" />
        </div>
        @if (data().recommendations.length) {
          <ol class="recommendation-list">
            @for (recommendation of data().recommendations; track $index) {
              <li>
                <span>{{ $index + 1 }}</span>
                <p>{{ recommendation }}</p>
              </li>
            }
          </ol>
        } @else {
          <p class="empty-state">
            No recommendations were generated &mdash; no prior art was found to distinguish this
            invention from.
          </p>
        }
      </section>

    </div>
  `,
})
export class ReportScreen {
  readonly data = input.required<AnalysisReport>();
  readonly back = output<void>();

  readonly topPatent = computed(() => this.data().patents[0]);

  readonly displayRisk = computed<Risk>(() => {
    const score = this.data().top_score;
    return score >= 75 ? 'High' : score >= 25 ? 'Medium' : 'Low';
  });

  clamp(value: number) {
    return Math.max(0, Math.min(100, value));
  }

  riskClass(risk: Risk) {
    return `risk-${risk.toLowerCase()}`;
  }

  verdictClass(verdict: string) {
    return `verdict-${verdict.toLowerCase()}`;
  }
}
