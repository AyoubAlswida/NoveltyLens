import { Injectable, inject } from '@angular/core';
import { API_BASE_URL } from '../config';
import { StreamEvent } from '../models/report';
import { AuthService } from './auth';

@Injectable({ providedIn: 'root' })
export class AnalysisService {
  private readonly auth = inject(AuthService);

  /**
   * Reads the SSE body of POST /api/analyze/stream.
   *
   * Deliberately raw `fetch` + `getReader()`, NOT Angular's HttpClient and NOT
   * EventSource:
   *   - EventSource cannot carry a POST body at all.
   *   - HttpClient buffers the whole response before emitting, so progress
   *     events would arrive in one lump when the run finished, silently
   *     destroying the live phase display while still "working".
   * If you refactor this to HttpClient, the UI will look fine until you watch
   * a real 60-second run.
   *
   * EventSource being unusable is now load-bearing for a second reason: the
   * endpoint requires a bearer token, and EventSource cannot set headers
   * either. `authorizedFetch` attaches it and ends the session on a 401.
   *
   * The endpoint, method and request shape are otherwise unchanged from the
   * React version.
   */
  async stream(
    payload: { description: string; claims: string },
    onEvent: (event: StreamEvent) => void,
  ): Promise<void> {
    const response = await this.auth.authorizedFetch(`${API_BASE_URL}/api/analyze/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    if (!response.ok || !response.body) {
      throw new Error('Backend query failed');
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split('\n\n');
      buffer = frames.pop() ?? '';

      for (const frame of frames) {
        const line = frame.trim();
        if (!line.startsWith('data:')) continue;
        const json = line.slice('data:'.length).trim();
        if (json) onEvent(JSON.parse(json) as StreamEvent);
      }
    }
  }
}
