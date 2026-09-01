-- depends: 0006.create-verification-runs

-- ONE LEASE ROW PER RUN, CREATED WITH THE RUN, AND NEVER DELETED WHILE THE RUN
-- EXISTS.
--
-- The row is the run's PERMANENT fencing counter, not a record of a current
-- holder that comes and goes. An earlier design had release_run_lease() DELETE
-- the row; the next acquisition then INSERTed a fresh one at fence_token = 1,
-- and monotonicity was gone. A recycled token is indistinguishable from a live
-- one: an attempt fenced at token 3, whose owner_id happens to be reused (a
-- restarted pod on the same host and pid, a deterministic worker id, a container
-- scheduled back onto the same slot), would match a LATER lease that had cycled
-- back through 3 and could write under authority it never held.
--
-- So: created once by create_run(), released by CLEARING THE HOLDER, never by
-- removing the row, and fence_token only ever goes up. It is deleted only with
-- its run, by cascade.
CREATE TABLE run_leases (
    run_id       UUID PRIMARY KEY REFERENCES verification_runs(id) ON DELETE CASCADE,
    owner_id     TEXT,               -- host:pid:uuid while held; NULL when unheld
    fence_token  BIGINT NOT NULL DEFAULT 0 CHECK (fence_token >= 0),
    acquired_at  TIMESTAMPTZ,        -- of the CURRENT holding; NULL when unheld
    heartbeat_at TIMESTAMPTZ,
    expires_at   TIMESTAMPTZ,        -- NULL when unheld
    released_at  TIMESTAMPTZ,        -- when the last holding ended, for operators
    -- Held and unheld are the only two shapes. A row with an owner and no expiry,
    -- or an expiry and no owner, is a lease nobody can reason about.
    CONSTRAINT run_leases_held_consistently CHECK (
        (owner_id IS NULL     AND acquired_at IS NULL     AND expires_at IS NULL) OR
        (owner_id IS NOT NULL AND acquired_at IS NOT NULL AND expires_at IS NOT NULL
                              AND expires_at > acquired_at)
    ),
    -- fence_token = 0 is the never-yet-acquired state, and it is never a valid
    -- holder token: every acquisition increments first, so the first holder gets 1.
    CONSTRAINT run_leases_zero_is_unheld CHECK (fence_token > 0 OR owner_id IS NULL)
);
CREATE INDEX run_leases_claimable_idx ON run_leases (expires_at)
    WHERE owner_id IS NOT NULL;
-- Partial: the reclaim sweep only cares about rows that are currently held, and
-- an unheld row has no expiry to compare.

COMMENT ON TABLE run_leases IS
    'The run''s permanent fencing counter. One row per run, created with the run, '
    'released by clearing the holder, and removed only by the run''s cascade. Never '
    'deleted by application code and never reset -- a recycled token is '
    'indistinguishable from a live one.';
COMMENT ON COLUMN run_leases.fence_token IS
    '0 = never acquired. Every acquisition increments, so the first holder gets 1 and '
    'a superseded token is burned forever.';

-- ===================== THE ONE ACTIVE / CLAIMABLE CONTRACT =====================
--
--   active lease:      owner_id IS NOT NULL
--                      AND released_at IS NULL
--                      AND expires_at > now()
--
--   claimable lease:   owner_id IS NULL
--                      OR  released_at IS NOT NULL
--                      OR  expires_at <= now()
--
-- These are exact negations, and the boundary is deliberate. With
-- `expires_at < now()` for claimability there is an instant -- expires_at = now()
-- -- at which the old lease is no longer active but the new worker cannot yet
-- claim it: the run is stuck for one tick with nobody entitled to advance it.
-- Every comparison here uses `> now()` for active and `<= now()` for claimable,
-- with no exceptions.
-- ===============================================================================

-- Monotonicity as a database property, not a convention: nothing may lower a
-- fence token.
--
-- UPDATE ONLY. There is deliberately no BEFORE DELETE guard: a row-level BEFORE
-- DELETE trigger DOES fire for rows removed by a foreign-key cascade, so such a
-- guard would have made deleting an approved parent run impossible -- turning a
-- retention operation into a permanent failure. Deletion is prevented by not
-- having a delete path (asserted by a source-contract test) and by revoking the
-- privilege where the deployment has roles, which stops the application without
-- standing in the cascade's way.
CREATE FUNCTION enforce_lease_monotonicity() RETURNS trigger AS $$
BEGIN
    IF NEW.fence_token < OLD.fence_token THEN
        RAISE EXCEPTION 'fence token for run % would go backwards: % -> %',
            OLD.run_id, OLD.fence_token, NEW.fence_token USING ERRCODE = 'FN006';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER run_leases_monotonic
    BEFORE UPDATE ON run_leases
    FOR EACH ROW EXECUTE FUNCTION enforce_lease_monotonicity();

