-- depends: 0015.create-report-versions

-- Close the last unfenced writes in the system: the three `node_attempts`
-- mutations that were not lease-guarded.
--
-- WHAT WAS WRONG. `commit_node_attempt` (migration 0007) was fenced from the
-- start, but attempts were *opened*, *failed* and *closed* by plain UPDATE and
-- INSERT statements in `workflow/attempts.py` with no lease check at all. Three
-- concrete holes followed, and the second is the one that actually destroys
-- evidence:
--
--   1. A fenced worker could still INSERT a fresh `RUNNING` attempt, so telemetry
--      could grow under authority the writer no longer held.
--   2. A fenced worker could mark its own attempt `FAILED` -- and that OVERWRITES
--      the `FENCED` marking reconciliation is supposed to apply. `node_attempts`
--      exists to make a duplicate external read visible ("this node ran twice,
--      one attempt committed"), and a stale worker relabelling its own row as
--      `FAILED` hides exactly that. It also reads as a node that errored when in
--      fact it was superseded, which is a different operational story.
--   3. `close_superseded_attempts` took no owner and no token whatsoever, so any
--      caller could mass-close another holder's attempts.
--
-- The rule this restores is not new; it is stated in the implementation prompt:
-- **a fenced worker must never write telemetry using the authority it just
-- lost**, and the marking is done by reconciliation under the new holder's own
-- valid lease. An application-side `if` cannot enforce that, because the lease
-- can be superseded between the check and the write. Only holding the lease row
-- for the life of the transaction can.
--
-- WHY A NEW MIGRATION rather than an edit to 0007: 0007 is applied. Rewriting an
-- applied migration leaves every database that already ran it silently different
-- from the file that claims to describe it.
--
-- All three take THE MANDATORY LOCK ORDER through `_lock_run_and_require_lease`:
-- `verification_runs FOR UPDATE`, then `run_leases FOR UPDATE`, held to commit.
-- A stale, expired, released or wrong-owner caller raises `FN001`.

-- Open a physical attempt. Fenced: only the current holder may add telemetry,
-- and the row it adds is attributed to the holder's own owner/token rather than
-- to anything the caller supplies separately.
CREATE FUNCTION begin_node_attempt(
    p_run_id        UUID,
    p_owner_id      TEXT,
    p_fence_token   BIGINT,
    p_node          TEXT,
    p_execution_key TEXT
) RETURNS UUID AS $$
DECLARE
    v_attempt_id UUID;
BEGIN
    PERFORM _lock_run_and_require_lease(p_run_id, p_owner_id, p_fence_token);

    INSERT INTO node_attempts (run_id, node, execution_key, fence_token, owner_id)
    VALUES (p_run_id, p_node, p_execution_key, p_fence_token, p_owner_id)
    RETURNING attempt_id INTO v_attempt_id;

    RETURN v_attempt_id;
END;
$$ LANGUAGE plpgsql;

-- Close an attempt that raised. Fenced, and restricted to the caller's OWN
-- attempt: a worker may record its own failure, never someone else's.
--
-- A stale worker gets `FN001` and its attempt is left `RUNNING` -- which is the
-- correct outcome, because reconciliation will then close it as `FENCED` under
-- the new holder's valid lease. `FAILED` and `FENCED` are different facts and the
-- system must not let a worker that lost the run choose between them.
CREATE FUNCTION fail_node_attempt(
    p_run_id      UUID,
    p_owner_id    TEXT,
    p_fence_token BIGINT,
    p_attempt_id  UUID,
    p_detail      TEXT
) RETURNS VOID AS $$
DECLARE
    v_rows INTEGER;
BEGIN
    PERFORM _lock_run_and_require_lease(p_run_id, p_owner_id, p_fence_token);

    UPDATE node_attempts
       SET outcome = 'FAILED', finished_at = now(), detail = COALESCE(p_detail, '')
     WHERE attempt_id = p_attempt_id
       AND run_id = p_run_id
       AND owner_id = p_owner_id
       AND fence_token = p_fence_token
       AND outcome = 'RUNNING';
    GET DIAGNOSTICS v_rows = ROW_COUNT;

    IF v_rows <> 1 THEN
        RAISE EXCEPTION 'attempt % of run % cannot be failed by % at token %',
            p_attempt_id, p_run_id, p_owner_id, p_fence_token USING ERRCODE = 'FN001';
    END IF;
END;
$$ LANGUAGE plpgsql;

-- Reconciliation: close attempts left `RUNNING` under a SUPERSEDED token.
--
-- This is the ONLY place an attempt is ever marked `FENCED`, and it is done by
-- the new holder under its own valid lease -- never by the fenced worker itself,
-- which by definition no longer has the authority to write telemetry.
--
-- Only tokens strictly below the caller's are touched, so the caller's own
-- in-flight attempts are never disturbed and this is safe to call at the start of
-- every acquisition.
CREATE FUNCTION close_superseded_node_attempts(
    p_run_id      UUID,
    p_owner_id    TEXT,
    p_fence_token BIGINT
) RETURNS INTEGER AS $$
DECLARE
    v_rows INTEGER;
BEGIN
    PERFORM _lock_run_and_require_lease(p_run_id, p_owner_id, p_fence_token);

    UPDATE node_attempts
       SET outcome = 'FENCED', finished_at = now(),
           detail = CASE WHEN detail = '' THEN 'superseded by a later lease' ELSE detail END
     WHERE run_id = p_run_id
       AND outcome = 'RUNNING'
       AND fence_token < p_fence_token;
    GET DIAGNOSTICS v_rows = ROW_COUNT;

    RETURN v_rows;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION begin_node_attempt(UUID, TEXT, BIGINT, TEXT, TEXT) IS
    'Fenced. The only way an attempt row is created.';
COMMENT ON FUNCTION fail_node_attempt(UUID, TEXT, BIGINT, UUID, TEXT) IS
    'Fenced, own-attempt only. A stale worker leaves its attempt RUNNING for '
    'reconciliation to close as FENCED -- FAILED and FENCED are different facts.';
COMMENT ON FUNCTION close_superseded_node_attempts(UUID, TEXT, BIGINT) IS
    'Fenced. The only place an attempt is marked FENCED, always by the NEW holder.';

-- Where the deployment has separate database roles, the application roles get no
-- direct DML on the table at all -- the four functions are the whole surface.
-- On a single-role local Compose database these are no-ops and the
-- source-contract tests in tests/test_node_attempts.py carry the weight.
--
--   -- REVOKE INSERT, UPDATE, DELETE ON node_attempts FROM novelty_api, novelty_worker;
--   -- GRANT EXECUTE ON FUNCTION begin_node_attempt, fail_node_attempt,
--   --     close_superseded_node_attempts, commit_node_attempt TO novelty_worker;
