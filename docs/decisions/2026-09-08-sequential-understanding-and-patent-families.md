# Sequential invention understanding and visible patent families

**Date:** 2026-09-08
**Status:** implemented

## Decision

Advanced Search now performs three independent human approvals in this order:

1. generate, edit, and approve the technical summary;
2. generate technical limitations from that exact approved summary, then edit and approve them;
3. generate, edit, and approve the search queries.

The first two pauses retain the durable `WorkflowGate.A` value for compatibility, but carry
an explicit `understanding_review_stage` (`summary` or `limitations`). They have distinct
idempotency scopes, audit fields, proposals, and approval writes. A Stage-1 retry replays its
stored response even if the graph has already reached Stage 2.

The result payload also carries Lens simple-family metadata. `family_id` is a stable digest
of the provider-returned simple-family member Lens IDs, and `family_size` is Lens's reported
size. The UI and direct exports group matched publications with the same non-empty family ID,
while retaining each publication as a separate ranked result. If Lens supplies no family
metadata, the publication remains ungrouped; the application does not infer family ties from
titles or publication numbers.

## Why

Generating the summary and limitations together made Stage 1 review cosmetic: edits to the
summary could not influence limitations that already existed. The new graph topology makes
that dependency structural rather than prompt-only.

Multiple jurisdictional publications from one patent family can otherwise look like many
independent inventions and overstate the breadth of the result set. Grouping makes the
relationship explicit without deleting evidence or changing ranking.

Overlap summaries are prompted to state both the genuinely shared mechanism and the important
claimed mechanisms absent from the supplied excerpt. Recommendations must be grounded in those
observed gaps; any newly invented feature must be labelled `Optional enhancement:`. Missing or
malformed LLM output still fails the run—there is no canned fallback.

## Cost

Advanced Search uses one additional LLM round trip because summary and limitation generation
are now separate. This cost is accepted: Stage 2 must reflect the user's approved interpretation.
Patent-family grouping adds no LLM or embedding call and uses metadata returned in the existing
Lens search response.
