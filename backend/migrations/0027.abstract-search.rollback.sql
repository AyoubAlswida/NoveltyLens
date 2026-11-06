ALTER TABLE verification_runs DROP COLUMN retrieval_version;
ALTER TABLE search_run_matches DROP COLUMN analysis_snapshot;
ALTER TABLE search_runs DROP COLUMN analysis_metadata;
DROP TABLE epo_request_clock;
