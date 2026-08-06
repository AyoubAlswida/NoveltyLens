import { useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  BarChart3,
  Check,
  ChevronDown,
  ChevronLeft,
  Clock3,
  Database,
  Download,
  FileJson,
  FileText,
  FileType2,
  Home,
  LockKeyhole,
  Moon,
  Network,
  PenLine,
  Radar,
  RotateCcw,
  Search,
  ShieldCheck,
  Sparkles,
  Sun,
} from "lucide-react";

type Screen = "submit" | "running" | "report" | "error";

const EXAMPLE_DESC =
  "A portable, refillable drinking-water bottle that automatically sterilizes its contents with an integrated UV-C emitter and filters particulates through a replaceable graphene-oxide membrane. A flow-rate sensor in the cap detects use and triggers a self-cleaning sterilization cycle without manual activation.";

// This order must remain aligned with backend/graph/events.py.
const PHASE_ORDER = [
  "understand",
  "queries",
  "search",
  "enrich",
  "semantic_search",
  "fuse",
  "validate",
  "compare",
  "analyze",
] as const;

type Phase = (typeof PHASE_ORDER)[number];

const PHASE_LABELS: Record<Phase, string> = {
  understand: "Extracting technical concepts & keywords…",
  queries: "Expanding into search queries…",
  search: "Searching patent databases…",
  enrich: "Fetching full patent text…",
  semantic_search: "Searching indexed patents by meaning…",
  fuse: "Combining keyword and semantic results…",
  validate: "Checking relevance of candidates…",
  compare: "Scoring patent similarities…",
  analyze: "Writing detailed analysis…",
};

const PHASE_ICONS: Record<Phase, typeof Sparkles> = {
  understand: Sparkles,
  queries: Search,
  search: Database,
  enrich: FileText,
  semantic_search: Radar,
  fuse: Network,
  validate: ShieldCheck,
  compare: BarChart3,
  analyze: PenLine,
};

const API_BASE_URL = import.meta.env.VITE_API_URL ?? "http://127.0.0.1:8001";

interface DynamicPatent {
  num: string;
  title: string;
  pct: number;
  risk: "High" | "Medium" | "Low";
  rationale: string;
  source: string;
}

interface AnalysisReport {
  title: string;
  date: string;
  concepts: string[];
  patents: DynamicPatent[];
  top_score: number;
  verdict: "Strong" | "Moderate" | "Weak";
  recommendations: string[];
  query_count: number;
}

interface BackendPatent {
  num: string;
  title: string;
  pct: number;
  risk: "High" | "Medium" | "Low";
  rationale: string;
  source: string;
}

interface BackendAnalysisResponse {
  title: string;
  date: string;
  concepts: string[];
  patents: BackendPatent[];
  top_score: number;
  verdict: "Strong" | "Moderate" | "Weak";
  query_count: number;
  recommendations: string[];
}

function riskClass(risk: "High" | "Medium" | "Low") {
  return `risk-${risk.toLowerCase()}`;
}

function verdictClass(verdict: "Strong" | "Moderate" | "Weak") {
  return `verdict-${verdict.toLowerCase()}`;
}

function escapeHtml(value: string) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function downloadReportJson(data: AnalysisReport) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  triggerDownload(blob, `${reportFileName(data)}.json`);
}

function reportFileName(data: AnalysisReport) {
  return (
    (data.title || "patent-report")
      .replace(/[^a-z0-9]+/gi, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 40) || "report"
  );
}

function triggerDownload(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}

/**
 * The one source of the exported report's markup. Both the PDF export (which
 * prints it) and the Word export (which saves it as .doc) render this, so the
 * two formats cannot drift apart.
 *
 * `forWord` swaps the print script for Word's page-setup metadata; Word opens
 * an HTML document saved as .doc natively, which is why no OOXML library is
 * needed here.
 */
