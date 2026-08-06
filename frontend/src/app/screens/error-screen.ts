import { Component, ChangeDetectionStrategy, input, output, computed } from '@angular/core';
import { Icon } from '../shared/icon';
import { BrandHeader, PageFooter } from '../shared/chrome';
import { Phase, PHASE_ORDER, PHASE_LABELS } from '../models/report';
import { API_BASE_URL } from '../config';

@Component({
  selector: 'app-error-screen',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Icon, BrandHeader, PageFooter],
  template: `
    <div class="screen screen-scroll">
      <app-brand-header />

      <section class="intro-copy error-intro">
        <div class="failed-label"><span></span> RUN FAILED</div>
        <h1>The analysis could not be completed</h1>
        <p>
          No partial results are shown &mdash; the pipeline reports a genuine answer or none at
          all, so nothing below was estimated or filled in.
        </p>
      </section>

      <section class="error-card glass-card" role="alert">
        <div class="error-heading">
          <div class="error-icon">
            <app-icon name="AlertTriangle" [size]="22" [strokeWidth]="2" />
          </div>
          <div>
            <p class="eyebrow">What the backend reported</p>
            <h2>{{ heading() }}</h2>
          </div>
        </div>

        <pre class="error-detail">{{ detail() }}</pre>

        @if (reachedIndex() >= 0) {
          <div class="error-progress" aria-label="Phases completed before the failure">
            @for (pip of pips(); track pip.phase) {
              <span class="error-pip {{ pip.state }}" [title]="pip.label"></span>
            }
            <span class="error-progress-note">
              {{ reachedIndex() }} of {{ totalPhases }} phases completed
            </span>
          </div>
        }

        <div class="error-actions">
          <button class="primary-action compact" (click)="retry.emit()">
            <app-icon name="RotateCcw" [size]="17" /> Try again
          </button>
          <button class="secondary-button" (click)="back.emit()">
            <app-icon name="ChevronLeft" [size]="17" /> Edit description
          </button>
        </div>

        <p class="error-hint">
          If this keeps happening, check that the backend is running on
          <code>{{ apiBaseUrl }}</code> with Qdrant up, and that the SerpAPI and HuggingFace
          quotas are not exhausted.
        </p>
      </section>

      <app-page-footer />
    </div>
  `,
})
export class ErrorScreen {
  readonly detail = input.required<string>();
  readonly failedPhase = input.required<Phase | null>();
  readonly retry = output<void>();
  readonly back = output<void>();

  readonly apiBaseUrl = API_BASE_URL;
  readonly totalPhases = PHASE_ORDER.length;

  readonly reachedIndex = computed(() => {
    const phase = this.failedPhase();
    return phase ? PHASE_ORDER.indexOf(phase) : -1;
  });

  readonly heading = computed(() => {
    const phase = this.failedPhase();
    return phase
      ? `Failed during “${PHASE_LABELS[phase].replace(/…$/, '')}”`
      : 'Failed before the first phase reported';
  });

  readonly pips = computed(() => {
    const reached = this.reachedIndex();
    return PHASE_ORDER.map((phase, index) => ({
      phase,
      label: PHASE_LABELS[phase],
      state: index < reached ? 'done' : index === reached ? 'failed' : '',
    }));
  });
}
