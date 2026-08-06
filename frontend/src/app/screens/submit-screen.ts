import { Component, ChangeDetectionStrategy, input, output } from '@angular/core';
import { Icon } from '../shared/icon';
import { BrandHeader, PageFooter } from '../shared/chrome';

@Component({
  selector: 'app-submit-screen',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Icon, BrandHeader, PageFooter],
  template: `
    <div class="screen screen-scroll">
      <app-brand-header />

      <section class="intro-copy submit-intro">
        <p>
          Describe your idea in plain language. Novelty Lens expands it into search
          <br class="desktop-break" />
          queries and scores it against real prior art.
        </p>
      </section>

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
          <span class="character-count">{{ description().length }} / 2000</span>
        </div>

        <div class="form-actions">
          <button class="primary-action" (click)="run.emit()" [disabled]="!description().trim()">
            <app-icon name="Sparkles" [size]="20" fill="currentColor" />
            <span>Run Novelty Lens</span>
            <span aria-hidden="true">&rarr;</span>
          </button>
          <div class="time-note">
            <app-icon name="Clock3" [size]="22" />
            <span>Typical result in about 60 seconds</span>
          </div>
        </div>
      </section>

      <app-page-footer />
    </div>
  `,
})
export class SubmitScreen {
  readonly description = input.required<string>();
  readonly descriptionChange = output<string>();
  readonly run = output<void>();
}
