import { TestBed } from '@angular/core/testing';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ActiveRunStore, RunApiError, RunsService, StreamCursor } from './runs';
import { AuthService } from './auth';

/**
 * The verified-run API client, exercised for real.
 *
 * These replace nothing: `backend/tests/test_frontend_review_contract.py` still
 * greps the source as a second layer. What that layer cannot do is *run* the
 * code, so it cannot tell whether a mutation actually carries its key, whether a
 * 404 actually produces the state that clears the stored run, or whether the SSE
 * reader actually splits frames correctly. These do.
 *
 * `authorizedFetch` is the only seam stubbed. Nothing here reaches the network.
 */

class FakeAuth {
  email = () => 'reviewer@example.invalid';
  readonly calls: Array<{ url: string; init: RequestInit }> = [];
  responder: (url: string, init: RequestInit) => Response = () =>
    new Response('{}', { status: 200 });

  authorizedFetch(url: string, init: RequestInit = {}): Promise<Response> {
    this.calls.push({ url, init });
    return Promise.resolve(this.responder(url, init));
  }
}

/**
 * A `Response` whose body is a real `ReadableStream`.
 *
 * jsdom does not implement `Blob.stream()`, so the stream is constructed
 * directly. That is closer to what the browser hands the reader anyway: one
 * chunk of bytes, which is exactly the shape the frame splitter has to cope with.
 */
function streaming(text: string): Response {
  const bytes = new TextEncoder().encode(text);
  return new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(bytes);
        controller.close();
      },
    }),
    { status: 200 },
  );
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('RunsService', () => {
  let auth: FakeAuth;
  let runs: RunsService;

  beforeEach(() => {
    auth = new FakeAuth();
    TestBed.configureTestingModule({
      providers: [RunsService, { provide: AuthService, useValue: auth }],
    });
    runs = TestBed.inject(RunsService);
  });

  it('sends an idempotency key on every mutating call', async () => {
    auth.responder = () => json({ status: 'RESUMING' });

    await runs.approveGateA('r1', { version: 1 }, 'key-a');
    await runs.approveGateB('r1', ['EP1'], 1, 'key-b');
    await runs.addCandidate('r1', 'EP2', 'key-c');
    await runs.completeGateC('r1', 'key-d');
    await runs.approveGateD('r1', 1, 'key-e');
    await runs.reviewCell('r1', 'c1', {}, 'key-f');
    await runs.waiveCell('r1', 'c1', {}, 'key-g');
    await runs.rerunCell('r1', 'c1', [], 1, 'key-h');

    expect(auth.calls).toHaveLength(8);
    for (const call of auth.calls) {
      const body = JSON.parse(String(call.init.body));
      expect(body.idempotency_key, `${call.url} sent no key`).toBeTruthy();
    }
  });

  it('mints a different key each time one is asked for', () => {
    const keys = new Set([runs.newKey(), runs.newKey(), runs.newKey()]);
    expect(keys.size).toBe(3);
  });

  it('raises RunApiError carrying the backend code and detail', async () => {
    auth.responder = () => json({ code: 'RUN_NOT_FOUND', detail: 'nope' }, 404);

    await expect(runs.getRun('missing')).rejects.toBeInstanceOf(RunApiError);
    try {
      await runs.getRun('missing');
    } catch (error) {
      const failure = error as RunApiError;
      expect(failure.status).toBe(404);
      expect(failure.failure.code).toBe('RUN_NOT_FOUND');
    }
  });

  it('surfaces a stale-result conflict as its own code', async () => {
    auth.responder = () =>
      json({ code: 'STALE_RESULT_VERSION', detail: 'cell is at v2' }, 409);
    try {
      await runs.rerunCell('r1', 'c1', [], 1, 'k');
      expect.unreachable('a 409 should have thrown');
    } catch (error) {
      expect((error as RunApiError).failure.code).toBe('STALE_RESULT_VERSION');
    }
  });

  it('does not invent a failure shape when the body is not JSON', async () => {
    auth.responder = () => new Response('<html>502</html>', { status: 502 });
    try {
      await runs.listRuns();
      expect.unreachable('a 502 should have thrown');
    } catch (error) {
      const failure = error as RunApiError;
      expect(failure.failure.code).toBe('UNKNOWN');
      expect(failure.failure.detail).toContain('502');
    }
  });

  it('sends the reconnection cursor as a query parameter, not a header', async () => {
    // The browser will not send `Last-Event-ID` for a raw fetch, so the client
    // must carry it itself -- which is why the backend accepts both forms.
    auth.responder = () => new Response(null, { status: 200 });
    await runs.stream('r1', 41, () => {}, () => {}).catch(() => {});
    expect(auth.calls[0].url).toContain('last_event_id=41');
  });

  it('parses SSE frames and reports each id to the cursor callback', async () => {
    const frames =
      'id: 1\ndata: {"type":"progress","phase":"queued"}\n\n' +
      'id: 2\ndata: {"type":"paused","gate":"A"}\n\n';
    auth.responder = () => streaming(frames);

    const seen: string[] = [];
    const ids: number[] = [];
    await runs.stream('r1', null, (event) => seen.push(event.type), (id) => ids.push(id));

    expect(seen).toEqual(['progress', 'paused']);
    expect(ids).toEqual([1, 2]);
  });

  it('treats a frame that is not JSON as a contract violation, not a skip', async () => {
    auth.responder = () => streaming('id: 1\ndata: not-json\n\n');
    await expect(runs.stream('r1', null, () => {}, () => {})).rejects.toMatchObject({
      failure: { code: 'SSE_CONTRACT' },
    });
  });
});

