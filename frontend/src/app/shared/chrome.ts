import { Component, ChangeDetectionStrategy } from '@angular/core';

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
      <img src="/novelty-lens-mark.svg" alt="" class="brand-mark" />
      <span class="brand-wordmark">
        <span class="brand-name">
          Novelty<span class="brand-name-accent">Lens</span>
        </span>
        <span class="brand-sub">Patent Agent</span>
      </span>
    </header>
  `,
})
export class BrandHeader {}
