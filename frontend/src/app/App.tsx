import { useState } from "react";

type Screen = "submit" | "running" | "report" | "history";

const EXAMPLE_DESC =
  "A portable, refillable drinking-water bottle that automatically sterilizes its contents with an integrated UV-C emitter and filters particulates through a replaceable graphene-oxide membrane. A flow-rate sensor in the cap detects use and triggers a self-cleaning sterilization cycle without manual activation.";

// Must stay identical to backend/graph/events.py's PHASES tuple - this is the
// checklist the real SSE stream (POST /api/analyze/stream) drives, not a
// simulated timer. Order here is the checklist's display order; the running
// state ("how far the request has actually gotten") is computed from
// whichever of these phases is the furthest-along one seen so far, since
// semantic_search runs in parallel with search/enrich on the backend and can
// arrive interleaved with them.
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

// The checklist shown to the user groups the 9 real backend phases into 5
// high-level stages - still driven by real progress (which of the 9 phases
// has actually been reached), just not exposing the internal step names,
// which read as too granular/technical for a running-status screen.
const DISPLAY_STAGES: { label: string; phases: (typeof PHASE_ORDER)[number][] }[] = [
  { label: "Understanding your invention", phases: ["understand", "queries"] },
  { label: "Searching for prior art", phases: ["search", "enrich", "semantic_search", "fuse"] },
  { label: "Checking relevance", phases: ["validate"] },
  { label: "Scoring similarity", phases: ["compare"] },
  { label: "Writing the analysis", phases: ["analyze"] },
];

const PHASE_TO_STAGE: Partial<Record<(typeof PHASE_ORDER)[number], number>> = {};
DISPLAY_STAGES.forEach((stage, stageIdx) => {
  stage.phases.forEach((phase) => {
    PHASE_TO_STAGE[phase] = stageIdx;
  });
});

const API_BASE_URL = import.meta.env.VITE_API_URL ?? "http://127.0.0.1:8001";

// Structural pattern (light page + dark icon rail, centered card layout) is inspired by
// search.projectpq.ai; the palette itself is PatentScout's own brand green, not PQAI's.
const COLORS = {
  page: "#F3F4F6",
  sidebar: "#FFFFFF",
  sidebarBorder: "#E2E5EA",
  sidebarHover: "rgba(27,154,107,0.08)",
  sidebarActive: "rgba(27,154,107,0.14)",
  brandOnDark: "#1B9A6B",
  brand: "#1B9A6B",
  brandDark: "#158058",
  brandTint: "#E6F7EF",
  brandTintBorder: "rgba(27,154,107,0.25)",
  card: "#FFFFFF",
  border: "#E2E5EA",
  inputBg: "#FAFAFB",
  textPrimary: "#1F2937",
  textSecondary: "#5B6472",
  textMuted: "#9AA3AF",
  riskHighText: "#DC2626",
  riskHighBg: "rgba(220,38,38,0.08)",
  riskMediumText: "#B45309",
  riskMediumBg: "rgba(180,83,9,0.08)",
  riskLowText: "#158058",
  riskLowBg: "rgba(27,154,107,0.10)",
};

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
  // The actual query strings, not just the count. Captured client-side from
  // the "queries" progress event during the run - the backend's terminal
  // result payload only ever carried query_count, and extending that shared
  // shape would also mean touching legacy's frozen _run_pipeline, so this
  // stays a frontend-only addition sourced from the SSE stream instead.
  queries: string[];
  // Pipeline funnel counts, same sourcing story as `queries` above - each
  // captured from its own progress event's extra fields as the run streams
  // in. Undefined (not 0) for a phase that either never emitted (e.g. no
  // patents found at all, so filter/enrich are skipped) or predates the
  // frontend rewiring, so a stale History entry doesn't render "0 found".
  foundCount?: number;
  filteredCount?: number;
  fullTextCount?: number;
  droppedCount?: number;
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

function riskColor(risk: "High" | "Medium" | "Low") {
  if (risk === "High") return { text: COLORS.riskHighText, bg: COLORS.riskHighBg };
  if (risk === "Medium") return { text: COLORS.riskMediumText, bg: COLORS.riskMediumBg };
  return { text: COLORS.riskLowText, bg: COLORS.riskLowBg };
}

function verdictColor(v: "Strong" | "Moderate" | "Weak") {
  if (v === "Strong") return COLORS.riskLowText;
  if (v === "Moderate") return COLORS.riskMediumText;
  return COLORS.riskHighText;
}

