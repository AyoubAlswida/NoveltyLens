-- depends: 0014.create-human-overlays

-- Report drafts and their approvals (Workflow Gate D).
--
-- A draft is not a report. Only a row with all three approval columns set may be
-- presented, exported, or described as the final report; editing anything creates
-- a new version requiring fresh approval, and an approved version is immutable.
CREATE TABLE report_versions (
    run_id       UUID NOT NULL REFERENCES verification_runs(id) ON DELETE CASCADE,
    version      INTEGER NOT NULL CHECK (version >= 1),
    draft        JSONB NOT NULL,
    comments     TEXT NOT NULL DEFAULT '',
    caveats      TEXT NOT NULL DEFAULT '',
    is_approved  BOOLEAN NOT NULL DEFAULT FALSE,
    approved_by  TEXT REFERENCES users(email),
    approved_at  TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, version),
    CHECK (is_approved = (approved_by IS NOT NULL AND approved_at IS NOT NULL))
);

COMMENT ON TABLE report_versions IS
    'Draft and approved report versions. A draft carries DRAFT -- NOT APPROVED until an '
    'approval transaction stamps approved_by and approved_at.';
