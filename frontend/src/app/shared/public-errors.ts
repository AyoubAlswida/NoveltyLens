/**
 * Concise user-facing error copy. Backend details are diagnostic data and must
 * never be rendered because they can contain implementation details.
 */
export const PUBLIC_ERRORS = {
  general: 'Something went wrong. Please try again.',
  analysis: 'We couldn\u2019t complete the analysis. Please try again.',
  connection: 'We couldn\u2019t connect. Please try again.',
  history: 'We couldn\u2019t load your search history. Please try again.',
  clearHistory: 'We couldn\u2019t clear your search history. Please try again.',
  cancel: 'We couldn\u2019t cancel the search. Please try again.',
  signIn: 'We couldn\u2019t sign you in. Please try again.',
  unavailable: 'This search is no longer available.',
  changed: 'This step has changed. Reload it and try again.',
  blocked: 'Review the highlighted fields and try again.',
} as const;

export function publicRunError(code?: string): string {
  switch (code) {
    case 'RUN_NOT_FOUND':
      return PUBLIC_ERRORS.unavailable;
    case 'STALE_RESULT_VERSION':
    case 'IDEMPOTENCY_CONFLICT':
    case 'WRONG_GATE':
    case 'RUN_NOT_RESUMABLE':
      return PUBLIC_ERRORS.changed;
    case 'GATE_BLOCKED':
      return PUBLIC_ERRORS.blocked;
    case 'STREAM_UNAVAILABLE':
    case 'SSE_CONTRACT':
      return PUBLIC_ERRORS.connection;
    default:
      return PUBLIC_ERRORS.analysis;
  }
}

export function publicSignInError(error: unknown): string {
  if (error instanceof Error && error.message === 'Incorrect email or password.') {
    return error.message;
  }
  return PUBLIC_ERRORS.signIn;
}