function buildReport(backendData: any, desc: string): AnalysisReport {
  const mappedPatents: DynamicPatent[] = (backendData.patents || []).map((p: any) => {
    const finalPct = p.pct <= 1 ? Math.floor(p.pct * 100) : Math.floor(p.pct);
    let structuralRisk: "High" | "Medium" | "Low" = "Medium";
    if (finalPct >= 75) structuralRisk = "High";
    else if (finalPct <= 25) structuralRisk = "Low";
    return {
      num: p.num || p.number || "US Unknown",
      title: p.title || "Patent Prior Art Record",
      pct: finalPct,
      // Never substitute prose here. This used to fall back to "Shares common
      // vocabulary or systemic overlap with your primary description." for any
      // patent the backend did not explain, which reads as model output and is
      // not. The backend guarantees a rationale per match or fails the request,
      // so an empty one means something is wrong and must look wrong.
      rationale: p.rationale || "",
    };
  });

  const topScore = mappedPatents.length > 0 ? mappedPatents[0].pct : 0;
  let finalVerdict: "Strong" | "Moderate" | "Weak" = "Strong";
  if (topScore >= 75) finalVerdict = "Weak";
  else if (topScore >= 25) finalVerdict = "Moderate";

  return {
    title: desc.length > 50 ? desc.slice(0, 47) + "..." : desc,
    date: new Date().toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }),
    // No invented placeholder concept. The backend always sends the real list.
    concepts: backendData.concepts || [],
    patents: mappedPatents,
    top_score: topScore,
    verdict: finalVerdict,
    query_count: backendData.query_count,
    recommendations: backendData.recommendations || [],
    queries: backendData.queries || [],
  };
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
  const safeName = (data.title || "patent-report").replace(/[^a-z0-9]+/gi, "-").replace(/^-+|-+$/g, "").slice(0, 40) || "report";
  const a = document.createElement("a");
  a.href = url;
  a.download = `${safeName}.json`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
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
      (p) => `
        <tr>
          <td class="mono">${escapeHtml(p.num)}</td>
          <td>${escapeHtml(p.title)}<div class="rationale">${escapeHtml(p.rationale)}</div></td>
          <td class="center">${p.pct}%</td>
          <td class="center risk-${p.risk.toLowerCase()}">${escapeHtml(p.risk)}</td>
        </tr>`
    )
    .join("");

  const recs = data.recommendations.map((r) => `<li>${escapeHtml(r)}</li>`).join("");
  const concepts = data.concepts.map((c) => `<span class="chip">${escapeHtml(c)}</span>`).join("");

  win.document.write(`<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<title>${escapeHtml(data.title)}</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; color: #12211a; margin: 40px; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .meta { color: #5c7266; font-size: 12px; margin-bottom: 24px; }
  .summary { display: flex; gap: 32px; margin-bottom: 24px; }
  .summary .big { font-size: 40px; font-weight: 700; }
  .label { text-transform: uppercase; letter-spacing: 0.08em; font-size: 10px; color: #5c7266; }
  .chip { display: inline-block; background: #eaf7f0; color: #1a7a4c; border-radius: 20px; padding: 3px 10px; font-size: 11px; margin: 0 6px 6px 0; }
  table { width: 100%; border-collapse: collapse; margin: 8px 0 24px; font-size: 12px; }
  th { text-align: left; border-bottom: 2px solid #d5e3db; padding: 8px 6px; color: #5c7266; font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em; }
  td { border-bottom: 1px solid #eef3f0; padding: 8px 6px; vertical-align: top; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; white-space: nowrap; }
  .center { text-align: center; }
  .rationale { color: #6b8177; font-size: 11px; margin-top: 3px; }
  .risk-high { color: #c0392b; font-weight: 700; }
  .risk-medium { color: #b8860b; font-weight: 700; }
  .risk-low { color: #1a7a4c; font-weight: 700; }
  h2 { font-size: 13px; text-transform: uppercase; letter-spacing: 0.06em; color: #5c7266; margin: 24px 0 8px; }
  ol { padding-left: 18px; } li { margin-bottom: 8px; font-size: 12px; }
  .foot { margin-top: 32px; color: #93a69c; font-size: 10px; border-top: 1px solid #eef3f0; padding-top: 12px; }
</style>
</head>
<body>
  <h1>${escapeHtml(data.title)}</h1>
  <div class="meta">PatentScout AI &middot; Submitted ${escapeHtml(data.date)} &middot; ${data.patents.length} patents scanned</div>
  <div class="summary">
    <div><div class="label">Top similarity</div><div class="big">${data.top_score}%</div></div>
    <div><div class="label">Novelty verdict</div><div class="big">${escapeHtml(data.verdict)}</div></div>
  </div>
  <div class="label">Key concepts</div>
  <div style="margin:8px 0 8px">${concepts || "<span class='rationale'>None extracted</span>"}</div>
  <h2>Prior-art matches</h2>
  <table>
    <thead><tr><th>Number</th><th>Title</th><th class="center">Match</th><th class="center">Risk</th></tr></thead>
    <tbody>${rows || "<tr><td colspan='4' class='rationale'>No matches found.</td></tr>"}</tbody>
  </table>
  <h2>Recommendations</h2>
  <ol>${recs || "<li class='rationale'>None.</li>"}</ol>
  <div class="foot">Decision-support only &middot; not legal advice. Generated by PatentScout AI.</div>
  <script>window.onload = function () { window.print(); };</script>
</body>
</html>`);
  win.document.close();
}

