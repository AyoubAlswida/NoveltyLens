import { Component, ChangeDetectionStrategy, input, output, computed } from '@angular/core';
import { Icon } from '../shared/icon';
import { Phase, PHASE_LABELS } from '../models/report';

@Component({
  selector: 'app-error-screen',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Icon],
  template: `
    <div class="screen screen-scroll">
      <section class="intro-copy error-intro">
        <div class="failed-label"><span></span> RUN FAILED</div>
        <h1>The analysis could not be completed</h1>
        <p>
          No partial results are shown.
        </p>
      </section>

      <section class="error-card glass-card" role="alert">
        <div class="error-heading">
          <div class="error-icon">
            <app-icon name="AlertTriangle" [size]="22" [strokeWidth]="2" />
          </div>
          <div>
            <p class="eyebrow">What happened</p>
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
              {{ reachedIndex() }} of {{ totalPhases() }} phases completed
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

        <p class="error-hint"></p>
      </section>

    </div>
  `,
})
export class ErrorScreen {
  readonly detail = input.required<string>();
  readonly failedPhase = input.required<Phase | null>();
  readonly phaseOrder = input.required<readonly Phase[]>();
  readonly retry = output<void>();
  readonly back = output<void>();

  readonly totalPhases = computed(() => this.phaseOrder().length);

  readonly reachedIndex = computed(() => {
    const phase = this.failedPhase();
    return phase ? this.phaseOrder().indexOf(phase) : -1;
  });

  readonly heading = computed(() => {
    const phase = this.failedPhase();
    return phase
      ? `Failed during “${PHASE_LABELS[phase].replace(/…$/, '')}”`
      : 'Failed before the first phase reported';
  });

  readonly pips = computed(() => {
    const reached = this.reachedIndex();
    return this.phaseOrder().map((phase, index) => ({
      phase,
      label: PHASE_LABELS[phase],
      state: index < reached ? 'done' : index === reached ? 'failed' : '',
    }));
  });
}
