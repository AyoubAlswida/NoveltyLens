-- depends: 0013.create-verification-cells

-- Human decisions, stored BESIDE the machine result and never over it.
CREATE TABLE cell_reviews (
    cell_id                TEXT NOT NULL,
    result_version         INTEGER NOT NULL,
    run_id                 UUID NOT NULL REFERENCES verification_runs(id) ON DELETE CASCADE,
    status                 review_status NOT NULL,
    overlay                JSONB NOT NULL,     -- the review object
    reviewed_outcome       TEXT NOT NULL,      -- derived, never accepted from input
    reviewer_id            TEXT NOT NULL REFERENCES users(email),
    reviewed_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_cell_id         TEXT NOT NULL,
    source_result_version  INTEGER NOT NULL,
    approved_via_bulk      BOOLEAN NOT NULL DEFAULT FALSE,
    idempotency_key        TEXT NOT NULL,      -- PROVENANCE only; the mechanism is the transaction
    payload_sha256         TEXT NOT NULL,      -- PROVENANCE only
    PRIMARY KEY (cell_id, result_version),
    FOREIGN KEY (cell_id, result_version)
        REFERENCES verification_results (cell_id, result_version) ON DELETE CASCADE,
    -- The binding, in the schema as well as in the review checks.
    CHECK (source_cell_id = cell_id),
    CHECK (source_result_version = result_version)
);

COMMENT ON TABLE cell_reviews IS
    'The human overlay. idempotency_key and payload_sha256 record WHICH REQUEST '
    'produced this row -- they are not the idempotency mechanism and must not be used '
    'as one: a unique index cannot make a multi-statement mutation replay-safe and '
    'cannot return the original response body.';

CREATE TABLE cell_waivers (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cell_id                TEXT NOT NULL,
    result_version         INTEGER NOT NULL,
    run_id                 UUID NOT NULL,
    feature_id             TEXT NOT NULL,
    feature_version        TEXT NOT NULL,
    publication_number     TEXT NOT NULL,
    requirement_id         TEXT,               -- NULL = the whole cell
    reviewer_id            TEXT NOT NULL REFERENCES users(email),
    reason                 TEXT NOT NULL CHECK (reason <> ''),
    unresolved_limitation  TEXT NOT NULL CHECK (unresolved_limitation <> ''),
    idempotency_key        TEXT NOT NULL,      -- PROVENANCE only
    payload_sha256         TEXT NOT NULL,      -- PROVENANCE only
    at                     TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- (1) The waiver binds to the EXACT verification result, not merely to a cell.
    FOREIGN KEY (cell_id, result_version)
        REFERENCES verification_results (cell_id, result_version) ON DELETE CASCADE,

    -- (2) And the run, feature, feature version and publication it claims are the
    --     cell's own. Declarative, so a mismatched waiver cannot be inserted by any
    --     code path.
    FOREIGN KEY (cell_id, run_id, feature_id, feature_version, publication_number)
        REFERENCES verification_cells (cell_id, run_id, feature_id, feature_version,
                                       publication_number),

    -- (3) One waiver per (cell, result version, requirement). NULLS NOT DISTINCT
    --     so a second whole-cell waiver (requirement_id IS NULL) is rejected too.
    UNIQUE NULLS NOT DISTINCT (cell_id, result_version, requirement_id)
);
CREATE INDEX cell_waivers_run_idx ON cell_waivers (run_id);

COMMENT ON TABLE cell_waivers IS
    'A waiver changes NOTHING about its cell. There is deliberately no column here '
    'that could alter state, machine_result or outcome, and no trigger writes back to '
    'those tables. A waived VALIDATION_FAILURE is still a VALIDATION_FAILURE.';

CREATE TABLE review_audit (
    audit_id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id                 UUID NOT NULL REFERENCES verification_runs(id) ON DELETE CASCADE,
    cell_id                TEXT,
    workflow_stage         TEXT NOT NULL,
    reviewer_id            TEXT NOT NULL REFERENCES users(email),
    at                     TIMESTAMPTZ NOT NULL DEFAULT now(),
    requirement_type       TEXT CHECK (requirement_type IN ('ELEMENT', 'RELATIONSHIP')),
    requirement_id         TEXT,
    field                  TEXT NOT NULL,
    previous_value         TEXT NOT NULL,
    new_value              TEXT NOT NULL,
    reason                 TEXT NOT NULL,
    source_result_version  INTEGER,
    covered_cell_ids       TEXT[] NOT NULL DEFAULT '{}'   -- bulk approvals
);
CREATE INDEX review_audit_run_idx ON review_audit (run_id, at DESC);

COMMENT ON TABLE review_audit IS
    'APPEND ONLY. reviewer_id is the AUTHENTICATED human, never the worker''s internal '
    'identity. With one shared seeded account the trail is real but attributes every '
    'decision to the same person -- that limitation is disclosed, not solved.';

-- Append-only, enforced.
--
-- UPDATE is refused outright. DELETE is refused for any delete the APPLICATION
-- could issue, but must NOT stand in the way of the run's ON DELETE CASCADE: a
-- row-level BEFORE DELETE trigger fires for cascaded rows too, and an
-- unconditional guard here would make deleting a run permanently impossible --
-- the same defect that removed the equivalent guard from run_leases.
--
-- The discriminator is exact rather than heuristic. A foreign-key cascade deletes
-- the parent row FIRST and then issues the child delete, so by the time this
-- trigger runs the parent verification_runs row is already gone from this
-- transaction's view. A direct `DELETE FROM review_audit` still sees it. So:
-- allow the delete exactly when the parent run no longer exists, which is another
-- way of saying "an audit row is removed only with its run".
CREATE FUNCTION enforce_review_audit_append_only() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'review_audit is append-only; audit row % cannot be updated',
            OLD.audit_id USING ERRCODE = 'check_violation';
    END IF;

    IF EXISTS (SELECT 1 FROM verification_runs WHERE id = OLD.run_id) THEN
        RAISE EXCEPTION 'review_audit is append-only; audit row % is removed only with '
            'its run', OLD.audit_id USING ERRCODE = 'check_violation';
    END IF;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER review_audit_append_only
    BEFORE UPDATE OR DELETE ON review_audit
    FOR EACH ROW EXECUTE FUNCTION enforce_review_audit_append_only();