/** Reads the SSE body of POST /api/analyze/stream and invokes onEvent for
 * each `data: {...}` frame as it arrives. Native EventSource can't be used
 * here since it only supports GET with no body - this endpoint is a POST
 * carrying the invention description/claims as JSON. */
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
    buffer = frames.pop() ?? ""; // keep the trailing partial frame for next chunk
    for (const frame of frames) {
      const line = frame.trim();
      if (!line.startsWith("data:")) continue;
      const jsonStr = line.slice("data:".length).trim();
      if (jsonStr) onEvent(JSON.parse(jsonStr));
    }
  }
}

export default function App() {
  const [screen, setScreen] = useState<Screen>("submit");
  const [desc, setDesc] = useState(EXAMPLE_DESC);
  const [claims, setClaims] = useState("");

  // Real progress state, driven by SSE events - not a simulated timer.
  // phaseIdx is the furthest-along index into PHASE_ORDER seen so far; the
  // checklist (see DISPLAY_STAGES) collapses that into 5 high-level stages.
  const [phaseIdx, setPhaseIdx] = useState(-1);
  // Live pipeline funnel numbers, updated as each phase's real progress event
  // arrives - shown on RunningScreen while the request is still in flight,
  // not just after the fact. A field stays undefined until its phase has
  // actually reported it (e.g. "filtered" has no value until the filter
  // phase runs), so the display only ever shows measured numbers.
  const [liveStats, setLiveStats] = useState<{
    queries?: string[];
    found?: number;
    filtered?: number;
    fullText?: number;
    dropped?: number;
  }>({});

  const [reportData, setReportData] = useState<AnalysisReport | null>(null);
  const [history, setHistory] = useState<AnalysisReport[]>([]);

  async function startRunning() {
    console.log("🚀 Starting analysis request loop...");
    setScreen("running");
    setPhaseIdx(-1);
    setLiveStats({});
    setReportData(null);

    try {
      let finalReport: AnalysisReport | null = null;
      let capturedQueries: string[] = [];
      // Same numbers as liveStats above, captured into plain variables too
      // so building finalReport below doesn't depend on React state having
      // flushed yet - these are the values actually used for the report.
      let capturedFoundCount: number | undefined;
      let capturedFilteredCount: number | undefined;
      let capturedFullTextCount: number | undefined;
      let capturedDroppedCount: number | undefined;

      await streamAnalysis({ description: desc, claims: claims }, (event) => {
        if (event.type === "progress") {
          const idx = (PHASE_ORDER as readonly string[]).indexOf(event.phase);
          if (idx >= 0) {
            setPhaseIdx((prev) => Math.max(prev, idx));
          }
          if (event.phase === "queries" && Array.isArray(event.queries)) {
            capturedQueries = event.queries;
            setLiveStats((prev) => ({ ...prev, queries: event.queries }));
          }
          if (event.phase === "search" && typeof event.found === "number") {
            capturedFoundCount = event.found;
            setLiveStats((prev) => ({ ...prev, found: event.found }));
          }
          if (event.phase === "filter" && typeof event.kept === "number") {
            capturedFilteredCount = event.kept;
            setLiveStats((prev) => ({ ...prev, filtered: event.kept }));
          }
          if (event.phase === "enrich" && typeof event.enriched === "number") {
            capturedFullTextCount = event.enriched;
            capturedDroppedCount = typeof event.dropped === "number" ? event.dropped : undefined;
            setLiveStats((prev) => ({ ...prev, fullText: event.enriched, dropped: event.dropped }));
          }
        } else if (event.type === "error") {
          throw new Error(event.detail || "Backend query failed");
        } else if (event.type === "result") {
          const backendData: BackendAnalysisResponse = event.data;
          console.log("📦 Result received from backend:", backendData);

          // Keep backend rankings, risk labels, verdict, and recommendations intact.
          const mappedPatents: DynamicPatent[] = (backendData.patents ?? []).map((patent) => ({
            num: patent.num || "Unknown publication",
            title: patent.title || "Untitled patent record",
            pct: Math.round(patent.pct),
            risk: patent.risk,
            rationale: patent.rationale || "No detailed rationale was returned for this record.",
            source: patent.source || "Patent database",
          }));

          finalReport = {
            title: backendData.title || (desc.length > 50 ? desc.slice(0, 47) + "..." : desc),
            date: backendData.date || new Date().toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }),
            concepts: backendData.concepts ?? [],
            patents: mappedPatents,
            top_score: Math.round(backendData.top_score ?? 0),
            verdict: backendData.verdict ?? "Strong",
            recommendations: backendData.recommendations ?? [],
            query_count: backendData.query_count ?? 0,
            queries: capturedQueries,
            foundCount: capturedFoundCount,
            filteredCount: capturedFilteredCount,
            fullTextCount: capturedFullTextCount,
            droppedCount: capturedDroppedCount,
          };
        }
      });

      if (!finalReport) {
        throw new Error("Stream ended without a result.");
      }
      setReportData(finalReport);
      setHistory((prev) => [finalReport!, ...prev]);
      setScreen("report");

    } catch (err) {
      console.error("❌ Link Error:", err);
      alert(err instanceof Error ? err.message : "Failed to read server data. Please make sure server.py is running on port 8001.");
      setScreen("submit");
    }
  }

  return (
    <div
      style={{
        display: "flex",
        width: "100vw",
        height: "100vh",
        overflow: "hidden",
        background: COLORS.page,
        fontFamily: "'IBM Plex Sans', sans-serif",
      }}
    >
      {/* Icon rail */}
      <aside
        style={{
          width: 64,
          minWidth: 64,
          background: COLORS.sidebar,
          borderRight: `1px solid ${COLORS.sidebarBorder}`,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          height: "100vh",
          padding: "16px 0",
        }}
      >
        <div
          title="PatentScout AI"
          style={{
            width: 34,
            height: 34,
            borderRadius: 9,
            background: COLORS.brandTint,
            border: `1px solid ${COLORS.brandTintBorder}`,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            fontFamily: "'JetBrains Mono', monospace",
            fontWeight: 700,
            fontSize: 16,
            color: COLORS.brandOnDark,
            marginBottom: 20,
            flexShrink: 0,
          }}
        >
          P
        </div>

        <nav style={{ display: "flex", flexDirection: "column", gap: 6, width: "100%", alignItems: "center" }}>
          <RailIcon active={screen === "submit" || screen === "running" || screen === "report"} icon="✎" label="New search" onClick={() => setScreen("submit")} />
          <RailIcon active={screen === "history"} icon="≣" label="History" onClick={() => setScreen("history")} />
        </nav>

        <div style={{ flex: 1 }} />
      </aside>

      {/* Workspace Display Routing Manager */}
      <main style={{ flex: 1, overflow: "hidden", display: "flex", flexDirection: "column", background: COLORS.page }}>
        {screen === "submit" && (
          <SubmitScreen desc={desc} setDesc={setDesc} claims={claims} setClaims={setClaims} concepts={reportData?.concepts || ["Water treatment", "Consumer hardware"]} onRun={startRunning} />
        )}
        {screen === "running" && <RunningScreen phaseIdx={phaseIdx} liveStats={liveStats} />}
        {screen === "report" && (
          <ReportScreen onBack={() => setScreen("submit")} data={reportData} />
        )}
        {screen === "history" && (
          <HistoryScreen historyList={history} onSelectReport={(selectedReport) => {
            setReportData(selectedReport);
            setScreen("report");
          }} />
        )}
      </main>
    </div>
  );
}

