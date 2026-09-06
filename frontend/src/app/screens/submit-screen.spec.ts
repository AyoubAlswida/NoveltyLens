import { ComponentFixture, TestBed } from '@angular/core/testing';
import { beforeEach, describe, expect, it } from 'vitest';
import { SubmitScreen } from './submit-screen';

/**
 * The submit screen's one action, and the mode that decides what it does.
 *
 * Advanced Search used to be a second button. It is now a switch on the
 * description box, so the single action starts either a quick search or the
 * guided three-stage run -- which makes "which run did that button start?" a
 * behaviour worth pinning rather than a layout detail. Every assertion below is
 * about what the component EMITS and what the button READS, because those two
 * disagreeing is the actual failure mode: a control that silently starts the
 * other kind of run.
 */

const DESCRIPTION = 'A bottle that filters and sterilises water as you drink.';

function press(fixture: ComponentFixture<SubmitScreen>) {
  const button: HTMLButtonElement = fixture.nativeElement.querySelector('.primary-action');
  button.click();
  fixture.detectChanges();
}

function label(fixture: ComponentFixture<SubmitScreen>): string {
  return fixture.nativeElement.querySelector('.primary-action').textContent.trim();
}

function toggle(fixture: ComponentFixture<SubmitScreen>) {
  const t: HTMLButtonElement = fixture.nativeElement.querySelector('.composer-toggle');
  t.click();
  fixture.detectChanges();
}

describe('Submit screen — Advanced is a mode, not a second button', () => {
  let fixture: ComponentFixture<SubmitScreen>;
  let emitted: string[];

  beforeEach(async () => {
    await TestBed.configureTestingModule({ imports: [SubmitScreen] }).compileComponents();
    fixture = TestBed.createComponent(SubmitScreen);
    fixture.componentRef.setInput('description', DESCRIPTION);

    emitted = [];
    fixture.componentInstance.run.subscribe(() => emitted.push('run'));
    fixture.componentInstance.verify.subscribe(() => emitted.push('verify'));
    fixture.detectChanges();
  });

  it('offers exactly one action', () => {
    const actions = fixture.nativeElement.querySelectorAll('.form-actions button');
    expect(actions.length).toBe(1);
  });

  it('starts a quick search when the switch is off', () => {
    press(fixture);
    expect(emitted).toEqual(['run']);
  });

  it('starts an Advanced Search when the switch is on', () => {
    toggle(fixture);
    press(fixture);
    expect(emitted).toEqual(['verify']);
  });

  it('goes back to a quick search when the switch is turned off again', () => {
    toggle(fixture);
    toggle(fixture);
    press(fixture);
    expect(emitted).toEqual(['run']);
  });

  /**
   * The label is the only thing telling a person which run they are about to
   * start. A switch whose state did not reach the button would be a mode you
   * trigger by accident -- the whole reason the second button was removed.
   */
  it('says which run the button will start, in both modes', () => {
    expect(label(fixture)).toBe('Run Novelty Lens');
    toggle(fixture);
    expect(label(fixture)).toBe('Start Advanced Search');
  });

  it('announces its on/off state to assistive technology', () => {
    const t: HTMLButtonElement = fixture.nativeElement.querySelector('.composer-toggle');
    expect(t.getAttribute('role')).toBe('switch');
    expect(t.getAttribute('aria-checked')).toBe('false');
    toggle(fixture);
    expect(t.getAttribute('aria-checked')).toBe('true');
  });

  /**
   * Off by default, every time the screen is built. Advanced commits the person
   * to a review before anything is searched for, so it is not a setting that
   * should survive from some earlier visit.
   */
  it('defaults to off on a freshly created screen', () => {
    const second = TestBed.createComponent(SubmitScreen);
    second.componentRef.setInput('description', DESCRIPTION);
    second.detectChanges();
    expect(second.componentInstance.advanced()).toBe(false);
    expect(second.nativeElement.querySelector('.primary-action').textContent.trim()).toBe(
      'Run Novelty Lens',
    );
  });

  it('starts nothing at all while the description is empty', () => {
    fixture.componentRef.setInput('description', '   ');
    fixture.detectChanges();
    press(fixture);
    toggle(fixture);
    press(fixture);
    expect(emitted).toEqual([]);
  });
});
