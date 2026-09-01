-- depends: 0007.create-run-leases

-- The replayable event log, and the FOUR authorized writers that are the only
-- way anything ever reaches it.
--
-- This table is NOT the LangGraph checkpoint and never replaces it. The
-- checkpoint answers "where is execution?"; this answers "what has the client
-- been told?". Two stores, two jobs.

CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- digest() for payload hashing below

CREATE TYPE workflow_event_type AS ENUM
    ('progress', 'verification_progress', 'review_required', 'paused', 'resumed',
     'result', 'error');
-- A CLOSED vocabulary, deliberately. `snapshot` is absent because a snapshot is
-- connection-local and never persisted. A typo'd event type is now a database
-- error rather than a row nothing will ever render.

CREATE TABLE workflow_events (
    run_id      UUID NOT NULL REFERENCES verification_runs(id) ON DELETE CASCADE,
    sequence    BIGINT NOT NULL CHECK (sequence >= 1),
    event_type  workflow_event_type NOT NULL,
    phase       TEXT,
    payload     JSONB NOT NULL,
    payload_sha256 TEXT NOT NULL,   -- over `payload`; makes a dedupe replay verifiable
    dedupe_key  TEXT NOT NULL,
    is_terminal BOOLEAN NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, sequence),
    UNIQUE (run_id, dedupe_key),
    -- is_terminal is not an independent fact a caller may assert. It is a
    -- FUNCTION of the event type, and the database says so: a `progress` row
    -- marked terminal and a `result` row marked non-terminal are both rejected.
    CONSTRAINT workflow_events_terminal_matches_type
        CHECK (is_terminal = (event_type IN ('result', 'error')))
);
CREATE UNIQUE INDEX workflow_events_one_terminal_idx
    ON workflow_events (run_id) WHERE is_terminal;
-- Combined with the CHECK above, the partial unique index means a run can hold
-- AT MOST ONE of `result` or `error` -- not one of each, and not two of either.
-- No reconnection can ever be served two terminals.
--
-- What neither of them can express is ORDERING or AUTHORITY: nothing above stops
-- a `progress` row being appended at sequence 49 after a terminal at 48, and
-- nothing above asks who is writing. Both are enforced by the four authorized
-- functions below, inside the same transaction, under a row lock.

COMMENT ON TABLE workflow_events IS
    'The replayable per-run event log. NOT the LangGraph checkpoint: the checkpoint '
    'holds execution position, this holds what the client has been told. Written only '
    'by create_run, append_worker_event, append_api_event and the two finalizers.';

-- Application code NEVER inserts into workflow_events directly. There are exactly
-- FOUR authorized writers, and each one declares whose authority it acts under. A
-- source-contract test greps the backend and fails if any module outside
-- workflow/events.py touches the table.
--
--   create_run(...)            admission          -- the seq-1 queued event, no lease yet
--   append_worker_event(...)   worker, FENCED     -- non-terminal, RUNNING only, lease-locked
--   append_api_event(...)      API, OWNERSHIP     -- `resumed` only, actor-scoped
--   finalize_run_success/failure(...)  worker, FENCED + ATOMIC  -- the ONLY terminal writers
--
-- A single generic append_workflow_event(run_id, type, ...) is deliberately
-- absent. It would take no owner and no fence token, so a worker whose lease had
-- expired could still append progress, cell completions, a pause -- or a terminal
-- `result` -- and it would let a caller write a terminal event without atomically
-- transitioning the run. Both are execution-integrity holes, not style problems.

-- Internal. Called only after the caller has taken BOTH locks in order.
CREATE FUNCTION _append_event_locked(
    p_run_id      UUID,
    p_event_type  workflow_event_type,
    p_phase       TEXT,
    p_payload     JSONB,
    p_dedupe_key  TEXT
) RETURNS BIGINT AS $$
DECLARE
    v_existing  BIGINT;
    v_hash      TEXT;
    v_prev_hash TEXT;
    v_terminal  BOOLEAN := p_event_type IN ('result', 'error');
    v_sequence  BIGINT;