function RailIcon({ active, icon, label, onClick }: { active: boolean; icon: string; label: string; onClick: () => void }) {
  const [hover, setHover] = useState(false);
  return (
    <button
      title={label}
      aria-label={label}
      onClick={onClick}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        width: 40,
        height: 40,
        borderRadius: 10,
        border: "none",
        cursor: "pointer",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        background: active ? COLORS.sidebarActive : hover ? COLORS.sidebarHover : "transparent",
        color: active ? COLORS.brand : hover ? COLORS.textPrimary : COLORS.textMuted,
        fontSize: 17,
        transition: "background 0.15s, color 0.15s",
      }}
    >
      {icon}
    </button>
  );
}

function SubmitScreen({ desc, setDesc, claims, setClaims, concepts, onRun }: { desc: string; setDesc: (v: string) => void; claims: string; setClaims: (v: string) => void; concepts: string[]; onRun: () => void }) {
  return (
    <div style={{ flex: 1, overflowY: "auto", padding: "48px 24px" }}>
      <div style={{ maxWidth: 640, margin: "0 auto" }}>
        <div style={{ textAlign: "center", marginBottom: 28 }}>
          <div style={{ fontFamily: "'Space Grotesk', sans-serif", fontWeight: 700, fontSize: 15, color: COLORS.brand, letterSpacing: "0.02em", marginBottom: 10 }}>
            PatentScout
          </div>
          <h1 style={{ color: COLORS.textPrimary, marginBottom: 10, fontSize: 30, fontFamily: "'Space Grotesk', sans-serif" }}>
            AI-Powered Prior-Art Search
          </h1>
          <p style={{ color: COLORS.textSecondary, fontSize: 15, lineHeight: 1.6, margin: "0 auto" }}>
            Describe your idea in plain language — PatentScout expands it into search queries and scores it against real prior art.
          </p>
        </div>

        <div style={{ background: COLORS.card, border: `1px solid ${COLORS.border}`, borderRadius: 14, padding: 24, boxShadow: "0 1px 2px rgba(16,24,25,0.04)" }}>
          <label style={{ display: "block", fontFamily: "'Space Grotesk', sans-serif", fontWeight: 600, fontSize: 13, color: COLORS.textPrimary, letterSpacing: "0.02em", marginBottom: 8 }}>
            Invention description
          </label>
          <textarea
            value={desc}
            onChange={(e) => setDesc(e.target.value)}
            rows={6}
            style={{ width: "100%", background: COLORS.inputBg, border: `1px solid ${COLORS.border}`, borderRadius: 10, padding: "14px 16px", color: COLORS.textPrimary, fontFamily: "'IBM Plex Sans', sans-serif", fontSize: 14, lineHeight: 1.7, resize: "vertical", outline: "none", transition: "border-color 0.15s", boxSizing: "border-box" }}
            onFocus={(e) => (e.currentTarget.style.borderColor = COLORS.brand)}
            onBlur={(e) => (e.currentTarget.style.borderColor = COLORS.border)}
          />

          <label style={{ display: "block", fontFamily: "'Space Grotesk', sans-serif", fontWeight: 600, fontSize: 13, color: COLORS.textPrimary, letterSpacing: "0.02em", marginTop: 20, marginBottom: 8 }}>
            Draft claims · optional
          </label>
          <textarea
            value={claims}
            onChange={(e) => setClaims(e.target.value)}
            rows={3}
            placeholder="Paste any draft independent claims to sharpen the search…"
            style={{ width: "100%", background: COLORS.inputBg, border: `1px solid ${COLORS.border}`, borderRadius: 10, padding: "14px 16px", color: COLORS.textPrimary, fontFamily: "'IBM Plex Sans', sans-serif", fontSize: 14, lineHeight: 1.7, resize: "vertical", outline: "none", transition: "border-color 0.15s", boxSizing: "border-box" }}
            onFocus={(e) => (e.currentTarget.style.borderColor = COLORS.brand)}
            onBlur={(e) => (e.currentTarget.style.borderColor = COLORS.border)}
          />

          <div style={{ display: "flex", gap: 8, marginTop: 16, flexWrap: "wrap" }}>
            {concepts.map((tag) => (
              <span key={tag} style={{ padding: "4px 12px", borderRadius: 20, background: COLORS.brandTint, border: `1px solid ${COLORS.brandTintBorder}`, color: COLORS.brand, fontFamily: "'Space Grotesk', sans-serif", fontWeight: 600, fontSize: 12 }}>
                {tag}
              </span>
            ))}
            <span style={{ padding: "4px 12px", borderRadius: 20, background: "transparent", border: `1px dashed ${COLORS.border}`, color: COLORS.textMuted, fontFamily: "'Space Grotesk', sans-serif", fontWeight: 600, fontSize: 12, cursor: "pointer" }}>
              + detected concepts
            </span>
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: 16, marginTop: 24 }}>
            <button
              onClick={onRun}
              style={{ padding: "12px 24px", borderRadius: 10, border: "none", background: COLORS.brand, color: "#FFFFFF", fontFamily: "'Space Grotesk', sans-serif", fontWeight: 700, fontSize: 15, cursor: "pointer", boxShadow: "0 1px 2px rgba(21,128,88,0.3)", transition: "background 0.15s" }}
              onMouseEnter={(e) => {
                (e.currentTarget as HTMLButtonElement).style.background = COLORS.brandDark;
              }}
              onMouseLeave={(e) => {
                (e.currentTarget as HTMLButtonElement).style.background = COLORS.brand;
              }}
            >
              Run PatentScout →
            </button>
            <span style={{ color: COLORS.textMuted, fontSize: 13 }}>Typical result in under 60 seconds</span>
          </div>
        </div>
      </div>
    </div>
  );
}

