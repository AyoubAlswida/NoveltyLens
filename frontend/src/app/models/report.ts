/** Mirrors the terminal `result` payload documented in CLAUDE.md. */
export type Risk = 'High' | 'Medium' | 'Low';
export type Verdict = 'Strong' | 'Moderate' | 'Weak';

export interface PatentMatch {
  num: string;
  title: string;
  pct: number;
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
  top_score: number;
  verdict: Verdict;
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
  // --- Verification workflow (Implementation Gate C) ---
  //
  // Emitted only on a run that requested feature verification. A search-only
  // run never sends these, so the checklist renders the seven above exactly as
  // it does today -- `analysis.ts` matches on the phase name, and a name that
  // never arrives is simply a step that never lights up.
  //
  // This list must stay in step with `backend/graph/events.py`'s PHASES.
  // The backend refuses to emit a phase outside its own tuple; nothing catches
  // a name that exists there and is missing HERE except a checklist that
  // silently stalls in a browser.
  'decompose',
  // The SEARCH STRATEGY stage: the queries are proposed here, and
  // `gate_strategy` below is the run waiting for a person to approve them.
  'strategy',
  'normalize',
  'screen_dates',
  'enrich_epo',
  'build_passages',
  'confirm_eligibility',
  'rank_candidates',
  'verify',
  // --- Human-in-the-Loop gates (Implementation Gate D) ---
  //
  // Emitted only on a verified run, immediately before it pauses for a person.
  // They belong in the checklist because the run genuinely is at that step; the
  // pause itself arrives as a `review_required` / `paused` event, which the
  // running screen handles separately from the checklist.
  'gate_a',
  'gate_strategy',
  'gate_b',
  'gate_c',
  'draft_report',
  'gate_d',
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
  decompose: 'Proposing a feature decomposition…',
  strategy: 'Planning the search…',
  normalize: 'Normalizing publication numbers…',
  screen_dates: 'Screening publications against the cutoff…',
  enrich_epo: 'Retrieving full text from EPO…',
  build_passages: 'Building citable passages…',
  confirm_eligibility: 'Confirming which publications can be cited…',
  rank_candidates: 'Preparing candidates for review…',
  verify: 'Verifying features against publications…',
  gate_a: 'Awaiting feature decomposition approval…',
  gate_strategy: 'Awaiting search strategy approval…',
  gate_b: 'Awaiting candidate publication review…',
  gate_c: 'Awaiting evidence and verdict review…',
  draft_report: 'Assembling the draft report…',
  gate_d: 'Awaiting draft report approval…',

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
  normalize: 'Search',
  screen_dates: 'History',
  enrich_epo: 'FileText',
  build_passages: 'FileType2',
  confirm_eligibility: 'ShieldCheck',
  rank_candidates: 'BarChart3',
  verify: 'Radar',
  gate_a: 'ShieldCheck',
  gate_strategy: 'ShieldCheck',
  gate_b: 'ShieldCheck',
  gate_c: 'ShieldCheck',
  draft_report: 'FileText',
  gate_d: 'ShieldCheck',

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
  top_score: number;
  verdict: Verdict;
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
export type WorkflowGate = 'A' | 'STRATEGY' | 'B' | 'C' | 'D';

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
      current_gate: WorkflowGate | null;
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
