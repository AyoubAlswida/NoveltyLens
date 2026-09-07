import { Injectable, inject, signal } from '@angular/core';
import { API_BASE_URL } from '../config';
import { AuthService } from './auth';
import type { AnalysisReport, StoredGate, StreamEvent, WorkflowGate } from '../models/report';
import { PUBLIC_ERRORS } from '../shared/public-errors';

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
  current_gate: StoredGate | null;
  description: string;
  created_at: string | null;
  updated_at: string | null;
  last_event_id?: number;
  /** Present only on a FAILED run. Diagnostic data; never render it directly. */
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
        detail: PUBLIC_ERRORS.general,
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
   * Admit an Advanced Search run.
   *
   * **A description and optionally claims: that is the whole body.** The backend
   * model is strict (`extra="forbid"`), so `problem_solved`,
   * `inventor_novelty_statement` and `cutoff_date` are not merely ignored -- a
   * request carrying one is refused with a 422. That is deliberate on both
   * sides: silently accepting them would drop what the caller sent while
   * reporting success.
   *
   * There is no `capability_qualified` in the response. Advanced Search runs no
   * verifier, so there is no capability for admission to qualify.
   */
  createRun(
    description: string,
    context: { claims?: string } = {},
  ): Promise<{
    run_id: string;
    status: RunStatus;
    stream_url: string;
  }> {
    return this.post('/api/runs', {
      description,
      claims: context.claims ?? '',
    });
  }

  gateA(runId: string): Promise<Record<string, unknown>> {
    return this.call(`/api/runs/${runId}/gates/A`);
  }

  gateStrategy(runId: string): Promise<Record<string, unknown>> {
    return this.call(`/api/runs/${runId}/gates/STRATEGY`);
  }

  /**
   * The completed run's report: the **standard** `AnalysisReport`, not a
   * verification report.
   *
   * The same object the terminal `result` event carries and the same one
   * `services/history.ts` maps a stored run into, so a durable run, a quick
   * search and a run reopened from History all render through `ReportScreen`
   * with no second display path to keep in step.
   */
  report(runId: string): Promise<AnalysisReport> {
    return this.call(`/api/runs/${runId}/report`) as Promise<AnalysisReport>;
  }

  /** Cancel a live Advanced Search. `keepalive` lets pagehide finish the request. */
  cancelRun(runId: string, keepalive = false): Promise<void> {
    return this.call<void>(`/api/runs/${runId}`, { method: 'DELETE', keepalive });
  }

  // --- mutations -----------------------------------------------------------

  /**
   * Approve the invention understanding: stages 1 and 2, one backend approval.
   *
   * A summary and a flat list of element **strings**. No version (the server
   * mints it), no capability objects, no `core`, no relationships and no
   * per-element ids -- the ids the stage-2 screen keeps are its own bookkeeping
   * for tracking rows while editing, and are not part of the contract.
   */
  approveGateA(runId: string, summary: string, elements: string[], key: string) {
    return this.post(`/api/runs/${runId}/gates/A/approve`, {
      technical_summary: summary,
      technical_elements: elements,
      idempotency_key: key,
    });
  }

  /**
   * Approve the search queries: stage 3, and the approval that releases the run
   * into the search.
   *
   * A plain list of strings. Every approved query runs -- enable/disable is gone,
   * so there is no "sent but not dispatched" state to represent, and nothing
   * here can claim coverage of an element.
   */
  approveGateStrategy(runId: string, queries: string[], key: string) {
    return this.post(`/api/runs/${runId}/gates/STRATEGY/approve`, {
      queries,
      idempotency_key: key,
    });
  }

  // `gateB`, `gateC`, `cell`, `approveGateB`, `addCandidate`, `reviewCell`,
  // `waiveCell`, `rerunCell`, `completeGateC`, `approveGateD`,
  // `regenerateUnderstanding` and `regenerateStrategy` were REMOVED with the
  // routes behind them. Every one of them now answers 404 as an unknown path;
  // a client method for a route that does not exist is a call site waiting to
  // fail at runtime rather than at build time.

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
        detail: PUBLIC_ERRORS.connection,
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
            detail: PUBLIC_ERRORS.connection,
          });
        }
      }
    }
  }
}

/**
 * The active verified run, remembered only so a reload can cancel it.
 *
 * The key is namespaced by account so one user can never cancel another user's
 * remembered run. It is not navigation state and is never used to restore a
 * screen.
 */
@Injectable({ providedIn: 'root' })
export class ActiveRunStore {
  private key(email: string): string {
    return `novelty-lens-active-run:${email}`;
  }

  /** Remember the run this account is working on. */
  remember(email: string, runId: string): void {
    try {
      localStorage.setItem(this.key(email), runId);
    } catch {
      // A browser with storage disabled loses the startup cancellation retry;
      // pagehide still makes its best-effort request. Do not fail a working
      // session merely because storage is unavailable.
    }
  }

  /** Read the id to cancel. It is never sufficient authority to render a run. */
  peek(email: string): string | null {
    try {
      return localStorage.getItem(this.key(email));
    } catch {
      return null;
    }
  }

  /** Forget it after cancellation, completion, failure, sign-out, or a 404. */
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
