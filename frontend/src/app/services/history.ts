import { Injectable, inject } from '@angular/core';
import { API_BASE_URL } from '../config';
import { AnalysisReport, HistoryEntry } from '../models/report';
import { AuthService } from './auth';
import { PUBLIC_ERRORS } from '../shared/public-errors';

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
    if (!response.ok) throw new Error(PUBLIC_ERRORS.history);
    return (await response.json()) as HistoryEntry[];
  }

  /**
   * Delete every stored run. Irreversible — there is no undo on either side.
   *
   * Returns the number deleted, so the caller can report what actually
   * happened rather than assuming the request did what it asked for.
   */
  async clear(): Promise<number> {
    const response = await this.auth.authorizedFetch(`${API_BASE_URL}/api/history`, {
      method: 'DELETE',
    });
    if (!response.ok) throw new Error(PUBLIC_ERRORS.clearHistory);
    return ((await response.json()) as { deleted: number }).deleted;
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
    // Normalised field by field rather than passed straight through. The report
    // card reads `patent.key_overlap.length` directly, so a single entry stored
    // before migration 0005 added that column would throw and blank the screen.
    // An empty list is the honest value there: no tags were recorded for that
    // run, which is not the same as the run having found no overlap.
    patents: (entry.patents ?? []).map((patent) => ({
      ...patent,
      key_overlap: patent.key_overlap ?? [],
      url: patent.url ?? '',
    })),
    recommendations: entry.recommendations ?? [],
    query_count: entry.query_count ?? 0,
  };
}

/** Matches the truncation `startRunning` applies to a live run's title. */
export function titleFor(description: string): string {
  return description.length > 50 ? `${description.slice(0, 47)}...` : description;
}
