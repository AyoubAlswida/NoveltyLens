"""The evidence layer: a discovered candidate becomes citable passages, or an
honest exclusion, and never anything in between.

**Pure modules, not graph nodes.** Everything here is a function over explicit
inputs and is unit-testable without a `StateGraph`. Implementation Gate C
registers them as nodes; keeping the split means these tests need no graph and
that wiring has nothing left to get wrong except the wiring.

The pipeline, in order:

``normalize``
    Publication numbers to epodoc form -- country plus document number, no kind
    code. EPO's full-text endpoints 404 on the suffixed form. Both forms recorded.

``dates``
    Screening against the run's cutoff under the **approved** policy, with
    provenance and certainty. An uncertain date never silently passes.

``enrichment`` (in `search/`)
    Provider-neutral routing to EPO. Three outcomes -- `TEXT`, `NO_TEXT`,
    `FAILED` -- because a provider outage and a publication with no text are
    different facts and only one of them is an exclusion.

``segmenter``
    Deterministic, versioned splitting of description text. A pure function of
    the exact retrieved string.

``passages``
    `EnrichmentResult` -> `verification.Passage`, with real locators: a claim's
    own number, a provider's own paragraph id, or a reproducible character range.
    Never an invented paragraph number. **No `PatentRecord`** -- evidence comes
    from the enrichment provider, never from discovery metadata.

``eligibility``
    **Derived from passages, never asserted.** A publication is eligible exactly
    when at least one valid passage was actually built for it.

**Only `CLAIMS` and `DESCRIPTION` are ever evidence.** A title, an abstract, a
search snippet and anything landing in `OTHER` are refused on the *section*,
not on a flag -- `verification/passages.py`'s `EVIDENCE_SECTIONS` enforces it
structurally and `evidence_passages`' CHECK constraint enforces it in the
schema.

**Three exclusion states, all publication-scoped**, and none of them is a
verdict:

* ``EXCLUDED_DATE`` -- not date-eligible against the cutoff.
* ``EXCLUDED_NO_FULL_TEXT`` -- the provider answered and had no eligible text.
* ``EXCLUDED_INELIGIBLE`` -- text exists but no trustworthy passage could be
  built from it: borrowed family text, an unproducible locator, or no
  trustworthy text version.

An excluded publication creates **no verification cells at all** -- not empty
ones, not greyed ones. It appears only on exclusion surfaces. `NOT_EVALUATED`
and `VALIDATION_FAILURE` are cell-scoped and are a different thing entirely.

**Fabricate nothing.** Not a publication identity, not a date, not a claim
number, not a paragraph number, not a locator, not passage text, not a
translation. Missing means excluded or failed, never invented.
"""
