/** Mirrors the terminal `result` payload documented in CLAUDE.md. */
export type Risk = 'High' | 'Medium' | 'Low';
export type Verdict = 'Strong' | 'Moderate' | 'Weak';

export interface PatentMatch {
  num: string;
  title: string;
  pct: number;
  risk: Risk;
  rationale: string;
  source: string;
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

/** This order must remain aligned with backend/graph/events.py. */
export const PHASE_ORDER = [
  'understand',
  'queries',
  'search',
  'enrich',
  'semantic_search',
  'fuse',
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
  semantic_search: 'Searching indexed patents by meaning…',
  fuse: 'Combining keyword and semantic results…',
  validate: 'Checking relevance of candidates…',
  compare: 'Scoring patent similarities…',
  analyze: 'Writing detailed analysis…',
};

export const PHASE_ICONS: Record<Phase, string> = {
  understand: 'Sparkles',
  queries: 'Search',
  search: 'Database',
  enrich: 'FileText',
  semantic_search: 'Radar',
  fuse: 'Network',
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
