import { ComponentFixture, TestBed } from '@angular/core/testing';
import type { Type } from '@angular/core';
import { beforeEach, describe, expect, it } from 'vitest';
import { AdvancedSearchStages, STAGE_RULES } from './advanced-search-stages';
import { RunApiError, RunsService } from '../services/runs';

/**
 * The three Advanced Search stages, rendered and driven.
 *
 * These assert **behaviour**, not source text: what the component does when the
 * backend answers a certain way, what it refuses to send, and what the user
 * actually sees. The Python contract tests remain as a second layer for the
 * things a component test cannot see (the dark-mode CSS pairing, the
 * backend/frontend vocabulary agreement).
 *
 * `RunsService` is the only seam stubbed. Nothing here reaches the network.
 */

const UNDERSTANDING = {
  original_description: 'A bottle that filters and sterilises water as you drink.',
  technical_summary: 'A drinking vessel combining UV-C sterilisation with membrane filtration.',
  technical_elements: ['a graphene-oxide membrane', 'a UV-C emitter'],
  editable: true,
  completion_rules: [],
};

const STRATEGY = {
  queries: ['graphene oxide membrane water filter', 'uv-c sterilisation drinking bottle'],
  editable: true,
  completion_rules: [],
};

class FakeRuns {
  gateAPayload: unknown = UNDERSTANDING;
  gateStrategyPayload: unknown = STRATEGY;

  failWith: RunApiError | null = null;
  readonly sent: Array<{ method: string; args: unknown[] }> = [];

  private maybeFail<T>(value: T): Promise<T> {
    return this.failWith ? Promise.reject(this.failWith) : Promise.resolve(value);
  }

  /** What `getRun` reports; the component polls it after the first approval. */
  runStatus: { status: string; current_gate: string | null } = {
    status: 'WAITING_FOR_REVIEW',
    current_gate: 'STRATEGY',
  };

  newKey() {
    return `key-${this.sent.length + 1}`;
  }
  getRun() {
    return Promise.resolve(this.runStatus as unknown as Record<string, unknown>);
  }
  gateA() {
    return this.maybeFail(this.gateAPayload as Record<string, unknown>);
  }
  gateStrategy() {
    return this.maybeFail(this.gateStrategyPayload as Record<string, unknown>);
  }
  approveGateA(...args: unknown[]) {
    this.sent.push({ method: 'approveGateA', args });
    return this.maybeFail({});
  }
  approveGateStrategy(...args: unknown[]) {
    this.sent.push({ method: 'approveGateStrategy', args });
    return this.maybeFail({});
  }
}

function failure(code: string, status = 409): RunApiError {
  return new RunApiError(status, { code, detail: `simulated ${code}` });
}

async function settle<T>(fixture: ComponentFixture<T>): Promise<void> {
  await fixture.whenStable();
  fixture.detectChanges();
  await fixture.whenStable();
  fixture.detectChanges();
}

function text(fixture: ComponentFixture<unknown>): string {
  return (fixture.nativeElement as HTMLElement).textContent ?? '';
}

function inputs(fixture: ComponentFixture<unknown>): HTMLInputElement[] {
  return Array.from(
    (fixture.nativeElement as HTMLElement).querySelectorAll('input.draft-input'),
  );
}

function mount<T>(type: Type<T>, runs: FakeRuns, props: Record<string, unknown>) {
  TestBed.configureTestingModule({
    imports: [type],
    providers: [{ provide: RunsService, useValue: runs }],
  });
  const fixture = TestBed.createComponent(type);
  for (const [key, value] of Object.entries(props)) {
    fixture.componentRef.setInput(key, value);
  }
  fixture.detectChanges();
  return fixture;
}

async function atStageOne(runs: FakeRuns) {
  const fixture = mount(AdvancedSearchStages, runs, {
    runId: 'r1',
    gate: 'A',
    pollIntervalMs: 0,
  });
  await settle(fixture);
  return fixture;
}

