import { Injectable, inject, signal } from '@angular/core';
import { API_BASE_URL } from '../config';
import { AuthService } from './auth';
import type { StreamEvent, WorkflowGate } from '../models/report';

/**
 * The verified-run API (backend `api/runs.py`, spec §10).
 *
 * Every method goes through `AuthService.authorizedFetch`, which attaches the
 * bearer token and ends the session on any 401. That is the only status the app
 * treats as "your session is gone"; a 404 means the run does not exist *or is
 * not yours*, and the two are deliberately indistinguishable so ids cannot be
 * enumerated.
 *
 * **Every mutating call carries an `idempotency_key`.** The backend refuses a
 * mutation without one (422) rather than generating one, because a
 * server-generated key would make every retry a new mutation — which is exactly
 * what the mechanism exists to prevent. `newKey()` mints one per user action,
 * and a retry of that action deliberately reuses it.
 */

export type RunStatus =
  | 'QUEUED'
  | 'RUNNING'
  | 'WAITING_FOR_REVIEW'
  | 'RESUMING'
  | 'COMPLETED'
  | 'FAILED';

export interface RunSummary {
  run_id: string;
  status: RunStatus;
  current_gate: WorkflowGate | null;
  description: string;
  created_at: string | null;
  updated_at: string | null;
  last_event_id?: number;
  /** Present only on a FAILED run. Already sanitized server-side
   *  (`workflow/redaction.py`), so it is rendered verbatim -- paraphrasing it
   *  would hide exactly the fact that tells the user which quota ran out. */
  failure_code?: string;
  failure_detail?: string;
}

/** The shape every failing response carries (spec §10.2), flattened by the
 *  backend's `ApiError` handler so `code` is at the top level where a client
 *  branches on it. */
export interface ApiFailure {
  code: string;
  detail: string;
  missing?: string[];
  blocking?: Array<Record<string, unknown>>;
}

export class RunApiError extends Error {
  constructor(
    readonly status: number,
    readonly failure: ApiFailure,
  ) {
    super(failure.detail);
  }
}

let keyCounter = 0;

@Injectable({ providedIn: 'root' })
export class RunsService {
  private readonly auth = inject(AuthService);

  /** The last failure, so a screen can render `code` without re-fetching. */
  readonly lastFailure = signal<ApiFailure | null>(null);

  /**
   * A fresh idempotency key. Minted per *user action*, not per request: a retry
   * of the same action reuses its key so the backend replays the stored
   * response instead of mutating twice.
   */
  newKey(): string {
    keyCounter += 1;
    const random = Math.random().toString(36).slice(2, 10);
    return `ui-${Date.now().toString(36)}-${keyCounter}-${random}`;
  }

  private async call<T>(path: string, init: RequestInit = {}): Promise<T> {
    const response = await this.auth.authorizedFetch(`${API_BASE_URL}${path}`, init);
    if (!response.ok) {
      let failure: ApiFailure = {
        code: 'UNKNOWN',
        detail: `The request failed with status ${response.status}.`,
      };
      try {
        const body = await response.json();
        if (body && typeof body === 'object' && 'code' in body) failure = body as ApiFailure;
      } catch {
        // A non-JSON body is not something to guess at. The generic failure
        // above already says the status, which is the honest amount of detail.
      }
      this.lastFailure.set(failure);
      throw new RunApiError(response.status, failure);
    }
    this.lastFailure.set(null);
    if (response.status === 204) return undefined as T;
    return (await response.json()) as T;
  }

