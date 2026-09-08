import { Component, ChangeDetectionStrategy, input, output, computed } from '@angular/core';
import { Icon } from '../shared/icon';
import { ExportMenu } from '../shared/export-menu';
import { AnalysisReport, PatentMatch, Risk } from '../models/report';

@Component({
  selector: 'app-report-screen',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Icon, ExportMenu],
  template: `
    <div class="screen screen-scroll report-screen">
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
            {{ data().patents.length }} publications
            @if (familyCount()) {
              &middot; {{ familyCount() }} identified patent families
            }
            @if (unknownFamilyPublications()) {
              &middot; {{ unknownFamilyPublications() }} without family metadata
            }
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

            <!--
              The legend is a key to the ratings in the list below it. With no
              matches there are no ratings to explain, and printing the scale
              anyway would put "Novelty risk / High / Medium / Low" on screen
              for a run that rated nothing. Section 5.6.
            -->
            @if (data().patents.length) {
            <div class="risk-legend">
              <p class="risk-legend-title">Novelty risk</p>
              <div class="risk-legend-row">
                <span class="risk-legend-item risk-high"><i></i>High</span>
                <span class="risk-legend-item risk-medium"><i></i>Medium</span>
                <span class="risk-legend-item risk-low"><i></i>Low</span>
                <details class="inline-help risk-help">
                  <summary class="risk-legend-help" aria-label="What novelty risk means">
                    <app-icon name="Info" [size]="14" />
                  </summary>
                  <p class="inline-help-popover">
                    Novelty risk reflects how closely each patent overlaps with your
                    invention. High means stronger overlap; Low means more limited overlap.
                  </p>
                </details>
              </div>
            </div>
            }

            <div class="matches-list">
              @for (family of patentFamilies(); track family.key) {
                <section
                  class="patent-family"
                  [class.patent-family-grouped]="family.identified && (family.members.length > 1 || family.totalSize > 1)"
                >
                @if (family.identified && (family.members.length > 1 || family.totalSize > 1)) {
                  <header class="patent-family-header">
                    <span>Patent family</span>
                    <strong>
                      {{ family.members.length }} matched
                      {{ family.members.length === 1 ? 'publication' : 'publications' }}
                    </strong>
                    @if (family.totalSize > family.members.length) {
                      <small>{{ family.totalSize }} publications reported in this family</small>
                    }
                  </header>
                }
              @for (patent of family.members; track patent.num + patent.title) {
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

                  <div class="patent-details">
                    <section class="patent-detail-section patent-similarity">
                      <span class="card-label">Novelty risk</span>
                      <span class="risk-label {{ riskClass(patent.risk) }}">
                        <i></i>{{ patent.risk }}
                      </span>
                    </section>

                    <section class="patent-detail-section patent-summary">
                      <p class="card-label">Overlap summary</p>
                      <p>{{ patent.rationale }}</p>
                    </section>
                  </div>

                  <span class="patent-action">View patent &rarr;</span>
                </a>
              }
                </section>
              } @empty {
                <div class="empty-state padded">No matching patent records were returned.</div>
              }
            </div>
          </article>
        </div>

        <div class="report-col-right">
          <article class="summary-card glass-card">
            <header class="summary-copy summary-card-header">
              <p class="eyebrow">Closest prior-art match</p>
              <h2>{{ topPatent()?.title ?? 'No matching prior art indexed' }}</h2>
              <p>
                {{
                  topPatent()
                    ? 'Top match: ' + topPatent()!.num
                    : 'No patent record exceeded the current matching threshold.'
                }}
              </p>
            </header>

            <!--
              Everything except Detected concepts is inside this @if on purpose.
              With no match there is nothing to rate, and a rating is a claim:
              printing "Novelty risk: Low" over an empty result set would state
              a finding the run did not make.
            -->
            @if (topPatent(); as top) {
              <section class="summary-card-section">
                <h3 class="summary-section-title">Key overlap</h3>
                @if (top.key_overlap.length) {
                  <div class="overlap-tags">
                    @for (tag of top.key_overlap; track tag) {
                      <span class="overlap-tag">{{ tag }}</span>
                    }
                  </div>
                } @else {
                  <span class="empty-state">No key overlaps returned.</span>
                }
              </section>

              <section class="summary-card-section">
                <h3 class="summary-section-title">Novelty risk</h3>
                <div class="risk-pill {{ riskClass(top.risk) }}">
                  <app-icon name="AlertTriangle" [size]="14" />
                  {{ top.risk }}
                </div>
              </section>

              <section class="summary-card-section">
                <h3 class="summary-section-title">Overall assessment</h3>
                <p class="summary-why">{{ top.rationale }}</p>
              </section>
            }

            <section class="summary-card-section summary-concepts-section">
              <h3 class="summary-section-title">Detected concepts</h3>
              <div class="concept-row report-concepts">
                @for (concept of data().concepts; track concept) {
                  <span class="concept-chip">{{ concept }}</span>
                } @empty {
                  <span class="empty-state">No concepts returned.</span>
                }
              </div>
            </section>
          </article>
        </div>
      </section>

      <section class="recommendations-section">
        <article class="recommendations-card glass-card">
          <div class="section-heading">
            <app-icon name="Sparkles" [size]="24" />
            <div>
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
      </section>
    </div>
  `,
})
export class ReportScreen {
  readonly data = input.required<AnalysisReport>();
  readonly back = output<void>();

  /**
   * The strongest match, or `undefined` when nothing was found.
   *
   * `patents` arrives already ordered strongest-first -- by the backend for a
   * live run, by `search_run_matches.rank` for one reopened from History -- so
   * index 0 is the closest match without this screen re-sorting anything. It
   * could not re-sort even if it wanted to: no similarity number is published.
   */
  readonly topPatent = computed(() => this.data().patents[0]);
  readonly patentFamilies = computed(() => {
    const groups = new Map<
      string,
      { key: string; identified: boolean; members: PatentMatch[]; totalSize: number }
    >();
    for (const patent of this.data().patents) {
      const familyId = patent.family_id?.trim() ?? '';
      const key = familyId || `publication:${patent.num}`;
      const existing = groups.get(key);
      if (existing) {
        existing.members.push(patent);
        existing.totalSize = Math.max(existing.totalSize, patent.family_size ?? 0);
      } else {
        groups.set(key, {
          key,
          identified: Boolean(familyId),
          members: [patent],
          totalSize: patent.family_size ?? 0,
        });
      }
    }
    return [...groups.values()];
  });
  readonly familyCount = computed(
    () => this.patentFamilies().filter((family) => family.identified).length,
  );
  readonly unknownFamilyPublications = computed(
    () =>
      this.patentFamilies()
        .filter((family) => !family.identified)
        .reduce((count, family) => count + family.members.length, 0),
  );

  riskClass(risk: Risk) {
    return `risk-${risk.toLowerCase()}`;
  }
}
