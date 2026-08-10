import { Injectable, computed, signal } from '@angular/core';
import { API_BASE_URL } from '../config';

const TOKEN_KEY = 'novelty-lens-token';
const EMAIL_KEY = 'novelty-lens-email';

/**
 * The session: a bearer token from `POST /api/auth/login`, kept in
 * localStorage so a reload does not sign you out.
 *
 * There is no signup counterpart anywhere in this app, by design -- accounts
 * are rows inserted by a backend migration (`migrations/0004.create-users.sql`).
 * Nothing here creates one, and adding a "Create account" form would need a
 * backend endpoint that does not exist.
 *
 * The token is a signed claim the backend verifies, not a secret this app
 * checks: `signedIn()` is only ever a guess about whether the backend will
 * still accept it. `restore()` turns that guess into an answer at startup, and
 * `authorizedFetch` ends the session the moment any request comes back 401 --
 * so an expired token shows the login screen rather than a wall of failures.
 */
@Injectable({ providedIn: 'root' })
export class AuthService {
  private readonly tokenSignal = signal<string | null>(localStorage.getItem(TOKEN_KEY));

  readonly email = signal<string | null>(localStorage.getItem(EMAIL_KEY));
  readonly signedIn = computed(() => this.tokenSignal() !== null);

  async login(email: string, password: string): Promise<void> {
    const response = await fetch(`${API_BASE_URL}/api/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
    });

    // The backend's own detail is surfaced verbatim, the same as the error
    // screen does: "Incorrect email or password." is the message it chose, and
    // a 500 or a connection failure must not be relabelled as a bad password.
    if (!response.ok) {
      throw new Error(await this.detailOf(response, 'Sign in failed.'));
    }

    const body = (await response.json()) as { token: string; email: string };
    this.persist(body.token, body.email);
  }

  /**
   * Confirm a stored token is still valid, before the app trusts it.
   *
   * Returns false (and clears the session) for an expired or rejected token.
   * A network failure re-throws instead: the backend being unreachable is not
   * evidence that the session ended, and silently signing the user out on it
   * would lose their session every time the backend restarts.
   */
  async restore(): Promise<boolean> {
    if (!this.tokenSignal()) return false;
    const response = await fetch(`${API_BASE_URL}/api/auth/me`, { headers: this.headers() });
    if (response.status === 401) {
      this.signOut();
      return false;
    }
    if (!response.ok) throw new Error(await this.detailOf(response, 'Could not verify session.'));
    this.email.set(((await response.json()) as { email: string }).email);
    return true;
  }

  signOut(): void {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(EMAIL_KEY);
    this.tokenSignal.set(null);
    this.email.set(null);
  }

  /** The Authorization header every authenticated request carries. */
  headers(): Record<string, string> {
    const token = this.tokenSignal();
    return token ? { Authorization: `Bearer ${token}` } : {};
  }

  /**
   * `fetch` with the session attached, which ends the session on a 401.
   *
   * Every authenticated call in the app goes through this, including the SSE
   * stream -- that is why the analysis service uses raw `fetch` rather than
   * `EventSource`, which cannot send an Authorization header at all.
   */
  async authorizedFetch(input: string, init: RequestInit = {}): Promise<Response> {
    const response = await fetch(input, {
      ...init,
      headers: { ...(init.headers ?? {}), ...this.headers() },
    });
    if (response.status === 401) {
      this.signOut();
      throw new Error(await this.detailOf(response, 'Your session has expired. Sign in again.'));
    }
    return response;
  }

  private persist(token: string, email: string): void {
    localStorage.setItem(TOKEN_KEY, token);
    localStorage.setItem(EMAIL_KEY, email);
    this.tokenSignal.set(token);
    this.email.set(email);
  }

  /** FastAPI puts its message in `detail`; fall back if the body is not JSON. */
  private async detailOf(response: Response, fallback: string): Promise<string> {
    try {
      const body = await response.json();
      return typeof body?.detail === 'string' ? body.detail : fallback;
    } catch {
      return fallback;
    }
  }
}