function RunningScreen({
  phaseIdx,
  liveStats,
}: {
  phaseIdx: number;
  liveStats: { queries?: string[]; found?: number; filtered?: number; fullText?: number; dropped?: number };
}) {
  // Map the real furthest-reached backend phase onto its high-level display
  // stage. -1 (nothing reported yet) stays -1, so every stage starts pending.
  const stageIdx = phaseIdx >= 0 ? (PHASE_TO_STAGE[PHASE_ORDER[phaseIdx]] ?? -1) : -1;

  // Real pipeline numbers, appearing one at a time as their phase actually
  // reports them - this is the transparency layer the 5-stage checklist
  // above deliberately doesn't show (it's high-level on purpose; this row is
  // where the real backend counts are visible while the run is in flight).
  const funnel = [
    { label: "Queries", value: liveStats.queries?.length },
    { label: "Found", value: liveStats.found },
    { label: "Filtered", value: liveStats.filtered },
    { label: "Full text", value: liveStats.fullText },
    { label: "Dropped", value: liveStats.dropped },
  ].filter((stat) => stat.value !== undefined);

  return (
    <div style={{ flex: 1, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", padding: 48 }}>
      <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 11, color: COLORS.brand, letterSpacing: "0.14em", marginBottom: 16, display: "flex", alignItems: "center", gap: 6 }}>
        <span style={{ display: "inline-block", width: 7, height: 7, borderRadius: "50%", background: COLORS.brand, animation: "pulse 1.2s ease-in-out infinite" }} />
        WORKING
      </div>
      <style>{`@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }`}</style>
      <h2 style={{ color: COLORS.textPrimary, marginBottom: 10, fontFamily: "'Space Grotesk',sans-serif" }}>Analyzing your invention</h2>
      <p style={{ color: COLORS.textSecondary, fontSize: 14, marginBottom: 36 }}>
        This can take a couple of minutes — several uncached LLM and embedding calls run in sequence.
      </p>

      <div style={{ width: "100%", maxWidth: 520, background: COLORS.card, border: `1px solid ${COLORS.border}`, borderRadius: 12, padding: "20px 24px", boxShadow: "0 1px 2px rgba(16,24,25,0.04)" }}>
        {DISPLAY_STAGES.map((stage, i) => {
          const done = i < stageIdx;
          const active = i === stageIdx;
          return (
            <div key={stage.label} style={{ display: "flex", alignItems: "center", gap: 12, padding: "10px 0", borderBottom: i < DISPLAY_STAGES.length - 1 ? `1px solid ${COLORS.border}` : "none" }}>
              <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 14, color: done ? COLORS.brand : active ? COLORS.textPrimary : COLORS.textMuted, width: 16, flexShrink: 0 }}>
                {done ? "✔" : active ? "▸" : "·"}
              </span>
              <span style={{ fontFamily: "'IBM Plex Sans', sans-serif", fontSize: 14, color: done ? COLORS.brand : active ? COLORS.textPrimary : COLORS.textMuted }}>
                {stage.label}
              </span>
            </div>
          );
        })}
      </div>

      {funnel.length > 0 && (
        <div style={{ width: "100%", maxWidth: 520, marginTop: 20, display: "flex", flexWrap: "wrap", justifyContent: "center", gap: 28 }}>
          {funnel.map((stat) => (
            <div key={stat.label} style={{ textAlign: "center" }}>
              <div style={{ fontFamily: "'JetBrains Mono', monospace", fontWeight: 700, fontSize: 20, color: COLORS.brand, lineHeight: 1.2 }}>
                {stat.value}
              </div>
              <div style={{ fontSize: 11, color: COLORS.textMuted }}>{stat.label}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function ReportScreen({ onBack, data }: { onBack: () => void; data: AnalysisReport | null }) {
  if (!data) return null;

  const topPatent = data.patents[0];
  const displayRiskLabel = topPatent ? topPatent.risk.toUpperCase() : "LOW";
  const displayRiskColor = topPatent ? riskColor(topPatent.risk).text : COLORS.riskLowText;

  return (
    <div style={{ flex: 1, overflowY: "auto" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 14, padding: "14px 32px", background: COLORS.card, borderBottom: `1px solid ${COLORS.border}`, position: "sticky", top: 0, zIndex: 10 }}>
        <button onClick={onBack} style={{ background: COLORS.inputBg, border: `1px solid ${COLORS.border}`, borderRadius: 8, color: COLORS.textSecondary, fontFamily: "'Space Grotesk', sans-serif", fontWeight: 600, fontSize: 16, cursor: "pointer", padding: "4px 12px", lineHeight: 1.4 }}>
          ‹
        </button>
        <div style={{ flex: 1 }}>
          <div style={{ fontFamily: "'Space Grotesk', sans-serif", fontWeight: 600, fontSize: 15, color: COLORS.textPrimary, lineHeight: 1.2 }}>
            {data.title}
          </div>
          <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 10, color: COLORS.textMuted, letterSpacing: "0.06em", marginTop: 2 }}>
            Submitted {data.date} · {data.query_count} queries · {data.patents.length} patents scanned
          </div>
        </div>
        <button style={{ background: COLORS.inputBg, border: `1px solid ${COLORS.border}`, borderRadius: 8, color: COLORS.textSecondary, fontFamily: "'Space Grotesk', sans-serif", fontWeight: 600, fontSize: 13, cursor: "pointer", padding: "7px 16px" }}>
          View JSON
        </button>
        <button
          onClick={() => exportReportPdf(data)}
          style={{ background: COLORS.brand, border: "none", borderRadius: 8, color: "#FFFFFF", fontFamily: "'Space Grotesk', sans-serif", fontWeight: 700, fontSize: 13, cursor: "pointer", padding: "7px 16px" }}
        >
          Export PDF
        </button>
      </div>

      <div style={{ padding: "32px 32px 48px" }}>
        <div style={{ marginBottom: 24 }}>
          {/* Score Circle Card */}
          <div style={{ background: COLORS.card, border: `1px solid ${COLORS.border}`, borderRadius: 12, padding: "28px 28px 24px", boxShadow: "0 1px 2px rgba(16,24,25,0.04)" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 28, marginBottom: 20 }}>
              <div style={{ position: "relative", width: 100, height: 100, flexShrink: 0 }}>
                <svg width="100" height="100" style={{ transform: "rotate(-90deg)" }}>
                  <circle cx="50" cy="50" r="40" fill="none" stroke={COLORS.border} strokeWidth="10" />
                  <circle
                    cx="50"
                    cy="50"
                    r="40"
                    fill="none"
                    stroke={displayRiskColor}
                    strokeWidth="10"
                    strokeDasharray={`${2 * Math.PI * 40 * (data.top_score / 100)} ${2 * Math.PI * 40 * (1 - data.top_score / 100)}`}
                    strokeLinecap="round"
                  />
                </svg>
                <div style={{ position: "absolute", inset: 0, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: 1 }}>
                  <span style={{ fontFamily: "'JetBrains Mono', monospace", fontWeight: 700, fontSize: 22, color: displayRiskColor, lineHeight: 1 }}>
                    {data.top_score}%
                  </span>
                  <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 10, color: displayRiskColor, letterSpacing: "0.1em" }}>
                    {displayRiskLabel}
                  </span>
                </div>
              </div>
              <div>
                <div style={{ fontFamily: "'Space Grotesk', sans-serif", fontWeight: 600, fontSize: 15, color: COLORS.textPrimary, marginBottom: 4 }}>
                  Closest prior-art similarity
                </div>
                <div style={{ color: COLORS.textSecondary, fontSize: 13, lineHeight: 1.5 }}>
                  {topPatent ? `Top match: ${topPatent.num}` : "No matching prior art indexed"}
                </div>
              </div>
            </div>

            {data.concepts.length > 0 && (
              <div style={{ borderTop: `1px solid ${COLORS.border}`, paddingTop: 16, marginBottom: 16 }}>
                <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 10, color: COLORS.textMuted, letterSpacing: "0.1em", marginBottom: 10 }}>
                  DETECTED CONCEPTS
                </div>
                <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                  {data.concepts.map((concept) => (
                    <span key={concept} style={{ padding: "4px 12px", borderRadius: 20, background: COLORS.brandTint, border: `1px solid ${COLORS.brandTintBorder}`, color: COLORS.brand, fontFamily: "'Space Grotesk', sans-serif", fontWeight: 600, fontSize: 12 }}>
                      {concept}
                    </span>
                  ))}
                </div>
              </div>
            )}

            {data.queries.length > 0 && (
              <div style={{ borderTop: `1px solid ${COLORS.border}`, paddingTop: 16, marginBottom: 16 }}>
                <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 10, color: COLORS.textMuted, letterSpacing: "0.1em", marginBottom: 10 }}>
                  SEARCH QUERIES USED ({data.queries.length})
                </div>
                <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
                  {data.queries.map((query, i) => (
                    <div key={i} style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
                      <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 10, color: COLORS.textMuted, flexShrink: 0 }}>
                        {String(i + 1).padStart(2, "0")}
                      </span>
                      <span style={{ fontSize: 12.5, color: COLORS.textSecondary }}>{query}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            <div style={{ borderTop: `1px solid ${COLORS.border}`, paddingTop: 16, display: "flex", alignItems: "center", gap: 8 }}>
              <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 10, color: COLORS.textMuted, letterSpacing: "0.1em" }}>NOVELTY VERDICT:</span>
              <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 11, color: verdictColor(data.verdict), letterSpacing: "0.06em", fontWeight: 600 }}>
                {data.verdict}
              </span>
            </div>
          </div>
        </div>

        {/* Dynamic Prior Art List matches */}
        <div style={{ background: COLORS.card, border: `1px solid ${COLORS.border}`, borderRadius: 12, marginBottom: 20, overflow: "hidden", boxShadow: "0 1px 2px rgba(16,24,25,0.04)" }}>
          <div style={{ padding: "16px 24px", borderBottom: `1px solid ${COLORS.border}`, display: "flex", alignItems: "center", gap: 10 }}>
            <span style={{ fontFamily: "'Space Grotesk', sans-serif", fontWeight: 700, fontSize: 15, color: COLORS.textPrimary }}>Prior-art matches</span>
            <span style={{ padding: "2px 8px", borderRadius: 20, background: COLORS.brandTint, fontFamily: "'JetBrains Mono', monospace", fontSize: 11, color: COLORS.brand }}>
              {data.patents.length}
            </span>
          </div>
          {data.patents.map((p, i) => {
            const c = riskColor(p.risk);
            return (
              <div key={i} style={{ display: "flex", alignItems: "center", gap: 20, padding: "16px 24px", borderBottom: i < data.patents.length - 1 ? `1px solid ${COLORS.border}` : "none" }}>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 3 }}>
                    <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 11, color: COLORS.textMuted, letterSpacing: "0.05em", flexShrink: 0 }}>{p.num}</span>
                    <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 10, color: COLORS.brand, letterSpacing: "0.04em", flexShrink: 0 }}>{p.source}</span>
                    <span style={{ fontFamily: "'Space Grotesk', sans-serif", fontWeight: 600, fontSize: 14, color: COLORS.textPrimary, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {p.title}
                    </span>
                  </div>
                  <div style={{ fontSize: 12, color: COLORS.textSecondary, lineHeight: 1.5 }}>{p.rationale}</div>
                </div>

                <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 6, flexShrink: 0, minWidth: 100 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <span style={{ fontFamily: "'JetBrains Mono', monospace", fontWeight: 700, fontSize: 15, color: c.text }}>{p.pct}%</span>
                    <span style={{ padding: "2px 8px", borderRadius: 6, background: c.bg, color: c.text, fontFamily: "'JetBrains Mono', monospace", fontSize: 10, letterSpacing: "0.08em", fontWeight: 600 }}>
                      {p.risk.toUpperCase()}
                    </span>
                  </div>
                  <div style={{ width: 100, height: 4, background: COLORS.inputBg, borderRadius: 2, overflow: "hidden" }}>
                    <div style={{ height: "100%", width: `${p.pct}%`, background: c.text, borderRadius: 2, opacity: 0.75 }} />
                  </div>
                </div>
              </div>
            );
          })}
        </div>

        {/* Dynamic Action Recommendations */}
        <div style={{ background: COLORS.brandTint, border: `1px solid ${COLORS.brandTintBorder}`, borderRadius: 12, padding: "22px 24px" }}>
          <div style={{ fontFamily: "'Space Grotesk', sans-serif", fontWeight: 700, fontSize: 15, color: COLORS.brandDark, marginBottom: 16, display: "flex", alignItems: "center", gap: 8 }}>
            <span>✦</span> How to make it more unique
          </div>
          <ol style={{ margin: 0, padding: 0, listStyle: "none" }}>
    {/* Only ever renders recommendations the backend actually sent. The empty
        case says so plainly -- it must NOT substitute invented advice, which is
        what this block used to do. */}
    {data.recommendations.length > 0 ? (
      data.recommendations.map((rec, i) => (
        <li key={i} style={{ display: "flex", gap: 12, marginBottom: i < data.recommendations.length - 1 ? 12 : 0, fontSize: 13, color: COLORS.textPrimary, lineHeight: 1.6 }}>
          <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 11, color: COLORS.brand, fontWeight: 700, flexShrink: 0, marginTop: 2 }}>
            {i + 1}.
          </span>
          {rec}
        </li>
      ))
    ) : (
      <li style={{ fontSize: 13, color: COLORS.textSecondary, lineHeight: 1.6 }}>
        No recommendations were generated — no prior art was found to distinguish this invention from.
      </li>
    )}
  </ol>
        </div>
      </div>
    </div>
  );
}

