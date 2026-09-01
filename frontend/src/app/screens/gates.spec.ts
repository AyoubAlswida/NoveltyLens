import { ComponentFixture, TestBed } from '@angular/core/testing';
import type { Type } from '@angular/core';
import { beforeEach, describe, expect, it } from 'vitest';
import { UnderstandingReview } from './understanding-review';
import { StrategyReview } from './strategy-review';
import { CandidateReview } from './candidate-review';
import { EvidenceReview } from './evidence-review';
import { DraftReview } from './draft-review';
import { VerificationReport } from './verification-report';
import { RunApiError, RunsService } from '../services/runs';

/**
 * The workflow-gate screens, rendered and driven.
 *
 * These assert **behaviour**, not source text: what the component does when the
 * backend answers a certain way, what it refuses to send, and what the user
 * actually sees. The Python contract tests remain as a second layer for the
 * things a component test cannot see (the dark-mode CSS pairing, the
 * backend/frontend vocabulary agreement).
 *
 * `RunsService` is the only seam stubbed. Nothing here reaches the network.
 */

class FakeRuns {
  gateAPayload: unknown = { candidate_decomposition: null, completion_rules: [] };
  gateStrategyPayload: unknown = {
    candidate_search_strategy: null,
    elements: [],
    completion_rules: [],
  };
  gateBPayload: unknown = { ranked_candidates: [], excluded: [], completion_rules: [] };
  gateCPayload: unknown = { matrix: [], blocking: [], completion_rules: [] };
  cellPayload: unknown = { machine_result: {}, passages: [] };
  reportPayload: unknown = { version: 1, status: 'DRAFT', draft: {} };

  failWith: RunApiError | null = null;
  lastFailureValue: { code: string; detail: string; blocking?: Array<Record<string, unknown>> } | null = null;
  readonly sent: Array<{ method: string; args: unknown[] }> = [];

  private maybeFail<T>(value: T): Promise<T> {
    return this.failWith ? Promise.reject(this.failWith) : Promise.resolve(value);
  }

