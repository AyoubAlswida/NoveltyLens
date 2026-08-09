# Novelty Lens — frontend

Angular 22 (standalone components, no NgModules). This replaced a React/Vite build; see
`CLAUDE.md` at the repo root for the full architecture notes.

```sh
npm install
npm run dev        # ng serve -> http://localhost:4200
npm run build      # -> dist/novelty-lens/
npm run typecheck  # tsc --noEmit
```

The backend must be running separately on `http://127.0.0.1:8001` (with Qdrant up). To
point the UI somewhere else without a rebuild, set the global before the bundle loads:

```js
window.NOVELTY_LENS_API_URL = 'http://127.0.0.1:8002';
```

## Things that will bite you

**`services/analysis.ts` uses raw `fetch`, not `HttpClient`.** That is deliberate.
`EventSource` cannot carry a POST body, and `HttpClient` buffers the whole response before
emitting — which would make every progress event arrive at once when the run finished. It
would look correct against any instant stub and lose live progress on a real run.

**Styles are global, not component-scoped.** `src/styles.css` imports `styles/fonts.css`,
`styles/theme.css` and `styles/globals.css`, and components carry plain `class` attributes
matching them. Components declare no `styleUrls` on purpose: moving a rule into a
component's styles gets it scoped by view encapsulation and the rest of the app stops
matching it.

**Icons are inlined in `shared/icon-paths.ts`,** extracted from lucide. `lucide-angular`
peers on Angular 13–21 and this app is on 22. To add one, take its markup from lucide
rather than hand-drawing a path.

**`services/export.ts` is a verbatim port.** Its `buildReportHtml` template produces
byte-identical `.doc` and `.json` output to the React build. Changing whitespace in that
template changes exported documents.

`npm test` exists from the Angular scaffold but there are no frontend tests.
