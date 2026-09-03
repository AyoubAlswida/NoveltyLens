/**
 * Mirrors the terminal `result` payload documented in CLAUDE.md.
 *
 * There is no numeric similarity anywhere in this file, and no overall verdict.
 * `risk` is the only judgement the backend publishes: a per-patent
 * High/Medium/Low rating derived from a score that is discarded before the
 * payload is built. A report with no patents therefore carries no rating at
 * all, and the report screen must not synthesise one.
 */
export type Risk = 'High' | 'Medium' | 'Low';

export interface PatentMatch {
  num: string;
  title: string;
  risk: Risk;
  rationale: string;
  key_overlap: string[];
  source: string;
  url: string;
}

export interface AnalysisReport {
  title: string;
  date: string;
  concepts: string[];
  patents: PatentMatch[];
  recommendations: string[];
  query_count: number;
}

/**
 * This order must remain aligned with backend/graph/events.py's `PHASES` tuple
 * MINUS `semantic_search` and `fuse`. Those two nodes are currently commented
 * out of `graph/pipeline.py` (search runs keyword-only), so the backend never
 * emits those phases - listing them here would let the checklist's
 * `complete: index < activeIndex` logic mark them done once `validate` fires,
 * showing steps as finished that never ran. Re-add both, in this same
 * position, the day `graph/pipeline.py` re-enables those nodes.
 */
export const PHASE_ORDER = [
  'understand',
  'queries',
  'search',
  // Emitted by the live `keyword_search` node on every search run. It was
  // missing here while the backend emitted it, so the checklist skipped a step
  // the run genuinely performed -- `analysis.ts` matches on the phase name and
  // silently drops a name it does not know.
  'filter',
  'enrich',
  'validate',
  'compare',
  'analyze',
  // --- Advanced Search ---
  //
  // Emitted only on an Advanced Search run. A search-only run never sends
  // these, so the checklist renders the eight above exactly as it does today --
  // `analysis.ts` matches on the phase name, and a name that never arrives is
  // simply a step that never lights up.
  //
  // This list must stay in step with `backend/graph/events.py`'s PHASES.
  // The backend refuses to emit a phase outside its own tuple; nothing catches
  // a name that exists there and is missing HERE except a checklist that
  // silently stalls in a browser.
  'decompose',
  // The SEARCH STRATEGY stage: the queries are proposed here, and
  // `gate_strategy` below is the run waiting for a person to approve them.
  'strategy',
  // --- Human-in-the-Loop gates ---
  //
  // Emitted immediately before the run pauses for a person. They belong in the
  // checklist because the run genuinely is at that step; the pause itself
  // arrives as a `review_required` / `paused` event, which the running screen
  // handles separately from the checklist.
  //
  // An Advanced Search run rejoins the ordinary pipeline after `gate_strategy`
  // and ends with it, so the evidence-layer and Gate B/C/D phases that used to
  // follow here were removed along with their nodes.
  'gate_a',
  'gate_strategy',
] as const;

export type Phase = (typeof PHASE_ORDER)[number];

export const PHASE_LABELS: Record<Phase, string> = {
  understand: 'Extracting technical concepts & keywords…',
  queries: 'Expanding into search queries…',
  search: 'Searching patent databases…',
  filter: 'Filtering candidates by title and abstract…',
  enrich: 'Fetching full patent text…',
  validate: 'Checking relevance of candidates…',
  compare: 'Scoring patent similarities…',
  analyze: 'Writing detailed analysis…',
  decompose: 'Proposing a technical summary and elements…',
  strategy: 'Planning the search…',
  gate_a: 'Awaiting your review of the understanding…',
  gate_strategy: 'Awaiting your approval of the search queries…',
};

export const PHASE_ICONS: Record<Phase, string> = {
  understand: 'Sparkles',
  queries: 'Search',
  search: 'Database',
  filter: 'Radar',
  enrich: 'FileText',
  validate: 'ShieldCheck',
  compare: 'BarChart3',
  analyze: 'PenLine',
  decompose: 'Sparkles',
  strategy: 'Search',
  gate_a: 'ShieldCheck',
  gate_strategy: 'ShieldCheck',
};

/**
 * One entry from `GET /api/history` (backend/reports.py's `list_history`).
 *
 * Deliberately NOT an `AnalysisReport`. The backend's `result` event carries a
 * truncated `title` and the literal string `"Today"` as its date -- display
 * values, not stored state -- so a history entry ships the real `description`
 * and a real ISO `created_at` instead, and `services/history.ts` derives the
 * title and date from those the same way a live run does.
 */
export interface HistoryEntry {
  id: string;
  description: string;
  created_at: string;
  concepts: string[];
  patents: PatentMatch[];
  recommendations: string[];
  query_count: number;
}

/**
 * A workflow gate: a point at which a verified run pauses for a person.
 *
 * `STRATEGY` is spelled out where the others are letters because it was added
 * between `A` and `B`; calling it `E` would name a stage that runs second. The
 * letters are internal either way — `gate-shell.ts` maps every one of them to a
 * stage name, and none of them reaches a screen.
 */
export type WorkflowGate = 'A' | 'STRATEGY';

/**
 * What a run row can actually carry, which is wider than what this app renders.
 *
 * `WorkflowGate` is the set of gates with a screen. A run parked before feature
 * verification was withdrawn still holds `B`, `C` or `D` in its `current_gate`,
 * and the backend still returns it -- the rows were kept, not deleted. Typing
 * the wire field as `WorkflowGate` would make the compiler agree that those
 * values cannot occur, and the app would then fall through to a blank review
 * screen instead of saying the run cannot be continued. Section 6.3.
 */
export type StoredGate = WorkflowGate | (string & {});

/**
 * Every frame `GET /api/runs/{id}/stream` can send.
 *
 * The three original members are `POST /api/analyze/stream`'s WHOLE vocabulary
 * and are unchanged: a search-only run sends exactly `progress`, `result` and
 * `error`, with no `id:` line and no replay. The five below it are verified-run
 * only. Keeping one union rather than two is deliberate -- the reader in
 * `services/analysis.ts` is the same code, and a second union would drift.
 *
 * `run_id` is optional for the same reason: the search-only stream has no run to
 * name. It is present on every verified-run frame.
 *
 * Mirrors `backend/workflow/stream.py` and the backend's `workflow_event_type`
 * enum. `snapshot` is deliberately NOT in that enum: it is connection-local,
 * never persisted, and sent only to a client attaching with no `last_event_id`.
 */
export type StreamEvent =
  | { type: 'progress'; phase: Phase; message: string; run_id?: string }
  | { type: 'result'; data: AnalysisReport; run_id?: string }
  | { type: 'error'; detail: string; code?: string; phase?: string; run_id?: string }
  // --- verified runs only (Implementation Gate E) ---
  | {
      type: 'snapshot';
      run_id: string;
      status: string;
      current_gate: StoredGate | null;
      last_event_id: number;
      events_compacted_below: number;
    }
  | {
      type: 'verification_progress';
      run_id: string;
      cell_id: string;
      feature_id: string;
      publication_number: string;
      state: string;
      outcome?: string;
      completed: number;
      total: number;
    }
  | {
      type: 'review_required';
      run_id: string;
      gate: WorkflowGate;
      summary: Record<string, number>;
      payload_url: string;
    }
  /** Ends the CONNECTION, not the run. The run stays WAITING_FOR_REVIEW. */
  | { type: 'paused'; run_id: string; gate: WorkflowGate; status: string }
  | {
      type: 'resumed';
      run_id: string;
      gate: WorkflowGate;
      status: string;
      at: string;
      approved_by: string;
    };
