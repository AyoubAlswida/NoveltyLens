import { Component, ChangeDetectionStrategy, input, computed, inject } from '@angular/core';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';
import { ICON_PATHS } from './icon-paths';

/**
 * Mirrors the React `<Home size={21} strokeWidth={1.8} />` call shape as
 * `<app-icon name="Home" [size]="21" [strokeWidth]="1.8" />`.
 *
 * The inner SVG is bypassed through the sanitizer because Angular strips
 * unknown SVG children from [innerHTML] otherwise. That is safe here and only
 * here: ICON_PATHS is a compile-time constant checked into the repo, never
 * user input, and `name` can only select from it.
 */
@Component({
  selector: 'app-icon',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<svg
    xmlns="http://www.w3.org/2000/svg"
    [attr.width]="size()"
    [attr.height]="size()"
    viewBox="0 0 24 24"
    [attr.fill]="fill()"
    stroke="currentColor"
    [attr.stroke-width]="strokeWidth()"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
    [innerHTML]="markup()"
  ></svg>`,
  styles: [':host { display: inline-flex; line-height: 0; }'],
})
export class Icon {
  private readonly sanitizer = inject(DomSanitizer);

  readonly name = input.required<string>();
  readonly size = input<number>(20);
  readonly strokeWidth = input<number>(2);
  readonly fill = input<string>('none');

  readonly markup = computed<SafeHtml>(() =>
    this.sanitizer.bypassSecurityTrustHtml(ICON_PATHS[this.name()] ?? ''),
  );
}
