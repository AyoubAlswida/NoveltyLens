import { Component, inject, input, output, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { classify, GateShell, type GateState } from './gate-shell';
import { VerificationReport } from './verification-report';
import { RunsService } from '../services/runs';

/**
 * **Workflow Gate D — draft report review** (spec §12.4).
 *
 * ## A report cannot be approved into existence
 *
 * Case AC. The draft must already exist and have been presented, so this screen
 * fetches it and refuses to offer approval if there is none — the control is
 * absent, not disabled, because a disabled button suggests the door exists.
 *
 * ## The draft says it is a draft
 *
 * It carries **DRAFT — NOT APPROVED** until approved, and the banner is rendered
 * from the report's own `status` rather than from which screen is showing it. A
 * caveat edited after approval creates a **new version** needing fresh approval;
 * the previously approved version stays immutable (case AD).
 */
@Component({
  selector: 'app-draft-review',
  standalone: true,
  imports: [FormsModule, GateShell, VerificationReport],
  template: `
    <app-gate-shell
      gate="D"
      title="Review the draft report"
      [runId]="runId()"
      [state]="state()"
      [detail]="detail()"
      [blocking]="[]"
      [rules]="RULES"
      [busy]="state() === 'validating'"
      approveLabel="Approve report"
      [canApprove]="version() !== null"
      (approve)="approve()"
      (back)="back.emit()"
      (reload)="reload()"
    >
      <section class="gate-body">
        @if (version() === null) {
          <p class="gate-empty">
            No draft exists yet. A report cannot be approved into existence — the
            draft is assembled after the evidence review completes.
          </p>
        } @else {
          <app-verification-report [report]="report()" [status]="reportStatus()" />

          <label for="report-comments">Comments (optional)</label>
          <textarea
            id="report-comments"
            rows="4"
            class="gate-editor"
            [(ngModel)]="comments"
            [disabled]="state() === 'validating'"
          ></textarea>

          <label for="report-caveats">Additional caveats (optional)</label>
          <textarea
            id="report-caveats"
            rows="3"
            class="gate-editor"
            [(ngModel)]="caveats"
            [disabled]="state() === 'validating'"
          ></textarea>
          <p class="gate-note">
            Editing a caveat after approval creates a new version needing fresh
            approval. The version you approve stays immutable.
          </p>
        }
      </section>
    </app-gate-shell>
  `,
})
export class DraftReview {
  private readonly runs = inject(RunsService);

  readonly runId = input.required<string>();
  readonly back = output<void>();
  readonly resumed = output<void>();

  readonly state = signal<GateState>('loading');
  readonly detail = signal('');
  readonly report = signal<Record<string, unknown> | null>(null);
  readonly version = signal<number | null>(null);
  readonly reportStatus = signal<string>('DRAFT');

  comments = '';
  caveats = '';
  private actionKey = this.runs.newKey();

  readonly RULES = [
    'a draft exists and was presented',
    'every unresolved cell is listed',
    'every excluded publication is listed',
    'every waiver appears with its unresolved limitation',
    "the draft carries 'DRAFT — NOT APPROVED' until approved",
  ];

  constructor() {
    queueMicrotask(() => this.reload());
  }

  async reload(): Promise<void> {
    this.state.set('loading');
    this.detail.set('');
    this.actionKey = this.runs.newKey();
    try {
      const payload = await this.runs.report(this.runId());
      const version = payload['version'];
      this.version.set(typeof version === 'number' ? version : null);
      this.reportStatus.set(String(payload['status'] ?? 'DRAFT'));
      // The WHOLE payload, not `payload.draft`. Unwrapping here threw away the
      // wrapper's own fields -- `report_version` among them -- so the draft
      // screen rendered "Report v—" while its approve button submitted version
      // 1. `VerificationReport` merges the envelope itself, and does it for the
      // approved view too, so there is one place that knows the shape.
      this.report.set(payload as Record<string, unknown>);
      this.state.set('review');
    } catch (error) {
      const next = classify(error, this.detail);
      // A 404 here means "no draft yet", which is an ordinary state rather than
      // a denial: the run simply has not reached the draft.
      this.state.set(next === 'forbidden' && this.detail().includes('report') ? 'empty' : next);
    }
  }

  async approve(): Promise<void> {
    const version = this.version();
    if (version === null) return;
    this.state.set('validating');
    try {
      await this.runs.approveGateD(this.runId(), version, this.actionKey, this.comments);
      this.state.set('resuming');
      this.resumed.emit();
    } catch (error) {
      this.state.set(classify(error, this.detail));
    }
  }
}
