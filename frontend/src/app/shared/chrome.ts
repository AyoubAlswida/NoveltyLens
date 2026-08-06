import { Component, ChangeDetectionStrategy } from '@angular/core';
import { Icon } from './icon';

/**
 * The wordmark used to be baked into a single PNG. It is the standalone mark
 * plus live text so the product name is editable; the navy/teal split
 * reproduces what the original artwork did.
 */
@Component({
  selector: 'app-brand-header',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <header class="brand-header">
      <img src="/aramco-ai-mark.png" alt="" class="brand-mark" />
      <span class="brand-wordmark">
        <span class="brand-name">
          Novelty <span class="brand-name-accent">Lens</span>
        </span>
        <span class="brand-sub">Patent Agent</span>
      </span>
    </header>
  `,
})
export class BrandHeader {}

@Component({
  selector: 'app-page-footer',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Icon],
  template: `
    <footer class="page-footer">
      <app-icon name="LockKeyhole" [size]="18" [strokeWidth]="1.8" />
      <span>Secure. Reliable. Powered by <strong>Digital &#64; AI Center of Excellence Department.</strong></span>
    </footer>
  `,
})
export class PageFooter {}
