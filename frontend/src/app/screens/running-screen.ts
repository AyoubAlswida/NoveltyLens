import { Component, ChangeDetectionStrategy, input, computed } from '@angular/core';
import { Icon } from '../shared/icon';
import { Phase, PHASE_LABELS, PHASE_ICONS } from '../models/report';

@Component({
  selector: 'app-running-screen',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Icon],
  template: `
    <div class="screen screen-scroll">
      <section class="intro-copy running-intro">
        <h1>Analyzing your invention</h1>
        <p>This can take a couple of minutes.</p>
      </section>

      <section class="progress-card glass-card" aria-live="polite">
        @for (step of steps(); track step.phase) {
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
      </section>
    </div>
  `,
})
export class RunningScreen {
  readonly phaseIdx = input.required<number>();
  readonly phaseMessages = input.required<Partial<Record<Phase, string>>>();
  readonly phaseOrder = input.required<readonly Phase[]>();

  readonly steps = computed(() => {
    const activeIndex = Math.max(this.phaseIdx(), 0);
    const messages = this.phaseMessages();
    const phaseOrder = this.phaseOrder();
    return phaseOrder.map((phase, index) => {
      const complete = index < activeIndex;
      const active = index === activeIndex;
      return {
        phase,
        icon: PHASE_ICONS[phase],
        label: messages[phase] || PHASE_LABELS[phase],
        complete,
        active,
        last: index === phaseOrder.length - 1,
        status: complete ? 'complete' : active ? 'active' : 'pending',
      };
    });
  });
}
