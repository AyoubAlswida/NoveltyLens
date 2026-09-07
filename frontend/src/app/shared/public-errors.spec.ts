import { describe, expect, it } from 'vitest';
import { PUBLIC_ERRORS, publicRunError, publicSignInError } from './public-errors';

describe('public error copy', () => {
  it('never exposes backend details for run failures', () => {
    expect(publicRunError('INTERNAL_ERROR')).toBe(PUBLIC_ERRORS.analysis);
    expect(publicRunError('SSE_CONTRACT')).toBe(PUBLIC_ERRORS.connection);
    expect(publicRunError('RUN_NOT_FOUND')).toBe(PUBLIC_ERRORS.unavailable);
  });

  it('keeps only the useful invalid-credentials message at sign in', () => {
    expect(publicSignInError(new Error('Incorrect email or password.'))).toBe(
      'Incorrect email or password.',
    );
    expect(publicSignInError(new Error('Failed to fetch'))).toBe(PUBLIC_ERRORS.signIn);
    expect(publicSignInError(new Error('HTTP 500: database unavailable'))).toBe(
      PUBLIC_ERRORS.signIn,
    );
  });
});
