import { Component, ChangeDetectionStrategy, input, output, signal, ElementRef, viewChild } from '@angular/core';
import { Icon } from '../shared/icon';

/** Every file type accepted here is parsed to plain text entirely in the
 * browser -- .txt/.md via FileReader.readAsText, .pdf via pdfjs-dist, and
 * .docx via mammoth's raw-text extraction (dynamically imported so neither
 * library's ~1MB+ bundle loads until a user actually picks a .pdf/.docx
 * file). Nothing is sent to the backend to parse; the extracted string ends
 * up in the same description field a typed entry would, same as before. */
const ACCEPTED_EXTENSIONS = ['.txt', '.md', '.pdf', '.docx'];

@Component({
  selector: 'app-submit-screen',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Icon],
  template: `
    <div class="screen screen-scroll">
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

        <div
          class="textarea-wrap textarea-wrap-primary"
          [class.dropzone-active]="dragActive()"
          (dragover)="onDragOver($event)"
          (dragleave)="onDragLeave($event)"
          (drop)="onDrop($event)"
        >
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

          <!--
            Attach lives INSIDE the box, bottom-left, the way a composer does.
            It was a full-width outlined button underneath, which gave the form
            three stacked rows of controls and made attaching look like a step
            of equal weight to running the search. It is an option on the
            description, so it sits on the description.

            The drop target is still the whole wrap, so dragging a file anywhere
            over the box works exactly as before.
          -->
          <div class="composer-tools">
            <input
              #fileInput
              type="file"
              accept=".txt,.md,.pdf,.docx,text/plain,text/markdown,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
              class="visually-hidden"
              (change)="onFileInputChange($event)"
            />
            <button
              type="button"
              class="composer-attach"
              (click)="fileInput.click()"
              title="Attach a .txt, .md, .pdf or .docx file, or drag one onto the box"
            >
              <app-icon name="Upload" [size]="15" />
              <span>Attach a file</span>
            </button>

            @if (uploadedFileName(); as name) {
              <span class="upload-chip">
                <app-icon name="FileText" [size]="14" />
                {{ name }}
                <button
                  type="button"
                  class="upload-chip-remove"
                  (click)="clearUpload()"
                  aria-label="Remove uploaded file"
                >
                  <app-icon name="X" [size]="12" />
                </button>
              </span>
            }

            <!--
              Advanced is a MODE on the search, not a second search. It was a
              second button, which put two actions of equal visual weight on the
              screen and left the difference between them to be guessed at.
              Switching it on changes what the one action does, and the button's
              own label changes with it -- a mode that did not show up in the
              control it modifies is exactly the kind that gets triggered by
              accident.

              role="switch" rather than a plain button: it has an on and an off
              state that a screen reader has to be able to announce, which
              aria-pressed on a generic button conveys less directly.
            -->
            <span class="composer-mode">
              <button
                type="button"
                class="composer-toggle"
                role="switch"
                [class.is-on]="advanced()"
                [attr.aria-checked]="advanced()"
                (click)="advanced.set(!advanced())"
              >
                <span class="composer-switch" aria-hidden="true">
                  <span class="composer-switch-knob"></span>
                </span>
                <span>Advanced</span>
              </button>

              <span
                class="action-hint"
                tabindex="0"
                aria-label="What Advanced Search does"
                title="Advanced Search pauses for your review before it searches: you confirm how the invention was understood, edit the technical elements, then edit and approve the search queries that will run. Nothing is searched for until you approve it."
              >
                <app-icon name="Info" [size]="15" />
              </span>
            </span>
          </div>
        </div>

        @if (uploadError(); as message) {
          <p class="upload-error" role="alert">{{ message }}</p>
        }

        <div class="form-actions">
          <!--
            One action. Which run it starts is the switch's business, and the
            label says which one that is, so the button never means something
            different from what it reads.
          -->
          <button class="primary-action" (click)="start()" [disabled]="!description().trim()">
            @if (advanced()) {
              <app-icon name="ShieldCheck" [size]="18" />
              <span>Start Advanced Search</span>
            } @else {
              <app-icon name="Sparkles" [size]="18" fill="currentColor" />
              <span>Run Novelty Lens</span>
            }
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
  /** An Advanced Search run: the guided three-stage flow, rather than a search
   * on its own. Still a separate output from `run` -- the two are genuinely
   * different code paths in the shell, and collapsing them into one event with
   * a boolean would push the branch into `app.ts` for no gain. */
  readonly verify = output<void>();

  /**
   * Whether the next run is an Advanced Search.
   *
   * **Deliberately not persisted.** The sidebar's collapsed state is a
   * workspace preference and is remembered; this is an intent about one
   * search. Restoring it would mean a choice made days ago silently changes
   * what the button does today, and Advanced commits the person to a review
   * before anything is searched for. The screen is destroyed and rebuilt by
   * the shell's `@switch` on every visit, so off-by-default costs nothing to
   * maintain.
   */
  readonly advanced = signal(false);

  /** The single action, dispatched by the mode the switch is in. */
  start() {
    if (this.advanced()) this.verify.emit();
    else this.run.emit();
  }

  readonly fileInputRef = viewChild<ElementRef<HTMLInputElement>>('fileInput');

  readonly uploadedFileName = signal('');
  readonly uploadError = signal('');
  readonly dragActive = signal(false);

  onFileInputChange(event: Event) {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    if (file) this.readFile(file);
    // Reset so choosing the same filename again still fires a change event.
    input.value = '';
  }

  onDragOver(event: DragEvent) {
    event.preventDefault();
    this.dragActive.set(true);
  }

  onDragLeave(event: DragEvent) {
    event.preventDefault();
    this.dragActive.set(false);
  }

  onDrop(event: DragEvent) {
    event.preventDefault();
    this.dragActive.set(false);
    const file = event.dataTransfer?.files?.[0];
    if (file) this.readFile(file);
  }

  clearUpload() {
    this.uploadedFileName.set('');
    this.uploadError.set('');
  }

  /** Populates the same description field a typed entry would -- uploading
   * replaces the current text with the file's content, but the field stays
   * a normal textarea afterward, so the user can still edit or add to it by
   * hand. That covers "file only", "text only", and "both" with one field,
   * without touching the request shape sent to the backend. */
  private readFile(file: File) {
    this.uploadError.set('');

    const lowerName = file.name.toLowerCase();
    const hasAcceptedExtension = ACCEPTED_EXTENSIONS.some((ext) => lowerName.endsWith(ext));
    if (!hasAcceptedExtension) {
      this.uploadedFileName.set('');
      this.uploadError.set(
        `"${file.name}" isn't a supported file type. Upload a .txt, .md, .pdf, or .docx file, or type your description directly.`,
      );
      return;
    }

    if (lowerName.endsWith('.pdf')) {
      this.extractPdfText(file);
    } else if (lowerName.endsWith('.docx')) {
      this.extractDocxText(file);
    } else {
      this.extractPlainText(file);
    }
  }

  private extractPlainText(file: File) {
    const reader = new FileReader();
    reader.onload = () => {
      const text = String(reader.result ?? '').trim();
      if (!text) {
        this.uploadError.set(`"${file.name}" appears to be empty.`);
        return;
      }
      this.uploadedFileName.set(file.name);
      this.descriptionChange.emit(text);
    };
    reader.onerror = () => {
      this.uploadError.set(`Couldn't read "${file.name}". Please try again or type your description directly.`);
    };
    reader.readAsText(file);
  }

  /** pdf.js needs its worker script served as a static file rather than
   * bundled -- copied once into public/ (see public/pdf.worker.min.mjs) and
   * referenced by absolute path the same way the rest of this app's static
   * assets are, per CLAUDE.md's public/ convention. */
  private async extractPdfText(file: File) {
    try {
      const pdfjs = await import('pdfjs-dist');
      pdfjs.GlobalWorkerOptions.workerSrc = '/pdf.worker.min.mjs';

      const buffer = await file.arrayBuffer();
      const doc = await pdfjs.getDocument({ data: buffer }).promise;

      const pageTexts: string[] = [];
      for (let pageNum = 1; pageNum <= doc.numPages; pageNum++) {
        const page = await doc.getPage(pageNum);
        const content = await page.getTextContent();
        pageTexts.push(content.items.map((item) => ('str' in item ? item.str : '')).join(' '));
      }

      const text = pageTexts.join('\n\n').trim();
      if (!text) {
        this.uploadError.set(
          `"${file.name}" has no extractable text -- it may be a scanned image PDF. Try typing the description directly.`,
        );
        return;
      }
      this.uploadedFileName.set(file.name);
      this.descriptionChange.emit(text);
    } catch {
      this.uploadError.set(`Couldn't read "${file.name}". Please try again or type your description directly.`);
    }
  }

  private async extractDocxText(file: File) {
    try {
      const mammoth = await import('mammoth');
      const buffer = await file.arrayBuffer();
      const result = await mammoth.extractRawText({ arrayBuffer: buffer });

      const text = result.value.trim();
      if (!text) {
        this.uploadError.set(`"${file.name}" appears to be empty.`);
        return;
      }
      this.uploadedFileName.set(file.name);
      this.descriptionChange.emit(text);
    } catch {
      this.uploadError.set(`Couldn't read "${file.name}". Please try again or type your description directly.`);
    }
  }
}