-- How lease rows are actually protected from deletion, without blocking the
-- cascade:
--
--   * No application code path deletes one. workflow/store.py and
--     workflow/lease.py contain no `DELETE FROM run_leases`, and
--     tests/test_lease_lifecycle.py asserts it over the whole backend package.
--   * Where the deployment has separate database roles, the privilege is removed
--     outright. Uncomment and adapt for that deployment; on a single-role local
--     Compose database these are no-ops and the source-contract test carries the
--     weight.
--
--   -- REVOKE DELETE ON run_leases FROM novelty_api, novelty_worker;
--   -- GRANT EXECUTE ON FUNCTION acquire_run_lease, heartbeat_run_lease, release_run_lease
--   --     TO novelty_worker;
--
--   * The schema-owner / migration role keeps DELETE, so the approved retention
--     policy can still remove a run -- and ON DELETE CASCADE removes its permanent
--     lease row with it. That is the ONLY way a lease row ever disappears.

-- ============================ THE MANDATORY LOCK ORDER ============================
--
--   (1) verification_runs   FOR UPDATE
--   (2) run_leases          FOR UPDATE
--
-- EVERY lease-sensitive operation takes these two locks, in THIS order, and holds
-- both until the transaction commits: lease acquisition, takeover, heartbeat,
-- worker event append, worker status transition, node-attempt commitment, both
-- terminal finalizers, and lease release.
--
-- Two reasons, and both are load-bearing:
--
--   * Holding the LEASE row -- not merely reading it -- is what makes fencing hold
--     for the whole transaction. Validating the lease and then persisting an event
--     in two separate steps leaves a window in which a takeover can bump
--     fence_token between them, so the write commits under authority the writer no
--     longer has.
--   * A single order prevents deadlock. If one operation locked run_leases first
--     while another locked verification_runs first, two concurrent workers would
--     each hold what the other needs. There is one order; there are no exceptions.
--
-- Custom SQLSTATEs, so callers map failures to HTTP codes instead of parsing
-- messages:
--   FN001  fenced: no live lease for this owner and token
--   FN002  a terminal event already exists for this run
--   FN003  terminal or dedupe replay with a conflicting payload, type, key or code
--   FN004  ownership or actor mismatch
--   FN005  run status incompatible with this event
--   FN006  lease-lifecycle violation (missing lease row, backwards fence token)

-- Internal. Called only after the caller intends to hold BOTH locks in order.
-- The FOR UPDATE on run_leases is the point: the lease row stays locked until the
-- calling transaction commits, so a takeover cannot slip between validation and
-- the write.
CREATE FUNCTION _lock_run_and_require_lease(
    p_run_id UUID, p_owner TEXT, p_token BIGINT
) RETURNS VOID AS $$
BEGIN
    -- (1) run row
    PERFORM 1 FROM verification_runs WHERE id = p_run_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown run %', p_run_id USING ERRCODE = 'foreign_key_violation';
    END IF;

    -- (2) lease row -- LOCKED, not merely read, and held to commit.
    PERFORM 1 FROM run_leases
      WHERE run_id = p_run_id AND owner_id = p_owner AND fence_token = p_token
        AND released_at IS NULL AND expires_at > now()   -- the ACTIVE predicate, verbatim
      FOR UPDATE;
    IF NOT FOUND THEN
        -- Zero rows: fenced. A worker that lost its lease -- to expiry, to a
        -- reclaim sweep, or to a takeover -- writes NOTHING from here on. Not
        -- telemetry, not a pause, not a terminal.
        RAISE EXCEPTION 'run % is not leased to % at token %', p_run_id, p_owner, p_token
            USING ERRCODE = 'FN001';
    END IF;
END;
$$ LANGUAGE plpgsql;

-- Lease acquisition and takeover. A FUNCTION rather than one INSERT ... ON
-- CONFLICT, because the statement form could not take the run-row lock first and
-- could not consult run status. Correctness beats keeping it to one statement.
CREATE FUNCTION acquire_run_lease(
    p_run_id UUID, p_owner TEXT, p_ttl INTERVAL
) RETURNS BIGINT AS $$
DECLARE
    v_status run_status;
    v_token  BIGINT;
