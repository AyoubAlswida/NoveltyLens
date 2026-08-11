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
  'enrich',
  'validate',
  'compare',
  'analyze',
] as const;

export type Phase = (typeof PHASE_ORDER)[number];

export const PHASE_LABELS: Record<Phase, string> = {
  understand: 'Extracting technical concepts & keywords…',
  queries: 'Expanding into search queries…',
  search: 'Searching patent databases…',
  enrich: 'Fetching full patent text…',
  validate: 'Checking relevance of candidates…',
  compare: 'Scoring patent similarities…',
  analyze: 'Writing detailed analysis…',
};

export const PHASE_ICONS: Record<Phase, string> = {
  understand: 'Sparkles',
  queries: 'Search',
  search: 'Database',
  enrich: 'FileText',
  validate: 'ShieldCheck',
  compare: 'BarChart3',
  analyze: 'PenLine',
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

export type StreamEvent =
  | { type: 'progress'; phase: Phase; message: string }
  | { type: 'result'; data: AnalysisReport }
  | { type: 'error'; detail: string };
