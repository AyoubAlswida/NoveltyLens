import {
  Component,
  ChangeDetectionStrategy,
  input,
  signal,
  inject,
  ElementRef,
  HostListener,
} from '@angular/core';
import { Icon } from './icon';
import { AnalysisReport } from '../models/report';
import { ExportService } from '../services/export';

@Component({
  selector: 'app-export-menu',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Icon],
  template: `
    <div class="export-menu">
      <button
        class="primary-action compact"
        (click)="open.set(!open())"
        aria-haspopup="menu"
        [attr.aria-expanded]="open()"
      >
        <app-icon name="Download" [size]="17" /> Export
        <app-icon name="ChevronDown" [size]="16" class="export-caret" [class.open]="open()" />
      </button>

      @if (open()) {
        <div class="export-dropdown glass-card" role="menu">
          @for (option of options; track option.key) {
            <button role="menuitem" class="export-option" (click)="choose(option.key)">
              <app-icon [name]="option.icon" [size]="17" />
              <span class="export-option-text">
                <strong>{{ option.label }}</strong>
                <small>{{ option.hint }}</small>
              </span>
            </button>
          }
        </div>
      }
    </div>
  `,
})
export class ExportMenu {
  private readonly exporter = inject(ExportService);
  private readonly host = inject(ElementRef<HTMLElement>);

  readonly data = input.required<AnalysisReport>();
  readonly open = signal(false);

  readonly options = [
    { key: 'pdf', label: 'PDF', hint: 'Opens the print dialog', icon: 'FileText' },
    { key: 'word', label: 'Word', hint: 'Downloads a .doc file', icon: 'FileType2' },
    { key: 'json', label: 'JSON', hint: 'Raw report data', icon: 'FileJson' },
  ] as const;

  // Close on an outside click or Escape. HostListener replaces the React
  // version's manual addEventListener/removeEventListener pair; Angular
  // detaches these with the component.
  @HostListener('document:mousedown', ['$event'])
  onPointerDown(event: MouseEvent) {
    if (this.open() && !this.host.nativeElement.contains(event.target as Node)) {
      this.open.set(false);
    }
  }

  @HostListener('document:keydown.escape')
  onEscape() {
    this.open.set(false);
  }

  choose(key: 'pdf' | 'word' | 'json') {
    this.open.set(false);
    const report = this.data();
    if (key === 'pdf') this.exporter.exportReportPdf(report);
    else if (key === 'word') this.exporter.exportReportWord(report);
    else this.exporter.downloadReportJson(report);
  }
}
