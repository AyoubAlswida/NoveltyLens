-- depends: 0026.add-advanced-search-flow-version
CREATE TABLE epo_request_clock (id integer PRIMARY KEY CHECK (id=1), next_at timestamptz NOT NULL);
INSERT INTO epo_request_clock VALUES (1, now());
ALTER TABLE search_runs ADD COLUMN analysis_metadata jsonb;
ALTER TABLE search_run_matches ADD COLUMN analysis_snapshot jsonb;
ALTER TABLE verification_runs ADD COLUMN retrieval_version text NOT NULL DEFAULT 'legacy';
ALTER TABLE verification_runs ALTER COLUMN retrieval_version SET DEFAULT 'epo-abstract-v1';
