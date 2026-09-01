import { defineConfig } from 'vitest/config';

/**
 * The `forks` pool -- Vitest's default -- never starts a worker on this machine:
 * every run ends in "Timeout waiting for worker to respond" after 60s, with zero
 * tests collected. `threads` starts immediately and runs the same specs.
 *
 * This is an environment fact, not a preference, and it is recorded here rather
 * than passed on a command line so `ng test`, `npx vitest` and CI all behave the
 * same way. It changes nothing about what the tests assert.
 */
export default defineConfig({
  test: {
    pool: 'threads',
    environment: 'jsdom',
    globals: true,
  },
});