function buildReportHtml(data: AnalysisReport, { forWord = false } = {}) {
  const rows = data.patents
    .map(
      (patent) => `
        <tr>
          <td class="mono">${escapeHtml(patent.num)}</td>
          <td>${escapeHtml(patent.title)}<div class="rationale">${escapeHtml(patent.rationale)}</div></td>
          <td class="center">${patent.pct}%</td>
          <td class="center risk-${patent.risk.toLowerCase()}">${escapeHtml(patent.risk)}</td>
        </tr>`,
    )
    .join("");

  const recommendations = data.recommendations
    .map((recommendation) => `<li>${escapeHtml(recommendation)}</li>`)
    .join("");
  const concepts = data.concepts
    .map((concept) => `<span class="chip">${escapeHtml(concept)}</span>`)
    .join("");

  return `<!doctype html>
<html${forWord ? ' xmlns:o="urn:schemas-microsoft-com:office:office" xmlns:w="urn:schemas-microsoft-com:office:word"' : ""}>
<head>
<meta charset="utf-8" />
<title>${escapeHtml(data.title)}</title>
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
  <h1>${escapeHtml(data.title)}</h1>
  <div class="meta">Novelty Lens &middot; Submitted ${escapeHtml(data.date)} &middot; ${data.patents.length} patents scanned</div>
  <div class="summary">
    <div><div class="label">Top similarity</div><div class="big">${data.top_score}%</div></div>
    <div><div class="label">Novelty verdict</div><div class="big">${escapeHtml(data.verdict)}</div></div>
  </div>
  <div class="label">Key concepts</div>
  <div style="margin:8px 0">${concepts || "<span class='rationale'>None extracted</span>"}</div>
  <h2>Prior-art matches</h2>
  <table>
    <thead><tr><th>Number</th><th>Title</th><th class="center">Match</th><th class="center">Risk</th></tr></thead>
    <tbody>${rows || "<tr><td colspan='4' class='rationale'>No matches found.</td></tr>"}</tbody>
  </table>
  <h2>Recommendations</h2>
  <ol>${recommendations || "<li class='rationale'>None.</li>"}</ol>
  <div class="foot">Decision-support only &middot; not legal advice. Generated by Novelty Lens.</div>
${forWord ? "</div>" : "  <script>window.onload = function () { window.print(); };</script>"}
</body>
</html>`;
}

function exportReportPdf(data: AnalysisReport) {
  const win = window.open("", "_blank");
  if (!win) {
    alert("Please allow pop-ups for this site to export the PDF.");
    return;
  }
  win.document.write(buildReportHtml(data));
  win.document.close();
}

function exportReportWord(data: AnalysisReport) {
  // Word reads an HTML document saved as .doc, so this needs no OOXML library
  // and stays byte-for-byte consistent with the PDF export's markup.
  const blob = new Blob(["﻿", buildReportHtml(data, { forWord: true })], {
    type: "application/msword",
  });
  triggerDownload(blob, `${reportFileName(data)}.doc`);
}

/**
 * Reads the SSE body of POST /api/analyze/stream. The request contract and
 * endpoint are intentionally unchanged so the redesigned UI keeps the exact
 * existing backend integration.
 */
