import { Component, ChangeDetectionStrategy, input, output } from '@angular/core';
import { Icon } from '../shared/icon';
import { BrandHeader } from '../shared/chrome';

@Component({
  selector: 'app-submit-screen',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Icon, BrandHeader],
  template: `
    <div class="screen screen-scroll">
      <app-brand-header />

      <section class="form-card glass-card" aria-label="Patent search input">
        <div class="field-heading">
          <div class="field-title">
            <app-icon name="FileText" [size]="22" />
            <label for="invention-description">
              Invention description
              <span class="required-star" title="Required" aria-label="required">*</span>
            </label>
          </div>
        </div>

        <div class="textarea-wrap textarea-wrap-primary">
          <textarea
            id="invention-description"
            [value]="description()"
            (input)="descriptionChange.emit($any($event.target).value)"
            maxlength="2000"
            rows="6"
            placeholder="Describe your invention in plain language: what it does, how it works, and what makes it different from what already exists…"
          ></textarea>
          <!-- The "n / 2000" counter was removed. maxlength stays: it is the
               actual limit, and dropping it would let the field accept text the
               browser then silently truncates. -->
        </div>

        <div class="form-actions">
          <button class="primary-action" (click)="run.emit()" [disabled]="!description().trim()">
            <app-icon name="Sparkles" [size]="20" fill="currentColor" />
            <span>Run Novelty Lens</span>
            <span aria-hidden="true">&rarr;</span>
          </button>
        </div>
      </section>

    </div>
  `,
})
export class SubmitScreen {
  readonly description = input.required<string>();
  readonly descriptionChange = output<string>();
  readonly run = output<void>();
}