  private post<T>(path: string, body: Record<string, unknown>): Promise<T> {
    return this.call<T>(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  }

  // --- reads ---------------------------------------------------------------

  listRuns(): Promise<RunSummary[]> {
    return this.call<RunSummary[]>('/api/runs');
  }

  getRun(runId: string): Promise<RunSummary> {
    return this.call<RunSummary>(`/api/runs/${runId}`);
  }

  /**
   * Admit a verified run.
   *
   * `problemSolved` and `noveltyStatement` are the inventor's optional context.
   * They steer the analysis and are **never** treated as evidence: what an
   * inventor believes is new is the claim the run exists to test.
   */
  createRun(
    description: string,
    context: { problemSolved?: string; noveltyStatement?: string; claims?: string } = {},
  ): Promise<{
    run_id: string;
    status: RunStatus;
    stream_url: string;
    capability_qualified: boolean;
  }> {
    return this.post('/api/runs', {
      description,
      claims: context.claims ?? '',
      problem_solved: context.problemSolved ?? '',
      inventor_novelty_statement: context.noveltyStatement ?? '',
    });
  }

  gateA(runId: string): Promise<Record<string, unknown>> {
    return this.call(`/api/runs/${runId}/gates/A`);
  }

  gateStrategy(runId: string): Promise<Record<string, unknown>> {
    return this.call(`/api/runs/${runId}/gates/STRATEGY`);
  }

  gateB(runId: string): Promise<Record<string, unknown>> {
    return this.call(`/api/runs/${runId}/gates/B`);
  }

  gateC(runId: string): Promise<Record<string, unknown>> {
    return this.call(`/api/runs/${runId}/gates/C`);
  }

  cell(runId: string, cellId: string): Promise<Record<string, unknown>> {
    return this.call(`/api/runs/${runId}/cells/${encodeURIComponent(cellId)}`);
  }

  report(runId: string): Promise<Record<string, unknown>> {
    return this.call(`/api/runs/${runId}/report`);
  }

  // --- mutations -----------------------------------------------------------

  approveGateA(runId: string, decomposition: unknown, key: string) {
    return this.post(`/api/runs/${runId}/gates/A/approve`, {
      decomposition,
      idempotency_key: key,
    });
  }

  /**
   * Ask for a fresh analysis of the invention. **202 — the worker does the work.**
   *
   * `fromVersion` names the proposal the inventor was looking at, so a request
   * composed against a version that has since been replaced is refused rather
   * than silently applied to a different one.
   */
  regenerateUnderstanding(runId: string, fromVersion: number, feedback: string, key: string) {
    return this.post(`/api/runs/${runId}/understanding/regenerate`, {
      from_version: fromVersion,
      feedback,
      idempotency_key: key,
    });
  }

  /**
   * Approve the search strategy: the queries, and which of them run.
   *
   * `strategy` carries every query the reviewer saw, enabled or not, plus the
   * version and the approved-analysis version it covers. **Disabled queries are
   * sent, not filtered out here**: switching one off is a decision about the
   * search, and the record has to be able to say what was deliberately not
   * searched for. Only the enabled ones are ever dispatched, and the backend is
   * what enforces that.
   */
  approveGateStrategy(runId: string, strategy: unknown, key: string) {
    return this.post(`/api/runs/${runId}/gates/STRATEGY/approve`, {
      strategy,
      idempotency_key: key,
    });
  }

  /**
   * Ask for fresh search queries. **202 — the worker does the work.**
   *
   * `fromVersion` names the strategy the reviewer was looking at, so a request
   * composed against a version that has since been replaced is refused rather
   * than silently applied to a different one.
   */
  regenerateStrategy(runId: string, fromVersion: number, feedback: string, key: string) {
    return this.post(`/api/runs/${runId}/strategy/regenerate`, {
      from_version: fromVersion,
      feedback,
      idempotency_key: key,
    });
  }

  approveGateB(runId: string, publicationNumbers: string[], version: number, key: string) {
    return this.post(`/api/runs/${runId}/gates/B/approve`, {
      publication_numbers: publicationNumbers,
      version,
      idempotency_key: key,
    });
  }

  addCandidate(runId: string, publicationNumber: string, key: string) {
    return this.post(`/api/runs/${runId}/candidates`, {
      publication_number: publicationNumber,
      idempotency_key: key,
    });
  }

  reviewCell(runId: string, cellId: string, overlay: unknown, key: string) {
    return this.post(`/api/runs/${runId}/cells/${encodeURIComponent(cellId)}/review`, {
      overlay,
      idempotency_key: key,
    });
  }

  waiveCell(runId: string, cellId: string, waiver: unknown, key: string) {
    return this.post(`/api/runs/${runId}/cells/${encodeURIComponent(cellId)}/waiver`, {
      waiver,
      idempotency_key: key,
    });
  }

  rerunCell(
    runId: string,
    cellId: string,
    contextRequests: unknown[],
    sourceResultVersion: number,
    key: string,
  ) {
    return this.post(`/api/runs/${runId}/cells/${encodeURIComponent(cellId)}/rerun`, {
      context_requests: contextRequests,
      source_result_version: sourceResultVersion,
      idempotency_key: key,
    });
  }

  completeGateC(runId: string, key: string) {
    return this.post(`/api/runs/${runId}/gates/C/complete`, { idempotency_key: key });
  }

  approveGateD(runId: string, reportVersion: number, key: string, comments?: string) {
    return this.post(`/api/runs/${runId}/gates/D/approve`, {
      report_version: reportVersion,
      comments: comments ?? '',
      idempotency_key: key,
    });
  }

  // --- the stream ----------------------------------------------------------

  /**
   * Read a verified run's SSE stream, resuming from `lastEventId`.
   *
   * Raw `fetch` rather than `EventSource`, for the same two reasons
   * `services/analysis.ts` gives: `EventSource` can carry neither a POST body
   * nor an `Authorization` header. The consequence here is that the browser
   * sends **no** `Last-Event-ID` header and performs **no** automatic
   * reconnection, so this tracks the id itself and passes it as a query
   * parameter — which is exactly why the backend accepts both forms.
   *
   * `onEvent` receives every frame; `onId` receives each frame's `id:` so the
   * caller can persist the cursor and reconnect from it after a refresh.
   */
  async stream(
    runId: string,
    lastEventId: number | null,
    onEvent: (event: StreamEvent) => void,
    onId: (id: number) => void,
    signal?: AbortSignal,
  ): Promise<void> {
    const query = lastEventId === null ? '' : `?last_event_id=${lastEventId}`;
    const response = await this.auth.authorizedFetch(
      `${API_BASE_URL}/api/runs/${runId}/stream${query}`,
      { signal },
    );
    if (!response.ok || !response.body) {
      const failure: ApiFailure = {
        code: 'STREAM_UNAVAILABLE',
        detail: `The run stream could not be opened (status ${response.status}).`,
      };
      this.lastFailure.set(failure);
      throw new RunApiError(response.status, failure);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // SSE frames are separated by a blank line. Anything after the last one is
      // a partial frame and stays in the buffer until the rest arrives.
      const frames = buffer.split('\n\n');
      buffer = frames.pop() ?? '';

      for (const frame of frames) {
        let id: number | null = null;
        let data = '';
        for (const line of frame.split('\n')) {
          if (line.startsWith('id: ')) id = Number(line.slice(4).trim());
          else if (line.startsWith('data: ')) data += line.slice(6);
        }
        if (!data) continue;
        if (id !== null && Number.isFinite(id)) onId(id);
        try {
          onEvent(JSON.parse(data) as StreamEvent);
        } catch {
          // A frame that is not JSON is a backend contract violation. Dropping
          // it silently would hide that, so it is surfaced as a stream failure
          // rather than skipped.
          throw new RunApiError(200, {
            code: 'SSE_CONTRACT',
            detail: 'The server sent a frame that was not valid JSON.',
          });
        }
      }
    }
  }
}

/**
 * The active verified run, remembered across a reload — **namespaced by account**.
 *
 * Spec Gate F item 1: there is no router, so a paused run survives a reload only
 * if the client remembers which run it was on. Item 2 is why the key carries the
 * email: two accounts on one browser must not inherit each other's run, and a
 * signed-out browser must not remember one at all.
 *
 * **Never render a run from this key alone.** `restore()` calls
 * `GET /api/runs/{id}` first; a 404 clears the key silently and returns to
 * submit. localStorage is a hint about what to ask for, never a source of truth
 * about what exists or who owns it.
 */
@Injectable({ providedIn: 'root' })
export class ActiveRunStore {
  private readonly auth = inject(AuthService);
  private readonly runs = inject(RunsService);