BEGIN
    IF p_ttl IS NULL OR p_ttl <= interval '0 seconds' THEN
        RAISE EXCEPTION 'lease TTL must be a positive interval, got %', p_ttl
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- (1) run row, first and always.
    SELECT status INTO v_status FROM verification_runs WHERE id = p_run_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown run %', p_run_id USING ERRCODE = 'foreign_key_violation';
    END IF;

    -- Only a run that is actually claimable. A terminal run has nothing left to
    -- execute, and a WAITING_FOR_REVIEW run is parked on a human -- leasing it
    -- would let a worker resume work nobody approved.
    IF v_status NOT IN ('QUEUED', 'RUNNING', 'RESUMING') THEN
        RETURN NULL;
    END IF;

    -- (2) lease row. It ALWAYS exists -- create_run() made it -- so this is an
    -- UPDATE, never an insert-or-update. Locked before it is inspected, so two
    -- workers racing here serialize instead of both reading "claimable" and both
    -- writing.
    PERFORM 1 FROM run_leases WHERE run_id = p_run_id FOR UPDATE;
    IF NOT FOUND THEN
        -- A run with no lease row was not created by create_run(). Fail loudly
        -- rather than inserting one, which would restart the fencing counter at a
        -- value already used.
        RAISE EXCEPTION 'run % has no lease row; it was not created by create_run()', p_run_id
            USING ERRCODE = 'FN006';
    END IF;

    UPDATE run_leases
       SET owner_id     = p_owner,
           fence_token  = fence_token + 1,   -- ALWAYS up, never reset, never reused
           acquired_at  = now(),
           heartbeat_at = now(),
           expires_at   = now() + p_ttl,
           released_at  = NULL
     WHERE run_id = p_run_id
       AND (owner_id IS NULL OR released_at IS NOT NULL OR expires_at <= now())
    RETURNING fence_token INTO v_token;

    -- NULL means a LIVE lease is held elsewhere: do not execute, return to the
    -- poll. Not "retry in a loop", not "assume it is stale".
    RETURN v_token;
END;
$$ LANGUAGE plpgsql;

-- Heartbeat. EXTENDS AN ACTIVE LEASE; it can never revive an expired or released
-- one.
--
-- Matching only (run_id, owner_id, fence_token) would let a beat arriving after
-- expiry -- or after the holder had released -- quietly push expires_at forward
-- and bring the lease back to life. Worse, one racing a takeover could resurrect
-- the old lease in the instant after the reclaim sweep became entitled to claim
-- it, producing two workers that both believe they hold the run. Reactivation is
-- acquire_run_lease's job alone, and it always increments the token.
--
-- Lock order is explicit rather than implied by the UPDATE: run row, then lease row.
CREATE FUNCTION heartbeat_run_lease(
    p_run_id UUID, p_owner TEXT, p_token BIGINT, p_ttl INTERVAL
) RETURNS BOOLEAN AS $$
DECLARE
    v_ok BIGINT;
BEGIN
    IF p_ttl IS NULL OR p_ttl <= interval '0 seconds' THEN
        RAISE EXCEPTION 'lease TTL must be a positive interval, got %', p_ttl
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- (1) run row.
    PERFORM 1 FROM verification_runs WHERE id = p_run_id FOR UPDATE;
    -- (2) lease row.
    PERFORM 1 FROM run_leases WHERE run_id = p_run_id FOR UPDATE;

    UPDATE run_leases SET heartbeat_at = now(), expires_at = now() + p_ttl
     WHERE run_id = p_run_id
       AND owner_id = p_owner
       AND fence_token = p_token
       AND released_at IS NULL
       AND expires_at > now()          -- ACTIVE only; `=` is already too late
    RETURNING fence_token INTO v_ok;

    -- FALSE = fenced. Nothing was touched: not heartbeat_at, not expires_at, not
    -- owner_id, not released_at, not fence_token. The worker stops scheduling work.
    RETURN v_ok IS NOT NULL;
END;
$$ LANGUAGE plpgsql;
-- Contract note: heartbeat returns FALSE rather than raising, because it runs on
-- a thread whose job is to answer "do we still hold this?" -- and an exception
-- there would have to be caught and converted into exactly this boolean. Every
-- OTHER lease-sensitive path raises FN001 instead. That asymmetry is deliberate
-- and is the documented contract.

-- Release. Also lock-ordered: a release racing a terminal finalization must
-- serialize behind it, never interleave with it.
--
-- It CLEARS THE HOLDER; it does not delete the row and it does not touch
-- fence_token. The released token stays burned forever, so the worker that just
-- let go can never match again -- not after a restart, not if its owner_id is
-- regenerated identically, not ever.
CREATE FUNCTION release_run_lease(
    p_run_id UUID, p_owner TEXT, p_token BIGINT
) RETURNS VOID AS $$
DECLARE
    v_rows INTEGER;