describe('Advanced Search — stage 1, the understanding', () => {
  let runs: FakeRuns;
  beforeEach(() => {
    runs = new FakeRuns();
  });

  it('renders the description read-only and the summary editable', async () => {
    const fixture = await atStageOne(runs);
    const host = fixture.nativeElement as HTMLElement;

    // The description is a <p>, never an input: it is never rewritten, and a box
    // that accepts a caret invites the belief that it could be.
    const description = host.querySelector('.original-description');
    expect(description?.textContent).toContain('A bottle that filters');
    expect(host.querySelector('input.original-description')).toBeNull();
    expect(host.querySelector('textarea.original-description')).toBeNull();

    const summary = host.querySelector('#summary-input') as HTMLTextAreaElement;
    expect(summary).not.toBeNull();
    expect(summary.value).toContain('UV-C sterilisation');
  });

  it('renders the stage counter as text', async () => {
    const fixture = await atStageOne(runs);
    expect(text(fixture)).toContain('1 / 3');
  });

  it('blocks on an empty summary, and on one that is empty after trimming', async () => {
    const fixture = await atStageOne(runs);
    const component = fixture.componentInstance;

    for (const blank of ['', '   ', '\t\n ']) {
      component.summary.set(blank);
      expect(component.violations()).toHaveLength(1);
    }
    component.summary.set('a real summary');
    expect(component.violations()).toEqual([]);
  });

  it('calls no approval endpoint moving from stage 1 to stage 2', async () => {
    const fixture = await atStageOne(runs);
    await fixture.componentInstance.next();
    await settle(fixture);

    expect(fixture.componentInstance.stage()).toBe(2);
    // The whole point: "Next" is not an approval anybody would recognise as one.
    expect(runs.sent).toEqual([]);
  });
});

describe('Advanced Search — stage 2, the technical elements', () => {
  let runs: FakeRuns;
  beforeEach(() => {
    runs = new FakeRuns();
  });

  async function atStageTwo() {
    const fixture = await atStageOne(runs);
    await fixture.componentInstance.next();
    await settle(fixture);
    return fixture;
  }

  it('renders a flat list of element strings', async () => {
    const fixture = await atStageTwo();
    expect(inputs(fixture).map((i) => i.value)).toEqual([
      'a graphene-oxide membrane',
      'a UV-C emitter',
    ]);
    expect(text(fixture)).toContain('2 / 3');
  });

  it('adds, edits and deletes an element', async () => {
    const fixture = await atStageTwo();
    const component = fixture.componentInstance;

    component.addRow(component.elements, 'E');
    await settle(fixture);
    expect(component.elements()).toHaveLength(3);

    const added = component.elements()[2];
    component.editRow(component.elements, added.key, 'a flow sensor');
    await settle(fixture);
    expect(inputs(fixture).map((i) => i.value)).toContain('a flow sensor');

    component.removeRow(component.elements, component.elements()[0].key);
    await settle(fixture);
    expect(component.elements().map((row) => row.text)).toEqual([
      'a UV-C emitter',
      'a flow sensor',
    ]);
  });

  it('keys a new row above every key already present', async () => {
    const fixture = await atStageTwo();
    const component = fixture.componentInstance;

    const first = component.elements()[0].key;
    component.removeRow(component.elements, first);
    component.addRow(component.elements, 'E');

    // Deleting the first row and adding one must not reuse the deleted key, or
    // `@for` matches the new row to the removed row's DOM node and carries its
    // input state across.
    expect(component.elements().map((row) => row.key)).not.toContain(first);
    expect(new Set(component.elements().map((row) => row.key)).size).toBe(
      component.elements().length,
    );
  });

  it('blocks on zero non-empty elements', async () => {
    const fixture = await atStageTwo();
    const component = fixture.componentInstance;

    // Blanking every element is approving no elements, whatever the row count.
    for (const row of component.elements()) {
      component.editRow(component.elements, row.key, '   ');
    }
    expect(component.violations()).toHaveLength(1);

    component.editRow(component.elements, component.elements()[0].key, 'a membrane');
    expect(component.violations()).toEqual([]);
  });

  it('approves the understanding on the way out, with the edited text', async () => {
    const fixture = await atStageTwo();
    const component = fixture.componentInstance;

    component.summary.set('  an edited summary  ');
    component.editRow(component.elements, component.elements()[0].key, '  an edited element  ');
    await component.next();
    await settle(fixture);

    expect(runs.sent).toHaveLength(1);
    const [runId, summary, elements] = runs.sent[0].args as [string, string, string[]];
    expect(runs.sent[0].method).toBe('approveGateA');
    expect(runId).toBe('r1');
    expect(summary).toBe('an edited summary');
    expect(elements).toEqual(['an edited element', 'a UV-C emitter']);
  });
});

