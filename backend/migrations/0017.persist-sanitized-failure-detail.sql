-- depends: 0016.fence-node-attempt-telemetry

-- Populate `verification_runs.failure_detail`, which migration 0006 created and
-- nothing ever wrote.
--
-- WHY THIS MATTERS beyond tidiness. The column documents itself as "sanitized;
-- never a traceback", and the Gate A acceptance criteria require that no secret
-- can reach it -- but a column nothing writes satisfies that requirement
-- *vacuously*. A test asserting "no credential in failure_detail" passed because
-- the value was always NULL, which would have kept passing right up until the
-- day something started writing to it unsanitized.
--
-- So: the finalizer writes it, from the payload's own already-sanitized `detail`
-- field. `workflow/executor.py` builds that through `workflow/redaction.py`,
-- which treats the exception text as hostile -- redacting credential-bearing URL
-- parameters, DSN userinfo, Authorization headers and bearer tokens, and dropping
-- provider bodies and tracebacks entirely.
--
-- No signature change, so nothing that calls this function needs to know: the
-- detail is read out of the JSONB payload the caller already passes. That keeps
-- ONE sanitized string as the source for both the terminal event and the run row,
-- rather than two that could disagree about what was safe to say.
--
-- CREATE OR REPLACE rather than an edit to 0008, which is applied.

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
       SET status = 'FAILED',
           failure_code = p_failure_code,
           -- Straight from the payload, which is the same sanitized string the
           -- terminal event carries. One source, so the row and the event can
           -- never disagree about what was safe to say.
           failure_detail = p_payload ->> 'detail',
           updated_at = now()
     WHERE id = p_run_id;

    RETURN v_sequence;
END;
$$ LANGUAGE plpgsql;

COMMENT ON COLUMN verification_runs.failure_detail IS
    'The terminal error event''s sanitized detail, written by finalize_run_failure. '
    'Never a traceback, never a provider body, never a credential -- see '
    'backend/workflow/redaction.py, which produces it.';