async function streamAnalysis(payload: object, onEvent: (event: any) => void) {
  const response = await fetch(`${API_BASE_URL}/api/analyze/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  if (!response.ok || !response.body) {
    throw new Error("Backend query failed");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";

    for (const frame of frames) {
      const line = frame.trim();
      if (!line.startsWith("data:")) continue;
      const json = line.slice("data:".length).trim();
      if (json) onEvent(JSON.parse(json));
    }
  }
}

export default function App() {
  const [screen, setScreen] = useState<Screen>("submit");
  const [description, setDescription] = useState(EXAMPLE_DESC);
  const [phaseIdx, setPhaseIdx] = useState(-1);
  const [phaseMessages, setPhaseMessages] = useState<Partial<Record<Phase, string>>>({});
  const [reportData, setReportData] = useState<AnalysisReport | null>(null);
  const [errorDetail, setErrorDetail] = useState("");
  const [failedPhase, setFailedPhase] = useState<Phase | null>(null);
  const [theme, setTheme] = useState<"light" | "dark">(
    () => (localStorage.getItem("novelty-lens-theme") as "light" | "dark") ?? "light",
  );

  // The theme lives on <html> rather than a wrapper so the dark background
  // covers the full viewport, including any overscroll area.
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("novelty-lens-theme", theme);
  }, [theme]);

  // The rail's two buttons deliberately differ. The logo is plain navigation
  // and preserves whatever you had typed; "New Search" is the destructive one
  // that empties the form and drops the previous run.
  function goHome() {
    setScreen("submit");
  }

  function startNewSearch() {
    setDescription("");
    setReportData(null);
    setErrorDetail("");
    setFailedPhase(null);
    setPhaseIdx(-1);
    setPhaseMessages({});
    setScreen("submit");
  }

  async function startRunning() {
    if (!description.trim()) return;

    setScreen("running");
    setPhaseIdx(-1);
    setPhaseMessages({});
    setReportData(null);
    setErrorDetail("");
    setFailedPhase(null);

    // Tracked locally as well as in state, and declared outside the try so the
    // catch can read it: the error screen needs the phase synchronously, before
    // a state update would have landed.
    let lastPhase: Phase | null = null;

    try {
      let finalReport: AnalysisReport | null = null;

      await streamAnalysis({ description, claims: "" }, (event) => {
        if (event.type === "progress") {
          const idx = (PHASE_ORDER as readonly string[]).indexOf(event.phase);
          if (idx >= 0) {
            lastPhase = PHASE_ORDER[idx];
            setPhaseIdx((previous) => Math.max(previous, idx));
            setPhaseMessages((previous) => ({ ...previous, [event.phase]: event.message }));
          }
        } else if (event.type === "error") {
          throw new Error(event.detail || "Backend query failed");
        } else if (event.type === "result") {
          const backendData: BackendAnalysisResponse = event.data;
          const patents: DynamicPatent[] = (backendData.patents ?? []).map((patent) => ({
            num: patent.num || "Unknown publication",
            title: patent.title || "Untitled patent record",
            pct: Math.round(patent.pct),
            risk: patent.risk,
            rationale: patent.rationale || "No detailed rationale was returned for this record.",
            source: patent.source || "Patent database",
          }));

          finalReport = {
            title:
              backendData.title ||
              (description.length > 50 ? `${description.slice(0, 47)}...` : description),
            date:
              backendData.date ||
              new Date().toLocaleDateString("en-US", {
                month: "short",
                day: "numeric",
                year: "numeric",
              }),
            concepts: backendData.concepts ?? [],
            patents,
            top_score: Math.round(backendData.top_score ?? 0),
            verdict: backendData.verdict ?? "Strong",
            recommendations: backendData.recommendations ?? [],
            query_count: backendData.query_count ?? 0,
          };
        }
      });

      if (!finalReport) throw new Error("Stream ended without a result.");
      setReportData(finalReport);
      setScreen("report");
    } catch (error) {
      console.error("Backend connection error:", error);
      // The backend's own `error` detail is shown verbatim — the zero-tolerance
      // policy means a failed run reports what actually failed, never a guess.
      setErrorDetail(
        error instanceof Error && error.message
          ? error.message
          : "The analysis stream ended without a readable error.",
      );
      setFailedPhase(lastPhase);
      setScreen("error");
    }
  }

  return (
    <div className="app-shell">
      <aside className="brand-rail" aria-label="Primary navigation">
        <button className="rail-brand" aria-label="Novelty Lens home" onClick={goHome}>
          <img src="/aramco-ai-mark.png" alt="" />
        </button>

        <nav className="rail-nav">
          <button
            className="rail-item rail-item-active"
            onClick={startNewSearch}
            aria-label="Start a new search, clearing the current description and report"
          >
            <Home size={21} strokeWidth={1.8} />
            <span>New Search</span>
          </button>
        </nav>

        <div className="rail-spacer" />
        <button
          className="rail-mode"
          onClick={() => setTheme((previous) => (previous === "dark" ? "light" : "dark"))}
          aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} appearance`}
        >
          {theme === "dark" ? <Moon size={20} strokeWidth={1.7} /> : <Sun size={20} strokeWidth={1.7} />}
          <span>{theme === "dark" ? "Dark" : "Light"}</span>
        </button>
      </aside>

      <main className="workspace">
        {screen === "submit" && (
          <SubmitScreen
            description={description}
            setDescription={setDescription}
            onRun={startRunning}
          />
        )}
        {screen === "running" && (
          <RunningScreen phaseIdx={phaseIdx} phaseMessages={phaseMessages} />
        )}
        {screen === "report" && reportData && (
          <ReportScreen data={reportData} onBack={() => setScreen("submit")} />
        )}
        {screen === "error" && (
          <ErrorScreen
            detail={errorDetail}
            failedPhase={failedPhase}
            onRetry={startRunning}
            onBack={() => setScreen("submit")}
          />
        )}
      </main>
    </div>
  );
}