function HistoryScreen({ historyList, onSelectReport }: { historyList: AnalysisReport[]; onSelectReport: (report: AnalysisReport) => void }) {
  return (
    <div style={{ flex: 1, overflowY: "auto", padding: "40px 48px" }}>
      <h1 style={{ color: COLORS.textPrimary, marginBottom: 8, fontSize: 26, fontFamily: "'Space Grotesk', sans-serif" }}>Search history</h1>
      <p style={{ color: COLORS.textSecondary, fontSize: 14, marginBottom: 32 }}>Sessions and reports from this workspace, most recent first.</p>

      <div style={{ background: COLORS.card, border: `1px solid ${COLORS.border}`, borderRadius: 12, overflow: "hidden", boxShadow: "0 1px 2px rgba(16,24,25,0.04)" }}>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 140px 120px 100px", padding: "10px 24px", borderBottom: `1px solid ${COLORS.border}`, gap: 16 }}>
          {["INVENTION", "DATE", "TOP MATCH", "NOVELTY"].map((h) => (
            <span key={h} style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 9, color: COLORS.textMuted, letterSpacing: "0.14em" }}>{h}</span>
          ))}
        </div>

        {historyList.length === 0 ? (
          <div style={{ padding: "24px", color: COLORS.textSecondary, fontSize: 13, textAlign: "center" }}>No searches logged.</div>
        ) : (
          historyList.map((row, i) => {
            const vc = verdictColor(row.verdict);
            return (
              <button
                key={i}
                onClick={() => onSelectReport(row)}
                style={{ display: "grid", gridTemplateColumns: "1fr 140px 120px 100px", width: "100%", padding: "14px 24px", background: "transparent", border: "none", borderBottom: i < historyList.length - 1 ? `1px solid ${COLORS.border}` : "none", cursor: "pointer", textAlign: "left", gap: 16, alignItems: "center", transition: "background 0.15s" }}
                onMouseEnter={(e) => (e.currentTarget as HTMLButtonElement).style.background = COLORS.inputBg}
                onMouseLeave={(e) => (e.currentTarget as HTMLButtonElement).style.background = "transparent"}
              >
                <span style={{ fontFamily: "'Space Grotesk', sans-serif", fontWeight: 600, fontSize: 14, color: COLORS.textPrimary, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {row.title}
                </span>
                <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 11, color: COLORS.textMuted, letterSpacing: "0.04em" }}>{row.date}</span>
                <span style={{ fontFamily: "'JetBrains Mono', monospace", fontWeight: 700, fontSize: 14, color: vc }}>{row.top_score}%</span>
                <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 11, color: vc, letterSpacing: "0.06em", fontWeight: 600 }}>{row.verdict}</span>
              </button>
            );
          })
        )}
      </div>
    </div>
  );
}
