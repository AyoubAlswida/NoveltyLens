import { ComponentFixture, TestBed } from '@angular/core/testing';
import {
  ADVANCED_EXECUTION_PHASE_ORDER,
  ADVANCED_PRE_REVIEW_PHASE_ORDER,
  QUICK_SEARCH_PHASE_ORDER,
} from '../models/report';
import { RunningScreen } from './running-screen';

describe('RunningScreen', () => {
  let fixture: ComponentFixture<RunningScreen>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({ imports: [RunningScreen] }).compileComponents();
    fixture = TestBed.createComponent(RunningScreen);
    fixture.componentRef.setInput('phaseIdx', 0);
    fixture.componentRef.setInput('phaseMessages', {});
  });

  it('names the current Advanced Search task before the first review', () => {
    fixture.componentRef.setInput('mode', 'advanced');
    fixture.componentRef.setInput('phaseOrder', ADVANCED_PRE_REVIEW_PHASE_ORDER);
    fixture.detectChanges();

    const text = fixture.nativeElement.textContent;
    expect(text).toContain('Advanced Search');
    expect(fixture.nativeElement.querySelectorAll('.progress-step')).toHaveLength(1);
    expect(text).toContain('Preparing the technical summary');
  });

  it('shows four high-level execution stages after Advanced Search approval', () => {
    fixture.componentRef.setInput('mode', 'advanced');
    fixture.componentRef.setInput('phaseOrder', ADVANCED_EXECUTION_PHASE_ORDER);
    fixture.componentRef.setInput('phaseMessages', {
      strategy: 'Generated 6 search queries; starting the search',
      search: 'Searching patent databases... 6/6 · 23 found',
    });
    fixture.detectChanges();

    const steps = [...fixture.nativeElement.querySelectorAll('.progress-step')];
    expect(steps.map((step: Element) => step.querySelector('.phase-label')?.textContent)).toEqual([
      'Planning the search',
      'Searching patents and families',
      'Evaluating prior art',
      'Preparing your report',
    ]);
    expect(fixture.nativeElement.textContent).not.toContain('6/6');
    expect(fixture.nativeElement.textContent).not.toContain('23 found');
  });

  it('groups Quick Search progress by real phase events rather than counts', () => {
    fixture.componentRef.setInput('mode', 'quick');
    fixture.componentRef.setInput('phaseOrder', QUICK_SEARCH_PHASE_ORDER);
    fixture.componentRef.setInput('phaseMessages', {
      queries: 'Generated 8 search queries',
      search: 'Searching patent databases... 8/8 · 37 found',
      enrich: 'Full text retrieved for 18/20 patents',
    });
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('Novelty search');
    expect(fixture.nativeElement.querySelectorAll('.progress-step')).toHaveLength(4);
    expect(fixture.nativeElement.textContent).not.toContain('8/8');
    expect(fixture.nativeElement.textContent).not.toContain('18/20');

    fixture.componentRef.setInput('phaseIdx', QUICK_SEARCH_PHASE_ORDER.indexOf('search'));
    fixture.detectChanges();
    let steps = [...fixture.nativeElement.querySelectorAll('.progress-step')];
    expect(steps[0].classList.contains('complete')).toBe(true);
    expect(steps[1].classList.contains('active')).toBe(true);
    expect(steps[1].querySelector('.phase-detail')).toBeNull();

    fixture.componentRef.setInput('phaseIdx', QUICK_SEARCH_PHASE_ORDER.indexOf('validate'));
    fixture.detectChanges();
    steps = [...fixture.nativeElement.querySelectorAll('.progress-step')];
    expect(steps[1].classList.contains('complete')).toBe(true);
    expect(steps[2].classList.contains('active')).toBe(true);

    for (const [phase, activeIndex] of [['expand', 1], ['compare', 2], ['analyze', 3]] as const) {
      fixture.componentRef.setInput('phaseIdx', QUICK_SEARCH_PHASE_ORDER.indexOf(phase));
      fixture.detectChanges();
      steps = [...fixture.nativeElement.querySelectorAll('.progress-step')];
      expect(steps.findIndex((step: Element) => step.classList.contains('active'))).toBe(activeIndex);
    }
  });

  it('does not display an elapsed-time footer', () => {
    fixture.componentRef.setInput('phaseOrder', QUICK_SEARCH_PHASE_ORDER);
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('.progress-meta')).toBeNull();
    expect(fixture.nativeElement.textContent).not.toContain('Elapsed on this screen');
    expect(fixture.nativeElement.textContent).not.toContain('Current step');
  });
});
