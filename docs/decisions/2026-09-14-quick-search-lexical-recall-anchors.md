# Quick Search adds bounded lexical recall anchors

Date: 2026-09-14  
Status: Accepted  
Supersedes the "Quick Search is untouched" statement in
`2026-09-14-advanced-search-two-stages.md`; the two-stage Advanced Search decision
itself remains unchanged.

## Problem

A paraphrase-plus-new-subsystem benchmark reproduced a retrieval miss. The model
correctly extracted the source mechanism into technical concepts, but its five search
queries used broader synonyms and omitted the older patent's literal component
vocabulary. Lens returns at most five full-text records per query under the current
transport limit, so the source publication never entered filtering, ranking, or risk
classification. A downstream scorer cannot recover a publication it never received.

## Decision

Quick Search keeps its five model-planned search angles and adds at most three lexical
concept anchors. An anchor is derived only from:

1. the leading concrete domain phrase in the user's description; and
2. one of the first three technical concepts returned by the existing understanding
   call, whose prompt orders the underlying mechanism before added subsystems. Adjacent
   essential-keyword pairs fill unused anchor slots if fewer than three concepts exist.

The combined plan is capped at eight distinct queries. The report's `query_count` and
the SSE query event state the number actually executed. No result, patent identifier,
domain dictionary, or benchmark-specific term participates in construction.

This is not a fallback. The anchors are built before provider contact and execute on
every Quick Search, whether the model-planned queries would have succeeded or not. The
change adds no OpenAI call and adds at most three Lens calls. Advanced Search already
uses reviewed requirements, a domain anchor, coverage validation, and up to ten planned
queries, so its query construction is unchanged.

## Failure policy

The understanding response must contain exactly five distinct, non-blank string
queries. An incomplete, duplicate, blank, or malformed plan fails before patent-provider
contact. The product does not silently search with a degraded subset.

## Verification

The regression suite includes multiple unrelated invention domains plus a labelled
paraphrase-and-added-subsystem case. In that case the provider double returns the source
publication only for the new mechanism anchor, proving the complete production graph
executes the generic anchor and carries the source into the report.
