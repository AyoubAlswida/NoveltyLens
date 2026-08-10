import { Component, ChangeDetectionStrategy, input, output, computed } from '@angular/core';
import { Icon } from '../shared/icon';
import { BrandHeader } from '../shared/chrome';
import { ExportMenu } from '../shared/export-menu';
import { AnalysisReport, Risk, Verdict } from '../models/report';

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

      <section class="report-columns">
        <div class="report-col-left">
          <article class="matches-card glass-card">
            <div class="section-heading">
              <div>
                <p class="eyebrow">Prior-art matches</p>
                <h2>
                  <span class="result-count">{{ data().patents.length }}</span>
                  Ranked patent results
                </h2>
              </div>
            </div>

            <div class="risk-legend">
              <p class="risk-legend-title">Match level</p>
              <div class="risk-legend-row">
                <span class="risk-legend-item risk-high"><i></i>Strong</span>
                <span class="risk-legend-item risk-medium"><i></i>Partial</span>
                <span class="risk-legend-item risk-low"><i></i>Limited</span>
                <span
                  class="risk-legend-help"
                  title="How closely a candidate's technology overlaps with your invention, based on embedding similarity. High overlap means it's less likely your idea is novel over that patent."
                  aria-label="What prior-art similarity means"
                  tabindex="0"
                >
                  <app-icon name="Info" [size]="14" />
                </span>
              </div>
            </div>

            <div class="matches-list">
              @for (patent of data().patents; track patent.num + patent.title) {
                <a
                  class="patent-card"
                  [attr.href]="patent.url || null"
                  [attr.target]="patent.url ? '_blank' : null"
                  rel="noopener noreferrer"
                  [attr.aria-label]="'Open ' + patent.title + ' on ' + patent.source"
                >
                  <div class="patent-meta">
                    <span>{{ patent.num }}</span>
                    <span>{{ patent.source }}</span>
                  </div>
                  <h3 class="patent-title">{{ patent.title }}</h3>

                  <div class="patent-similarity">
                    <div class="patent-similarity-header">
                      <span class="card-label">Match level</span>
                      <strong>{{ patent.pct }}%</strong>
                    </div>
                    <div class="score-bar {{ riskClass(patent.risk) }}">
                      <i [style.width.%]="clamp(patent.pct)"></i>
                    </div>
                    <span class="risk-label {{ riskClass(patent.risk) }}">
                      <i></i>{{ riskWord(patent.risk) }}
                    </span>
                  </div>

                  <div class="patent-summary">
                    <p class="card-label">Overlap summary</p>
                    <p>{{ patent.rationale }}</p>
                  </div>

                  <span class="patent-action">View patent &rarr;</span>
                </a>
              } @empty {
                <div class="empty-state padded">No matching patent records were returned.</div>
              }
            </div>
          </article>
        </div>

        <div class="report-col-right">
          <article class="summary-card glass-card">
            <p class="eyebrow">Closest prior-art match</p>
            <div class="summary-top">
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
                  <strong>{{ data().top_score }}%</strong>
                </div>
              </div>
              @if (topPatent()) {
                <span class="risk-label {{ riskClass(displayRisk()) }}">
                  <i></i>{{ riskLabel(displayRisk()) }}
                </span>
              }
            </div>

            <div class="summary-copy">
              <h2>{{ topPatent()?.title ?? 'No matching prior art indexed' }}</h2>
              <p>
                {{
                  topPatent()
                    ? 'Top match: ' + topPatent()!.num
                    : 'No patent record exceeded the current matching threshold.'
                }}
              </p>

              @if (topPatent(); as top) {
                @if (top.key_overlap.length) {
                  <p class="card-label">Key overlap</p>
                  <div class="overlap-tags">
                    @for (tag of top.key_overlap; track tag) {
                      <span class="overlap-tag">{{ tag }}</span>
                    }
                  </div>
                }
              }

              <p class="card-label">Novelty risk</p>
              <div class="verdict-pill {{ verdictClass(data().verdict) }}">
                <app-icon name="AlertTriangle" [size]="14" />
                {{ verdictRisk(data().verdict) }}
              </div>

              @if (topPatent()) {
                <p class="card-label">Overall assessment</p>
                <p class="summary-why">{{ topPatent()!.rationale }}</p>
              }
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

          <article class="recommendations-card glass-card">
            <div class="section-heading">
              <app-icon name="Sparkles" [size]="24" />
              <div>
                <p class="eyebrow">AI Recommendation</p>
                <h2>How to make it more unique</h2>
              </div>
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
                No recommendations were generated &mdash; no prior art was found to distinguish
                this invention from.
              </p>
            }
          </article>
        </div>
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

  private readonly riskLabels: Record<Risk, string> = {
    High: 'Strong match',
    Medium: 'Partial match',
    Low: 'Limited match',
  };

  /** Used in the summary card, standalone with no preceding section label. */
  riskLabel(risk: Risk) {
    return this.riskLabels[risk];
  }

  private readonly riskWords: Record<Risk, string> = {
    High: 'Strong',
    Medium: 'Partial',
    Low: 'Limited',
  };

  /** Used inline on each patent card, directly under the "Match level"
   * label, so the bare word already has context. */
  riskWord(risk: Risk) {
    return this.riskWords[risk];
  }

  verdictClass(verdict: string) {
    return `verdict-${verdict.toLowerCase()}`;
  }

  private readonly verdictRiskWords: Record<Verdict, string> = {
    Strong: 'Low',
    Moderate: 'Medium',
    Weak: 'High',
  };

  /** The backend's verdict is framed as novelty strength (Strong/Moderate/Weak);
   * the UI frames the same fact as risk (Low/Medium/High) for the reader. */
  verdictRisk(verdict: Verdict) {
    return this.verdictRiskWords[verdict];
  }
}
