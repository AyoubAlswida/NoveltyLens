-- Re-add the dropped score columns.
--
-- THIS ROLLBACK DOES NOT RESTORE THE VALUES. The forward migration dropped the
-- columns outright; the scores and verdicts that were in them are gone and
-- nothing here can reconstruct them. Rolling back recovers the *shape* of the
-- schema, not its contents.
--
-- The columns come back NULLABLE, unlike the NOT NULL originals in 0002/0003.
-- That difference is intentional and is the honest encoding of what a rolled
-- back row actually knows. Restoring NOT NULL would require a DEFAULT, and any
-- default here (`0`, `'Strong'`) would write a measurement no run ever made
-- into the slot the UI reads as a measurement -- the exact substitution this
-- project's zero-tolerance policy forbids. A NULL says "not known"; a fabricated
-- 0 says "we scored this and it was zero", and those are different claims.
--
-- Consequence: code written against the pre-0024 schema will need to tolerate
-- NULL in these three columns, or backfill them from a source that genuinely
-- has the values (a database backup taken before 0024 was applied).

ALTER TABLE search_run_matches
    ADD COLUMN pct INTEGER CHECK (pct BETWEEN 0 AND 100);

ALTER TABLE search_runs
    ADD COLUMN top_score INTEGER CHECK (top_score BETWEEN 0 AND 100),
    ADD COLUMN verdict TEXT CHECK (verdict IN ('Strong', 'Moderate', 'Weak'));

COMMENT ON TABLE search_runs IS
    'One row per completed analyze pipeline run, enough to rebuild the "result" '
    'SSE event''s concepts/query_count/top_score/verdict/recommendations fields.';

COMMENT ON TABLE search_run_matches IS
    'Per-run patent matches; joins search_runs to patents to reconstruct patents[] '
    'in the "result" SSE payload, in original rank order.';
