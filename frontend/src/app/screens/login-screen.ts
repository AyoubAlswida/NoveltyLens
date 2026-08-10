import { Component, ChangeDetectionStrategy, signal, inject, output } from '@angular/core';
import { Icon } from '../shared/icon';
import { BrandHeader } from '../shared/chrome';
import { AuthService } from '../services/auth';

/**
 * The sign-in gate. There is no signup screen and no link to one: accounts are
 * rows a backend migration inserts, and the API exposes no endpoint that would
 * create one.
 *
 * The card used to print the seeded demo credentials. That was removed on
 * request -- note it changes nothing about how secret they are: the password is
 * still in `migrations/0004.create-users.sql` and in CLAUDE.md, both in the
 * repository. Treat the seeded account as public regardless of what this screen
 * shows.
 */
@Component({
  selector: 'app-login-screen',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Icon, BrandHeader],
  template: `
    <div class="screen screen-scroll">
      <app-brand-header />

      <section class="login-card glass-card">
        <form (submit)="submit($event)">
          <div class="login-field">
            <label for="login-email">
              <app-icon name="Mail" [size]="17" [strokeWidth]="1.9" />
              <span>Email</span>
            </label>
            <input
              id="login-email"
              type="email"
              name="email"
              autocomplete="username"
              spellcheck="false"
              placeholder="you@example.com"
              [value]="email()"
              (input)="email.set($any($event.target).value)"
            />
          </div>

          <div class="login-field">
            <label for="login-password">
              <app-icon name="LockKeyhole" [size]="17" [strokeWidth]="1.9" />
              <span>Password</span>
            </label>
            <input
              id="login-password"
              type="password"
              name="password"
              autocomplete="current-password"
              placeholder="••••••••"
              [value]="password()"
              (input)="password.set($any($event.target).value)"
            />
          </div>

          @if (error(); as message) {
            <p class="login-error" role="alert">
              <app-icon name="AlertTriangle" [size]="16" [strokeWidth]="2" />
              <span>{{ message }}</span>
            </p>
          }

          <button class="primary-action login-submit" type="submit" [disabled]="!canSubmit()">
            @if (busy()) {
              <span>Signing in…</span>
            } @else {
              <app-icon name="LockKeyhole" [size]="18" />
              <span>Sign in</span>
            }
          </button>
        </form>
      </section>
    </div>
  `,
})
export class LoginScreen {
  private readonly auth = inject(AuthService);

  /** Emitted once the token is stored. `auth.signedIn()` flipping is what swaps
   * the screen; this exists so the shell can also fetch what a signed-in user
   * needs (the sidebar's Recents) at the one moment it becomes fetchable. */
  readonly signedIn = output<void>();

  readonly email = signal('');
  readonly password = signal('');
  readonly error = signal('');
  readonly busy = signal(false);

  canSubmit() {
    return !this.busy() && this.email().trim().length > 0 && this.password().length > 0;
  }

  async submit(event: Event) {
    event.preventDefault();
    if (!this.canSubmit()) return;

    this.busy.set(true);
    this.error.set('');
    try {
      await this.auth.login(this.email().trim(), this.password());
      // No navigation here: `signedIn()` flips and app.ts swaps the screen.
      this.password.set('');
      this.signedIn.emit();
    } catch (failure) {
      // Verbatim, like the error screen: "Incorrect email or password." and
      // "Failed to fetch" mean different things and must stay distinguishable.
      this.error.set(failure instanceof Error ? failure.message : 'Sign in failed.');
    } finally {
      this.busy.set(false);
    }
  }
}
