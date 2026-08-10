import { Injectable, inject } from '@angular/core';
import { API_BASE_URL } from '../config';
import { AnalysisReport, HistoryEntry } from '../models/report';
import { AuthService } from './auth';

/**
 * Reads `GET /api/history` -- the runs the backend stored in Postgres before
 * sending each result (backend/graph/pipeline.py's `persist_run`).
 *
 * Nothing is cached client-side and nothing is written client-side: the server
 * is the only record of a past run, so a reload shows exactly what it holds.
 */
@Injectable({ providedIn: 'root' })
export class HistoryService {
  private readonly auth = inject(AuthService);

  async list(): Promise<HistoryEntry[]> {
    const response = await this.auth.authorizedFetch(`${API_BASE_URL}/api/history`);
    if (!response.ok) throw new Error(`History request failed (${response.status}).`);
    return (await response.json()) as HistoryEntry[];
  }
}

/**
 * Turn a history entry into the report shape `ReportScreen` renders, so a past
 * run and a fresh one go through exactly one display path.
 *
 * `title` and `date` are derived here rather than stored: the backend's own
 * values for them are a truncated description and the literal string "Today",
 * which is meaningless once read back weeks later. Nothing else is invented --
 * every other field is what the run actually produced.
 */
export function reportFromHistory(entry: HistoryEntry): AnalysisReport {
  return {
    title: titleFor(entry.description),
    date: new Date(entry.created_at).toLocaleDateString('en-US', {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
    }),
    concepts: entry.concepts ?? [],
    patents: entry.patents ?? [],
    top_score: entry.top_score ?? 0,
    verdict: entry.verdict,
    recommendations: entry.recommendations ?? [],
    query_count: entry.query_count ?? 0,
  };
}

/** Matches the truncation `startRunning` applies to a live run's title. */
export function titleFor(description: string): string {
  return description.length > 50 ? `${description.slice(0, 47)}...` : description;
}
