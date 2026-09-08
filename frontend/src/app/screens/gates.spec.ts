import { ComponentFixture, TestBed } from '@angular/core/testing';
import type { Type } from '@angular/core';
import { beforeEach, describe, expect, it } from 'vitest';
import { AdvancedSearchStages } from './advanced-search-stages';
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
  review_stage: 'summary',
};

const LIMITATIONS = { ...UNDERSTANDING, review_stage: 'limitations' };

const STRATEGY = {
  queries: ['graphene oxide membrane water filter', 'uv-c sterilisation drinking bottle'],
  editable: true,
  completion_rules: [],
};

class FakeRuns {
  gateAPayload: unknown = UNDERSTANDING;
  readonly gateAResponses: unknown[] = [];
  gateStrategyPayload: unknown = STRATEGY;

  failWith: RunApiError | null = null;
  readonly sent: Array<{ method: string; args: unknown[] }> = [];

  private maybeFail<T>(value: T): Promise<T> {
    return this.failWith ? Promise.reject(this.failWith) : Promise.resolve(value);
  }

  newKey() {
    return `key-${this.sent.length + 1}`;
  }
  gateA() {
    const payload = this.gateAResponses.shift() ?? this.gateAPayload;
    return this.maybeFail(payload as Record<string, unknown>);
  }
  gateStrategy() {
    return this.maybeFail(this.gateStrategyPayload as Record<string, unknown>);
  }
  approveGateSummary(...args: unknown[]) {
    this.sent.push({ method: 'approveGateSummary', args });
    this.gateAPayload = { ...LIMITATIONS, technical_summary: String(args[1] ?? '') };
    return this.maybeFail({});
  }
  approveGateLimitations(...args: unknown[]) {
    this.sent.push({ method: 'approveGateLimitations', args });
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
  });
  await settle(fixture);
  return fixture;
}

async function arriveAtStrategy(
  fixture: ComponentFixture<AdvancedSearchStages>,
): Promise<void> {
  // The app's single durable SSE watcher reports the next pause by updating
  // this input. The same component instance stays mounted, so earlier edits
  // remain available when the reviewer walks back.
  fixture.componentRef.setInput('gate', 'STRATEGY');
  await settle(fixture);
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
    expect(text(fixture)).toContain('Editable');
    expect(text(fixture)).toContain('This is how Novelty Lens understood your invention');
    expect(text(fixture)).not.toContain('Your original description, shown for reference.');
    expect(host.querySelectorAll('.comparison-panel')).toHaveLength(2);
    expect(text(fixture)).toContain('Original');
  });

  it('renders the stage counter as text', async () => {
    // "Step 1 of 3", not "1 / 3". Spec section 3.1 fixes the *requirement* --
    // the counter is rendered text, never an aria-label alone -- and the owner
    // chose this wording. The requirement is unchanged; only the phrasing moved.
    const fixture = await atStageOne(runs);
    expect(text(fixture)).toContain('Step 1 of 3');
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

  it('approves the summary before stage 2 is shown', async () => {
    const fixture = await atStageOne(runs);
    await fixture.componentInstance.next();
    await settle(fixture);

    expect(fixture.componentInstance.stage()).toBe(2);
    expect(runs.sent[0].method).toBe('approveGateSummary');
  });

  it('names the work between summary approval and limitations review', async () => {
    const fixture = await atStageOne(runs);
    fixture.componentInstance.state.set('resuming');
    fixture.detectChanges();

    const transition = text(fixture).toLowerCase();
    expect(transition).toContain('preparing your technical limitations');
    expect(transition).toContain('approved invention summary');
    expect(transition).not.toContain('search strategy');
  });

  it('waits through the intermediate limitations checkpoint until elements exist', async () => {
    const fixture = await atStageOne(runs);
    runs.gateAResponses.push(
      { ...LIMITATIONS, technical_elements: [] },
      LIMITATIONS,
    );

    await fixture.componentInstance.next();
    await settle(fixture);

    expect(fixture.componentInstance.stage()).toBe(2);
    expect(fixture.componentInstance.elements().map((row) => row.text)).toEqual(
      LIMITATIONS.technical_elements,
    );
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
    expect(text(fixture)).toContain('Step 2 of 3');
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

    component.editRow(component.elements, component.elements()[0].key, '  an edited element  ');
    await component.next();
    await settle(fixture);

    expect(runs.sent).toHaveLength(2);
    const limitationCall = runs.sent.find((call) => call.method === 'approveGateLimitations')!;
    const [runId, elements] = limitationCall.args as [string, string[]];
    expect(limitationCall.method).toBe('approveGateLimitations');
    expect(runId).toBe('r1');
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
    expect(text(fixture)).toContain('Step 3 of 3');
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
    await component.next(); // approve summary, then load generated limitations
    component.editRow(component.elements, component.elements()[0].key, 'my element');
    await component.next(); // 2 -> 3, approves the understanding
    await settle(fixture);
    await arriveAtStrategy(fixture);
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
    await arriveAtStrategy(fixture);
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

    await component.next(); // approve summary, then load limitations
    await component.next(); // 2 -> 3, one approval
    await settle(fixture);
    await arriveAtStrategy(fixture);
    expect(runs.sent.filter((c) => c.method === 'approveGateLimitations')).toHaveLength(1);

    component.previous(); // 3 -> 2
    await component.next(); // 2 -> 3 again
    await settle(fixture);

    // A second approval would be a WRONG_GATE conflict for a reviewer who did
    // nothing wrong: the run has already moved past Gate A.
    expect(runs.sent.filter((c) => c.method === 'approveGateLimitations')).toHaveLength(1);
    expect(component.stage()).toBe(3);
  });
});