function BrandHeader() {
  // The old lockup was a single PNG with the wordmark baked in, so renaming it
  // meant rebuilding the lockup: the standalone mark plus live text. The
  // two-tone split (navy / teal) mirrors what the original artwork did.
  return (
    <header className="brand-header">
      <img src="/aramco-ai-mark.png" alt="" className="brand-mark" />
      <span className="brand-wordmark">
        <span className="brand-name">
          Novelty <span className="brand-name-accent">Lens</span>
        </span>
        <span className="brand-sub">Patent Agent</span>
      </span>
    </header>
  );
}

function PageFooter() {
  return (
    <footer className="page-footer">
      <LockKeyhole size={18} strokeWidth={1.8} />
      <span>Secure. Reliable. Powered by <strong>Digital @ AI Center of Excellence Department.</strong></span>
    </footer>
  );
}

function SubmitScreen({
  description,
  setDescription,
  onRun,
}: {
  description: string;
  setDescription: (value: string) => void;
  onRun: () => void;
}) {
  return (
    <div className="screen screen-scroll">
      <BrandHeader />

      <section className="intro-copy submit-intro">
        <p>
          Describe your idea in plain language. Novelty Lens expands it into search
          <br className="desktop-break" /> queries and scores it against real prior art.
        </p>
      </section>

      <section className="form-card glass-card" aria-label="Patent search input">
        <div className="field-heading">
          <div className="field-title">
            <FileText size={22} />
            <label htmlFor="invention-description">
              Invention description
              <span className="required-star" title="Required" aria-label="required">*</span>
            </label>
          </div>
        </div>

        <div className="textarea-wrap textarea-wrap-primary">
          <textarea
            id="invention-description"
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            maxLength={2000}
            rows={6}
            placeholder="Describe your invention in plain language: what it does, how it works, and what makes it different from what already exists…"
          />
          <span className="character-count">{description.length} / 2000</span>
        </div>

        <div className="form-actions">
          <button
            className="primary-action"
            onClick={onRun}
            disabled={!description.trim()}
          >
            <Sparkles size={20} fill="currentColor" />
            <span>Run Novelty Lens</span>
            <span aria-hidden="true">→</span>
          </button>
          <div className="time-note">
            <Clock3 size={22} />
            <span>Typical result in about 60 seconds</span>
          </div>
        </div>
      </section>

      <PageFooter />
    </div>
  );
}

function RunningScreen({
  phaseIdx,
  phaseMessages,
}: {
  phaseIdx: number;
  phaseMessages: Partial<Record<Phase, string>>;
}) {
  const activeIndex = Math.max(phaseIdx, 0);

  return (
    <div className="screen screen-scroll">
      <BrandHeader />

      <section className="intro-copy running-intro">
        <h1>Analyzing your invention</h1>
        <p>This can take a couple of minutes — several uncached LLM and embedding calls run in sequence.</p>
      </section>

      <section className="progress-card glass-card" aria-live="polite">
        {PHASE_ORDER.map((phase, index) => {
          const Icon = PHASE_ICONS[phase];
          const complete = index < activeIndex;
          const active = index === activeIndex;
          const statusClass = complete ? "complete" : active ? "active" : "pending";

          return (
            <div className={`progress-step ${statusClass}`} key={phase}>
              <div className="phase-track">
                <div className="phase-icon">
                  {complete ? <Check size={18} /> : <Icon size={18} />}
                </div>
                {index < PHASE_ORDER.length - 1 && <span className="phase-line" />}
              </div>
              <div className="phase-label">
                {phaseMessages[phase] || PHASE_LABELS[phase]}
              </div>
              {active && <span className="activity-ring" aria-label="In progress" />}
            </div>
          );
        })}
      </section>

      <PageFooter />
    </div>
  );
}

