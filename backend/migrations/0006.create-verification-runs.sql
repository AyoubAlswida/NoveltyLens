-- depends: 0005.add-match-key-overlap

-- The durable-workflow vocabulary and the run row itself.
--
-- A verification_run is a REVIEWED workflow execution: it pauses at human gates,
-- survives the HTTP request that created it, and is advanced by a worker process
-- rather than by a socket. It is NOT the search-only run that search_runs
-- records -- those two are deliberately separate, and POST /api/analyze keeps
-- writing search_runs with no run row, no lease and no event log.
--
-- PostgreSQL is 17.10 here, so UNIQUE NULLS NOT DISTINCT and partial unique
-- indexes are both available and are used deliberately in later migrations.

CREATE TYPE workflow_gate AS ENUM ('A', 'B', 'C', 'D');
CREATE TYPE run_status AS ENUM
    ('QUEUED', 'RUNNING', 'WAITING_FOR_REVIEW', 'RESUMING', 'COMPLETED', 'FAILED');
CREATE TYPE cell_state AS ENUM ('VERIFIED', 'NOT_EVALUATED', 'VALIDATION_FAILURE');
CREATE TYPE exclusion_state AS ENUM
    ('EXCLUDED_DATE', 'EXCLUDED_NO_FULL_TEXT', 'EXCLUDED_INELIGIBLE');
CREATE TYPE review_status AS ENUM
    ('PENDING', 'APPROVED', 'REJECTED', 'OVERRIDDEN', 'NEEDS_EXPERT_REVIEW');
CREATE TYPE date_certainty AS ENUM ('CERTAIN', 'UNCERTAIN');
CREATE TYPE locator_type AS ENUM ('CLAIM_NUMBER', 'EPO_PARAGRAPH', 'SOURCE_RANGE');
CREATE TYPE text_version_source AS ENUM
    ('PROVIDER_PUBLICATION_DATE', 'PROVIDER_REVISION_ID', 'NONE');

CREATE TABLE verification_runs (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    description             TEXT NOT NULL,
    claims                  TEXT,
    cutoff_date             DATE,
    date_policy             TEXT NOT NULL,   -- the approved DATE_SCREENING_POLICY, frozen per run
    text_version_policy     TEXT NOT NULL,   -- the approved TEXT_VERSION_POLICY, frozen per run
    status                  run_status NOT NULL DEFAULT 'QUEUED',
    current_gate            workflow_gate,
    thread_id               TEXT NOT NULL UNIQUE,   -- LangGraph thread; = id::text
    created_by              TEXT NOT NULL REFERENCES users(email),
    next_sequence           BIGINT NOT NULL DEFAULT 0,  -- event-log allocator
    events_compacted_below  BIGINT NOT NULL DEFAULT 0,  -- replay watermark
    failure_code            TEXT,
    failure_detail          TEXT,            -- sanitized; never a traceback
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (status <> 'WAITING_FOR_REVIEW' OR current_gate IS NOT NULL),
    CHECK (status <> 'FAILED' OR failure_code IS NOT NULL)
);
CREATE INDEX verification_runs_owner_idx ON verification_runs (created_by, created_at DESC);
CREATE INDEX verification_runs_claimable_idx ON verification_runs (status, updated_at)
    WHERE status IN ('QUEUED', 'RUNNING', 'RESUMING');

COMMENT ON TABLE verification_runs IS
    'One reviewed workflow execution. NOT a search-only run -- search_runs still records '
    'those, with no lease and no event log. created_by is the ownership key: every '
    'run-scoped query filters on it and another user''s run answers 404, never 403.';
COMMENT ON COLUMN verification_runs.date_policy IS
    'FROZEN at admission, so changing a setting cannot retroactively reinterpret a '
    'finished run''s date screening.';
COMMENT ON COLUMN verification_runs.text_version_policy IS
    'FROZEN at admission, for the same reason as date_policy.';
COMMENT ON COLUMN verification_runs.next_sequence IS
    'The event-log sequence allocator. Incremented inside the same transaction as the '
    'event insert, under a FOR UPDATE lock on this row.';
COMMENT ON COLUMN verification_runs.events_compacted_below IS
    'Replay watermark. A client reconnecting below it cannot be given a correct replay '
    'and is sent a snapshot instead -- which is why this is a column and not an implicit '
    'consequence of deletion.';
COMMENT ON COLUMN verification_runs.failure_detail IS
    'Sanitized. Never a traceback, never a provider body, never a URL carrying a key.';

-- The run state machine, as data rather than as prose in a docstring.
--
-- This table describes UPDATES ONLY. Creation is not a transition and is not
-- represented here: a primary key column is implicitly NOT NULL in PostgreSQL, so
-- a (NULL, 'QUEUED') creation row cannot exist, and inventing a synthetic
-- 'NEW'/'CREATING' state purely to satisfy the constraint would put a status in
-- the enum that nothing ever observes at runtime. Creation is an INSERT INVARIANT
-- instead, enforced by the trigger below.
CREATE TABLE run_status_transitions (
    from_status  run_status NOT NULL,
    to_status    run_status NOT NULL,
    PRIMARY KEY (from_status, to_status),
    CHECK (from_status <> to_status)
);
INSERT INTO run_status_transitions (from_status, to_status) VALUES
    ('QUEUED',             'RUNNING'),
    -- QUEUED -> FAILED is for failures discovered AFTER admission only: an
    -- executor that cannot start, or an adapter revision that changed between
    -- admission and execution. Policy validation never reaches this edge -- it
    -- refuses the request before a run row exists at all.
    ('QUEUED',             'FAILED'),
    ('RUNNING',            'WAITING_FOR_REVIEW'),
    ('RUNNING',            'COMPLETED'),
    ('RUNNING',            'FAILED'),
    ('WAITING_FOR_REVIEW', 'RESUMING'),
    ('WAITING_FOR_REVIEW', 'FAILED'),
    ('RESUMING',           'RUNNING'),
    ('RESUMING',           'FAILED');
-- COMPLETED and FAILED appear only as targets: they are terminal, with no
-- outgoing rows. Lease takeover does NOT appear here because it does not change
-- status -- status tracks workflow position, the lease tracks who may advance it.

COMMENT ON TABLE run_status_transitions IS
    'Permitted status UPDATES. Not a creation table: a run is born QUEUED by insert '
    'invariant, and COMPLETED/FAILED have no outgoing rows at all.';

-- Creation invariant: a run is born QUEUED and nothing else. Admission has
-- already validated every policy by the time this INSERT runs, so there is no
-- legitimate reason to insert a run in any other state -- and a run inserted
-- directly as RUNNING or COMPLETED would bypass the lease and the event log.
CREATE FUNCTION enforce_run_status_rules() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'QUEUED' THEN
            RAISE EXCEPTION 'a run must be inserted as QUEUED, not %', NEW.status
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF NOT EXISTS (SELECT 1 FROM run_status_transitions
                        WHERE from_status = OLD.status AND to_status = NEW.status) THEN
            RAISE EXCEPTION 'illegal run status transition % -> %', OLD.status, NEW.status
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER verification_runs_status_rules
    BEFORE INSERT OR UPDATE ON verification_runs
    FOR EACH ROW EXECUTE FUNCTION enforce_run_status_rules();
-- One trigger, two rules: born QUEUED on INSERT, and only a listed pair on
-- UPDATE. Because COMPLETED and FAILED have no outgoing rows, any UPDATE that
-- changes their status raises -- which is what makes "terminal" a database
-- property rather than a convention.
