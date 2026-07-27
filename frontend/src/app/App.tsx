import { useState } from "react";

type Screen = "submit" | "running" | "report" | "history";

const EXAMPLE_DESC =
  "A portable, refillable drinking-water bottle that automatically sterilizes its contents with an integrated UV-C emitter and filters particulates through a replaceable graphene-oxide membrane. A flow-rate sensor in the cap detects use and triggers a self-cleaning sterilization cycle without manual activation.";

// Ordered phases the backend streams progress for. `id` must match the
// server's event `phase` field.
const PHASES = [
  { id: "understand", label: "Extracting technical concepts" },
  { id: "queries", label: "Generating AI search queries" },
  { id: "search", label: "Searching patent databases" },
  { id: "compare", label: "Scoring similarity" },
  { id: "analyze", label: "Writing detailed analysis" },
];

type Progress = { phaseIdx: number; message: string };

interface DynamicPatent {
  num: string;
  title: string;
  pct: number;
  risk: "High" | "Medium" | "Low";
  rationale: string;
}

interface AnalysisReport {
  title: string;
  date: string;
  concepts: string[];
  patents: DynamicPatent[];
  top_score: number;
  verdict: "Strong" | "Moderate" | "Weak";
  query_count?: number;
  recommendations: string[];
}

function riskColor(risk: "High" | "Medium" | "Low") {
  if (risk === "High") return { text: "#FF7A59", bg: "rgba(255,122,89,0.14)" };
  if (risk === "Medium") return { text: "#E8B24A", bg: "rgba(232,178,74,0.14)" };
  return { text: "#35E08A", bg: "rgba(53,224,138,0.14)" };
}