function ExportMenu({ data }: { data: AnalysisReport }) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);

  // Close on an outside click or Escape. Registered only while open so the
  // report screen carries no listeners in its resting state.
  useEffect(() => {
    if (!open) return;
    function onPointerDown(event: MouseEvent) {
      if (!wrapRef.current?.contains(event.target as Node)) setOpen(false);
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  const options = [
    { key: "pdf", label: "PDF", hint: "Opens the print dialog", icon: FileText, run: exportReportPdf },
    { key: "word", label: "Word", hint: "Downloads a .doc file", icon: FileType2, run: exportReportWord },
    { key: "json", label: "JSON", hint: "Raw report data", icon: FileJson, run: downloadReportJson },
  ] as const;

  return (
    <div className="export-menu" ref={wrapRef}>
      <button
        className="primary-action compact"
        onClick={() => setOpen((previous) => !previous)}
        aria-haspopup="menu"
        aria-expanded={open}
      >
        <Download size={17} /> Export
        <ChevronDown size={16} className={`export-caret ${open ? "open" : ""}`} />
      </button>

      {open && (
        <div className="export-dropdown glass-card" role="menu">
          {options.map(({ key, label, hint, icon: Icon, run }) => (
            <button
              key={key}
              role="menuitem"
              className="export-option"
              onClick={() => {
                setOpen(false);
                run(data);
              }}
            >
              <Icon size={17} />
              <span className="export-option-text">
                <strong>{label}</strong>
                <small>{hint}</small>
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function ErrorScreen({
  detail,
  failedPhase,
  onRetry,
  onBack,
}: {
  detail: string;
  failedPhase: Phase | null;
  onRetry: () => void;
  onBack: () => void;
}) {
  const reachedIndex = failedPhase ? PHASE_ORDER.indexOf(failedPhase) : -1;

  return (
    <div className="screen screen-scroll">
      <BrandHeader />

      <section className="intro-copy error-intro">
        <div className="failed-label"><span /> RUN FAILED</div>
        <h1>The analysis could not be completed</h1>
        <p>
          No partial results are shown — the pipeline reports a genuine answer or none at
          all, so nothing below was estimated or filled in.
        </p>
      </section>

      <section className="error-card glass-card" role="alert">
        <div className="error-heading">
          <div className="error-icon"><AlertTriangle size={22} strokeWidth={2} /></div>
          <div>
            <p className="eyebrow">What the backend reported</p>
            <h2>
              {failedPhase
                ? `Failed during “${PHASE_LABELS[failedPhase].replace(/…$/, "")}”`
                : "Failed before the first phase reported"}
            </h2>
          </div>
        </div>

        <pre className="error-detail">{detail}</pre>

        {reachedIndex >= 0 && (
          <div className="error-progress" aria-label="Phases completed before the failure">
            {PHASE_ORDER.map((phase, index) => (
              <span
                key={phase}
                className={`error-pip ${index < reachedIndex ? "done" : index === reachedIndex ? "failed" : ""}`}
                title={PHASE_LABELS[phase]}
              />
            ))}
            <span className="error-progress-note">
              {reachedIndex} of {PHASE_ORDER.length} phases completed
            </span>
          </div>
        )}

        <div className="error-actions">
          <button className="primary-action compact" onClick={onRetry}>
            <RotateCcw size={17} /> Try again
          </button>
          <button className="secondary-button" onClick={onBack}>
            <ChevronLeft size={17} /> Edit description
          </button>
        </div>

        <p className="error-hint">
          If this keeps happening, check that the backend is running on{" "}
          <code>{API_BASE_URL}</code> with Qdrant up, and that the SerpAPI and HuggingFace
          quotas are not exhausted.
        </p>
      </section>

      <PageFooter />
    </div>
  );
}

function ReportScreen({ data, onBack }: { data: AnalysisReport; onBack: () => void }) {
  const topPatent = data.patents[0];
  const displayRisk: "High" | "Medium" | "Low" =
    data.top_score >= 75 ? "High" : data.top_score >= 25 ? "Medium" : "Low";

  return (
    <div className="screen screen-scroll report-screen">
      <BrandHeader />

      <section className="report-toolbar glass-card">
        <button className="secondary-button icon-button" onClick={onBack} aria-label="Back to new search">
          <ChevronLeft size={20} />
        </button>
        <div className="report-title-block">
          <h1>{data.title}</h1>
          <p>Submitted {data.date} · {data.query_count} queries · {data.patents.length} patents scanned</p>
        </div>
        <div className="toolbar-actions">
          <ExportMenu data={data} />
        </div>
      </section>

      <section className="report-grid">
        <article className="summary-card glass-card">
          <div className={`score-ring ${riskClass(displayRisk)}`}>
            <svg viewBox="0 0 120 120" aria-hidden="true">
              <circle className="score-track" cx="60" cy="60" r="48" />
              <circle
                className="score-value"
                cx="60"
                cy="60"
                r="48"
                pathLength="100"
                strokeDasharray={`${Math.max(0, Math.min(100, data.top_score))} 100`}
              />
            </svg>
            <div><strong>{data.top_score}%</strong><span>{displayRisk}</span></div>
          </div>

          <div className="summary-copy">
            <p className="eyebrow">Closest prior-art similarity</p>
            <h2>{topPatent ? topPatent.title : "No matching prior art indexed"}</h2>
            <p>{topPatent ? `Top match: ${topPatent.num}` : "No patent record exceeded the current matching threshold."}</p>
            <div className={`verdict-pill ${verdictClass(data.verdict)}`}>Novelty verdict: {data.verdict}</div>
          </div>
        </article>

        <article className="concept-card glass-card">
          <p className="eyebrow">Detected concepts</p>
          <div className="concept-row report-concepts">
            {data.concepts.length ? data.concepts.map((concept) => (
              <span className="concept-chip" key={concept}>{concept}</span>
            )) : <span className="empty-state">No concepts returned.</span>}
          </div>
        </article>
      </section>

      <section className="matches-card glass-card">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Prior-art matches</p>
            <h2>Ranked patent results</h2>
          </div>
          <span className="result-count">{data.patents.length}</span>
        </div>

        <div className="matches-list">
          {data.patents.length ? data.patents.map((patent) => (
            <article className="patent-row" key={`${patent.num}-${patent.title}`}>
              <div className="patent-main">
                <div className="patent-meta">
                  <span>{patent.num}</span>
                  <span>{patent.source}</span>
                </div>
                <h3>{patent.title}</h3>
                <p>{patent.rationale}</p>
              </div>
              <div className={`patent-score ${riskClass(patent.risk)}`}>
                <div><strong>{patent.pct}%</strong><span>{patent.risk}</span></div>
                <div className="score-bar"><i style={{ width: `${Math.max(0, Math.min(100, patent.pct))}%` }} /></div>
              </div>
            </article>
          )) : <div className="empty-state padded">No matching patent records were returned.</div>}
        </div>
      </section>

      <section className="recommendations-card glass-card">
        <div className="section-heading">
          <div>
            <p className="eyebrow">AI guidance</p>
            <h2>How to make it more unique</h2>
          </div>
          <Sparkles size={24} />
        </div>
        {data.recommendations.length ? (
          <ol className="recommendation-list">
            {data.recommendations.map((recommendation, index) => (
              <li key={`${index}-${recommendation}`}><span>{index + 1}</span><p>{recommendation}</p></li>
            ))}
          </ol>
        ) : (
          <p className="empty-state">No recommendations were generated — no prior art was found to distinguish this invention from.</p>
        )}
      </section>

      <PageFooter />
    </div>
  );
}
