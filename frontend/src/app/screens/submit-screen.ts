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
            placeholder="Describe your invention in plain language: what it does, how it works, and what makes it different from what already exists… or upload a file below."
          ></textarea>
          <!-- The "n / 2000" counter was removed. maxlength stays: it is the
               actual limit, and dropping it would let the field accept text the
               browser then silently truncates. -->
        </div>

        <div class="upload-row">
          <input
            #fileInput
            type="file"
            accept=".txt,.md,.pdf,.docx,text/plain,text/markdown,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            class="visually-hidden"
            (change)="onFileInputChange($event)"
          />
          <button type="button" class="secondary-button upload-button" (click)="fileInput.click()">
            <app-icon name="Upload" [size]="16" />
            <span>Upload a file instead (optional)</span>
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
        </div>

        @if (uploadError(); as message) {
          <p class="upload-error" role="alert">{{ message }}</p>
        }

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