BEGIN
    v_hash := encode(digest(p_payload::text, 'sha256'), 'hex');

    -- Idempotent re-emission: reconcile may replay, and replay must be a no-op.
    -- The payload hash is compared too: the same dedupe key with DIFFERENT
    -- content is a conflict, not a replay, and silently returning the old row
    -- would hide it.
    SELECT sequence, payload_sha256 INTO v_existing, v_prev_hash
      FROM workflow_events WHERE run_id = p_run_id AND dedupe_key = p_dedupe_key;
    IF FOUND THEN
        IF v_prev_hash <> v_hash THEN
            RAISE EXCEPTION 'dedupe key % replayed with a different payload for run %',
                p_dedupe_key, p_run_id USING ERRCODE = 'FN003';
        END IF;
        RETURN v_existing;
    END IF;

    IF EXISTS (SELECT 1 FROM workflow_events WHERE run_id = p_run_id AND is_terminal) THEN
        -- Nothing follows a terminal event. Not a progress row, not a paused, not
        -- a second terminal. The run is over.
        RAISE EXCEPTION 'run % already has a terminal event; % cannot be appended',
            p_run_id, p_event_type USING ERRCODE = 'FN002';
    END IF;

    UPDATE verification_runs SET next_sequence = next_sequence + 1
     WHERE id = p_run_id RETURNING next_sequence INTO v_sequence;

    INSERT INTO workflow_events
        (run_id, sequence, event_type, phase, payload, payload_sha256, dedupe_key, is_terminal)
    VALUES (p_run_id, v_sequence, p_event_type, p_phase, p_payload, v_hash, p_dedupe_key,
            v_terminal);

    RETURN v_sequence;
END;
$$ LANGUAGE plpgsql;

-- (1) ADMISSION. The only writer of a run row and of its seq-1 queued event, both
-- in one transaction. No lease exists yet, which is precisely why this is its own
-- function rather than a call into the worker path. Policy validation has already
-- passed before this runs; if it had not, nothing here would have been called.
CREATE FUNCTION create_run(
    p_description          TEXT,
    p_claims               TEXT,
    p_cutoff_date          DATE,
    p_date_policy          TEXT,
    p_text_version_policy  TEXT,
    p_created_by           TEXT
) RETURNS UUID AS $$
DECLARE
    v_run_id UUID;
BEGIN
    INSERT INTO verification_runs
        (description, claims, cutoff_date, date_policy, text_version_policy,
         created_by, thread_id, status)
    VALUES (p_description, p_claims, p_cutoff_date, p_date_policy, p_text_version_policy,
            p_created_by, gen_random_uuid()::text, 'QUEUED')
    RETURNING id INTO v_run_id;

    UPDATE verification_runs SET thread_id = v_run_id::text WHERE id = v_run_id;

    -- The run's permanent fencing counter, created unheld at token 0. It is never
    -- deleted while the run exists, and every acquisition increments it, so no
    -- token is ever reused.
    INSERT INTO run_leases (run_id) VALUES (v_run_id);

    -- The run row is already locked by this transaction's own INSERT, and the
    -- lease is unheld.
    PERFORM _append_event_locked(
        v_run_id, 'progress', 'queued',
        jsonb_build_object('type', 'progress', 'run_id', v_run_id, 'phase', 'queued',
                           'message', 'Run accepted.'),
        'queued');

    RETURN v_run_id;
END;
$$ LANGUAGE plpgsql;

-- (2) WORKER EVENTS. Non-terminal, RUNNING only, current lease holder only.
--
-- RUNNING only, and that is a real narrowing. The executor transitions
-- QUEUED -> RUNNING and RESUMING -> RUNNING *before* it executes any graph node,
-- so a worker emitting progress while the run still reads QUEUED or RESUMING has
-- skipped its own transition -- a bug the database should catch rather than a
-- state to accommodate.
CREATE FUNCTION append_worker_event(
    p_run_id      UUID,
    p_owner_id    TEXT,
    p_fence_token BIGINT,
    p_event_type  workflow_event_type,
    p_phase       TEXT,
    p_payload     JSONB,
    p_dedupe_key  TEXT
) RETURNS BIGINT AS $$
DECLARE
    v_status run_status;