  private key(email: string): string {
    return `novelty-lens-active-run:${email}`;
  }

  /** Remember the run this account is working on. */
  remember(email: string, runId: string): void {
    try {
      localStorage.setItem(this.key(email), runId);
    } catch {
      // A browser with storage disabled loses resume-after-reload and nothing
      // else. It is not worth failing a working session over.
    }
  }

  /** Forget it. Called on sign-out, on any 401, and on a 404 during restore. */
  forget(email: string): void {
    try {
      localStorage.removeItem(this.key(email));
    } catch {
      /* see remember() */
    }
  }

  /** Forget every account's key. Used when the signed-in email changes. */
  forgetAll(): void {
    try {
      for (let i = localStorage.length - 1; i >= 0; i -= 1) {
        const key = localStorage.key(i);
        if (key?.startsWith('novelty-lens-active-run:')) localStorage.removeItem(key);
      }
    } catch {
      /* see remember() */
    }
  }

  /**
   * The remembered run, **verified against the backend before it is returned**.
   *
   * Returns `null` when there is nothing remembered, when the run no longer
   * exists, or when it belongs to somebody else — the backend answers 404 for
   * the last two identically, and this must not try to tell them apart.
   */
  async restore(email: string): Promise<RunSummary | null> {
    let runId: string | null = null;
    try {
      runId = localStorage.getItem(this.key(email));
    } catch {
      return null;
    }
    if (!runId) return null;

    try {
      return await this.runs.getRun(runId);
    } catch (error) {
      if (error instanceof RunApiError && error.status === 404) {
        this.forget(email);
        return null;
      }
      // A transport failure is not proof the run is gone. The key stays, and the
      // caller shows a reconnecting state rather than silently dropping the run.
      throw error;
    }
  }
}

/**
 * The SSE cursor for one run, so a reconnect asks for exactly what it missed.
 *
 * Kept per run and in memory: it is only meaningful while a stream is open, and
 * persisting it would let a stale cursor outlive a compaction and ask for a
 * replay the server can no longer give. A fresh attach with no cursor gets a
 * `snapshot`, which is the correct answer in that case.
 */
@Injectable({ providedIn: 'root' })
export class StreamCursor {
  private readonly cursors = new Map<string, number>();

  get(runId: string): number | null {
    const value = this.cursors.get(runId);
    return value === undefined ? null : value;
  }

  advance(runId: string, id: number): void {
    const current = this.cursors.get(runId);
    // Monotonic only. A frame that arrived out of order must never move the
    // cursor backwards, or the next reconnect would replay what was already seen.
    if (current === undefined || id > current) this.cursors.set(runId, id);
  }

  clear(runId: string): void {
    this.cursors.delete(runId);
  }
}
