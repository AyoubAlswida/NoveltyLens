import { Component, ChangeDetectionStrategy, input, computed } from '@angular/core';
import { Icon } from '../shared/icon';
import { Phase, SearchMode } from '../models/report';

const EXECUTION_STAGES: readonly {
  label: string;
  icon: string;
  phases: readonly Phase[];
}[] = [
  { label: 'Planning the search', icon: 'Search', phases: ['understand', 'queries', 'strategy'] },
  { label: 'Searching patent databases', icon: 'Database', phases: ['search'] },
  { label: 'Evaluating prior art', icon: 'Radar', phases: ['filter', 'enrich', 'validate', 'compare'] },
  { label: 'Preparing your analysis', icon: 'PenLine', phases: ['analyze'] },
];

@Component({
  selector: 'app-running-screen',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Icon],
  template: `
    <div class="screen screen-scroll">
      <div class="running-shell">
        <section class="intro-copy running-intro">
          <span class="running-kicker">
            <app-icon name="Sparkles" [size]="15" />
            {{ mode() === 'advanced' ? 'Advanced Search' : 'Novelty search' }}
          </span>
          <h1>Analyzing your invention</h1>
          <p>{{ supportingText() }}</p>
        </section>

        <section class="progress-card glass-card" aria-live="polite" aria-busy="true">
          <div class="progress-list">
            @for (step of steps(); track step.label) {
              <div class="progress-step {{ step.status }}">
                <div class="phase-track">
                  <div class="phase-icon">
                    <app-icon [name]="step.complete ? 'Check' : step.icon" [size]="18" />
                  </div>
                  @if (!step.last) {
                    <span class="phase-line"></span>
                  }
                </div>
                <div class="phase-label">{{ step.label }}</div>
                @if (step.active) {
                  <span class="activity-ring" aria-label="In progress"></span>
                }
              </div>
            }
          </div>
        </section>
      </div>
    </div>
  `,
})
export class RunningScreen {
  readonly phaseIdx = input.required<number>();
  readonly phaseMessages = input.required<Partial<Record<Phase, string>>>();
  readonly phaseOrder = input.required<readonly Phase[]>();
  readonly mode = input<SearchMode>('quick');
  readonly supportingText = computed(() =>
    this.mode() === 'advanced'
      ? 'We’ll pause when your input is needed.'
      : 'This usually takes a couple of minutes.',
  );

  readonly steps = computed(() => {
    const phaseOrder = this.phaseOrder();
    const stages = phaseOrder.length === 1 && phaseOrder[0] === 'decompose'
      ? [{ label: 'Preparing the technical summary', icon: 'Sparkles', phases: ['decompose'] as readonly Phase[] }]
      : EXECUTION_STAGES;
    const currentPhase = phaseOrder[Math.max(this.phaseIdx(), 0)];
    const activeIndex = Math.max(
      stages.findIndex((stage) => stage.phases.includes(currentPhase)),
      0,
    );
    return stages.map((stage, index) => {
      const complete = index < activeIndex;
      const active = index === activeIndex;
      return {
        icon: stage.icon,
        label: stage.label,
        complete,
        active,
        last: index === stages.length - 1,
        status: complete ? 'complete' : active ? 'active' : 'pending',
      };
    });
  });
}