describe('Advanced Search — stage 3, the searches', () => {
  let runs: FakeRuns;
  beforeEach(() => {
    runs = new FakeRuns();
  });

  async function atStageThree() {
    const fixture = mount(AdvancedSearchStages, runs, {
      runId: 'r1',
      gate: 'STRATEGY',
      pollIntervalMs: 0,
    });
    await settle(fixture);
    return fixture;
  }

  it('renders a plain query list and the stage counter', async () => {
    const fixture = await atStageThree();
    expect(fixture.componentInstance.stage()).toBe(3);
    expect(inputs(fixture).map((i) => i.value)).toEqual([
      'graphene oxide membrane water filter',
      'uv-c sterilisation drinking bottle',
    ]);
    expect(text(fixture)).toContain('3 / 3');
  });

  it('adds, edits and deletes a query', async () => {
    const fixture = await atStageThree();
    const component = fixture.componentInstance;

    component.addRow(component.queries, 'Q');
    component.editRow(component.queries, component.queries()[2].key, 'self-cleaning cap');
    component.removeRow(component.queries, component.queries()[0].key);
    await settle(fixture);

    expect(component.queries().map((row) => row.text)).toEqual([
      'uv-c sterilisation drinking bottle',
      'self-cleaning cap',
    ]);
  });

  it('blocks on zero non-empty queries', async () => {
    const fixture = await atStageThree();
    const component = fixture.componentInstance;

    for (const row of component.queries()) {
      component.editRow(component.queries, row.key, '  ');
    }
    expect(component.violations()).toHaveLength(1);
  });

  it('sends only the edited, non-empty queries', async () => {
    const fixture = await atStageThree();
    const component = fixture.componentInstance;

    component.editRow(component.queries, component.queries()[0].key, '  edited query  ');
    component.editRow(component.queries, component.queries()[1].key, '   ');
    await component.next();
    await settle(fixture);

    expect(runs.sent[0].method).toBe('approveGateStrategy');
    expect(runs.sent[0].args[1]).toEqual(['edited query']);
  });
});

describe('Advanced Search — navigation preserves every edit', () => {
  let runs: FakeRuns;
  beforeEach(() => {
    runs = new FakeRuns();
  });

  it('preserves edits forward 1 → 2 → 3', async () => {
    const fixture = await atStageOne(runs);
    const component = fixture.componentInstance;

    component.summary.set('my summary');
    await component.next(); // 1 -> 2, no server call
    component.editRow(component.elements, component.elements()[0].key, 'my element');
    await component.next(); // 2 -> 3, approves the understanding
    await settle(fixture);
    component.queries.set([{ key: 'q1', text: 'my query' }]);

    expect(component.stage()).toBe(3);
    expect(component.summary()).toBe('my summary');
    expect(component.elements()[0].text).toBe('my element');
    expect(component.queries()[0].text).toBe('my query');
  });

  it('preserves edits backward 3 → 2 → 1', async () => {
    const fixture = await atStageOne(runs);
    const component = fixture.componentInstance;

    component.summary.set('my summary');
    await component.next();
    component.editRow(component.elements, component.elements()[0].key, 'my element');
    await component.next();
    await settle(fixture);
    component.queries.set([{ key: 'q1', text: 'my query' }]);

    component.previous(); // 3 -> 2
    expect(component.stage()).toBe(2);
    expect(component.elements()[0].text).toBe('my element');

    component.previous(); // 2 -> 1
    expect(component.stage()).toBe(1);
    expect(component.summary()).toBe('my summary');

    // And nothing was re-fetched or re-proposed on the way back.
    expect(component.queries()[0].text).toBe('my query');
  });

  it('does not re-approve the understanding when walking back and forward', async () => {
    const fixture = await atStageOne(runs);
    const component = fixture.componentInstance;

    await component.next(); // 1 -> 2
    await component.next(); // 2 -> 3, one approval
    await settle(fixture);
    expect(runs.sent.filter((c) => c.method === 'approveGateA')).toHaveLength(1);

    component.previous(); // 3 -> 2
    await component.next(); // 2 -> 3 again
    await settle(fixture);

    // A second approval would be a WRONG_GATE conflict for a reviewer who did
    // nothing wrong: the run has already moved past Gate A.
    expect(runs.sent.filter((c) => c.method === 'approveGateA')).toHaveLength(1);
    expect(component.stage()).toBe(3);
  });
});

describe('Advanced Search — the blocking list is exactly three rules', () => {
  it('states two rules for the understanding and one for the strategy', () => {
    // Spec section 3.5: nothing else blocks. Paired one-to-one with the
    // server's `GATE_A_RULES` / `GATE_STRATEGY_RULES`, which
    // `test_frontend_review_contract.py` compares by count.
    expect(STAGE_RULES['A']).toHaveLength(2);
    expect(STAGE_RULES['STRATEGY']).toHaveLength(1);
  });

  it('raises at most one violation per stage and none when satisfied', async () => {
    const runs = new FakeRuns();
    const fixture = await atStageOne(runs);
    const component = fixture.componentInstance;

    expect(component.violations()).toEqual([]);
    component.summary.set('');
    expect(component.violations()).toHaveLength(1);
  });
});

