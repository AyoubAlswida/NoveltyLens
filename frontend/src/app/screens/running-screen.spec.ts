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
    expect(text).not.toContain('Searching for relevant prior art');
  });

  it('names the search task after query approval', () => {
    fixture.componentRef.setInput('mode', 'advanced');
    fixture.componentRef.setInput('phaseOrder', ADVANCED_EXECUTION_PHASE_ORDER);
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('.progress-card-header')).toBeNull();
  });

  it('names Quick Search without adding workflow navigation', () => {
    fixture.componentRef.setInput('mode', 'quick');
    fixture.componentRef.setInput('phaseOrder', QUICK_SEARCH_PHASE_ORDER);
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('Novelty search');
  });
});
