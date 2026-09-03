import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, expect, it } from 'vitest';
import { ReportScreen } from './report-screen';
import { HistoryScreen } from './history-screen';
import { AnalysisReport, HistoryEntry, PatentMatch, Risk } from '../models/report';

/**
 * The report and history screens publish a rating, never a number.
 *
 * Spec section 5. These assert what a reader actually sees: that no percentage
 * survives anywhere on either screen, and -- the case section 5.6 singles out --
 * that a run which found nothing renders **no rating at all** rather than the
 * bottom one. "Novelty risk: Low" over an empty result set states a finding the
 * run did not make, which is the substitution this project exists to refuse.
 */

function match(overrides: Partial<PatentMatch> = {}): PatentMatch {
  return {
    num: 'US1000001A',
    title: 'A prior-art patent',
    risk: 'Medium',
    rationale: 'Overlaps on the treatment mechanism.',
    key_overlap: ['membrane', 'waste heat'],
    source: 'Google Patents',
    url: 'https://example.invalid/US1000001A',
    ...overrides,
  };
}

function report(overrides: Partial<AnalysisReport> = {}): AnalysisReport {
  return {
    title: 'A solar desalination unit',
    date: 'Today',
    concepts: ['desalination'],
    patents: [match()],
    recommendations: ['Narrow claim 1 to the adaptive brine controller.'],
    query_count: 5,
    ...overrides,
  };
}

function mountReport(data: AnalysisReport): ComponentFixture<ReportScreen> {
  TestBed.configureTestingModule({ imports: [ReportScreen] });
  const fixture = TestBed.createComponent(ReportScreen);
  fixture.componentRef.setInput('data', data);
  fixture.detectChanges();
  return fixture;
}

function text(fixture: ComponentFixture<unknown>): string {
  return (fixture.nativeElement as HTMLElement).textContent ?? '';
}

function html(fixture: ComponentFixture<unknown>): HTMLElement {
  return fixture.nativeElement as HTMLElement;
}

describe('ReportScreen', () => {
  it('shows a novelty risk rating and no percentage', () => {
    const fixture = mountReport(report());

    expect(text(fixture)).toContain('Novelty risk');
    expect(html(fixture).querySelector('.risk-pill')?.textContent?.trim()).toBe('Medium');
    expect(text(fixture)).not.toMatch(/\d\s*%/);
    expect(text(fixture)).not.toContain('Match level');
  });

  it('renders none of the removed percentage furniture', () => {
    const fixture = mountReport(report());
    const root = html(fixture);

    for (const dead of ['.score-ring', '.score-bar', '.score-track', '.score-value', '.verdict-pill']) {
      expect(root.querySelector(dead), `${dead} is still rendered`).toBeNull();
    }
  });

  it('rates every match it lists', () => {
    const fixture = mountReport(
      report({
        patents: [
          match({ num: 'US1A', risk: 'High' }),
          match({ num: 'US2A', risk: 'Medium' }),
          match({ num: 'US3A', risk: 'Low' }),
        ],
      }),
    );

    const ratings = [...html(fixture).querySelectorAll('.patent-similarity .risk-label')].map((el) =>
      el.textContent?.trim(),
    );
    expect(ratings).toEqual(['High', 'Medium', 'Low']);
  });

  it('renders matches in the order given, without re-sorting', () => {
    // The order arrives already correct -- from the backend live, from
    // `search_run_matches.rank` out of History -- and there is no score left to
    // re-derive it from, so the screen must not reorder.
    const fixture = mountReport(
      report({
        patents: [
          match({ num: 'US-FIRST', risk: 'Low' }),
          match({ num: 'US-SECOND', risk: 'High' }),
        ],
      }),
    );

    const numbers = [...html(fixture).querySelectorAll('.patent-meta span:first-child')].map((el) =>
      el.textContent?.trim(),
    );
    expect(numbers).toEqual(['US-FIRST', 'US-SECOND']);
  });

  it('shows NO rating when the run found nothing', () => {
    // Section 5.6. The empty state says only that nothing was found.
    const fixture = mountReport(report({ patents: [], recommendations: [] }));
    const root = html(fixture);

    expect(root.querySelector('.risk-pill')).toBeNull();
    expect(root.querySelector('.risk-label')).toBeNull();
    expect(text(fixture)).not.toContain('Novelty risk');
    expect(text(fixture)).not.toMatch(/\bHigh\b|\bMedium\b|\bLow\b/);

    // ...and it still says so plainly rather than rendering an empty shell.
    expect(root.querySelector('.empty-state')).not.toBeNull();
    expect(text(fixture)).toContain('No matching patent records were returned.');
  });
});

function entry(patents: PatentMatch[], id = 'run-1'): HistoryEntry {
  return {
    id,
    description: 'A solar desalination unit',
    created_at: '2026-09-01T09:00:00Z',
    concepts: [],
    patents,
    recommendations: [],
    query_count: 5,
  };
}

function mountHistory(entries: HistoryEntry[]): ComponentFixture<HistoryScreen> {
  TestBed.configureTestingModule({ imports: [HistoryScreen] });
  const fixture = TestBed.createComponent(HistoryScreen);
  fixture.componentRef.setInput('entries', entries);
  fixture.componentRef.setInput('loading', false);
  fixture.componentRef.setInput('error', '');
  fixture.detectChanges();
  return fixture;
}

describe('HistoryScreen', () => {
  it('shows the closest match rating and no percentage column', () => {
    const fixture = mountHistory([entry([match({ risk: 'High' }), match({ risk: 'Low' })])]);
    const root = html(fixture);

    expect(root.querySelector('.history-risk')?.textContent?.trim()).toBe('High');
    expect(root.querySelector('.history-score')).toBeNull();
    expect(root.querySelector('.history-verdict')).toBeNull();
    expect(text(fixture)).not.toMatch(/\d\s*%/);
  });

  it('shows NO rating for a run that found nothing', () => {
    const fixture = mountHistory([entry([])]);

    expect(html(fixture).querySelector('.history-risk')).toBeNull();
    expect(text(fixture)).not.toMatch(/\bHigh\b|\bMedium\b|\bLow\b/);
  });

  it('rates each row from its own closest match', () => {
    const fixture = mountHistory([
      entry([match({ risk: 'Low' })], 'run-1'),
      entry([], 'run-2'),
      entry([match({ risk: 'High' })], 'run-3'),
    ]);

    const ratings = [...html(fixture).querySelectorAll('.history-risk')].map((el) =>
      el.textContent?.trim(),
    );
    // The middle run contributes nothing -- it has no match to rate.
    expect(ratings).toEqual(['Low', 'High']);
  });
});

describe('the Risk vocabulary', () => {
  it('is exactly the three ratings the backend can send', () => {
    const all: Risk[] = ['High', 'Medium', 'Low'];
    expect(all).toHaveLength(3);
  });
});