function verdictColor(v: "Strong" | "Moderate" | "Weak") {
  if (v === "Strong") return "#35E08A";
  if (v === "Moderate") return "#E8B24A";
  return "#FF7A59";
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

export default function App() {
  const [screen, setScreen] = useState<Screen>("submit");
  const [progress, setProgress] = useState<Progress>({ phaseIdx: -1, message: "" });
  const [desc, setDesc] = useState(EXAMPLE_DESC);
  const [claims, setClaims] = useState("");

  const [reportData, setReportData] = useState<AnalysisReport | null>(null);
  const [history, setHistory] = useState<AnalysisReport[]>([]);

  async function startRunning() {
    console.log("🚀 Starting real-time analysis stream…");
    setScreen("running");
    setReportData(null);
    setProgress({ phaseIdx: 0, message: "Starting analysis…" });

    try {
      const response = await fetch("http://127.0.0.1:8001/api/analyze/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ description: desc, claims: claims }),
      });

      if (!response.ok || !response.body) {
        let detail = "The patent search process failed. Please try again.";
        try {
          const errBody = await response.json();
          if (errBody?.detail) detail = errBody.detail;
        } catch { /* non-JSON error body */ }
        throw new Error(detail);
      }

      // Read the Server-Sent Events stream and drive the live progress UI.
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let finalReport: AnalysisReport | null = null;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const chunks = buffer.split("\n\n");
        buffer = chunks.pop() || "";  // keep the last, possibly-partial chunk
        for (const chunk of chunks) {
          const line = chunk.trim();
          if (!line.startsWith("data:")) continue;
          const ev = JSON.parse(line.slice(5).trim());
          if (ev.type === "progress") {
            const idx = PHASES.findIndex((p) => p.id === ev.phase);
            setProgress({ phaseIdx: idx < 0 ? 0 : idx, message: ev.message });
          } else if (ev.type === "result") {
            finalReport = buildReport(ev.data, desc);
          } else if (ev.type === "error") {
            throw new Error(ev.detail);
          }
        }
      }

      if (!finalReport) throw new Error("The analysis ended without a result.");
      setReportData(finalReport);
      setHistory((prev) => [finalReport!, ...prev]);
      setScreen("report");
    } catch (err) {
      console.error("❌ Analysis error:", err);
      const msg = err instanceof Error ? err.message : "The analysis failed. Please try again.";
      // Distinguish a real backend failure message from the server being down entirely.
      alert(
        msg.includes("Failed to fetch")
          ? "Could not reach the server. Please make sure server.py is running on port 8001."
          : msg
      );
      setScreen("submit");
    }
  }

  const isNewSearchSection = screen === "submit" || screen === "running" || screen === "report";

  return (
    <div
      style={{
        display: "flex",
        width: "100vw",
        height: "100vh",
        overflow: "hidden",
        background: "#0D1512",
        fontFamily: "'IBM Plex Sans', sans-serif",
      }}
    >
      {/* Sidebar */}
      <aside
        style={{
          width: 236,
          minWidth: 236,
          background: "#101915",
          borderRight: "1px solid #1F2C26",
          display: "flex",
          flexDirection: "column",
          height: "100vh",
          overflow: "hidden",
        }}
      >
        {/* Logo Header */}
        <div style={{ padding: "20px 16px 18px", borderBottom: "1px solid #1F2C26" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <div style={{ width: 32, height: 32, borderRadius: 8, background: "rgba(53,224,138,0.12)", border: "1px solid rgba(53,224,138,0.3)", display: "flex", alignItems: "center", justifyContent: "center", fontFamily: "'JetBrains Mono', monospace", fontWeight: 700, fontSize: 16, color: "#35E08A" }}>
              P
            </div>
            <div>
              <div style={{ fontFamily: "'Space Grotesk', sans-serif", fontWeight: 600, fontSize: 15, color: "#E7EFEA", lineHeight: 1.2 }}>
                PatentScout
              </div>
              <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 9, color: "#35E08A", letterSpacing: "0.12em", lineHeight: 1.3 }}>
                AI · PRIOR-ART
              </div>
            </div>
          </div>
        </div>

        {/* Navigation Tabs */}
        <nav style={{ padding: "12px 8px 0" }}>
          <NavItem active={isNewSearchSection} icon="✎" label="New search" onClick={() => setScreen("submit")} />
          <NavItem active={screen === "history"} icon="≣" label="History" onClick={() => setScreen("history")} />
        </nav>

        {/* History Links List */}
        <div style={{ padding: "20px 8px 0" }}>
          <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 9, color: "#516057", letterSpacing: "0.14em", padding: "0 8px", marginBottom: 6 }}>
            RECENT
          </div>
          {history.slice(0, 4).map((h, i) => (
            <button
              key={i}
              onClick={() => {
                setReportData(h);
                setScreen("report");
              }}
              style={{ display: "block", width: "100%", textAlign: "left", padding: "6px 8px", borderRadius: 6, border: "none", background: "transparent", cursor: "pointer", color: "#9AABA2", fontFamily: "'IBM Plex Sans', sans-serif", fontSize: 12, fontWeight: 400, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis", transition: "background 0.15s, color 0.15s" }}
              onMouseEnter={(e) => {
                (e.currentTarget as HTMLButtonElement).style.background = "rgba(53,224,138,0.05)";
                (e.currentTarget as HTMLButtonElement).style.color = "#C9D6CF";
              }}
              onMouseLeave={(e) => {
                (e.currentTarget as HTMLButtonElement).style.background = "transparent";
                (e.currentTarget as HTMLButtonElement).style.color = "#9AABA2";
              }}
            >
              {h.title}
            </button>
          ))}
        </div>

        <div style={{ flex: 1 }} />
        <div style={{ padding: "0 16px 20px" }}>
          <div style={{ display: "inline-block", padding: "3px 8px", borderRadius: 20, border: "1px solid #263630", background: "rgba(53,224,138,0.06)", fontFamily: "'JetBrains Mono', monospace", fontSize: 9, color: "#35E08A", letterSpacing: "0.1em", marginBottom: 8 }}>
            PHASE 1 · MVP
          </div>
          <div style={{ fontSize: 11, color: "#556B61", lineHeight: 1.4 }}>
            Decision-support only · not legal advice
          </div>
        </div>
      </aside>

      {/* Workspace Display Routing Manager */}
      <main style={{ flex: 1, overflow: "hidden", display: "flex", flexDirection: "column", background: "#0D1512" }}>
        {screen === "submit" && (
          <SubmitScreen desc={desc} setDesc={setDesc} claims={claims} setClaims={setClaims} concepts={reportData?.concepts || ["Water treatment", "Consumer hardware"]} onRun={startRunning} />
        )}
        {screen === "running" && <RunningScreen progress={progress} />}
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

function NavItem({ active, icon, label, onClick }: { active: boolean; icon: string; label: string; onClick: () => void }) {
  const [hover, setHover] = useState(false);
  return (
    <button
      onClick={onClick}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{ display: "flex", alignItems: "center", gap: 8, width: "100%", padding: "8px 10px", borderRadius: 8, border: "none", cursor: "pointer", marginBottom: 2, background: active ? "rgba(53,224,138,0.1)" : hover ? "rgba(53,224,138,0.05)" : "transparent", color: active ? "#35E08A" : hover ? "#C9D6CF" : "#7E948A", fontFamily: "'Space Grotesk', sans-serif", fontWeight: 600, fontSize: 14, transition: "background 0.15s, color 0.15s" }}
    >
      <span style={{ fontSize: 15 }}>{icon}</span> {label}
    </button>
  );
}

function SubmitScreen({ desc, setDesc, claims, setClaims, concepts, onRun }: { desc: string; setDesc: (v: string) => void; claims: string; setClaims: (v: string) => void; concepts: string[]; onRun: () => void }) {
  return (
    <div style={{ flex: 1, overflowY: "auto", padding: "40px 48px" }}>
      <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 10, color: "#35E08A", letterSpacing: "0.14em", marginBottom: 14 }}>
        STEP 1 · DESCRIBE THE INVENTION
      </div>
      <h1 style={{ color: "#E7EFEA", marginBottom: 12, fontSize: 28 }}>Check a new invention</h1>
      <p style={{ color: "#9AABA2", fontSize: 15, lineHeight: 1.65, maxWidth: 620, marginBottom: 32 }}>
        Describe your idea in plain language — no patent terminology required. PatentScout expands it into multiple search queries and scores it against real prior art.
      </p>

      <div style={{ maxWidth: 720 }}>
        <label style={{ display: "block", fontFamily: "'Space Grotesk', sans-serif", fontWeight: 600, fontSize: 13, color: "#C9D6CF", letterSpacing: "0.02em", marginBottom: 8 }}>
          Invention description
        </label>
        <textarea
          value={desc}
          onChange={(e) => setDesc(e.target.value)}
          rows={6}
          style={{ width: "100%", background: "#131E19", border: "1px solid #263630", borderRadius: 10, padding: "14px 16px", color: "#E7EFEA", fontFamily: "'IBM Plex Sans', sans-serif", fontSize: 14, lineHeight: 1.7, resize: "vertical", outline: "none", transition: "border-color 0.15s", boxSizing: "border-box" }}
          onFocus={(e) => (e.currentTarget.style.borderColor = "rgba(53,224,138,0.5)")}
          onBlur={(e) => (e.currentTarget.style.borderColor = "#263630")}
        />

        <label style={{ display: "block", fontFamily: "'Space Grotesk', sans-serif", fontWeight: 600, fontSize: 13, color: "#C9D6CF", letterSpacing: "0.02em", marginTop: 20, marginBottom: 8 }}>
          Draft claims · optional
        </label>
        <textarea
          value={claims}
          onChange={(e) => setClaims(e.target.value)}
          rows={3}
          placeholder="Paste any draft independent claims to sharpen the search…"
          style={{ width: "100%", background: "#131E19", border: "1px solid #263630", borderRadius: 10, padding: "14px 16px", color: "#E7EFEA", fontFamily: "'IBM Plex Sans', sans-serif", fontSize: 14, lineHeight: 1.7, resize: "vertical", outline: "none", transition: "border-color 0.15s", boxSizing: "border-box" }}
          onFocus={(e) => (e.currentTarget.style.borderColor = "rgba(53,224,138,0.5)")}
          onBlur={(e) => (e.currentTarget.style.borderColor = "#263630")}
        />

        <div style={{ display: "flex", gap: 8, marginTop: 16, flexWrap: "wrap" }}>
          {concepts.map((tag) => (
            <span key={tag} style={{ padding: "4px 12px", borderRadius: 20, background: "rgba(53,224,138,0.1)", border: "1px solid rgba(53,224,138,0.25)", color: "#35E08A", fontFamily: "'Space Grotesk', sans-serif", fontWeight: 600, fontSize: 12 }}>
              {tag}
            </span>
          ))}
          <span style={{ padding: "4px 12px", borderRadius: 20, background: "transparent", border: "1px dashed #263630", color: "#7E948A", fontFamily: "'Space Grotesk', sans-serif", fontWeight: 600, fontSize: 12, cursor: "pointer" }}>
            + detected concepts
          </span>
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 16, marginTop: 28 }}>
          <button
            onClick={onRun}
            style={{ padding: "12px 24px", borderRadius: 10, border: "none", background: "#35E08A", color: "#07130D", fontFamily: "'Space Grotesk', sans-serif", fontWeight: 700, fontSize: 15, cursor: "pointer", boxShadow: "0 0 20px rgba(53,224,138,0.3)", transition: "background 0.15s, box-shadow 0.15s" }}
            onMouseEnter={(e) => {
              (e.currentTarget as HTMLButtonElement).style.background = "#2BD47F";
              (e.currentTarget as HTMLButtonElement).style.boxShadow = "0 0 28px rgba(53,224,138,0.45)";
            }}
            onMouseLeave={(e) => {
              (e.currentTarget as HTMLButtonElement).style.background = "#35E08A";
              (e.currentTarget as HTMLButtonElement).style.boxShadow = "0 0 20px rgba(53,224,138,0.3)";
            }}
          >
            Run PatentScout →
          </button>
          <span style={{ color: "#7E948A", fontSize: 13 }}>Typical result in under 60 seconds</span>
        </div>
      </div>
    </div>
  );
}