  newKey() {
    return 'key-1';
  }
  lastFailure() {
    return this.lastFailureValue;
  }
  gateA() {
    return this.maybeFail(this.gateAPayload as Record<string, unknown>);
  }
  gateStrategy() {
    return this.maybeFail(this.gateStrategyPayload as Record<string, unknown>);
  }
  gateB() {
    return this.maybeFail(this.gateBPayload as Record<string, unknown>);
  }
  gateC() {
    return this.maybeFail(this.gateCPayload as Record<string, unknown>);
  }
  cell() {
    return this.maybeFail(this.cellPayload as Record<string, unknown>);
  }
  report() {
    return this.maybeFail(this.reportPayload as Record<string, unknown>);
  }
  approveGateA(...args: unknown[]) {
    this.sent.push({ method: 'approveGateA', args });
    return this.maybeFail({});
  }
  regenerateUnderstanding(...args: unknown[]) {
    this.sent.push({ method: 'regenerateUnderstanding', args });
    return this.maybeFail({});
  }
  approveGateStrategy(...args: unknown[]) {
    this.sent.push({ method: 'approveGateStrategy', args });
    return this.maybeFail({});
  }
  regenerateStrategy(...args: unknown[]) {
    this.sent.push({ method: 'regenerateStrategy', args });
    return this.maybeFail({});
  }
  approveGateB(...args: unknown[]) {
    this.sent.push({ method: 'approveGateB', args });
    return this.maybeFail({});
  }
  addCandidate(...args: unknown[]) {
    this.sent.push({ method: 'addCandidate', args });
    return this.maybeFail({ already_requested: false });
  }
  reviewCell(...args: unknown[]) {
    this.sent.push({ method: 'reviewCell', args });
    return this.maybeFail({});
  }
  waiveCell(...args: unknown[]) {
    this.sent.push({ method: 'waiveCell', args });
    return this.maybeFail({});
  }
  rerunCell(...args: unknown[]) {
    this.sent.push({ method: 'rerunCell', args });
    return this.maybeFail({ result_version: 2 });
  }
  completeGateC(...args: unknown[]) {
    this.sent.push({ method: 'completeGateC', args });
    return this.maybeFail({});
  }
  approveGateD(...args: unknown[]) {
    this.sent.push({ method: 'approveGateD', args });
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

function mount<T>(type: Type<T>, runs: FakeRuns, inputs: Record<string, unknown>) {
  TestBed.configureTestingModule({
    imports: [type],
    providers: [{ provide: RunsService, useValue: runs }],
  });
  const fixture = TestBed.createComponent(type);
  for (const [key, value] of Object.entries(inputs)) {
    fixture.componentRef.setInput(key, value);
  }
  fixture.detectChanges();
  return fixture;
}

// --- Invention Understanding (the stage formerly rendered as Gate A) --------

describe('Invention Understanding', () => {
  let runs: FakeRuns;

  const PROPOSAL = {
    original_description: 'A bottle that filters and sterilises water as you drink.',
    problem_solved: 'Membranes foul and nobody notices.',
    inventor_novelty_statement: 'Automatic detection of fouling.',
    limits: { max_capabilities: 5, max_elements_per_capability: 8, max_elements_total: 25 },
    candidate_decomposition: {
      version: 2,
      summary: 'A portable device filtering through a membrane and sterilising with UV-C.',
      features: [
        {
          feature_id: 'F1',
          feature_text: 'flow-triggered self-cleaning',
          elements: [
            { element_id: 'E1', text: 'a graphene-oxide membrane', core: true },
            { element_id: 'E2', text: 'a flow rate sensor', core: true },
          ],
          relationships: [{ relationship_id: 'R1', text: 'E1 is monitored by E2', order: 1 }],
        },
      ],
    },
  };

  beforeEach(() => {
    TestBed.resetTestingModule();
    runs = new FakeRuns();
    runs.gateAPayload = structuredClone(PROPOSAL);
  });

  it('shows the inventor original description without making it editable', async () => {
    const fixture = mount(UnderstandingReview, runs, { runId: 'r1' });
    await settle(fixture);

    expect(text(fixture)).toContain('A bottle that filters and sterilises water');
    expect(text(fixture)).toContain('This is never changed.');
    // The description is rendered in a blockquote, never in a form control -- a
    // textarea holding it would invite an edit the backend would never store.
    const editable = (fixture.nativeElement as HTMLElement).querySelectorAll(
      'textarea, input[type="text"]',
    );
    for (const node of Array.from(editable)) {
      expect((node as HTMLInputElement).value).not.toContain('A bottle that filters');
    }
  });

  it('labels the summary as the system interpretation and lets it be edited', async () => {
    const fixture = mount(UnderstandingReview, runs, { runId: 'r1' });
    await settle(fixture);

    expect(text(fixture)).toContain('How the system understood it');
    expect(text(fixture)).toContain("This is the system's interpretation, not your description.");
    expect(fixture.componentInstance.summary()).toContain('A portable device filtering');
  });

  it('refuses to approve an empty summary', async () => {
    const fixture = mount(UnderstandingReview, runs, { runId: 'r1' });
    await settle(fixture);
    const component = fixture.componentInstance;

    component.summary.set('   ');
    fixture.detectChanges();
    expect(component.violations().some((v) => v.includes('summary'))).toBe(true);

    await component.approve();
    expect(runs.sent).toHaveLength(0);
  });

  it('refuses to approve when nothing is marked core', async () => {
    const fixture = mount(UnderstandingReview, runs, { runId: 'r1' });
    await settle(fixture);
    const component = fixture.componentInstance;

    for (const capability of component.capabilities()) {
      for (const element of capability.elements) element.core = false;
    }
    fixture.detectChanges();
    expect(component.violations().some((v) => v.includes('core'))).toBe(true);

    await component.approve();
    expect(runs.sent).toHaveLength(0);
    expect(text(fixture)).toContain('Not ready to approve');
  });

  it('refuses to approve an element with no text', async () => {
    const fixture = mount(UnderstandingReview, runs, { runId: 'r1' });
    await settle(fixture);
    const component = fixture.componentInstance;

    component.capabilities()[0].elements[0].text = '  ';
    fixture.detectChanges();
    expect(component.violations().some((v) => v.includes('non-empty'))).toBe(true);
  });

  it('lets a reviewer add and delete elements and capabilities', async () => {
    const fixture = mount(UnderstandingReview, runs, { runId: 'r1' });
    await settle(fixture);
    const component = fixture.componentInstance;

    const capability = component.capabilities()[0];
    component.addElement(capability);
    expect(component.capabilities()[0].elements).toHaveLength(3);
    component.removeElement(component.capabilities()[0], 0);
    expect(component.capabilities()[0].elements).toHaveLength(2);

    component.addCapability();
    expect(component.capabilities()).toHaveLength(2);
    component.removeCapability(1);
    expect(component.capabilities()).toHaveLength(1);
  });

  it('mints an element id above the highest present, not from the count', async () => {
    const fixture = mount(UnderstandingReview, runs, { runId: 'r1' });
    await settle(fixture);
    const component = fixture.componentInstance;

    // Delete E1, leaving E2, then add: the new element must not become a second E2.
    component.removeElement(component.capabilities()[0], 0);
    component.addElement(component.capabilities()[0]);
    const ids = component.capabilities()[0].elements.map((element) => element.id);
    expect(new Set(ids).size).toBe(ids.length);
    expect(ids).toContain('E3');
  });

  it('enforces the server limits it was given rather than restating them', async () => {
    runs.gateAPayload = {
      ...structuredClone(PROPOSAL),
      limits: { max_capabilities: 1, max_elements_per_capability: 2, max_elements_total: 3 },
    };
    const fixture = mount(UnderstandingReview, runs, { runId: 'r1' });
    await settle(fixture);
    const component = fixture.componentInstance;

    expect(component.maxCapabilities()).toBe(1);
    // At the capability limit already, so adding one is refused here rather than
    // by a 409 after the reviewer has finished editing.
    component.addCapability();
    expect(component.capabilities()).toHaveLength(1);
  });

  it('drops relationships when the elements they describe were edited', async () => {
    const fixture = mount(UnderstandingReview, runs, { runId: 'r1' });
    await settle(fixture);
    const component = fixture.componentInstance;

    // Delete E2. "E1 is monitored by E2" now names an element that is gone, and
    // sending it would be a requirement the verifier cannot check.
    component.removeElement(component.capabilities()[0], 1);
    await component.approve();

    const payload = runs.sent[0].args[1] as {
      features: Array<{ relationships: unknown[] }>;
    };
    expect(payload.features[0].relationships).toHaveLength(0);
  });

  it('keeps relationships when the elements were not touched', async () => {
    const fixture = mount(UnderstandingReview, runs, { runId: 'r1' });
    await settle(fixture);
    await fixture.componentInstance.approve();

    const payload = runs.sent[0].args[1] as {
      version: number;
      summary: string;
      features: Array<{ elements: unknown[]; relationships: unknown[] }>;
    };
    expect(payload.version).toBe(2);
    expect(payload.summary).toContain('A portable device filtering');
    expect(payload.features[0].elements).toHaveLength(2);
    expect(payload.features[0].relationships).toHaveLength(1);
  });

  it('asks for a regeneration without approving anything', async () => {
    const fixture = mount(UnderstandingReview, runs, { runId: 'r1' });
    await settle(fixture);
    const component = fixture.componentInstance;

    component.feedback = 'you missed the UV stage';
    await component.regenerate();

    expect(runs.sent[0].method).toBe('regenerateUnderstanding');
    expect(runs.sent[0].args[1]).toBe(2);
    expect(runs.sent[0].args[2]).toBe('you missed the UV stage');
    // A rejection is not an approval.
    expect(runs.sent.some((call) => call.method === 'approveGateA')).toBe(false);
    expect(component.state()).toBe('resuming');
  });

  it('shows an empty proposal as empty, not as a failure', async () => {
    runs.gateAPayload = { candidate_decomposition: null };
    const fixture = mount(UnderstandingReview, runs, { runId: 'r1' });
    await settle(fixture);
    expect(fixture.componentInstance.state()).toBe('empty');
  });

  it('renders access denial without guessing which kind of 404 it got', async () => {
    runs.failWith = failure('RUN_NOT_FOUND', 404);
    const fixture = mount(UnderstandingReview, runs, { runId: 'r1' });
    await settle(fixture);

    expect(fixture.componentInstance.state()).toBe('forbidden');
    expect(text(fixture)).toContain('does not distinguish');
  });

  it('never resubmits a decision the backend called stale', async () => {
    const fixture = mount(UnderstandingReview, runs, { runId: 'r1' });
    await settle(fixture);

    runs.failWith = failure('STALE_RESULT_VERSION');
    await fixture.componentInstance.approve();
    await settle(fixture);

    expect(fixture.componentInstance.state()).toBe('stale');
    // One attempt. No automatic retry against the new version.
    expect(runs.sent.filter((call) => call.method === 'approveGateA')).toHaveLength(1);
  });

  it('shows no internal gate, checkpoint or decomposition vocabulary', async () => {
    const fixture = mount(UnderstandingReview, runs, { runId: 'r1' });
    await settle(fixture);
    const rendered = text(fixture).toLowerCase();

    for (const word of ['gate a', 'decomposition', 'checkpoint', 'verification cell']) {
      expect(rendered).not.toContain(word);
    }
  });
});

// --- Workflow Gate B -------------------------------------------------------

// --- Search strategy (the stage between the understanding and the search) ---

describe('Search strategy', () => {
  let runs: FakeRuns;

  const STRATEGY = {
    approved_summary: 'A portable device filtering through a membrane and sterilising with UV-C.',
    understanding_version: 2,
    limits: { max_queries: 10 },
    elements: [
      { id: 'F1.E1', feature_id: 'F1', feature_text: 'filtering', text: 'a graphene-oxide membrane', core: true },
      { id: 'F1.E2', feature_id: 'F1', feature_text: 'filtering', text: 'a flow rate sensor', core: true },
      { id: 'F1.E3', feature_id: 'F1', feature_text: 'filtering', text: 'a cleaning actuator', core: false },
    ],
    candidate_search_strategy: {
      version: 1,
      understanding_version: 2,
      queries: [
        {
          query_id: 'Q1',
          query_text: 'graphene oxide filtration membrane',
          rationale: 'Looks for the membrane itself.',
          enabled: true,
          covered_element_ids: ['F1.E1'],
        },
        {
          query_id: 'Q2',
          query_text: 'flow rate sensor triggered cleaning',
          rationale: 'Looks for the sensing and the cycle.',
          enabled: true,
          covered_element_ids: ['F1.E2', 'F1.E3'],
        },
      ],
    },
  };

  beforeEach(() => {
    TestBed.resetTestingModule();
    runs = new FakeRuns();
    runs.gateStrategyPayload = structuredClone(STRATEGY);
  });

  it('names the stage without using any internal gate vocabulary', async () => {
    const fixture = mount(StrategyReview, runs, { runId: 'r1' });
    await settle(fixture);

    const rendered = text(fixture).toLowerCase();
    expect(rendered).toContain('search strategy');
    expect(rendered).toContain('ai-proposed query coverage');
    for (const internal of ['gate a', 'gate strategy', 'decomposition', 'verification cell']) {
      expect(rendered).not.toContain(internal);
    }
  });

  it('shows the approved understanding read-only beside the queries', async () => {
    const fixture = mount(StrategyReview, runs, { runId: 'r1' });
    await settle(fixture);

    expect(text(fixture)).toContain('A portable device filtering through a membrane');
    expect(text(fixture)).toContain('Not editable here.');
    const editable = (fixture.nativeElement as HTMLElement).querySelectorAll('textarea, input[type="text"]');
    for (const node of Array.from(editable)) {
      expect((node as HTMLInputElement).value).not.toContain('A portable device filtering');
    }
  });

  it('blocks approval while a core element has no query that will run', async () => {
    const fixture = mount(StrategyReview, runs, { runId: 'r1' });
    await settle(fixture);
    const component = fixture.componentInstance;
    expect(component.violations()).toEqual([]);

    // Switching Q1 off leaves F1.E1 -- a CORE element -- unsearched. That is the
    // one thing this stage exists to refuse: reporting no prior art for
    // something nobody searched for is not a finding.
    component.queries()[0].enabled = false;
    component.touch();
    fixture.detectChanges();

    expect(component.violations().join(' ')).toContain('F1.E1');
    expect(component.covered().has('F1.E1')).toBe(false);
    expect(text(fixture)).toContain('not searched for');
  });

  it('does not block on an uncovered NON-core element', async () => {
    const fixture = mount(StrategyReview, runs, { runId: 'r1' });
    await settle(fixture);
    const component = fixture.componentInstance;

    component.toggleCoverage(component.queries()[1], 'F1.E3');
    fixture.detectChanges();

    expect(component.covered().has('F1.E3')).toBe(false);
    expect(component.violations()).toEqual([]);
  });

  it('refuses to approve when every query is switched off', async () => {
    const fixture = mount(StrategyReview, runs, { runId: 'r1' });
    await settle(fixture);
    const component = fixture.componentInstance;

    for (const query of component.queries()) query.enabled = false;
    component.touch();

    expect(component.violations().join(' ')).toContain('switched on');
    await component.approve();
    expect(runs.sent).toEqual([]);
  });

  it('sends disabled queries too, so the record says what was not searched for', async () => {
    const fixture = mount(StrategyReview, runs, { runId: 'r1' });
    await settle(fixture);
    const component = fixture.componentInstance;

    component.queries()[1].enabled = false;
    component.toggleCoverage(component.queries()[0], 'F1.E2');
    component.touch();
    await component.approve();

    const sent = runs.sent.find((call) => call.method === 'approveGateStrategy');
    expect(sent).toBeTruthy();
    const strategy = sent!.args[1] as { queries: Array<{ query_id: string; enabled: boolean }> };
    expect(strategy.queries.map((query) => query.query_id)).toEqual(['Q1', 'Q2']);
    expect(strategy.queries.find((query) => query.query_id === 'Q2')!.enabled).toBe(false);
  });

  it('carries the approved analysis version it covers', async () => {
    const fixture = mount(StrategyReview, runs, { runId: 'r1' });
    await settle(fixture);
    await fixture.componentInstance.approve();

    const sent = runs.sent.find((call) => call.method === 'approveGateStrategy');
    const strategy = sent!.args[1] as { understanding_version: number; version: number };
    expect(strategy.understanding_version).toBe(2);
    expect(strategy.version).toBe(1);
  });

  it('asks for fresh queries as a request and never approves anything', async () => {
    const fixture = mount(StrategyReview, runs, { runId: 'r1' });
    await settle(fixture);

    fixture.componentInstance.feedback = 'too narrow';
    await fixture.componentInstance.regenerate();

    const sent = runs.sent.find((call) => call.method === 'regenerateStrategy');
    expect(sent!.args.slice(0, 3)).toEqual(['r1', 1, 'too narrow']);
    expect(runs.sent.some((call) => call.method === 'approveGateStrategy')).toBe(false);
    expect(fixture.componentInstance.state()).toBe('resuming');
  });

  it('mints a query id above the highest present rather than from the count', async () => {
    const fixture = mount(StrategyReview, runs, { runId: 'r1' });
    await settle(fixture);
    const component = fixture.componentInstance;

    component.removeQuery(0);
    component.addQuery();

    expect(component.queries().map((query) => query.id)).toEqual(['Q2', 'Q3']);
  });

  it('reports a run it does not own as forbidden, not as a failure', async () => {
    runs.failWith = failure('RUN_NOT_FOUND', 404);
    const fixture = mount(StrategyReview, runs, { runId: 'r1' });
    await settle(fixture);

    expect(fixture.componentInstance.state()).toBe('forbidden');
  });

  it('renders an empty proposal as empty rather than as a failed load', async () => {
    runs.gateStrategyPayload = { ...structuredClone(STRATEGY), candidate_search_strategy: null };
    const fixture = mount(StrategyReview, runs, { runId: 'r1' });
    await settle(fixture);

    expect(fixture.componentInstance.state()).toBe('empty');
  });
});

describe('Gate B — candidate and eligibility review', () => {
  let runs: FakeRuns;

  beforeEach(() => {
    TestBed.resetTestingModule();
    runs = new FakeRuns();
    runs.gateBPayload = {
      ranked_candidates: [{ publication_number: 'EP1', passages_built: 3 }],
      excluded: [
        { requested_number: 'US9', state: 'EXCLUDED_NO_FULL_TEXT', reason: 'abstract only' },
        { requested_number: 'CN1', state: 'EXCLUDED_DATE', reason: 'after the cutoff' },
        { requested_number: 'DE2', state: 'EXCLUDED_INELIGIBLE', reason: 'no locator' },
      ],
    };
  });

  it('renders exclusions in their own table, never as candidates', async () => {
    const fixture = mount(CandidateReview, runs, { runId: 'r1' });
    await settle(fixture);
    const html = (fixture.nativeElement as HTMLElement).innerHTML;

    expect(html).toContain('exclusion-table');
    expect(html).toContain('EXCLUDED_NO_FULL_TEXT');
    // The exclusion table exists separately from the candidate table.
    expect(html).toContain('candidate-table');
    expect(text(fixture)).toContain('not evaluated');
  });

  it('marks date and full-text exclusions as not overridable', async () => {
    const fixture = mount(CandidateReview, runs, { runId: 'r1' });
    await settle(fixture);
    const component = fixture.componentInstance;

    expect(component.overridable('EXCLUDED_DATE')).toBe(false);
    expect(component.overridable('EXCLUDED_NO_FULL_TEXT')).toBe(false);
    expect(component.overridable('EXCLUDED_INELIGIBLE')).toBe(true);
  });

  it('approves only the publications the reviewer left ticked', async () => {
    runs.gateBPayload = {
      ranked_candidates: [
        { publication_number: 'EP1' },
        { publication_number: 'EP2' },
      ],
      excluded: [],
    };
    const fixture = mount(CandidateReview, runs, { runId: 'r1' });
    await settle(fixture);

    fixture.componentInstance.toggle('EP2');
    await fixture.componentInstance.approve();

    expect(runs.sent[0].args[1]).toEqual(['EP1']);
  });

  it('says a manually added publication is queued, not approved', async () => {
    const fixture = mount(CandidateReview, runs, { runId: 'r1' });
    await settle(fixture);
    const component = fixture.componentInstance;

    component.newPublication = 'EP4778509';
    await component.addCandidate();
    await settle(fixture);

    expect(runs.sent[0].method).toBe('addCandidate');
    expect(component.addedNote()).toContain('not approved');
  });
});

// --- Workflow Gate C -------------------------------------------------------

describe('Gate C — evidence review', () => {
  let runs: FakeRuns;

  beforeEach(() => {
    TestBed.resetTestingModule();
    runs = new FakeRuns();
    runs.gateCPayload = {
      matrix: [
        { cell_id: 'c1', feature_id: 'F2', publication_number: 'EP1', state: 'VERIFIED', outcome: 'PARTIALLY_DISCLOSED_IN_SUPPLIED_PASSAGES', result_version: 1 },
        { cell_id: 'c2', feature_id: 'F2', publication_number: 'EP2', state: 'VALIDATION_FAILURE', failed_check: 'check 15', result_version: 1 },
        { cell_id: 'c3', feature_id: 'F2', publication_number: 'EP3', state: 'NOT_EVALUATED', reason: 'no passage', result_version: 1 },
      ],
      blocking: [],
    };
  });

  it('renders every non-verdict state as itself', async () => {
    const fixture = mount(EvidenceReview, runs, { runId: 'r1' });
    await settle(fixture);
    const html = (fixture.nativeElement as HTMLElement).innerHTML;

    for (const state of ['VERIFIED', 'VALIDATION_FAILURE', 'NOT_EVALUATED']) {
      expect(html).toContain(state);
    }
    // Each carries its own data attribute so CSS can style it distinctly.
    expect(html).toContain('data-cell-state="VALIDATION_FAILURE"');
    expect(html).toContain('data-cell-state="NOT_EVALUATED"');
  });

  it('refuses an overlay on a VALIDATION_FAILURE (check 0)', async () => {
    const fixture = mount(EvidenceReview, runs, { runId: 'r1' });
    await settle(fixture);
    const component = fixture.componentInstance;

    await component.open({ cell_id: 'c2', feature_id: 'F2', publication_number: 'EP2', state: 'VALIDATION_FAILURE', result_version: 1 });
    await settle(fixture);

    expect(component.canReview()).toBe(false);
    await component.saveReview();
    expect(runs.sent.filter((c) => c.method === 'reviewCell')).toHaveLength(0);
  });

  it('derives the reviewed outcome and never accepts one as input', async () => {
    const fixture = mount(EvidenceReview, runs, { runId: 'r1' });
    await settle(fixture);
    const component = fixture.componentInstance;

    component.requirements.set([
      { element_id: 'E1', status: 'FOUND', evidence: [] },
      { element_id: 'E2', status: 'NOT_FOUND_IN_SUPPLIED_PASSAGES', evidence: [] },
    ]);
    expect(component.derivedOutcome()).toBe('PARTIALLY_DISCLOSED_IN_SUPPLIED_PASSAGES');

    component.setOverride('E2', 'FOUND');
    expect(component.derivedOutcome()).toBe('FULLY_DISCLOSED_IN_SUPPLIED_PASSAGES');

    component.setOverride('E2', 'UNCLEAR');
    expect(component.derivedOutcome()).toBe('UNCLEAR');
  });

  it('sends the reviewed statuses beside the machine ones, never merged', async () => {
    const fixture = mount(EvidenceReview, runs, { runId: 'r1' });
    await settle(fixture);
    const component = fixture.componentInstance;

    await component.open({ cell_id: 'c1', feature_id: 'F2', publication_number: 'EP1', state: 'VERIFIED', result_version: 1 });
    component.requirements.set([{ element_id: 'E1', status: 'FOUND', evidence: [] }]);
    component.setOverride('E1', 'UNCLEAR');
    await component.saveReview();

    const overlay = runs.sent.find((c) => c.method === 'reviewCell')?.args[2] as {
      element_reviews: Array<{ machine_status: string; reviewed_status: string }>;
    };
    expect(overlay.element_reviews[0].machine_status).toBe('FOUND');
    expect(overlay.element_reviews[0].reviewed_status).toBe('UNCLEAR');
  });

  it('requires both a reason and an unresolved limitation before waiving', async () => {
    const fixture = mount(EvidenceReview, runs, { runId: 'r1' });
    await settle(fixture);
    const component = fixture.componentInstance;
    await component.open({ cell_id: 'c2', feature_id: 'F2', publication_number: 'EP2', state: 'VALIDATION_FAILURE', result_version: 1 });

    component.waiverReason = 'provider outage';
    component.waiverLimitation = '';
    await component.saveWaiver();
    expect(runs.sent.filter((c) => c.method === 'waiveCell')).toHaveLength(0);

    component.waiverLimitation = 'F2 was never evaluated against EP2';
    await component.saveWaiver();
    expect(runs.sent.filter((c) => c.method === 'waiveCell')).toHaveLength(1);
  });

  it('names the version a rerun will produce and keeps the current one', async () => {
    const fixture = mount(EvidenceReview, runs, { runId: 'r1' });
    await settle(fixture);
    const component = fixture.componentInstance;

    await component.open({ cell_id: 'c1', feature_id: 'F2', publication_number: 'EP1', state: 'VERIFIED', result_version: 1 });
    await component.requestRerun();

    const call = runs.sent.find((c) => c.method === 'rerunCell');
    expect(call?.args[3]).toBe(1); // source_result_version: what the reviewer saw
    expect(component.rerunNote()).toContain('version 2');
    expect(component.rerunNote()).toContain('stays retrievable');
  });

  it('highlights the exact quote inside the passage it came from', async () => {
    const fixture = mount(EvidenceReview, runs, { runId: 'r1' });
    await settle(fixture);
    const component = fixture.componentInstance;

    component.requirements.set([
      {
        element_id: 'E1',
        status: 'FOUND',
        evidence: [{ chunk_id: 'p1', locator: 'claim 1', exact_quote: 'heat exchanger' }],
      },
    ]);
    const rendered = component.highlighted({
      chunk_id: 'p1',
      section: 'CLAIMS',
      locator: 'claim 1',
      text: 'A system comprising a heat exchanger in the duct.',
    });
    expect(rendered).toContain('<mark class="quote-hit">heat exchanger</mark>');
  });

  it('does not highlight a quote that is not a verbatim substring', async () => {
    // Same plain substring test §8 applies before the call. No normalisation, no
    // case folding, no fuzzy matching -- and the absence is itself informative.
    const fixture = mount(EvidenceReview, runs, { runId: 'r1' });
    await settle(fixture);
    const component = fixture.componentInstance;

    component.requirements.set([
      {
        element_id: 'E1',
        status: 'FOUND',
        evidence: [{ chunk_id: 'p1', locator: 'claim 1', exact_quote: 'HEAT EXCHANGER' }],
      },
    ]);
    const rendered = component.highlighted({
      chunk_id: 'p1',
      section: 'CLAIMS',
      locator: 'claim 1',
      text: 'A system comprising a heat exchanger.',
    });
    expect(rendered).not.toContain('<mark');
  });

  it('lists every blocking condition rather than the first', async () => {
    runs.gateCPayload = {
      matrix: [{ cell_id: 'c1', feature_id: 'F', publication_number: 'E', state: 'PENDING', result_version: 1 }],
      blocking: [
        { cell_id: 'c1', reason: 'still PENDING' },
        { cell_id: 'c2', reason: 'unresolved UNCLEAR' },
      ],
    };
    const fixture = mount(EvidenceReview, runs, { runId: 'r1' });
    await settle(fixture);

    expect(fixture.componentInstance.state()).toBe('blocked');
    expect(text(fixture)).toContain('still PENDING');
    expect(text(fixture)).toContain('unresolved UNCLEAR');
    expect(text(fixture)).toContain('not just the first');
  });
});

// --- Workflow Gate D and the report ---------------------------------------

describe('Gate D — draft review', () => {
  let runs: FakeRuns;

  beforeEach(() => {
    TestBed.resetTestingModule();
    runs = new FakeRuns();
  });

  it('offers no approval when no draft exists (case AC)', async () => {
    runs.reportPayload = { version: null, status: 'DRAFT', draft: {} };
    const fixture = mount(DraftReview, runs, { runId: 'r1' });
    await settle(fixture);

    expect(fixture.componentInstance.version()).toBeNull();
    expect(text(fixture)).toContain('cannot be approved into existence');
    await fixture.componentInstance.approve();
    expect(runs.sent).toHaveLength(0);
  });

  it('approves the version it was shown', async () => {
    runs.reportPayload = { version: 3, status: 'DRAFT', draft: { report_version: 3 } };
    const fixture = mount(DraftReview, runs, { runId: 'r1' });
    await settle(fixture);
    await fixture.componentInstance.approve();

    expect(runs.sent[0].method).toBe('approveGateD');
    expect(runs.sent[0].args[1]).toBe(3);
  });
});

describe('VerificationReport', () => {
  beforeEach(() => TestBed.resetTestingModule());

  function render(report: Record<string, unknown>, status = 'DRAFT') {
    TestBed.configureTestingModule({ imports: [VerificationReport] });
    const fixture = TestBed.createComponent(VerificationReport);
    fixture.componentRef.setInput('report', report);
    fixture.componentRef.setInput('status', status);
    fixture.detectChanges();
    return fixture;
  }

  it('marks a draft as DRAFT — NOT APPROVED', () => {
    const fixture = render({ report_version: 1 });
    expect(text(fixture)).toContain('DRAFT — NOT APPROVED');
  });

  it('marks an approved report as APPROVED', () => {
    const fixture = render({ report_version: 1 }, 'APPROVED');
    expect(text(fixture)).toContain('APPROVED');
    expect(text(fixture)).not.toContain('NOT APPROVED');
  });

  it('keeps exclusions out of the matrix and says they were not evaluated', () => {
    const fixture = render({
      matrix: [{ cell_id: 'c1', feature_id: 'F2', publication_number: 'EP1', state: 'VERIFIED' }],
      excluded_publications: [
        { requested_number: 'US9', state: 'EXCLUDED_NO_FULL_TEXT', reason: 'abstract only' },
      ],
    });
    const html = (fixture.nativeElement as HTMLElement).innerHTML;
    const matrix = html.split('report-matrix')[1].split('report-exclusions')[0];

    expect(matrix).not.toContain('US9');
    expect(text(fixture)).toContain('not evaluated');
  });

  it('shows every waiver with its unresolved limitation', () => {
    const fixture = render({
      waivers: [
        {
          cell_id: 'c9',
          reason: 'provider outage',
          unresolved_limitation: 'F5 was never evaluated against EP4778509.',
        },
      ],
    });
    expect(text(fixture)).toContain('F5 was never evaluated against EP4778509.');
  });

  it('always carries the non-legal disclaimer', () => {
    const rendered = text(render({}));
    expect(rendered).toContain('not a legal opinion');
    expect(rendered).toContain('not an exhaustive prior-art search');
    expect(rendered).toContain('does');
    expect(rendered).toContain('novel');
  });

  it('never claims a guaranteed novelty determination', () => {
    // Substring matching alone is the wrong test here: the disclaimer legitimately
    // contains "is novel" inside "does not determine that anything is novel". What
    // must be absent is an AFFIRMATIVE claim, so the forbidden phrases are ones
    // that cannot appear inside a negation.
    const rendered = text(render({ matrix: [], excluded_publications: [] })).toLowerCase();
    for (const forbidden of [
      'guarantees novelty',
      'confirms novelty',
      'this invention is novel',
      'legal advice',
      'we searched all',
      'complete prior-art search',
    ]) {
      expect(rendered).not.toContain(forbidden);
    }
    // And the negations themselves must be present, so a future edit that drops
    // the disclaimer cannot pass this test by saying nothing at all.
    expect(rendered).toContain('not a legal opinion');
    expect(rendered).toContain('not an exhaustive prior-art search');
    expect(rendered).toContain('not determine that anything is novel');
  });

  it('keeps machine and human decisions in separate columns', () => {
    const fixture = render({
      matrix: [
        {
          cell_id: 'c1',
          feature_id: 'F2',
          publication_number: 'EP1',
          state: 'VERIFIED',
          outcome: 'PARTIALLY_DISCLOSED_IN_SUPPLIED_PASSAGES',
          review: { status: 'OVERRIDDEN' },
          class: 'human-overridden',
        },
      ],
    });
    const html = (fixture.nativeElement as HTMLElement).innerHTML;
    expect(html).toContain('cell-state');
    expect(html).toContain('human-decision');
    expect(html).toContain('data-cell-class="human-overridden"');
  });
});

// --- Phase 4: results and evidence, and the final report -------------------
//
// Phases 1 and 2 rebuilt the invention-understanding and search-strategy stages
// to speak to an inventor. These cover the two stages after them and the report
// a reader keeps: the words on the screen, and the four places the report read a
// shape the draft builder does not produce.

describe('Results and evidence', () => {
  let runs: FakeRuns;

  beforeEach(() => {
    TestBed.resetTestingModule();
    runs = new FakeRuns();
  });

  it('names a negative result in words that keep its scope', async () => {
    runs.gateCPayload = {
      matrix: [
        {
          cell_id: 'CELL-1',
          feature_id: 'F1',
          publication_number: 'US11002233',
          state: 'VERIFIED',
          outcome: 'NO_DISCLOSURE_FOUND_IN_SUPPLIED_PASSAGES',
          result_version: 1,
        },
      ],
      blocking: [],
    };
    const fixture = mount(EvidenceReview, runs, { runId: 'r1' });
    await settle(fixture);
    const rendered = text(fixture);

    // The words a person reads keep the scope the code carries...
    expect(rendered).toContain('No requirement found in the passages examined');
    // ...and the recorded value is still on the screen beside them.
    expect(rendered).toContain('NO_DISCLOSURE_FOUND_IN_SUPPLIED_PASSAGES');
    // Never the shortened claim. This is the substitution CLAUDE.md forbids.
    expect(rendered).not.toContain('Not disclosed');
    expect(rendered).not.toContain('No disclosure.');
  });

  it('renders a comparison that produced no result as itself', async () => {
    runs.gateCPayload = {
      matrix: [
        {
          cell_id: 'CELL-2',
          feature_id: 'F1',
          publication_number: 'US7654321',
          state: 'VALIDATION_FAILURE',
          failed_check: 'check 15',
          result_version: 1,
        },
      ],
      blocking: [],
    };
    const fixture = mount(EvidenceReview, runs, { runId: 'r1' });
    await settle(fixture);
    const rendered = text(fixture);

    expect(rendered).toContain('Could not be validated');
    expect(rendered).toContain('VALIDATION_FAILURE');
    expect(rendered).toContain('check 15');

    // It is not a finding of any kind, so the ROW borrows no disclosure
    // wording. Scoped to the row rather than the screen: the screen also
    // carries the note explaining what the scoped wording means, and asserting
    // over the whole of it would only prove that note exists.
    const row = (fixture.nativeElement as HTMLElement).querySelector('tbody tr');
    expect(row).toBeTruthy();
    const rowText = row!.textContent ?? '';
    expect(rowText).toContain('Could not be validated');
    expect(rowText).not.toContain('found in the passages examined');
    expect(rowText).not.toContain('No requirement found');
  });

  it('shows no internal workflow vocabulary', async () => {
    runs.gateCPayload = {
      matrix: [
        {
          cell_id: 'CELL-1',
          feature_id: 'F1',
          publication_number: 'US11002233',
          state: 'VERIFIED',
          result_version: 1,
        },
      ],
      blocking: [],
    };
    const fixture = mount(EvidenceReview, runs, { runId: 'r1' });
    await settle(fixture);
    const rendered = text(fixture).toLowerCase();

    expect(rendered).toContain('results and evidence');
    for (const internal of ['gate c', 'decomposition', 'checkpoint', 'verification cell']) {
      expect(rendered).not.toContain(internal);
    }
  });

  it('says a publication was not examined, never that nothing was found in it', async () => {
    runs.gateBPayload = {
      ranked_candidates: [],
      excluded: [
        {
          requested_number: 'KR20110066580',
          state: 'EXCLUDED_NO_FULL_TEXT',
          reason: 'the provider answered and had no eligible text',
        },
      ],
    };
    const fixture = mount(CandidateReview, runs, { runId: 'r1' });
    await settle(fixture);
    const rendered = text(fixture);

    expect(rendered).toContain('Not examined, no full text available');
    expect(rendered).toContain('EXCLUDED_NO_FULL_TEXT');
    expect(rendered).toContain('not evaluated');
    // An exclusion is a statement about coverage, never about the publication.
    expect(rendered).not.toContain('no prior art');
  });
});

describe('The reviewed outcome mirrors the backend derivation', () => {
  let runs: FakeRuns;

  /** Open one cell whose requirements carry the given statuses. */
  async function openWith(statuses: string[]) {
    runs.gateCPayload = {
      matrix: [
        {
          cell_id: 'CELL-1',
          feature_id: 'F1',
          publication_number: 'US11002233',
          state: 'VERIFIED',
          result_version: 1,
        },
      ],
      blocking: [],
    };
    runs.cellPayload = {
      machine_result: {
        elements: statuses.map((status, index) => ({
          element_id: `E${index + 1}`,
          status,
          evidence: [],
        })),
        relationships: [],
      },
      passages: [],
    };
    const fixture = mount(EvidenceReview, runs, { runId: 'r1' });
    await settle(fixture);
    await fixture.componentInstance.open({
      cell_id: 'CELL-1',
      feature_id: 'F1',
      publication_number: 'US11002233',
      state: 'VERIFIED',
      result_version: 1,
    } as never);
    await settle(fixture);
    return fixture;
  }

  const FOUND = 'FOUND';
  const NOT_FOUND = 'NOT_FOUND_IN_SUPPLIED_PASSAGES';
  const UNCLEAR = 'UNCLEAR';

  beforeEach(() => {
    TestBed.resetTestingModule();
    runs = new FakeRuns();
  });

  /**
   * `verification/outcome.py::derive_outcome`, case for case.
   *
   * The screen shows this value read-only and the backend recomputes it and
   * rejects a review that contradicts its own statuses, so a mirror that
   * disagrees does not merely look wrong -- it tells a reviewer their decisions
   * add up to something the server will refuse, or worse, to a claim the
   * application does not make.
   */
  const CASES: Array<[string, string[], string]> = [
    ['all found', [FOUND, FOUND], 'FULLY_DISCLOSED_IN_SUPPLIED_PASSAGES'],
    ['none found, nothing unclear', [NOT_FOUND, NOT_FOUND], 'NO_DISCLOSURE_FOUND_IN_SUPPLIED_PASSAGES'],
    ['found and not found', [FOUND, NOT_FOUND], 'PARTIALLY_DISCLOSED_IN_SUPPLIED_PASSAGES'],
    ['everything found but one ambiguity', [FOUND, UNCLEAR], 'UNCLEAR'],
    ['nothing but ambiguity', [UNCLEAR, UNCLEAR], 'UNCLEAR'],
    // **The case the mirror got wrong.** Nothing found, something not found and
    // something unclear derived PARTIALLY_DISCLOSED -- a disclosure claim -- on
    // the one screen whose job is to let a reviewer check exactly that. The
    // ambiguity might have resolved in favour of disclosure, so it is UNCLEAR.
    ['nothing found, with an ambiguity outstanding', [NOT_FOUND, UNCLEAR], 'UNCLEAR'],
    ['nothing found, an ambiguity, and more not found', [NOT_FOUND, NOT_FOUND, UNCLEAR], 'UNCLEAR'],
  ];

  for (const [name, statuses, expected] of CASES) {
    it(`derives ${expected} for ${name}`, async () => {
      const fixture = await openWith(statuses);
      expect(fixture.componentInstance.derivedOutcome()).toBe(expected);
    });
  }

  it('offers no control to type an outcome, and shows the derivation in words', async () => {
    const fixture = await openWith([FOUND, NOT_FOUND]);
    const rendered = text(fixture);

    expect(rendered).toContain('Some requirements found in the passages examined');
    expect(rendered).toContain('not typed');

    // The only selects on the screen are the per-requirement ones, and none of
    // them offers an outcome value.
    const selects = Array.from(
      (fixture.nativeElement as HTMLElement).querySelectorAll('select'),
    );
    for (const select of selects) {
      const values = Array.from(select.options).map((option) => option.value);
      expect(values).toEqual(['FOUND', 'NOT_FOUND_IN_SUPPLIED_PASSAGES', 'UNCLEAR']);
    }
  });

  it('sends the code, not the words, when a reviewer changes a status', async () => {
    const fixture = await openWith([FOUND]);
    fixture.componentInstance.setOverride('E1', 'NOT_FOUND_IN_SUPPLIED_PASSAGES');
    await fixture.componentInstance.saveReview();
    await settle(fixture);

    const call = runs.sent.find((entry) => entry.method === 'reviewCell');
    expect(call).toBeTruthy();
    const overlay = call!.args[2] as { element_reviews: Array<Record<string, string>> };
    expect(overlay.element_reviews[0]['reviewed_status']).toBe(
      'NOT_FOUND_IN_SUPPLIED_PASSAGES',
    );
  });
});

describe('The final report', () => {
  /**
   * A report version exactly as `GET /api/runs/{id}/report` returns one, wrapping
   * a draft exactly as `graph/report.py::build_draft` writes one.
   *
   * Built to that shape on purpose. Four sections of this report were written
   * against a shape the draft builder does not produce, and every one of them
   * failed as a plausible true negative -- "Gaps accepted without being resolved
   * (0), None" reads exactly like a run with nothing to disclose.
   */
  const REPORT_VERSION = {
    version: 2,
    report_version: 2,
    status: 'APPROVED',
    approved_by: 'testnovelty@gmail.com',
    comments: 'Checked the two US publications by hand.',
    caveats: 'Korean family members could not be retrieved.',
    draft: {
      status: 'DRAFT — NOT APPROVED',
      coverage_statement:
        'This report covers only the publications listed as evaluated, and only the passages retrieved for them.',
      decomposition_version: 3,
      candidate_set_version: 1,
      date_policy: 'PUBLICATION_DATE_EXCLUSIVE',
      text_version_policy: 'PROVIDER_PUBLICATION_DATE',
      matrix: [
        {
          cell_id: 'CELL-1',
          feature_id: 'F1',
          publication_number: 'US11002233',
          state: 'VERIFIED',
          machine_outcome: 'PARTIALLY_DISCLOSED_IN_SUPPLIED_PASSAGES',
          result_version: 1,
          review: { status: 'OVERRIDDEN' },
          reviewed_outcome: 'NO_DISCLOSURE_FOUND_IN_SUPPLIED_PASSAGES',
          waivers: [],
          class: 'human-overridden',
        },
        {
          cell_id: 'CELL-2',
          feature_id: 'F2',
          publication_number: 'US7654321',
          state: 'VALIDATION_FAILURE',
          machine_outcome: null,
          failed_check: 'check 15',
          result_version: 1,
          review: null,
          waivers: [
            {
              reason: 'Re-running twice produced the same rejection.',
              unresolved_limitation: 'F2 was never compared against US7654321.',
            },
          ],
          class: 'validation-failure',
        },
      ],
      unresolved: [
        {
          cell_id: 'CELL-2',
          state: 'VALIDATION_FAILURE',
          failed_check: 'check 15',
          reason: 'the response contradicted its own statuses',
          waived: true,
          unresolved_limitations: ['F2 was never compared against US7654321.'],
        },
      ],
      excluded_publications: [
        {
          publication_number: 'KR20110066580',
          state: 'EXCLUDED_NO_FULL_TEXT',
          reason: 'the provider answered and had no eligible text',
          evaluated: false,
          note: 'NOT EVALUATED for feature disclosure.',
        },
      ],
      counts: { cells_total: 2, unresolved: 1, excluded_publications: 1 },
    },
  };

  function render() {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({ imports: [VerificationReport] });
    const fixture = TestBed.createComponent(VerificationReport);
    fixture.componentRef.setInput('report', REPORT_VERSION);
    fixture.componentRef.setInput('status', 'APPROVED');
    fixture.detectChanges();
    return fixture;
  }

  it('lists every waiver with its unresolved limitation', () => {
    // The defect this pins: `build_draft` attaches waivers per matrix entry and
    // writes no top-level list, so a report with waivers showed none.
    const rendered = text(render());
    expect(rendered).toContain('Re-running twice produced the same rejection.');
    expect(rendered).toContain('F2 was never compared against US7654321.');
    expect(rendered).not.toContain('Gaps accepted without being resolved (0)');
  });

  it('lists the comparisons that produced no result, with the failing check', () => {
    // `unresolved` is a list, not a dict of counts. Read as a dict it rendered
    // three zeroes on every report.
    const rendered = text(render());
    expect(rendered).toContain('Comparisons left without an answer (1)');
    expect(rendered).toContain('CELL-2');
    expect(rendered).toContain('check 15');
    expect(rendered).toContain('Could not be validated');
  });

  it('names an excluded publication, under the key the draft builder writes', () => {
    const rendered = text(render());
    expect(rendered).toContain('KR20110066580');
    expect(rendered).toContain('Not examined, no full text available');
    expect(rendered).toContain('not evaluated');
  });

  it('shows the machine outcome under the name the draft gives it', () => {
    const rendered = text(render());
    expect(rendered).toContain('Some requirements found in the passages examined');
    expect(rendered).toContain('PARTIALLY_DISCLOSED_IN_SUPPLIED_PASSAGES');
  });

  it('keeps the reviewer decision beside the system result, never merged', () => {
    const rendered = text(render());
    // Both are present, and the machine outcome is not replaced by the reviewed
    // one even though they disagree.
    expect(rendered).toContain('Changed by a reviewer');
    expect(rendered).toContain('No requirement found in the passages examined');
    expect(rendered).toContain('Some requirements found in the passages examined');
  });

  it('prints the caveats and comments the reviewer recorded', () => {
    const rendered = text(render());
    expect(rendered).toContain('Korean family members could not be retrieved.');
    expect(rendered).toContain('Checked the two US publications by hand.');
  });

  it('prints the coverage statement the draft was built with', () => {
    expect(text(render())).toContain(
      'This report covers only the publications listed as evaluated',
    );
  });

  it('keeps the non-legal disclaimer and uses no internal vocabulary', () => {
    const rendered = text(render());
    expect(rendered).toContain('not a legal opinion');
    expect(rendered).toContain('not an exhaustive prior-art search');
    const lower = rendered.toLowerCase();
    for (const internal of ['gate d', 'decomposition', 'verification cell', 'matrix cell']) {
      expect(lower).not.toContain(internal);
    }
  });
});