describe('Advanced Search — failure states stay distinct', () => {
  it('renders a 404 as forbidden rather than guessing which kind it got', async () => {
    const runs = new FakeRuns();
    runs.failWith = failure('RUN_NOT_FOUND', 404);
    const fixture = mount(AdvancedSearchStages, runs, {
      runId: 'r1',
      gate: 'A',
      pollIntervalMs: 0,
    });
    await settle(fixture);
    expect(fixture.componentInstance.state()).toBe('forbidden');
  });

  it('renders a dropped stream as reconnecting, never as a run failure', async () => {
    const runs = new FakeRuns();
    runs.failWith = failure('STREAM_UNAVAILABLE', 503);
    const fixture = mount(AdvancedSearchStages, runs, {
      runId: 'r1',
      gate: 'A',
      pollIntervalMs: 0,
    });
    await settle(fixture);
    expect(fixture.componentInstance.state()).toBe('reconnecting');
  });

  it('lists every blocking condition the server returned, not just the first', async () => {
    const runs = new FakeRuns();
    const fixture = await atStageOne(runs);
    runs.failWith = new RunApiError(409, {
      code: 'GATE_BLOCKED',
      detail: 'blocked',
      blocking: [{ code: 'NO_SUMMARY' }, { code: 'NO_ELEMENTS' }],
    });
    await fixture.componentInstance.next();
    await fixture.componentInstance.next();
    await settle(fixture);

    expect(fixture.componentInstance.state()).toBe('blocked');
    expect(fixture.componentInstance.blocking()).toHaveLength(2);
  });
});

describe('Advanced Search — no internal vocabulary reaches the screen', () => {
  it('never renders a gate letter or a withdrawn concept', async () => {
    const runs = new FakeRuns();
    const fixture = await atStageOne(runs);
    const lower = text(fixture).toLowerCase();

    for (const internal of [
      'gate a',
      'gate strategy',
      'decomposition',
      'checkpoint',
      'verification cell',
      'capability',
      'core element',
      'relationship',
      'coverage',
    ]) {
      expect(lower, `"${internal}" reached the screen`).not.toContain(internal);
    }
    // And no percent sign in a rating position: this product publishes no score.
    expect(lower).not.toContain('%');
  });
});

describe('Advanced Search — the next stage arrives with no page reload', () => {
  /**
   * **Spec section 8.1 row 14, and the bug behind "I have to refresh".**
   *
   * Two consecutive approvals against a faked run API, with nothing reloading
   * anything. The first approval commits and the component walks the reviewer on
   * to stage 3 by itself; the second commits and hands back to the shell.
   *
   * The app-level half of this is `afterDecision()`, which restarts
   * `followToGate` for the run it just decided on --
   * `test_frontend_review_contract.py` asserts that, because it is a property of
   * `app.ts` rather than of this component.
   */
  it('drives two approvals in a row without remounting', async () => {
    const runs = new FakeRuns();
    const fixture = await atStageOne(runs);
    const component = fixture.componentInstance;

    let resumedCount = 0;
    component.resumed.subscribe(() => (resumedCount += 1));

    await component.next(); // 1 -> 2
    await component.next(); // 2 -> 3, approval one, then the hand-off poll
    await settle(fixture);

    expect(component.stage()).toBe(3);
    expect(component.state()).toBe('review');
    // The queries the worker proposed are on screen, fetched by the component
    // itself -- no reload, and no second mount.
    expect(component.queries().map((row) => row.text)).toEqual(STRATEGY.queries);
    expect(resumedCount).toBe(0);

    await component.next(); // approval two
    await settle(fixture);

    expect(runs.sent.map((call) => call.method)).toEqual([
      'approveGateA',
      'approveGateStrategy',
    ]);
    expect(component.state()).toBe('resuming');
    // Only now does the shell take over, to watch the run to its report.
    expect(resumedCount).toBe(1);
  });
});

describe('Advanced Search — the stage label names the stage you are on', () => {
  it('follows the stage rather than the gate the run was parked at', async () => {
    // Regression: the shell was passed `gate()`, the gate the run was parked at
    // when the component mounted. That does not move as the reviewer walks the
    // stages, so stage 3 was sub-titled "Invention understanding" -- naming the
    // wrong stage on the one screen whose job is to say where you are.
    const runs = new FakeRuns();
    const fixture = await atStageOne(runs);
    const component = fixture.componentInstance;

    expect(component.stageGate()).toBe('A');
    await component.next();
    expect(component.stageGate()).toBe('A');
    await component.next();
    await settle(fixture);
    expect(component.stage()).toBe(3);
    expect(component.stageGate()).toBe('STRATEGY');
  });
});