describe('Advanced Search — the blocking list is exactly three rules', () => {
  // The `states two rules ... and one for the strategy` test was REMOVED with the
  // upfront "What this step requires" list it checked. The rules are still
  // enforced and a reviewer is still told which one they broke -- that is what
  // the tests below and `names the rule it broke` assert. Nothing recites them
  // in advance any more, so there is no on-screen list to pair against.

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
    });
    await settle(fixture);
    expect(fixture.componentInstance.state()).toBe('forbidden');
    expect(text(fixture)).toContain('This search is no longer available.');
    expect(text(fixture)).not.toContain('simulated RUN_NOT_FOUND');
  });

  it('renders a dropped stream as reconnecting, never as a run failure', async () => {
    const runs = new FakeRuns();
    runs.failWith = failure('STREAM_UNAVAILABLE', 503);
    const fixture = mount(AdvancedSearchStages, runs, {
      runId: 'r1',
      gate: 'A',
    });
    await settle(fixture);
    expect(fixture.componentInstance.state()).toBe('reconnecting');
    expect(text(fixture)).not.toContain('simulated STREAM_UNAVAILABLE');
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
   * Two consecutive approvals against a faked run API, with no remount. The
   * app-level durable stream moves the same component from A to STRATEGY by
   * updating its gate input.
   */
  it('drives all three approvals without remounting', async () => {
    const runs = new FakeRuns();
    const fixture = await atStageOne(runs);
    const component = fixture.componentInstance;

    const resumed: string[] = [];
    component.resumed.subscribe((gate) => resumed.push(gate));

    await component.next(); // summary approval -> stage 2
    await component.next(); // limitations approval
    await settle(fixture);
    expect(component.state()).toBe('resuming');
    expect(resumed).toEqual(['A', 'A']);

    const transitionText = text(fixture).toLowerCase();
    expect(transitionText).toContain('preparing your search strategy');
    expect(transitionText).toContain('changes saved');
    expect(transitionText).not.toContain('worker');
    expect(transitionText).not.toContain('poll');
    expect(transitionText).not.toContain('r1');
    expect(fixture.nativeElement.querySelector('.stage-header')).toBeNull();
    expect(fixture.nativeElement.querySelector('.gate-transition-progress')).not.toBeNull();
    expect(fixture.nativeElement.querySelector('.gate-head button')).toBeNull();

    await arriveAtStrategy(fixture);

    expect(component.stage()).toBe(3);
    expect(component.state()).toBe('review');
    // The queries the worker proposed are on screen with no reload or remount.
    expect(component.queries().map((row) => row.text)).toEqual(STRATEGY.queries);

    await component.next(); // approval two
    await settle(fixture);

    expect(runs.sent.map((call) => call.method)).toEqual([
      'approveGateSummary',
      'approveGateLimitations',
      'approveGateStrategy',
    ]);
    expect(component.state()).toBe('resuming');
    expect(resumed).toEqual(['A', 'A', 'STRATEGY']);
    expect(text(fixture).toLowerCase()).toContain('starting your prior-art search');
    expect(text(fixture).toLowerCase()).toContain('searches you approved');
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
    await arriveAtStrategy(fixture);
    expect(component.stage()).toBe(3);
    expect(component.stageGate()).toBe('STRATEGY');
  });
});