describe('StreamCursor', () => {
  let cursor: StreamCursor;

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [StreamCursor] });
    cursor = TestBed.inject(StreamCursor);
  });

  it('starts with no cursor so a fresh attach gets a snapshot', () => {
    expect(cursor.get('r1')).toBeNull();
  });

  it('never moves backwards', () => {
    // A frame that arrived out of order must not rewind the cursor, or the next
    // reconnect would replay what was already seen.
    cursor.advance('r1', 7);
    cursor.advance('r1', 3);
    expect(cursor.get('r1')).toBe(7);
  });

  it('keeps runs separate', () => {
    cursor.advance('r1', 5);
    cursor.advance('r2', 9);
    expect(cursor.get('r1')).toBe(5);
    expect(cursor.get('r2')).toBe(9);
  });
});

describe('ActiveRunStore', () => {
  let auth: FakeAuth;
  let store: ActiveRunStore;
  let runs: RunsService;

  beforeEach(() => {
    localStorage.clear();
    auth = new FakeAuth();
    TestBed.configureTestingModule({
      providers: [RunsService, ActiveRunStore, { provide: AuthService, useValue: auth }],
    });
    runs = TestBed.inject(RunsService);
    store = TestBed.inject(ActiveRunStore);
  });

  it('namespaces the remembered run by account', () => {
    store.remember('a@example.invalid', 'run-a');
    store.remember('b@example.invalid', 'run-b');
    expect(localStorage.getItem('novelty-lens-active-run:a@example.invalid')).toBe('run-a');
    expect(localStorage.getItem('novelty-lens-active-run:b@example.invalid')).toBe('run-b');
  });

  it('verifies the run with the backend before returning it', async () => {
    store.remember('a@example.invalid', 'run-a');
    auth.responder = () => json({ run_id: 'run-a', status: 'WAITING_FOR_REVIEW' });

    const run = await store.restore('a@example.invalid');
    expect(run?.run_id).toBe('run-a');
    expect(auth.calls[0].url).toContain('/api/runs/run-a');
  });

  it('clears the key silently when the run is gone or not ours', async () => {
    store.remember('a@example.invalid', 'run-a');
    auth.responder = () => json({ code: 'RUN_NOT_FOUND', detail: 'no' }, 404);

    expect(await store.restore('a@example.invalid')).toBeNull();
    expect(localStorage.getItem('novelty-lens-active-run:a@example.invalid')).toBeNull();
  });

  it('keeps the key when the failure is transport, not a 404', async () => {
    // A dropped connection is not proof the run is gone. Dropping the key here
    // would lose a paused run over a flaky network.
    store.remember('a@example.invalid', 'run-a');
    auth.responder = () => json({ code: 'X', detail: 'boom' }, 503);

    await expect(store.restore('a@example.invalid')).rejects.toBeTruthy();
    expect(localStorage.getItem('novelty-lens-active-run:a@example.invalid')).toBe('run-a');
  });

  it('forgets every account when the signed-in identity changes', () => {
    store.remember('a@example.invalid', 'run-a');
    store.remember('b@example.invalid', 'run-b');
    store.forgetAll();
    expect(localStorage.getItem('novelty-lens-active-run:a@example.invalid')).toBeNull();
    expect(localStorage.getItem('novelty-lens-active-run:b@example.invalid')).toBeNull();
  });

  it('returns null when nothing was ever remembered', async () => {
    expect(await store.restore('nobody@example.invalid')).toBeNull();
  });
});
