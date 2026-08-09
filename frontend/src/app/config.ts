declare global {
  interface Window {
    NOVELTY_LENS_API_URL?: string;
  }
}

/**
 * Absolute backend URL. Replaces the React build's `import.meta.env.VITE_API_URL`.
 * Set `window.NOVELTY_LENS_API_URL` before the bundle loads (a small script in
 * index.html, or devtools) to point the UI at another backend without a rebuild.
 */
export const API_BASE_URL = window.NOVELTY_LENS_API_URL ?? 'http://127.0.0.1:8001';