describe('Advanced Search — the rules are enforced without being recited', () => {
  /**
   * The upfront "What this step requires" list is gone. What replaced it is
   * narrower and is asserted here: a reviewer who breaks a rule is told **which
   * one**, in words, at the moment they break it — and cannot approve until it
   * is fixed.
   *
   * Removing the list would be a real loss if the only signal were a disabled
   * button with no explanation. It is not.
   */
  it('never shows the requirements list on any stage', async () => {
    const runs = new FakeRuns();
    const fixture = await atStageOne(runs);
    const component = fixture.componentInstance;

    for (const stage of [1, 2] as const) {
      expect(text(fixture)).not.toContain('What this step requires');
      if (stage === 1) {
        await component.next();
        await settle(fixture);
      }
    }
    expect(text(fixture)).not.toContain('What this step requires');
  });

  it('names the rule it broke, at the moment it is broken', async () => {
    const runs = new FakeRuns();
    const fixture = await atStageOne(runs);
    const component = fixture.componentInstance;

    component.summary.set('   ');
    await settle(fixture);

    const shown = text(fixture);
    expect(shown).toContain('technical summary');
    expect(component.violations()).toHaveLength(1);
  });

  it('will not approve while a rule is broken', async () => {
    const runs = new FakeRuns();
    const fixture = await atStageOne(runs);
    const component = fixture.componentInstance;

    component.summary.set('');
    await component.next();
    await settle(fixture);

    // Still on stage 1, and nothing was sent.
    expect(component.stage()).toBe(1);
    expect(runs.sent).toEqual([]);
  });
});

describe('Advanced Search — the audit and disclaimer footer is gone', () => {
  it('shows neither the audit note nor the legal disclaimer on any stage', async () => {
    const runs = new FakeRuns();
    const fixture = await atStageOne(runs);
    const component = fixture.componentInstance;

    const banned = [
      'append-only audit trail',
      'no automatic approval',
      'not a legal opinion',
      'exhaustive prior-art search',
      'supplied passages',
    ];

    for (const stage of [1, 2] as const) {
      const shown = text(fixture).toLowerCase();
      for (const phrase of banned) {
        expect(shown, `"${phrase}" still on stage ${stage}`).not.toContain(phrase.toLowerCase());
      }
      if (stage === 1) {
        await component.next();
        await settle(fixture);
      }
    }
  });
});

describe('Advanced Search — one back control, and it is labelled', () => {
  /**
   * There used to be two: a chevron in the header and "Not yet" in the footer,
   * both emitting the same event and both calling `previous()`. Two controls
   * doing the identical thing on one screen is a choice the reviewer has to
   * work out rather than read, and the icon-only one said nothing about what it
   * did.
   */
  it('renders exactly one control that goes back', async () => {
    const runs = new FakeRuns();
    const fixture = await atStageOne(runs);
    const host = fixture.nativeElement as HTMLElement;

    const backs = [...host.querySelectorAll('button')].filter(
      (b) => /back/i.test(b.textContent ?? '') || /back/i.test(b.getAttribute('aria-label') ?? ''),
    );
    expect(backs).toHaveLength(1);
    expect(backs[0].textContent?.trim()).toBe('Back');
  });

  it('no longer says "Not yet"', async () => {
    const runs = new FakeRuns();
    const fixture = await atStageOne(runs);
    expect(text(fixture)).not.toContain('Not yet');
  });

  it('still steps back a stage', async () => {
    const runs = new FakeRuns();
    const fixture = await atStageOne(runs);
    const component = fixture.componentInstance;

    await component.next();
    await settle(fixture);
    expect(component.stage()).toBe(2);

    component.previous();
    await settle(fixture);
    expect(component.stage()).toBe(1);
  });
});
