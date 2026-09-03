import { Injectable } from '@angular/core';
import { AnalysisReport } from '../models/report';

/**
 * PDF / Word / JSON export. Ported from the React build -- this logic was
 * already plain TypeScript with no framework surface, so only the wrapper
 * changed.
 *
 * **buildReportHtml is no longer byte-identical to the React version, and the
 * standing promise that it would be is deliberately withdrawn.** Exports lost
 * their Similarity column, their "Top similarity" figure and their "Novelty
 * verdict" figure when the product stopped publishing a numeric score and an
 * overall verdict; keeping the exports byte-stable would have meant keeping
 * exactly the numbers the rest of the change removes. The export tests are
 * re-pinned against the current output. Everything else about the template is
 * still ported verbatim, and the .doc/.json output should stay stable from
 * here -- if you change it again, re-pin those tests in the same commit.
 *
 * The exported document is deliberately always light-themed: it is a separate
 * document that does not inherit the app's data-theme, and a dark-background
 * printout would be wrong.
 */
@Injectable({ providedIn: 'root' })
export class ExportService {
  private escapeHtml(value: string) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  private reportFileName(data: AnalysisReport) {
    return (
      (data.title || "patent-report")
        .replace(/[^a-z0-9]+/gi, "-")
        .replace(/^-+|-+$/g, "")
        .slice(0, 40) || "report"
    );
  }

  private triggerDownload(blob: Blob, filename: string) {
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
    document.body.removeChild(anchor);
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  }

