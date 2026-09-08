import { Injectable } from '@angular/core';
import { AnalysisReport, PatentMatch } from '../models/report';

/**
 * PDF / Word / JSON export. PDF is generated in-browser and downloaded
 * directly; it never opens a print window or asks the user to choose a PDF
 * printer. Word retains the HTML document format used by the React build.
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
  private patentFamilies(patents: PatentMatch[]) {
    const groups = new Map<
      string,
      { identified: boolean; members: PatentMatch[]; totalSize: number }
    >();
    for (const patent of patents) {
      const familyId = patent.family_id?.trim() ?? '';
      const key = familyId || `publication:${patent.num}`;
      const group = groups.get(key);
      if (group) {
        group.members.push(patent);
        group.totalSize = Math.max(group.totalSize, patent.family_size ?? 0);
      } else {
        groups.set(key, {
          identified: Boolean(familyId),
          members: [patent],
          totalSize: patent.family_size ?? 0,
        });
      }
    }
    return [...groups.values()];
  }

  private familySummary(patents: PatentMatch[]): string {
    const families = this.patentFamilies(patents);
    const identified = families.filter((family) => family.identified).length;
    const unknown = families
      .filter((family) => !family.identified)
      .reduce((count, family) => count + family.members.length, 0);
    return [
      `${patents.length} publications`,
      identified ? `${identified} identified patent families` : '',
      unknown ? `${unknown} without family metadata` : '',
    ]
      .filter(Boolean)
      .join(' - ');
  }
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
    const families = this.patentFamilies(data.patents);
    const rows = families
      .flatMap((family) => [
        ...(family.identified && (family.members.length > 1 || family.totalSize > 1)
          ? [`<tr class="family-row"><td colspan="3">Patent family - ${family.members.length} matched ${family.members.length === 1 ? 'publication' : 'publications'}${family.totalSize > family.members.length ? ` (${family.totalSize} total publications reported)` : ''}</td></tr>`]
          : []),
        ...family.members.map((patent) => `
        <tr>
          <td class="mono">${this.escapeHtml(patent.num)}</td>
          <td>${this.escapeHtml(patent.title)}<div class="rationale">${this.escapeHtml(patent.rationale)}</div></td>
          <td class="center risk-${patent.risk.toLowerCase()}">${this.escapeHtml(patent.risk)}</td>
        </tr>`),
      ])
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
  .family-row td { background:#edf9f5; color:#087f7b; font-weight:700; border-top:2px solid #64c8bd; }
  h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .06em; color: #66758d; margin: 24px 0 8px; }
  ol { padding-left: 18px; } li { margin-bottom: 8px; font-size: 12px; }
  .foot { margin-top: 32px; color: #91a0b3; font-size: 10px; border-top: 1px solid #edf2f5; padding-top: 12px; }
${forWord ? "  @page WordSection1 { size: A4; margin: 2cm; }\n  div.WordSection1 { page: WordSection1; }" : ""}
</style>
</head>
<body>
${forWord ? '<div class="WordSection1">' : ""}
  <h1>${this.escapeHtml(data.title)}</h1>
  <div class="meta">Novelty Lens &middot; Submitted ${this.escapeHtml(data.date)} &middot; ${this.escapeHtml(this.familySummary(data.patents))}</div>
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
${forWord ? "</div>" : ""}
</body>
</html>`;
  }

  downloadReportJson(data: AnalysisReport) {
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    this.triggerDownload(blob, `${this.reportFileName(data)}.json`);
  }

  async exportReportPdf(data: AnalysisReport) {
    // Lazy-loaded so the PDF generator does not increase the initial app
    // bundle for users who never export. `save()` creates the Blob and clicks
    // the download internally; no popup and no print-dialog fallback.
    const { jsPDF } = await import('jspdf');
    const pdf = new jsPDF({ unit: 'mm', format: 'a4', orientation: 'portrait' });
    const pageWidth = pdf.internal.pageSize.getWidth();
    const pageHeight = pdf.internal.pageSize.getHeight();
    const margin = 18;
    const contentWidth = pageWidth - margin * 2;
    const footerSpace = 16;
    let y = margin;

    const ensureSpace = (height: number) => {
      if (y + height <= pageHeight - margin - footerSpace) return;
      pdf.addPage();
      y = margin;
    };

    const writeLines = (
      value: string,
      options: {
        size?: number;
        color?: [number, number, number];
        bold?: boolean;
        indent?: number;
        gapAfter?: number;
      } = {},
    ) => {
      const size = options.size ?? 10;
      const indent = options.indent ?? 0;
      pdf.setFont('helvetica', options.bold ? 'bold' : 'normal');
      pdf.setFontSize(size);
      pdf.setTextColor(...(options.color ?? [16, 35, 74]));
      const lines = pdf.splitTextToSize(value, contentWidth - indent);
      const lineHeight = size * 0.42;
      ensureSpace(lines.length * lineHeight + (options.gapAfter ?? 0));
      pdf.text(lines, margin + indent, y);
      y += lines.length * lineHeight + (options.gapAfter ?? 0);
    };

    const sectionHeading = (value: string) => {
      ensureSpace(12);
      y += 3;
      pdf.setDrawColor(8, 140, 99);
      pdf.setLineWidth(0.7);
      pdf.line(margin, y - 3, margin, y + 3);
      writeLines(value.toUpperCase(), {
        size: 9,
        color: [8, 140, 99],
        bold: true,
        indent: 3,
        gapAfter: 3,
      });
    };

    writeLines(data.title, { size: 18, bold: true, gapAfter: 2 });
    writeLines(
      `Novelty Lens - Submitted ${data.date} - ${data.query_count} queries - ${this.familySummary(data.patents)}`,
      { size: 9, color: [102, 117, 141], gapAfter: 5 },
    );

    if (data.patents[0]) {
      sectionHeading('Closest-match novelty risk');
      const riskColor: [number, number, number] =
        data.patents[0].risk === 'High'
          ? [169, 47, 59]
          : data.patents[0].risk === 'Medium'
            ? [141, 89, 12]
            : [8, 140, 99];
      writeLines(data.patents[0].risk, { size: 16, color: riskColor, bold: true, gapAfter: 2 });
    }

    sectionHeading('Key concepts');
    writeLines(data.concepts.length ? data.concepts.join(' | ') : 'None extracted', {
      size: 9,
      color: [74, 91, 116],
      gapAfter: 3,
    });

    sectionHeading('Prior-art matches');
    if (!data.patents.length) {
      writeLines('No matching patent records were returned.', {
        color: [102, 117, 141],
      });
    }
    for (const family of this.patentFamilies(data.patents)) {
      if (family.identified && (family.members.length > 1 || family.totalSize > 1)) {
        ensureSpace(10);
        writeLines(
          `PATENT FAMILY - ${family.members.length} MATCHED ${family.members.length === 1 ? 'PUBLICATION' : 'PUBLICATIONS'}${family.totalSize > family.members.length ? ` - ${family.totalSize} TOTAL REPORTED` : ''}`,
          { size: 8, color: [8, 127, 123], bold: true, gapAfter: 3 },
        );
      }
    for (const patent of family.members) {
      ensureSpace(34);
      pdf.setDrawColor(197, 225, 215);
      pdf.setLineWidth(0.35);
      pdf.line(margin, y - 3, pageWidth - margin, y - 3);

      writeLines(`${patent.num} - ${patent.source}`, {
        size: 8,
        color: [8, 140, 99],
        bold: true,
        gapAfter: 1,
        indent: 3,
      });
      writeLines(patent.title, { size: 11, bold: true, gapAfter: 2, indent: 3 });
      writeLines(`Novelty risk: ${patent.risk}`, {
        size: 9,
        bold: true,
        gapAfter: 1,
        indent: 3,
      });
      writeLines(`Overlap summary: ${patent.rationale}`, {
        size: 9,
        color: [74, 91, 116],
        gapAfter: 3,
        indent: 3,
      });
      y += 3;
    }
    }

    sectionHeading('Recommendations');
    if (!data.recommendations.length) {
      writeLines('No recommendations were generated.', { color: [102, 117, 141] });
    }
    data.recommendations.forEach((recommendation, index) => {
      writeLines(`${index + 1}. ${recommendation}`, {
        size: 9,
        color: [74, 91, 116],
        indent: 2,
        gapAfter: 2,
      });
    });

    const pages = pdf.getNumberOfPages();
    for (let page = 1; page <= pages; page += 1) {
      pdf.setPage(page);
      pdf.setDrawColor(226, 233, 238);
      pdf.line(margin, pageHeight - 13, pageWidth - margin, pageHeight - 13);
      pdf.setFont('helvetica', 'normal');
      pdf.setFontSize(8);
      pdf.setTextColor(125, 139, 158);
      pdf.text('Decision-support only - not legal advice. Generated by Novelty Lens.', margin, pageHeight - 8);
      pdf.text(`${page} / ${pages}`, pageWidth - margin, pageHeight - 8, { align: 'right' });
    }

    pdf.save(`${this.reportFileName(data)}.pdf`);
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