BEGIN
    IF p_event_type IN ('result', 'error') THEN
        -- Terminal events are never appended on their own. They exist only as
        -- half of a finalization that also moves the run to COMPLETED or FAILED,
        -- atomically.
        RAISE EXCEPTION 'use finalize_run_success/failure for %, not append_worker_event',
            p_event_type USING ERRCODE = 'FN005';
    END IF;
    IF p_event_type = 'resumed' THEN
        -- `resumed` describes a human approval the API committed. A worker
        -- asserting it would be claiming an approval happened.
        RAISE EXCEPTION 'resumed is an API event, not a worker event' USING ERRCODE = 'FN005';
    END IF;

    PERFORM _lock_run_and_require_lease(p_run_id, p_owner_id, p_fence_token);

    SELECT status INTO v_status FROM verification_runs WHERE id = p_run_id;
    IF v_status <> 'RUNNING' THEN
        RAISE EXCEPTION 'run % is %, not RUNNING; % cannot be appended',
            p_run_id, v_status, p_event_type USING ERRCODE = 'FN005';
    END IF;

    RETURN _append_event_locked(p_run_id, p_event_type, p_phase, p_payload, p_dedupe_key);
END;
$$ LANGUAGE plpgsql;

-- The gate pause, as ONE call: both events and the status transition, under the
-- locks already held. Splitting it across three round trips would leave two
-- intermediate states a crash could expose, and the pause is the one place where
-- `paused` is legal *because* it accompanies the transition out of RUNNING.
CREATE FUNCTION pause_run_for_review(
    p_run_id      UUID,
    p_owner_id    TEXT,
    p_fence_token BIGINT,
    p_gate        workflow_gate,
    p_review_payload JSONB,
    p_pause_payload  JSONB,
    p_checkpoint_id  TEXT
) RETURNS BIGINT AS $$
DECLARE
    v_status run_status;
    v_paused BIGINT;
    v_rows   INTEGER;
BEGIN
    PERFORM _lock_run_and_require_lease(p_run_id, p_owner_id, p_fence_token);

    SELECT status INTO v_status FROM verification_runs WHERE id = p_run_id;
    IF v_status <> 'RUNNING' THEN
        RAISE EXCEPTION 'run % is %, not RUNNING; it cannot pause', p_run_id, v_status
            USING ERRCODE = 'FN005';
    END IF;

    -- Stable, checkpoint-derived dedupe keys: a reconcile replay is a no-op, and
    -- a repair after a crash between the checkpoint and this transaction adds
    -- exactly the missing rows.
    PERFORM _append_event_locked(p_run_id, 'review_required', NULL, p_review_payload,
                                 'review_required:' || p_gate || ':' || p_checkpoint_id);
    v_paused := _append_event_locked(p_run_id, 'paused', NULL, p_pause_payload,
                                     'paused:' || p_gate || ':' || p_checkpoint_id);

    UPDATE verification_runs
       SET status = 'WAITING_FOR_REVIEW', current_gate = p_gate, updated_at = now()
     WHERE id = p_run_id AND status = 'RUNNING';

    -- Release by clearing the holder, never by deleting the row. The lease was
    -- validated and LOCKED at the top of this function, so this must affect
    -- exactly one row; anything else means the invariant broke mid-transaction.
    UPDATE run_leases
       SET owner_id = NULL, acquired_at = NULL, heartbeat_at = NULL,
           expires_at = NULL, released_at = now()
     WHERE run_id = p_run_id
       AND owner_id = p_owner_id
       AND fence_token = p_fence_token
       AND released_at IS NULL
       AND expires_at > now();
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    IF v_rows <> 1 THEN
        RAISE EXCEPTION 'run % lost its lease during the pause transaction', p_run_id
            USING ERRCODE = 'FN001';
    END IF;

    RETURN v_paused;
END;
$$ LANGUAGE plpgsql;

