DROP TRIGGER verification_runs_terminal_agreement ON verification_runs;
DROP FUNCTION enforce_terminal_agreement();
DROP FUNCTION finalize_run_failure(UUID, TEXT, BIGINT, JSONB, TEXT, TEXT);
DROP FUNCTION finalize_run_success(UUID, TEXT, BIGINT, JSONB, TEXT);
DROP FUNCTION _terminal_replay_or_null(UUID, workflow_event_type, JSONB, TEXT, TEXT);
DROP FUNCTION append_api_event(UUID, TEXT, run_status, workflow_event_type, JSONB, TEXT);
DROP FUNCTION pause_run_for_review(UUID, TEXT, BIGINT, workflow_gate, JSONB, JSONB, TEXT);
DROP FUNCTION append_worker_event(UUID, TEXT, BIGINT, workflow_event_type, TEXT, JSONB, TEXT);
DROP FUNCTION create_run(TEXT, TEXT, DATE, TEXT, TEXT, TEXT);
DROP FUNCTION _append_event_locked(UUID, workflow_event_type, TEXT, JSONB, TEXT);
DROP TABLE workflow_events;
DROP TYPE workflow_event_type;

-- pgcrypto was created by this migration (CREATE EXTENSION IF NOT EXISTS), so
-- rolling back removes it. If another object in the database depends on it the
-- DROP raises rather than silently leaving a half-reversed migration, which is
-- the correct failure: an extension nothing here owns should not be removed
-- quietly. Note the IF NOT EXISTS on the way in means a pre-existing pgcrypto is
-- dropped by this rollback too -- nothing else in this schema uses it today.
DROP EXTENSION IF EXISTS pgcrypto;