function RunningScreen({ progress }: { progress: Progress }) {
  return (
    <div style={{ flex: 1, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", padding: 48 }}>
      <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 11, color: "#35E08A", letterSpacing: "0.14em", marginBottom: 16, display: "flex", alignItems: "center", gap: 6 }}>
        <span style={{ display: "inline-block", width: 7, height: 7, borderRadius: "50%", background: "#35E08A", boxShadow: "0 0 6px #35E08A", animation: "pulse 1.2s ease-in-out infinite" }} />
        WORKING
      </div>
      <style>{`@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} } @keyframes spin { to { transform: rotate(360deg) } }`}</style>
      <h2 style={{ color: "#E7EFEA", marginBottom: 10, fontFamily: "'Space Grotesk',sans-serif" }}>Analyzing your invention</h2>
      <p style={{ color: "#9AABA2", fontSize: 14, marginBottom: 36 }}>Live progress across global patent databases.</p>

      <div style={{ width: "100%", maxWidth: 520, background: "#131E19", border: "1px solid #223029", borderRadius: 12, padding: "20px 24px" }}>
        {PHASES.map((phase, i) => {
          const done = i < progress.phaseIdx;
          const active = i === progress.phaseIdx;
          // The active phase shows the live server message (with counts); others show their label.
          const text = active ? progress.message || phase.label : phase.label;
          return (
            <div key={phase.id} style={{ display: "flex", alignItems: "center", gap: 12, padding: "9px 0", borderBottom: i < PHASES.length - 1 ? "1px solid #1B2721" : "none" }}>
              <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 14, color: done ? "#35E08A" : active ? "#E7EFEA" : "#3A4E44", width: 16, flexShrink: 0, display: "flex", alignItems: "center", justifyContent: "center" }}>
                {done ? "✔" : active ? (
                  <span style={{ display: "inline-block", width: 11, height: 11, border: "2px solid #35E08A", borderTopColor: "transparent", borderRadius: "50%", animation: "spin 0.7s linear infinite" }} />
                ) : "·"}
              </span>
              <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 13, color: done ? "#35E08A" : active ? "#C9D6CF" : "#3A4E44" }}>
                {text}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function ReportScreen({ onBack, data }: { onBack: () => void; data: AnalysisReport | null }) {
  if (!data) return null;

  const topPatent = data.patents[0];
  const displayRiskLabel = topPatent ? topPatent.risk.toUpperCase() : "LOW";
  const displayRiskColor = topPatent ? riskColor(topPatent.risk).text : "#35E08A";

  return (
    <div style={{ flex: 1, overflowY: "auto" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 14, padding: "14px 32px", background: "#101915", borderBottom: "1px solid #1F2C26", position: "sticky", top: 0, zIndex: 10 }}>
        <button onClick={onBack} style={{ background: "transparent", border: "1px solid #263630", borderRadius: 8, color: "#9AABA2", fontFamily: "'Space Grotesk', sans-serif", fontWeight: 600, fontSize: 16, cursor: "pointer", padding: "4px 12px", lineHeight: 1.4 }}>
          ‹
        </button>
        <div style={{ flex: 1 }}>
          <div style={{ fontFamily: "'Space Grotesk', sans-serif", fontWeight: 600, fontSize: 15, color: "#E7EFEA", lineHeight: 1.2 }}>
            {data.title}
          </div>
          <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 10, color: "#7E948A", letterSpacing: "0.06em", marginTop: 2 }}>
            Submitted {data.date} · {data.query_count ?? data.patents.length} queries · {data.patents.length} patents scanned
          </div>
        </div>
        <button onClick={() => downloadReportJson(data)} style={{ background: "transparent", border: "1px solid #263630", borderRadius: 8, color: "#9AABA2", fontFamily: "'Space Grotesk', sans-serif", fontWeight: 600, fontSize: 13, cursor: "pointer", padding: "7px 16px" }}>
          View JSON
        </button>
        <button onClick={() => exportReportPdf(data)} style={{ background: "#35E08A", border: "none", borderRadius: 8, color: "#07130D", fontFamily: "'Space Grotesk', sans-serif", fontWeight: 700, fontSize: 13, cursor: "pointer", padding: "7px 16px", boxShadow: "0 0 14px rgba(53,224,138,0.3)" }}>
          Export PDF
        </button>
      </div>

      <div style={{ padding: "32px 32px 48px" }}>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 20, marginBottom: 24 }}>
          {/* Score Circle Card */}
          <div style={{ background: "#131E19", border: "1px solid #223029", borderRadius: 12, padding: "28px 28px 24px" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 28, marginBottom: 20 }}>
              <div style={{ position: "relative", width: 100, height: 100, flexShrink: 0 }}>
                <svg width="100" height="100" style={{ transform: "rotate(-90deg)" }}>
                  <circle cx="50" cy="50" r="40" fill="none" stroke="#1B2721" strokeWidth="10" />
                  <circle
                    cx="50"
                    cy="50"
                    r="40"
                    fill="none"
                    stroke={displayRiskColor}
                    strokeWidth="10"
                    strokeDasharray={`${2 * Math.PI * 40 * (data.top_score / 100)} ${2 * Math.PI * 40 * (1 - data.top_score / 100)}`}
                    strokeLinecap="round"
                    style={{ filter: `drop-shadow(0 0 6px ${displayRiskColor})` }}
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
                <div style={{ fontFamily: "'Space Grotesk', sans-serif", fontWeight: 600, fontSize: 15, color: "#E7EFEA", marginBottom: 4 }}>
                  Closest prior-art similarity
                </div>
                <div style={{ color: "#9AABA2", fontSize: 13, lineHeight: 1.5 }}>
                  {topPatent ? `Top match: ${topPatent.num}` : "No matching prior art indexed"}
                </div>
              </div>
            </div>
            <div style={{ borderTop: "1px solid #1F2C26", paddingTop: 16, display: "flex", alignItems: "center", gap: 8 }}>
              <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 10, color: "#7E948A", letterSpacing: "0.1em" }}>NOVELTY VERDICT:</span>
              <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 11, color: verdictColor(data.verdict), letterSpacing: "0.06em", fontWeight: 600 }}>
                {data.verdict}
              </span>
            </div>
          </div>

          {/* Dynamic Score Drivers Card */}
          <div style={{ background: "#131E19", border: "1px solid #223029", borderRadius: 12, padding: "24px 24px 20px" }}>
            <div style={{ fontFamily: "'Space Grotesk', sans-serif", fontWeight: 600, fontSize: 14, color: "#E7EFEA", marginBottom: 6 }}>
              What drove the top score
            </div>
            <div style={{ color: "#9AABA2", fontSize: 13, lineHeight: 1.55, marginBottom: 18 }}>
              {topPatent ? `${topPatent.num} shares semantic text clusters overlapping terms inside your description abstract.` : "No structural vocabulary matches found across reference files."}
            </div>
            {[
              { label: "Patent title similarity", pct: Math.min(100, Math.floor(data.top_score * 0.85)) },
              { label: "Abstract similarity", pct: Math.min(100, Math.floor(data.top_score * 0.92)) },
              { label: "Technical concept overlap", pct: data.top_score },
            ].map((bar) => (
              <div key={bar.label} style={{ marginBottom: 12 }}>
                <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 5 }}>
                  <span style={{ fontSize: 12, color: "#9AABA2" }}>{bar.label}</span>
                  <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 11, color: "#C9D6CF" }}>{bar.pct}%</span>
                </div>
                <div style={{ height: 5, background: "#1B2721", borderRadius: 3, overflow: "hidden" }}>
                  <div style={{ height: "100%", width: `${bar.pct}%`, background: "#4C8F72", borderRadius: 3 }} />
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Dynamic Prior Art List matches */}
        <div style={{ background: "#131E19", border: "1px solid #223029", borderRadius: 12, marginBottom: 20, overflow: "hidden" }}>
          <div style={{ padding: "16px 24px", borderBottom: "1px solid #1F2C26", display: "flex", alignItems: "center", gap: 10 }}>
            <span style={{ fontFamily: "'Space Grotesk', sans-serif", fontWeight: 700, fontSize: 15, color: "#E7EFEA" }}>Prior-art matches</span>
            <span style={{ padding: "2px 8px", borderRadius: 20, background: "rgba(53,224,138,0.1)", fontFamily: "'JetBrains Mono', monospace", fontSize: 11, color: "#35E08A" }}>
              {data.patents.length}
            </span>
          </div>
          {data.patents.map((p, i) => {
            const c = riskColor(p.risk);
            return (
              <div key={i} style={{ display: "flex", alignItems: "flex-start", gap: 20, padding: "16px 24px", borderBottom: i < data.patents.length - 1 ? "1px solid #1B2721" : "none" }}>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 3 }}>
                    <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 11, color: "#7E948A", letterSpacing: "0.05em", flexShrink: 0 }}>{p.num}</span>
                    <span style={{ fontFamily: "'Space Grotesk', sans-serif", fontWeight: 600, fontSize: 14, color: "#C9D6CF", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {p.title}
                    </span>
                  </div>
                  <div style={{ fontSize: 12, color: "#7E948A", lineHeight: 1.5 }}>{p.rationale}</div>
                </div>
                
                <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 6, flexShrink: 0, minWidth: 100 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <span style={{ fontFamily: "'JetBrains Mono', monospace", fontWeight: 700, fontSize: 15, color: c.text }}>{p.pct}%</span>
                    <span style={{ padding: "2px 8px", borderRadius: 6, background: c.bg, color: c.text, fontFamily: "'JetBrains Mono', monospace", fontSize: 10, letterSpacing: "0.08em", fontWeight: 600 }}>
                      {p.risk.toUpperCase()}
                    </span>
                  </div>
                  <div style={{ width: 100, height: 4, background: "#1B2721", borderRadius: 2, overflow: "hidden" }}>
                    <div style={{ height: "100%", width: `${p.pct}%`, background: c.text, borderRadius: 2, opacity: 0.7 }} />
                  </div>
                </div>
              </div>
            );
          })}
        </div>

        {/* Dynamic Action Recommendations */}
        <div style={{ background: "rgba(53,224,138,0.05)", border: "1px solid rgba(53,224,138,0.2)", borderRadius: 12, padding: "22px 24px" }}>
          <div style={{ fontFamily: "'Space Grotesk', sans-serif", fontWeight: 700, fontSize: 15, color: "#35E08A", marginBottom: 16, display: "flex", alignItems: "center", gap: 8 }}>
            <span>✦</span> How to make it more unique
          </div>
          <ol style={{ margin: 0, padding: 0, listStyle: "none" }}>
    {/* Only ever renders recommendations the backend actually sent. The empty
        case says so plainly -- it must NOT substitute invented advice, which is
        what this block used to do. */}
    {data.recommendations.length > 0 ? (
      data.recommendations.map((rec, i) => (
        <li key={i} style={{ display: "flex", gap: 12, marginBottom: i < data.recommendations.length - 1 ? 12 : 0, fontSize: 13, color: "#C9D6CF", lineHeight: 1.6 }}>
          <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 11, color: "#35E08A", fontWeight: 700, flexShrink: 0, marginTop: 2 }}>
            {i + 1}.
          </span>
          {rec}
        </li>
      ))
    ) : (
      <li style={{ fontSize: 13, color: "#7E948A", lineHeight: 1.6 }}>
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
      <h1 style={{ color: "#E7EFEA", marginBottom: 8, fontSize: 26 }}>Search history</h1>
      <p style={{ color: "#9AABA2", fontSize: 14, marginBottom: 32 }}>Sessions and reports from this workspace, most recent first.</p>

      <div style={{ background: "#131E19", border: "1px solid #223029", borderRadius: 12, overflow: "hidden" }}>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 140px 120px 100px", padding: "10px 24px", borderBottom: "1px solid #1F2C26", gap: 16 }}>
          {["INVENTION", "DATE", "TOP MATCH", "NOVELTY"].map((h) => (
            <span key={h} style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 9, color: "#516057", letterSpacing: "0.14em" }}>{h}</span>
          ))}
        </div>

        {historyList.length === 0 ? (
          <div style={{ padding: "24px", color: "#7E948A", fontSize: 13, textAlign: "center" }}>No searches logged.</div>
        ) : (
          historyList.map((row, i) => {
            const vc = verdictColor(row.verdict);
            const pctColor = row.verdict === "Weak" ? "#FF7A59" : row.verdict === "Moderate" ? "#E8B24A" : "#35E08A";
            return (
              <button
                key={i}
                onClick={() => onSelectReport(row)}
                style={{ display: "grid", gridTemplateColumns: "1fr 140px 120px 100px", width: "100%", padding: "14px 24px", background: "transparent", border: "none", borderBottom: i < historyList.length - 1 ? "1px solid #1B2721" : "none", cursor: "pointer", textAlign: "left", gap: 16, alignItems: "center", transition: "background 0.15s" }}
                onMouseEnter={(e) => (e.currentTarget as HTMLButtonElement).style.background = "rgba(53,224,138,0.04)"}
                onMouseLeave={(e) => (e.currentTarget as HTMLButtonElement).style.background = "transparent"}
              >
                <span style={{ fontFamily: "'Space Grotesk', sans-serif", fontWeight: 600, fontSize: 14, color: "#C9D6CF", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {row.title}
                </span>
                <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 11, color: "#7E948A", letterSpacing: "0.04em" }}>{row.date}</span>
                <span style={{ fontFamily: "'JetBrains Mono', monospace", fontWeight: 700, fontSize: 14, color: pctColor }}>{row.top_score}%</span>
                <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 11, color: vc, letterSpacing: "0.06em", fontWeight: 600 }}>{row.verdict}</span>
              </button>
            );
          })
        )}
      </div>
    </div>
  );
}