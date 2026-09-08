import { ComponentFixture, TestBed } from '@angular/core/testing';
import { beforeEach, describe, expect, it } from 'vitest';
import { SubmitScreen } from './submit-screen';

/**
 * The submit screen's one action, and the mode that decides what it does.
 *
 * Advanced Search used to be a second button. It is now a switch on the
 * description box, so the single action starts either a quick search or the
 * guided three-stage run. The primary action keeps one stable product label;
 * the switch communicates which mode will run.
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

  it('keeps the Run Novelty Lens label in both modes', () => {
    expect(label(fixture)).toBe('Run Novelty Lens');
    toggle(fixture);
    expect(label(fixture)).toBe('Run Novelty Lens');
  });

  it('places Advanced directly after Attach a file', () => {
    const options = fixture.nativeElement.querySelector('.composer-options');
    const attach = options.querySelector('.composer-attach');
    const mode = options.querySelector('.composer-mode');
    expect(attach.nextElementSibling).toBe(mode);
  });

  it('groups Attach and Advanced in one composer toolbar', () => {
    const options = fixture.nativeElement.querySelector('.composer-options');
    expect(options.querySelector('.composer-attach')).not.toBeNull();
    expect(options.querySelector('.composer-toggle')).not.toBeNull();
    expect(options.querySelector('.advanced-help')).not.toBeNull();
  });

  it('keeps the composer toolbar outside the editable text area', () => {
    const wrap = fixture.nativeElement.querySelector('.textarea-wrap-primary');
    const textarea = wrap.querySelector('textarea');
    const tools = wrap.querySelector('.composer-tools');

    expect(textarea.contains(tools)).toBe(false);
    expect(textarea.nextElementSibling).toBe(tools);
  });

  it('expands the description box to its full content height while typing', () => {
    const textarea: HTMLTextAreaElement = fixture.nativeElement.querySelector(
      '#invention-description',
    );
    Object.defineProperty(textarea, 'scrollHeight', { configurable: true, value: 348 });

    textarea.value = DESCRIPTION.repeat(8);
    textarea.dispatchEvent(new Event('input'));
    fixture.detectChanges();

    expect(textarea.style.height).toBe('348px');
  });

  it('announces its on/off state to assistive technology', () => {
    const t: HTMLButtonElement = fixture.nativeElement.querySelector('.composer-toggle');
    expect(t.getAttribute('role')).toBe('switch');
    expect(t.getAttribute('aria-checked')).toBe('false');
    toggle(fixture);
    expect(t.getAttribute('aria-checked')).toBe('true');
  });

  it('opens an explanation when the Advanced information control is clicked', () => {
    const help: HTMLDetailsElement = fixture.nativeElement.querySelector('.advanced-help');
    const control: HTMLElement = help.querySelector('.action-hint')!;

    expect(help.open).toBe(false);
    control.click();
    fixture.detectChanges();

    expect(help.open).toBe(true);
    expect(help.textContent).toContain('pauses for your review before searching');
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