BEGIN
    PERFORM 1 FROM verification_runs WHERE id = p_run_id FOR UPDATE;
    PERFORM 1 FROM run_leases WHERE run_id = p_run_id FOR UPDATE;

    UPDATE run_leases
       SET owner_id = NULL, acquired_at = NULL, heartbeat_at = NULL,
           expires_at = NULL, released_at = now()
     WHERE run_id = p_run_id
       AND owner_id = p_owner
       AND fence_token = p_token
       AND released_at IS NULL
       AND expires_at > now();         -- only an ACTIVE holder may release
    GET DIAGNOSTICS v_rows = ROW_COUNT;

    IF v_rows <> 1 THEN
        -- Zero rows is a FENCING FAILURE, not a no-op. A worker releasing a lease
        -- it no longer holds -- wrong owner, wrong token, expired, or already
        -- released -- has been operating on a false belief about its own
        -- authority, and it needs to find that out here rather than continue as if
        -- the handover were clean. Repeating a release is not harmless either: the
        -- second call is evidence of exactly that confusion.
        RAISE EXCEPTION 'run % is not actively leased to % at token %', p_run_id, p_owner,
            p_token USING ERRCODE = 'FN001';
    END IF;
END;
$$ LANGUAGE plpgsql;

-- Execution-attempt telemetry. One row per PHYSICAL attempt, NOT per result.
CREATE TYPE attempt_outcome AS ENUM ('RUNNING', 'COMMITTED', 'FENCED', 'FAILED');

CREATE TABLE node_attempts (
    attempt_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id            UUID NOT NULL REFERENCES verification_runs(id) ON DELETE CASCADE,
    node              TEXT NOT NULL,
    execution_key     TEXT NOT NULL CHECK (execution_key <> ''),
    fence_token       BIGINT NOT NULL,
    owner_id          TEXT NOT NULL,
    outcome           attempt_outcome NOT NULL DEFAULT 'RUNNING',
    provider_calls    INTEGER NOT NULL DEFAULT 0,   -- outbound calls this attempt made
    llm_calls         INTEGER NOT NULL DEFAULT 0,
    started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at       TIMESTAMPTZ,
    detail            TEXT NOT NULL DEFAULT ''      -- sanitized; never a traceback
);
CREATE INDEX node_attempts_run_idx ON node_attempts (run_id, node, started_at);
CREATE INDEX node_attempts_execution_idx ON node_attempts (run_id, node, execution_key);
CREATE UNIQUE INDEX node_attempts_one_committed_execution_idx
    ON node_attempts (run_id, node, execution_key) WHERE outcome = 'COMMITTED';
-- Scoped by execution_key, and that scoping is load-bearing. (run_id, node) alone
-- would permit ONE committed execution of a node for the entire life of a run,
-- which forbids every legitimate repeat: a cell rerun after a context request, a
-- second result_version, a re-verification against changed passage text, a revisit
-- after a new decomposition or candidate-set version, and any ordinary LangGraph
-- loop that reaches the same node again for a different unit of work. Those are
-- not retries; they are new logical executions, and the index has to be able to
-- tell the difference.
--
--   execution_key = the LOGICAL invocation ("verify cell X at result version 2")
--   attempt_id    = one PHYSICAL attempt at it
--
-- Many attempt_ids may share one execution_key during crash recovery. AT MOST ONE
-- of them may ever be COMMITTED.
--
-- A FENCED attempt is recorded so an operator can see that a provider was queried
-- twice during a recovery -- two attempts, one committed result. It is never
-- rendered as a result, never counted in coverage, and never cited as evidence.
-- This table exists to make duplicate external reads VISIBLE, not to make them
-- impossible.

COMMENT ON TABLE node_attempts IS
    'One row per PHYSICAL execution attempt. At most one COMMITTED per '
    '(run_id, node, execution_key). Makes duplicate external reads visible -- it does '
    'not make them impossible: external calls are at-least-once across recovery.';

-- Marking an attempt COMMITTED is a fenced write: only the current lease holder
-- may do it, and the check happens under the mandatory lock order so a takeover
-- cannot slip in between.
CREATE FUNCTION commit_node_attempt(
    p_run_id      UUID,
    p_owner_id    TEXT,
    p_fence_token BIGINT,
    p_attempt_id  UUID,
    p_provider_calls INTEGER,
    p_llm_calls   INTEGER,
    p_detail      TEXT
) RETURNS VOID AS $$
DECLARE
    v_rows INTEGER;
BEGIN
    PERFORM _lock_run_and_require_lease(p_run_id, p_owner_id, p_fence_token);

    UPDATE node_attempts
       SET outcome = 'COMMITTED', finished_at = now(),
           provider_calls = p_provider_calls, llm_calls = p_llm_calls,
           detail = COALESCE(p_detail, '')
     WHERE attempt_id = p_attempt_id
       AND run_id = p_run_id
       AND fence_token = p_fence_token
       AND owner_id = p_owner_id
       AND outcome = 'RUNNING';        -- a FENCED or FAILED attempt can never commit
    GET DIAGNOSTICS v_rows = ROW_COUNT;

    IF v_rows <> 1 THEN
        RAISE EXCEPTION 'attempt % of run % cannot be committed', p_attempt_id, p_run_id
            USING ERRCODE = 'FN001';
    END IF;
END;
$$ LANGUAGE plpgsql;
