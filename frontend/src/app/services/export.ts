import { Injectable } from '@angular/core';
import { AnalysisReport, PatentMatch } from '../models/report';
import { recommendationBullets } from '../shared/recommendation-points';

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

    const recommendations = recommendationBullets(data.recommendations)
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
  <ul>${recommendations || "<li class='rationale'>None.</li>"}</ul>
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
    const logo = new Image();
    logo.src = '/novelty-lens-mark.svg';
    await logo.decode();
    const canvas = document.createElement('canvas');
    canvas.width = canvas.height = 192;
    const context = canvas.getContext('2d');
    if (!context) throw new Error('Could not render the Novelty Lens logo.');
    context.drawImage(logo, 0, 0, canvas.width, canvas.height);
    const logoPng = canvas.toDataURL('image/png');
    const pdf = new jsPDF({ unit: 'mm', format: 'a4', orientation: 'portrait' });
    pdf.setLineHeightFactor(1.2);
    const pageWidth = pdf.internal.pageSize.getWidth();
    const pageHeight = pdf.internal.pageSize.getHeight();
    const margin = 16;
    const contentWidth = pageWidth - margin * 2;
    const contentBottom = pageHeight - margin - 10;
    let y = margin;

    const writeHeader = () => {
      pdf.addImage(logoPng, 'PNG', margin, margin - 3, 10, 10, 'novelty-lens-mark');
      pdf.setFont('helvetica', 'bold');
      pdf.setFontSize(9);
      pdf.setTextColor(8, 127, 123);
      pdf.text('NOVELTY LENS', margin + 13, margin + 3);
      y = margin + 14;
    };

    const addPage = (continuedSection?: string) => {
      pdf.addPage();
      writeHeader();
      if (continuedSection) {
        pdf.setFont('helvetica', 'bold');
        pdf.setFontSize(8);
        pdf.setTextColor(102, 117, 141);
        pdf.text(`${continuedSection.toUpperCase()} - CONTINUED`, margin, y);
        y += 8;
      }
    };
    const ensureSpace = (height: number, continuedSection?: string) => {
      if (y + height <= contentBottom) return;
      addPage(continuedSection);
    };

    const lineHeight = (size: number) => size * 25.4 / 72 * 1.2;
    const linesFor = (value: string, size: number, width = contentWidth, bold = false) => {
      // Built-in Helvetica uses WinAnsi. Normalize typography before measuring
      // so unsupported characters cannot break spacing or line wrapping.
      const printable = value
        .replace(/[‐‑‒–—−]/gu, '-')
        .replace(/[‘’]/gu, "'")
        .replace(/[“”]/gu, '"')
        .replace(/[\u00a0\u2007\u202f]/gu, ' ')
        .replace(/[\u00ad\u200b]/gu, '');
      pdf.setFont('helvetica', bold ? 'bold' : 'normal');
      pdf.setFontSize(size);
      return pdf.splitTextToSize(printable, width) as string[];
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
      const lines = linesFor(value, size, contentWidth - indent, options.bold);
      ensureSpace(lines.length * lineHeight(size) + (options.gapAfter ?? 0));
      pdf.setFont('helvetica', options.bold ? 'bold' : 'normal');
      pdf.setFontSize(size);
      pdf.setTextColor(...(options.color ?? [16, 35, 74]));
      pdf.text(lines, margin + indent, y);
      y += lines.length * lineHeight(size) + (options.gapAfter ?? 0);
    };

    const sectionHeading = (value: string, prominent = false) => {
      ensureSpace(prominent ? 27 : 23);
      y += 3;
      pdf.setDrawColor(8, 140, 99);
      pdf.setLineWidth(0.7);
      pdf.line(margin, y - 3, margin, y + 3);
      writeLines(prominent ? value : value.toUpperCase(), {
        size: prominent ? 14 : 9,
        color: [8, 140, 99],
        bold: true,
        indent: 3,
        gapAfter: 5,
      });
    };

    writeHeader();
    writeLines(data.title, { size: 18, bold: true, gapAfter: 3 });
    writeLines(
      `Novelty Lens - Submitted ${data.date} - ${data.query_count} queries - ${this.familySummary(data.patents)}`,
      { size: 9, color: [102, 117, 141], gapAfter: 4 },
    );
    pdf.setDrawColor(226, 233, 238);
    pdf.setLineWidth(0.25);
    pdf.line(margin, y, pageWidth - margin, y);
    y += 7;

    if (data.patents[0]) {
      const summaryHeight = 17;
      ensureSpace(summaryHeight + 5);
      const riskColor: [number, number, number] =
        data.patents[0].risk === 'High'
          ? [169, 47, 59]
          : data.patents[0].risk === 'Medium'
            ? [141, 89, 12]
            : [8, 140, 99];
      const summaryTop = y;
      pdf.setFillColor(243, 248, 248);
      pdf.roundedRect(margin, summaryTop, contentWidth, summaryHeight, 2.5, 2.5, 'F');
      pdf.setFillColor(...riskColor);
      pdf.roundedRect(margin, summaryTop, 3.5, summaryHeight, 2.5, 2.5, 'F');
      pdf.setFont('helvetica', 'bold');
      pdf.setFontSize(8);
      pdf.setTextColor(102, 117, 141);
      pdf.text('CLOSEST PRIOR-ART MATCH', margin + 7, summaryTop + 6);
      pdf.setFont('helvetica', 'normal');
      pdf.setFontSize(9);
      pdf.setTextColor(74, 91, 116);
      pdf.text('Novelty risk assessment', margin + 7, summaryTop + 11.5);

      const riskLabel = data.patents[0].risk.toUpperCase();
      pdf.setFont('helvetica', 'bold');
      pdf.setFontSize(12);
      const riskWidth = pdf.getTextWidth(riskLabel) + 12;
      const riskLeft = margin + contentWidth - riskWidth - 5;
      pdf.setFillColor(...riskColor);
      pdf.roundedRect(riskLeft, summaryTop + 4.2, riskWidth, 8.5, 2, 2, 'F');
      pdf.setTextColor(255, 255, 255);
      pdf.text(riskLabel, riskLeft + riskWidth / 2, summaryTop + 10.25, { align: 'center' });
      y = summaryTop + summaryHeight + 5;
    }

    sectionHeading('Key concepts');
    if (!data.concepts.length) {
      writeLines('None extracted', { size: 9, color: [102, 117, 141], gapAfter: 3 });
    }
    data.concepts.forEach((concept) => {
      const size = 9;
      const indent = 5;
      const lines = linesFor(concept, size, contentWidth - indent);
      ensureSpace(lines.length * lineHeight(size) + 4, 'Key concepts');
      pdf.setFillColor(8, 140, 99);
      pdf.circle(margin + 1.5, y - 1.1, 0.9, 'F');
      pdf.setFont('helvetica', 'normal');
      pdf.setFontSize(size);
      pdf.setTextColor(74, 91, 116);
      pdf.text(lines, margin + indent, y);
      y += lines.length * lineHeight(size) + 3;
    });
    y += 1;

    const resultsHeading = `${data.patents.length} Ranked patent results`;
    sectionHeading(resultsHeading, true);
    if (!data.patents.length) {
      writeLines('No matching patent records were returned.', {
        color: [102, 117, 141],
      });
    }
    for (const family of this.patentFamilies(data.patents)) {
      const grouped = family.identified && (family.members.length > 1 || family.totalSize > 1);
      const familyLabel = `Patent family · ${family.members.length} matched ${family.members.length === 1 ? 'publication' : 'publications'}`;
      const familyDetails = family.totalSize > family.members.length
        ? ` · ${family.totalSize} reported`
        : '';
      const inset = grouped ? 4 : 0;
      const cardLeft = margin + inset;
      const cardWidth = contentWidth - 2 * inset;
      const groupHeaderHeight = 11;
      let familyPage = 0;
      let groupTop = 0;
      const closeFamilyCard = () => {
        pdf.setDrawColor(172, 214, 211);
        pdf.setLineWidth(0.35);
        pdf.roundedRect(margin, groupTop, contentWidth, y - groupTop, 2.5, 2.5, 'S');
      };
      for (const patent of family.members) {
        const innerWidth = cardWidth - 12;
        const titleLines = linesFor(patent.title, 11, innerWidth, true);
        const rationaleLines = linesFor(patent.rationale, 9, innerWidth);
        const cardPadding = 6;
        const metaHeight = lineHeight(8);
        const titleHeight = titleLines.length * lineHeight(11);
        const rationaleHeight = rationaleLines.length * lineHeight(9);
        const cardHeight =
          cardPadding +
          metaHeight +
          1.5 +
          titleHeight +
          3 +
          metaHeight +
          2 +
          metaHeight +
          1 +
          rationaleHeight +
          cardPadding;
        const page = pdf.getNumberOfPages();
        const headerSpace = grouped && (familyPage !== page || y + cardHeight + 4 > contentBottom)
          ? groupHeaderHeight
          : 0;
        if (grouped && familyPage === page && y + cardHeight + 4 > contentBottom) {
          closeFamilyCard();
        }
        ensureSpace(cardHeight + 4 + headerSpace, resultsHeading);
        if (grouped && familyPage !== pdf.getNumberOfPages()) {
          const continued = familyPage > 0 ? ' (continued)' : '';
          groupTop = y - 3;
          pdf.setFillColor(226, 243, 242);
          pdf.roundedRect(cardLeft, y - 1, cardWidth, 8, 1.5, 1.5, 'F');
          pdf.setFont('helvetica', 'bold');
          pdf.setFontSize(9);
          pdf.setTextColor(8, 127, 123);
          pdf.text(familyLabel + familyDetails + continued, cardLeft + 3, y + 4);
          familyPage = pdf.getNumberOfPages();
          y += groupHeaderHeight;
        }
        const cardTop = y;
        pdf.setFillColor(250, 252, 252);
        pdf.setDrawColor(211, 226, 225);
        pdf.setLineWidth(0.3);
        pdf.roundedRect(cardLeft, cardTop, cardWidth, cardHeight, 2.5, 2.5, 'FD');

        let cardY = cardTop + cardPadding;
        const writeCardText = (
          value: string | string[],
          size: number,
          color: [number, number, number],
          bold = false,
          gapAfter = 0,
        ) => {
          const lines = Array.isArray(value) ? value : linesFor(value, size, innerWidth, bold);
          pdf.setFont('helvetica', bold ? 'bold' : 'normal');
          pdf.setFontSize(size);
          pdf.setTextColor(...color);
          pdf.text(lines, cardLeft + 6, cardY);
          cardY += lines.length * lineHeight(size) + gapAfter;
        };

        writeCardText(`${patent.num} - ${patent.source}`, 8, [8, 127, 123], true, 1.5);
        writeCardText(titleLines, 11, [16, 35, 74], true, 3);
        const riskColor: [number, number, number] =
          patent.risk === 'High'
            ? [169, 47, 59]
            : patent.risk === 'Medium'
              ? [141, 89, 12]
              : [8, 140, 99];
        writeCardText(`NOVELTY RISK - ${patent.risk.toUpperCase()}`, 8, riskColor, true, 2);
        writeCardText('OVERLAP SUMMARY', 8, [102, 117, 141], true, 1);
        writeCardText(rationaleLines, 9, [74, 91, 116]);
        y = cardTop + cardHeight + 4;
      }
      if (grouped && familyPage) {
        closeFamilyCard();
        y += 4;
      }
    }

    sectionHeading('Recommendations', true);
    const suggestions = recommendationBullets(data.recommendations);
    if (!suggestions.length) {
      writeLines('No recommendations were generated.', { color: [102, 117, 141] });
    }
    for (const point of suggestions) {
      const size = 9;
      const indent = 5;
      const lines = linesFor(point, size, contentWidth - indent);
      const step = lineHeight(size);
      ensureSpace(Math.min(lines.length, 2) * step + 4, 'Recommendations');
      pdf.setFillColor(8, 127, 123);
      pdf.circle(margin + 1, y - 1, 0.7, 'F');
      for (const line of lines) {
        ensureSpace(step, 'Recommendations');
        pdf.setFont('helvetica', 'normal');
        pdf.setFontSize(size);
        pdf.setTextColor(74, 91, 116);
        pdf.text(line, margin + indent, y);
        y += step;
      }
      y += 4;
    }

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
