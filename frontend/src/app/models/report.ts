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
  family_id?: string;
  family_size?: number;
  publication_date?: string;
  abstract?: string;
  abstract_language?: string;
  evidence?: {quote: string; element_ids: string[]; relationship: boolean; span_id?: string; start?: number; end?: number}[];
  covered_element_ids?: string[];
  not_mentioned_element_ids?: string[];
}

export interface AnalysisReport {
  title: string;
  date: string;
  concepts: string[];
  patents: PatentMatch[];
  recommendations: string[];
  query_count: number;
  analysis_basis?: 'abstract' | 'full_text';
  analysis_version?: string;
  coverage?: {received: number; pages: number; unique_publications: number; analyzed_abstracts: number; missing_abstracts: number; unavailable_records: number; budget_exhausted: boolean; query_count: number; returned: number; families: number};
  elements?: Record<string, string>;
}

/**
 * The complete frontend progress vocabulary. The two search modes traverse
 * different orders below; this union exists only to type an SSE progress event.
 *
 * This vocabulary must remain aligned with backend/graph/events.py's `PHASES`
 * tuple MINUS `semantic_search` and `fuse`, whose nodes are currently disabled.
 */
export const PHASE_ORDER = [
  'filter',
  'enrich',
  'understand',
  'queries',
  'decompose',
  'gate_a',
  'strategy',
  'gate_strategy',
  'search',
  'expand',
  'compare',
  'validate',
  'analyze',
] as const;

export type Phase = (typeof PHASE_ORDER)[number];
export type SearchMode = 'quick' | 'advanced';

/** The exact phases traversed by POST /api/analyze/stream. */
export const QUICK_SEARCH_PHASE_ORDER: readonly Phase[] = [
  'understand',
  'queries',
  'search',
  'expand',
  'compare',
  'validate',
  'analyze',
];

/** The version-2 Advanced Search route, including both invention-review pauses. */
export const ADVANCED_SEARCH_PHASE_ORDER: readonly Phase[] = [
  'decompose',
  'gate_a',
  'strategy',
  'search',
  'expand',
  'compare',
  'validate',
  'analyze',
];

/** What the running screen shows before the first human review. */
export const ADVANCED_PRE_REVIEW_PHASE_ORDER: readonly Phase[] = ['decompose'];

/** What the running screen shows after invention requirements are approved. */
export const ADVANCED_EXECUTION_PHASE_ORDER: readonly Phase[] = [
  'strategy',
  'search',
  'expand',
  'compare',
  'validate',
  'analyze',
];

export const PHASE_LABELS: Record<Phase, string> = {
  filter: 'Filtering candidates...',
  understand: 'Extracting technical concepts & keywords…',
  queries: 'Expanding into search queries…',
  search: 'Searching patent databases…',
  expand: 'Reviewing retrieval gaps and expanding the search…',
  enrich: 'Fetching full patent text…',
  validate: 'Checking relevance of candidates…',
  compare: 'Scoring patent similarities…',
  analyze: 'Writing detailed analysis…',
  decompose: 'Preparing the technical summary…',
  strategy: 'Planning the search…',
  gate_a: 'Awaiting your review of the understanding…',
  gate_strategy: 'Awaiting your approval of the search queries…',
};

export const PHASE_ICONS: Record<Phase, string> = {
  understand: 'Sparkles',
  queries: 'Search',
  search: 'Database',
  expand: 'Radar',
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
  analysis_basis?: 'abstract' | 'full_text';
  analysis_version?: string;
  coverage?: {received: number; pages: number; unique_publications: number; analyzed_abstracts: number; missing_abstracts: number; unavailable_records: number; budget_exhausted: boolean; query_count: number; returned: number; families: number};
  elements?: Record<string, string>;
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
