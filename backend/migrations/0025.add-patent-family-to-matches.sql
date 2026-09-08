-- depends: 0024.drop-report-scores

ALTER TABLE search_run_matches
    ADD COLUMN family_id TEXT NOT NULL DEFAULT '',
    ADD COLUMN family_size INTEGER NOT NULL DEFAULT 0 CHECK (family_size >= 0);

COMMENT ON COLUMN search_run_matches.family_id IS
    'Provider-backed simple patent-family key snapshotted for report grouping. Empty means unavailable.';
COMMENT ON COLUMN search_run_matches.family_size IS
    'Provider-reported total simple-family publication count; zero means unavailable.';
