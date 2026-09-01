-- Restore migration 0008's finalize_run_failure verbatim: same signature,
-- same behaviour, without the failure_detail write. Reproduced from 0008
-- rather than hand-edited, so the two cannot drift.

CREATE OR REPLACE FUNCTION finalize_run_failure(
    p_run_id       UUID,
    p_owner_id     TEXT,
    p_fence_token  BIGINT,
    p_payload      JSONB,      -- SANITIZED: no traceback, no provider body, no credential
    p_failure_code TEXT,
    p_dedupe_key   TEXT
) RETURNS BIGINT AS $$
DECLARE
    v_status   run_status;
    v_sequence BIGINT;
BEGIN
    PERFORM 1 FROM verification_runs WHERE id = p_run_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown run %', p_run_id USING ERRCODE = 'foreign_key_violation';
    END IF;

    v_sequence := _terminal_replay_or_null(p_run_id, 'error', p_payload, p_dedupe_key,
                                           p_failure_code);
    IF v_sequence IS NOT NULL THEN
        RETURN v_sequence;
    END IF;

    PERFORM _lock_run_and_require_lease(p_run_id, p_owner_id, p_fence_token);

    SELECT status INTO v_status FROM verification_runs WHERE id = p_run_id;
    IF v_status NOT IN ('QUEUED', 'RUNNING', 'RESUMING') THEN
        RAISE EXCEPTION 'run % is %, which cannot be failed by its executor', p_run_id, v_status
            USING ERRCODE = 'FN005';
    END IF;

    v_sequence := _append_event_locked(p_run_id, 'error', NULL, p_payload, p_dedupe_key);

    UPDATE verification_runs
       SET status = 'FAILED', failure_code = p_failure_code, updated_at = now()
     WHERE id = p_run_id;

    RETURN v_sequence;
END;
$$ LANGUAGE plpgsql;

COMMENT ON COLUMN verification_runs.failure_detail IS
    'sanitized; never a traceback';
