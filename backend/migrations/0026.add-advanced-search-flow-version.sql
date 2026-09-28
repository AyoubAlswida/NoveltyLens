-- depends: 0025.add-patent-family-to-matches

-- Existing durable runs keep the three-stage route they were admitted under.
-- The default is changed only after the column has backfilled them as version 1,
-- so every run admitted after this migration uses the two-stage route.
ALTER TABLE verification_runs
    ADD COLUMN advanced_search_flow_version SMALLINT NOT NULL DEFAULT 1
        CHECK (advanced_search_flow_version >= 1);

ALTER TABLE verification_runs
    ALTER COLUMN advanced_search_flow_version SET DEFAULT 2;

COMMENT ON COLUMN verification_runs.advanced_search_flow_version IS
    '1 = legacy human-reviewed search strategy; 2 = system-generated queries after the two invention reviews.';
