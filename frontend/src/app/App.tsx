import { useState } from "react";
import {
  AlertTriangle,
  BarChart3,
  Check,
  ChevronLeft,
  Clock3,
  Database,
  FileJson,
  FileSearch,
  FileText,
  Home,
  LockKeyhole,
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
  const url = URL.createObjectURL(blob);
  const safeName =
    (data.title || "patent-report")
      .replace(/[^a-z0-9]+/gi, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 40) || "report";
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `${safeName}.json`;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}

function exportReportPdf(data: AnalysisReport) {
  const win = window.open("", "_blank");
  if (!win) {
    alert("Please allow pop-ups for this site to export the PDF.");
    return;
  }

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

  win.document.write(`<!doctype html>
<html>
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
</style>
</head>
<body>
  <h1>${escapeHtml(data.title)}</h1>
  <div class="meta">Aramco AI Patent Agent &middot; Submitted ${escapeHtml(data.date)} &middot; ${data.patents.length} patents scanned</div>
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
  <div class="foot">Decision-support only &middot; not legal advice. Generated by Aramco AI Patent Agent.</div>
  <script>window.onload = function () { window.print(); };</script>
</body>
</html>`);
  win.document.close();
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
  const [claims, setClaims] = useState("");
  const [phaseIdx, setPhaseIdx] = useState(-1);
  const [phaseMessages, setPhaseMessages] = useState<Partial<Record<Phase, string>>>({});
  const [reportData, setReportData] = useState<AnalysisReport | null>(null);
  const [errorDetail, setErrorDetail] = useState("");
  const [failedPhase, setFailedPhase] = useState<Phase | null>(null);

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

      await streamAnalysis({ description, claims }, (event) => {
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
        <button className="rail-brand" aria-label="Aramco AI Patent Agent" onClick={() => setScreen("submit")}>
          <img src="/aramco-ai-mark.png" alt="" />
        </button>

        <nav className="rail-nav">
          <button
            className="rail-item rail-item-active"
            onClick={() => setScreen("submit")}
            aria-label="New search"
          >
            <Home size={25} strokeWidth={1.8} />
            <span>New Search</span>
          </button>
        </nav>

        <div className="rail-spacer" />
        <div className="rail-mode" aria-label="Light appearance">
          <Sun size={24} strokeWidth={1.7} />
          <span>Light</span>
        </div>
      </aside>

      <main className="workspace">
        {screen === "submit" && (
          <SubmitScreen
            description={description}
            setDescription={setDescription}
            claims={claims}
            setClaims={setClaims}
            concepts={reportData?.concepts ?? []}
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
  return (
    <header className="brand-header">
      <img src="/aramco-ai-logo.png" alt="Aramco AI Patent Agent" className="brand-logo" />
    </header>
  );
}

function PageFooter() {
  return (
    <footer className="page-footer">
      <LockKeyhole size={18} strokeWidth={1.8} />
      <span>Secure. Reliable. Powered by <strong>Aramco.</strong></span>
    </footer>
  );
}

function SubmitScreen({
  description,
  setDescription,
  claims,
  setClaims,
  concepts,
  onRun,
}: {
  description: string;
  setDescription: (value: string) => void;
  claims: string;
  setClaims: (value: string) => void;
  concepts: string[];
  onRun: () => void;
}) {
  return (
    <div className="screen screen-scroll">
      <BrandHeader />

      <section className="intro-copy submit-intro">
        <p>
          Describe your idea in plain language — PatentScout expands it into search
          <br className="desktop-break" /> queries and scores it against real prior art.
        </p>
      </section>

      <section className="form-card glass-card" aria-label="Patent search input">
        <div className="field-heading">
          <div className="field-title">
            <FileText size={22} />
            <label htmlFor="invention-description">Invention description</label>
          </div>
          <span className="required-pill">Required</span>
        </div>

        <div className="textarea-wrap textarea-wrap-primary">
          <textarea
            id="invention-description"
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            maxLength={2000}
            rows={6}
          />
          <span className="character-count">{description.length} / 2000</span>
        </div>

        <div className="field-heading second-field">
          <div className="field-title">
            <FileSearch size={22} />
            <label htmlFor="draft-claims">Draft claims <span>· optional</span></label>
          </div>
        </div>

        <div className="textarea-wrap">
          <textarea
            id="draft-claims"
            value={claims}
            onChange={(event) => setClaims(event.target.value)}
            maxLength={2000}
            rows={3}
            placeholder="Paste any draft independent claims to sharpen the search…"
          />
          <span className="character-count">{claims.length} / 2000</span>
        </div>

        <div className="concept-row" aria-label="Detected concepts">
          {concepts.slice(0, 2).map((concept) => (
            <span className="concept-chip" key={concept}>
              <Sparkles size={15} />
              {concept}
            </span>
          ))}
          <span className="concept-chip concept-chip-outline">
            {concepts.length ? "+ detected concepts" : "Concepts appear once a search runs"}
          </span>
        </div>

        <div className="form-actions">
          <button
            className="primary-action"
            onClick={onRun}
            disabled={!description.trim()}
          >
            <Sparkles size={20} fill="currentColor" />
            <span>Run PatentScout</span>
            <span aria-hidden="true">→</span>
          </button>
          <div className="time-note">
            <Clock3 size={22} />
            <span>Typical result in under 60 seconds</span>
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
        <div className="working-label"><span /> WORKING</div>
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
          <button className="secondary-button" onClick={() => downloadReportJson(data)}>
            <FileJson size={17} /> View JSON
          </button>
          <button className="primary-action compact" onClick={() => exportReportPdf(data)}>
            Export PDF
          </button>
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
