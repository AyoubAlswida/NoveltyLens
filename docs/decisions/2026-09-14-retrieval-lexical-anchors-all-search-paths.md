# Retrieval lexical anchors across Quick and Advanced Search

Date: 2026-09-14

## Decision

Every live search path uses a bounded lexical-recall layer in addition to its
LLM-planned search queries.

- Quick Search keeps its five model-planned angles and adds at most three anchors
  from the submitted invention's concrete opening domain phrase and overlapping
  adjacent keyword-phrase pairs. Concepts fill slots only when the keyword list
  is too short. The LLM orders connected core keywords before added subsystems.
- Advanced Search generates at most five strategy queries from the approved
  requirements, then adds at most three anchors. Each anchor combines the original
  description's platform phrase with the intact trailing mechanism phrase of one
  of the first combined strategy angles. The final plan remains under the existing
  ten-query strategy cap.
- An anchor is deterministic executable retrieval text. It is not represented as
  a human approval or as an LLM assertion that it covers a particular requirement.
- Each anchor contains at most seven terms: the three platform terms nearest the
  opening descriptive boundary plus an intact four-term technical phrase. Quick
  Search gets that phrase from adjacent keyword pairs; Advanced Search
  gets it from a combined strategy angle so separately reviewed requirements stay
  joined in retrieval. This keeps Lens from burying the source under newer broad
  matches while retaining the mechanism's phrase boundaries.

## Reason

A paraphrased known mechanism plus a genuinely new subsystem can cause semantic
strategy queries to concentrate on the subsystem. The provider can then return a
strong match for the addition while never retrieving the older source mechanism.
The approved requirements already contain the vocabulary needed to preserve that
mechanism, so use it before any provider call rather than waiting for poor results.

## Consequences

The normal Quick and current Advanced plans contain up to eight Lens queries.
There is no additional OpenAI call and no post-search fallback. Invalid or empty
generation still fails before provider contact. Legacy Strategy runs retain their
review screen; their machine proposal receives the same deterministic anchors and
the reviewer may edit the final list within the existing ten-query limit.
