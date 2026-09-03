-- depends: 0023.create-search-strategies

-- Remove the numeric similarity score and the overall novelty verdict from the
-- stored report. The product no longer publishes either one: a percentage is a
-- precision the embedding cosine does not have, and a single-word verdict over
-- a whole run is a legal-sounding conclusion the pipeline never actually makes.
-- The per-match High/Medium/Low `risk` rating survives and is the only
-- judgement that ships.
--
-- This is the one destructive DDL in the Advanced Search change, and it is
-- deliberate rather than incidental. Leaving the columns in place would leave
-- stored scores the product has decided not to keep, and a nullable leftover
-- column is exactly how a removed field comes back by accident six months
-- later. The scores in existing rows are not migrated anywhere; they are
-- dropped, and the rollback cannot bring them back (see the .rollback.sql).
--
-- `search_run_matches.rank` is deliberately untouched. It already stored the
-- ordinal and `list_history` already ordered by it, so with `pct` gone it
-- becomes the ONLY surviving record of the order a run produced. Nothing may
-- re-sort those rows on read.

ALTER TABLE search_run_matches DROP COLUMN pct;

ALTER TABLE search_runs
    DROP COLUMN top_score,
    DROP COLUMN verdict;

-- 0002 and 0003 describe these columns in their table comments. Those files are
-- history and are not edited; the comments are restated here instead.
COMMENT ON TABLE search_runs IS
    'One row per completed analyze pipeline run, enough to rebuild the "result" '
    'SSE event''s concepts/query_count/recommendations fields. Carries no '
    'similarity score and no overall novelty verdict.';

COMMENT ON TABLE search_run_matches IS
    'Per-run patent matches; joins search_runs to patents to reconstruct patents[] '
    'in the "result" SSE payload, in original rank order. `rank` is the only '
    'record of that order -- the score it was derived from is not stored.';