-- (3) API EVENTS. A CLOSED SET OF ONE: `resumed`. Every other persisted event is
-- either admission, worker, or terminal, so a generic API writer would be an
-- unnecessary hole in the fencing model. Resolved by run_id + actor_email, so a
-- user cannot emit into another user's run. Called INSIDE the approval
-- transaction, alongside the human decision, the audit row, the idempotency
-- record and the WAITING_FOR_REVIEW -> RESUMING transition.
--
-- No lease is taken: a parked run has no holder. That is why this path is guarded
-- by ownership and by expected_status instead, and why it cannot be used to
-- bypass fencing -- it can only ever write `resumed`, and only to a run the caller
-- owns and that is parked exactly where the caller thinks it is.
CREATE FUNCTION append_api_event(
    p_run_id          UUID,
    p_actor_email     TEXT,
    p_expected_status run_status,
    p_event_type      workflow_event_type,
    p_payload         JSONB,
    p_dedupe_key      TEXT
) RETURNS BIGINT AS $$
DECLARE
    v_status run_status;
BEGIN
    IF p_event_type <> 'resumed' THEN
        RAISE EXCEPTION 'append_api_event accepts only resumed, not %', p_event_type
            USING ERRCODE = 'FN005';
    END IF;

    SELECT status INTO v_status FROM verification_runs
      WHERE id = p_run_id AND created_by = p_actor_email FOR UPDATE;
    IF NOT FOUND THEN
        -- Unknown run and another user's run are deliberately indistinguishable.
        RAISE EXCEPTION 'run % not found for %', p_run_id, p_actor_email USING ERRCODE = 'FN004';
    END IF;
    IF v_status <> p_expected_status THEN
        RAISE EXCEPTION 'run % is % but the caller expected %', p_run_id, v_status,
            p_expected_status USING ERRCODE = 'FN005';
    END IF;

    RETURN _append_event_locked(p_run_id, p_event_type, NULL, p_payload, p_dedupe_key);
END;
$$ LANGUAGE plpgsql;

-- (4) TERMINAL FINALIZATION. The ONLY writers of `result` and `error`, and the
-- only place a run reaches COMPLETED or FAILED. Event and status commit together
-- or not at all.
--
-- ORDER MATTERS, and the obvious order is wrong. Checking the lease and the
-- non-terminal status FIRST makes an identical retry impossible: by then the run
-- is already COMPLETED and the lease has usually been released, so the retry
-- would fail on status or on FN001 before it ever reached the dedupe check -- an
-- "idempotent" path that only works if it is never used. So: lock the run, look
-- for an existing terminal FIRST, and only take the lease path when there is
-- genuinely new work to do.
CREATE FUNCTION _terminal_replay_or_null(
    p_run_id        UUID,
    p_event_type    workflow_event_type,
    p_payload       JSONB,
    p_dedupe_key    TEXT,
    p_failure_code  TEXT
) RETURNS BIGINT AS $$
DECLARE
    v_seq   BIGINT;
    v_type  workflow_event_type;
    v_key   TEXT;
    v_hash  TEXT;
    v_code  TEXT;
BEGIN
    SELECT e.sequence, e.event_type, e.dedupe_key, e.payload_sha256, r.failure_code
      INTO v_seq, v_type, v_key, v_hash, v_code
      FROM workflow_events e
      JOIN verification_runs r ON r.id = e.run_id
     WHERE e.run_id = p_run_id AND e.is_terminal;
    IF NOT FOUND THEN
        RETURN NULL;                      -- no terminal yet: the caller does the real work
    END IF;

    IF v_key <> p_dedupe_key
       OR v_type <> p_event_type
       OR v_hash <> encode(digest(p_payload::text, 'sha256'), 'hex')
       OR v_code IS DISTINCT FROM p_failure_code THEN
        -- A conflicting terminal retry. Returning the stored one would answer a
        -- different question than the caller asked.
        RAISE EXCEPTION 'run % already terminated with a different terminal event', p_run_id
            USING ERRCODE = 'FN003';
    END IF;

    RETURN v_seq;                         -- a true replay: nothing is created or changed
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION finalize_run_success(
    p_run_id      UUID,
    p_owner_id    TEXT,
    p_fence_token BIGINT,
    p_payload     JSONB,
    p_dedupe_key  TEXT
) RETURNS BIGINT AS $$
DECLARE
    v_status   run_status;
    v_sequence BIGINT;
