# Aramco Ghawar font files

The UI is set in **Aramco Ghawar**, the corporate typeface from the Aramco Brand Portal
(<https://brand.aramco.com/guidelines/corporate/typography> → Downloads). The files in this
folder are the **OTF release `Ghawar 1.312-Beta 4.0`**, renamed from the shipped names
(`Ghawar_1.312-Beta_4.0-Regular.otf` → `AramcoGhawar-Regular.otf`) so that `fonts.css` does
not have to be edited every time the font is versioned.

No webfont CDN is involved anywhere in this app — these files are the only source of
typography, and `src/styles/fonts.css` is the only place they are referenced.

## What is here

| File | CSS `font-weight` |
|---|---:|
| `AramcoGhawar-Hairline.otf` | 100 |
| `AramcoGhawar-Thin.otf` | 200 |
| `AramcoGhawar-Light.otf` | 300 |
| `AramcoGhawar-Regular.otf` | 400 |
| `AramcoGhawar-Hefty.otf` | 500 |
| `AramcoGhawar-SemiBold.otf` | 600 |
| `AramcoGhawar-Bold.otf` | 700 |
| `AramcoGhawar-Heavy.otf` | 800 |
| `AramcoGhawar-Black.otf` | 900 |

The UI only requests four of these — Regular, Hefty, SemiBold, Bold — so a page load pulls
about 165 KB of font. The other five are declared so that changing a weight in the CSS
just works without touching this folder.

## Two quirks of this release worth knowing

**Each weight is its own family.** Internally these are nine separate families
(`Ghawar_1.312-Beta_4.0 Bold`, subfamily `Regular`), not one family with nine weights. The
`@font-face` blocks in `fonts.css` are what merge them into a single `Aramco Ghawar` family
with real weight values. That is why `font-weight: 600` works at all, and why those blocks
cannot be collapsed into one rule.

**There are no italic files.** Every italic in the UI is synthesised (obliqued) by the
browser. If a release with true italics arrives, add `font-style: italic` faces rather than
leaving the fake oblique in place.

Their `SemiBold` file is shipped misspelled as `SmeiBold`. The copy here is corrected, but
the *internal* name still reads `SmeiBold`, so the `local()` entry in `fonts.css` matches
the typo on purpose. Do not "fix" it.

## Replacing or updating the font

Drop the new OTFs in with the same `AramcoGhawar-<Weight>.otf` names. The only thing that
also needs touching is the `local()` line per face in `fonts.css`, which embeds the version
string from the font's internal name and will silently stop matching after an update — the
`url()` still resolves, so this degrades to "installed copy no longer used", not a break.

`.woff2` would be roughly half the size, but converting needs `fonttools` **plus** `brotli`,
and brotli would not install into the same interpreter here. Not worth blocking on; if you
do convert, add the `.woff2` `url()` *before* the `.otf` one in each `src`.

## Licensing

Aramco Ghawar is licensed, not open source. These binaries are currently sitting in the
working tree — decide deliberately whether they belong in git, especially if this repo is
ever mirrored anywhere public. If you decide against committing them, add
`frontend/public/fonts/*.otf` to the root `.gitignore` **and** make sure whatever builds the
deployed bundle supplies them another way, or production will quietly render in Segoe UI.