  private buildReportHtml(data: AnalysisReport, { forWord = false } = {}) {
    const rows = data.patents
      .map(
      (patent) => `
        <tr>
          <td class="mono">${this.escapeHtml(patent.num)}</td>
          <td>${this.escapeHtml(patent.title)}<div class="rationale">${this.escapeHtml(patent.rationale)}</div></td>
          <td class="center risk-${patent.risk.toLowerCase()}">${this.escapeHtml(patent.risk)}</td>
        </tr>`,
    )
    .join("");

    const recommendations = data.recommendations
    .map((recommendation) => `<li>${this.escapeHtml(recommendation)}</li>`)
    .join("");
    const concepts = data.concepts
    .map((concept) => `<span class="chip">${this.escapeHtml(concept)}</span>`)
    .join("");

    // The rating of the closest match, or undefined when the run found nothing.
    // The summary block below is omitted entirely in that case rather than
    // printing a rating over an empty table -- an exported document outlives the
    // screen it came from, so a fabricated "Low" there is the more durable lie.
    const topRisk = data.patents[0]?.risk;

    return `<!doctype html>
<html${forWord ? ' xmlns:o="urn:schemas-microsoft-com:office:office" xmlns:w="urn:schemas-microsoft-com:office:word"' : ""}>
<head>
<meta charset="utf-8" />
<title>${this.escapeHtml(data.title)}</title>
<style>
  /* The export window is a separate document and inherits none of the app's
     stylesheets, so the brand face is re-declared here. It is same-origin, so
     the /fonts/ paths resolve against the app. */
  @font-face { font-family: "Aramco Ghawar"; font-weight: 400; font-display: swap;
    src: local("Aramco Ghawar Regular"), url("/fonts/AramcoGhawar-Regular.woff2") format("woff2"),
         url("/fonts/AramcoGhawar-Regular.otf") format("opentype"), url("/fonts/AramcoGhawar-Regular.ttf") format("truetype"); }
  @font-face { font-family: "Aramco Ghawar"; font-weight: 600; font-display: swap;
    src: local("Aramco Ghawar SemiBold"), url("/fonts/AramcoGhawar-SemiBold.woff2") format("woff2"),
         url("/fonts/AramcoGhawar-SemiBold.otf") format("opentype"), url("/fonts/AramcoGhawar-SemiBold.ttf") format("truetype"); }
  @font-face { font-family: "Aramco Ghawar"; font-weight: 700; font-display: swap;
    src: local("Aramco Ghawar Bold"), url("/fonts/AramcoGhawar-Bold.woff2") format("woff2"),
         url("/fonts/AramcoGhawar-Bold.otf") format("opentype"), url("/fonts/AramcoGhawar-Bold.ttf") format("truetype"); }
  * { box-sizing: border-box; }
  body { font-family: "Aramco Ghawar", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #10234a; margin: 40px; }
  h1 { font-size: 22px; margin: 0 0 4px; }
  .meta { color: #66758d; font-size: 12px; margin-bottom: 24px; }
  .summary { display: flex; gap: 36px; margin-bottom: 24px; }
  .summary .big { font-size: 40px; font-weight: 700; }
  .label { text-transform: uppercase; letter-spacing: .08em; font-size: 10px; color: #66758d; }
  .chip { display: inline-block; background: #edf9f5; color: #0b9f70; border-radius: 20px; padding: 4px 10px; font-size: 11px; margin: 0 6px 6px 0; }
  table { width: 100%; border-collapse: collapse; margin: 8px 0 24px; font-size: 12px; }
  th { text-align: left; border-bottom: 2px solid #dce7ec; padding: 8px 6px; color: #66758d; font-size: 10px; text-transform: uppercase; letter-spacing: .06em; }
  td { border-bottom: 1px solid #edf2f5; padding: 9px 6px; vertical-align: top; }
  .mono { font-family: "Aramco Ghawar", ui-monospace, SFMono-Regular, Menlo, monospace; white-space: nowrap; }
  .center { text-align: center; }
  .rationale { color: #708097; font-size: 11px; margin-top: 4px; }
  .risk-high { color: #c43838; font-weight: 700; }
  .risk-medium { color: #aa7017; font-weight: 700; }
  .risk-low { color: #0b9f70; font-weight: 700; }
  h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .06em; color: #66758d; margin: 24px 0 8px; }
  ol { padding-left: 18px; } li { margin-bottom: 8px; font-size: 12px; }
  .foot { margin-top: 32px; color: #91a0b3; font-size: 10px; border-top: 1px solid #edf2f5; padding-top: 12px; }
${forWord ? "  @page WordSection1 { size: A4; margin: 2cm; }\n  div.WordSection1 { page: WordSection1; }" : ""}
</style>
</head>
<body>
${forWord ? '<div class="WordSection1">' : ""}
  <h1>${this.escapeHtml(data.title)}</h1>
  <div class="meta">Novelty Lens &middot; Submitted ${this.escapeHtml(data.date)} &middot; ${data.patents.length} patents scanned</div>
${topRisk ? `  <div class="summary">
    <div><div class="label">Novelty risk</div><div class="big">${this.escapeHtml(topRisk)}</div></div>
  </div>` : ""}
  <div class="label">Key concepts</div>
  <div style="margin:8px 0">${concepts || "<span class='rationale'>None extracted</span>"}</div>
  <h2>Prior-art matches</h2>
  <table>
    <thead><tr><th>Number</th><th>Title</th><th class="center">Novelty risk</th></tr></thead>
    <tbody>${rows || "<tr><td colspan='3' class='rationale'>No matches found.</td></tr>"}</tbody>
  </table>
  <h2>Recommendations</h2>
  <ol>${recommendations || "<li class='rationale'>None.</li>"}</ol>
  <div class="foot">Decision-support only &middot; not legal advice. Generated by Novelty Lens.</div>
${forWord ? "</div>" : "  <script>window.onload = function () { window.print(); };</script>"}
</body>
</html>`;
  }

  downloadReportJson(data: AnalysisReport) {
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    this.triggerDownload(blob, `${this.reportFileName(data)}.json`);
  }

  exportReportPdf(data: AnalysisReport) {
    const win = window.open("", "_blank");
    if (!win) {
      alert("Please allow pop-ups for this site to export the PDF.");
      return;
    }
    win.document.write(this.buildReportHtml(data));
    win.document.close();
  }

  exportReportWord(data: AnalysisReport) {
    // Word reads an HTML document saved as .doc, so this needs no OOXML library
    // and stays byte-for-byte consistent with the PDF export's markup.
    const blob = new Blob(["﻿", this.buildReportHtml(data, { forWord: true })], {
      type: "application/msword",
    });
    this.triggerDownload(blob, `${this.reportFileName(data)}.doc`);
  }
}