BEGIN
    -- (1) run row.
    PERFORM 1 FROM verification_runs WHERE id = p_run_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown run %', p_run_id USING ERRCODE = 'foreign_key_violation';
    END IF;

    -- (2) replay BEFORE any lease or status check -- see the note above.
    v_sequence := _terminal_replay_or_null(p_run_id, 'result', p_payload, p_dedupe_key, NULL);
    IF v_sequence IS NOT NULL THEN
        RETURN v_sequence;
    END IF;

    -- (3) genuinely new: now the lease and the status must both hold.
    PERFORM _lock_run_and_require_lease(p_run_id, p_owner_id, p_fence_token);

    SELECT status INTO v_status FROM verification_runs WHERE id = p_run_id;
    IF v_status <> 'RUNNING' THEN
        RAISE EXCEPTION 'run % is %; only a RUNNING run can succeed', p_run_id, v_status
            USING ERRCODE = 'FN005';
    END IF;

    v_sequence := _append_event_locked(p_run_id, 'result', NULL, p_payload, p_dedupe_key);

    UPDATE verification_runs SET status = 'COMPLETED', updated_at = now()
     WHERE id = p_run_id AND status = 'RUNNING';

    RETURN v_sequence;
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION finalize_run_failure(
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
-- Both finalizers are TRUSTED INTERNAL EXECUTOR OPERATIONS. They are not HTTP
-- endpoints, are not reachable through a user-supplied function name or SQL
-- fragment, and where the deployment supports separate database roles their
-- EXECUTE privilege belongs to the workflow service role alone. A replay returns
-- only the event sequence to the executor -- it is an internal acknowledgment,
-- not a data-retrieval path, and it grants no ownership bypass.
--
-- Both are executor-only and lease-fenced for NEW terminals. The
-- WAITING_FOR_REVIEW -> FAILED edge in run_status_transitions is reserved for an
-- operator "abandon run" action that this work does NOT build; when it is built
-- it needs its own ownership-scoped finalizer, because no lease is held while a
-- run is parked. Leaving the edge unused is deliberate.
--
-- Concurrency: both take the run-row lock first, so a success finalizer and a
-- failure finalizer racing serialize. The first commits; the second's replay
-- check finds a terminal whose type does not match and raises FN003. Exactly one
-- terminal outcome, always.

-- Terminal event and terminal status must agree: result -> COMPLETED,
-- error -> FAILED. The finalizers above are the only intended writers, but the
-- trigger is what makes it true of any writer, including a future one and a
-- hand-run UPDATE in psql.
CREATE FUNCTION enforce_terminal_agreement() RETURNS trigger AS $$
DECLARE
    v_type workflow_event_type;
BEGIN
    IF NEW.status IN ('COMPLETED', 'FAILED')
       AND NEW.status IS DISTINCT FROM OLD.status THEN
        SELECT event_type INTO v_type
          FROM workflow_events WHERE run_id = NEW.id AND is_terminal;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'run % cannot become % before its terminal event is appended',
                NEW.id, NEW.status USING ERRCODE = 'check_violation';
        END IF;
        IF (NEW.status = 'COMPLETED') <> (v_type = 'result') THEN
            RAISE EXCEPTION 'run % is % but its terminal event is %',
                NEW.id, NEW.status, v_type USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER verification_runs_terminal_agreement
    AFTER UPDATE ON verification_runs
    FOR EACH ROW EXECUTE FUNCTION enforce_terminal_agreement();
-- AFTER, not BEFORE: the terminal event is appended first in the same
-- transaction, and this checks that it is there and that it matches. Both
-- statements commit together or neither does, so a client can never observe a
-- COMPLETED run with no result to fetch.
