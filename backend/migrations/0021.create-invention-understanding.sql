-- depends: 0020.create-work-requests

-- ---------------------------------------------------------------------------
-- The inventor's own submission (Stage 1)
-- ---------------------------------------------------------------------------
--
-- Additive and nullable, so every run created before this migration stays
-- readable and is NOT retroactively reinterpreted. A NULL here means "this run
-- predates the field", which is a different fact from an empty string meaning
-- "the inventor left it blank", and the two must stay distinguishable.
--
-- `description` already exists on this table and remains THE source input. It is
-- never rewritten by a proposal, an edit or an approval: the approved summary is
-- stored separately, below, precisely so the original survives untouched.
ALTER TABLE verification_runs ADD COLUMN problem_solved            TEXT;
ALTER TABLE verification_runs ADD COLUMN inventor_novelty_statement TEXT;

COMMENT ON COLUMN verification_runs.problem_solved IS
    'Optional Stage 1 input, as typed. NULL means the run predates the column; '
    'empty string means the inventor left it blank.';
COMMENT ON COLUMN verification_runs.inventor_novelty_statement IS
    'Optional Stage 1 input: what the inventor believes is new. As typed, never '
    'rewritten, and never used as evidence -- it is context for the proposal.';

-- ---------------------------------------------------------------------------
-- Machine proposals and human edits (NOT approvals)
-- ---------------------------------------------------------------------------
--
-- **There is deliberately no is_approved column here, and no approver columns.**
-- feature_decompositions is the approved artifact and its CHECK makes an
-- unapproved row impossible; this table is the opposite kind of thing, and the
-- absence of those columns is what makes the two structurally impossible to
-- confuse. A reader cannot mistake a row here for an approval because there is
-- no field in which an approval could be recorded.
--
-- Both an LLM proposal and a human's edit of it are stored, as separate rows
-- with the same version, because "what the machine said" and "what the human
-- changed it to" are the two halves of the audit question and collapsing them
-- would lose the one that shows the human did anything.
CREATE TABLE understanding_proposals (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id        UUID NOT NULL REFERENCES verification_runs(id) ON DELETE CASCADE,
    version       INTEGER NOT NULL CHECK (version >= 1),
    source        TEXT NOT NULL CHECK (source IN ('LLM', 'USER_EDIT')),
    summary       TEXT NOT NULL,
    features      JSONB NOT NULL,
    feedback      TEXT,
    created_by    TEXT NOT NULL REFERENCES users(email),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (jsonb_typeof(features) = 'array'),
    -- One machine proposal and one human edit per version. A second regeneration
    -- is a NEW version, never an overwrite of this one.
    UNIQUE (run_id, version, source)
);

COMMENT ON TABLE understanding_proposals IS
    'MACHINE OUTPUT and HUMAN EDITS, never approvals. No is_approved column and no '
    'approver columns exist here on purpose: feature_decompositions is the approved '
    'artifact, and a row in this table cannot be mistaken for one because there is '
    'no field in which an approval could be recorded.';
COMMENT ON COLUMN understanding_proposals.feedback IS
    'What the inventor said the system misunderstood, when this version came from a '
    'regeneration request. NULL on a first proposal.';
COMMENT ON COLUMN understanding_proposals.features IS
    'The spec section 2 wire shape, verbatim: feature_id, feature_version, '
    'feature_text, elements[], relationships[]. Stored whole because a proposal is '
    'reviewed as a whole.';

CREATE INDEX understanding_proposals_run_idx
    ON understanding_proposals (run_id, version DESC);

-- ---------------------------------------------------------------------------
-- The APPROVED technical summary (Stage 2)
-- ---------------------------------------------------------------------------
--
-- Versioned in step with feature_decompositions: one human act approves the
-- summary and the decomposition together, at one version, so there is never a
-- version of one that no version of the other was approved beside.
--
-- Same approval CHECK as feature_decompositions, and the same single writer
-- (workflow/approvals.py). A summary that names no approver is not an approval.
CREATE TABLE approved_understandings (
    run_id        UUID NOT NULL REFERENCES verification_runs(id) ON DELETE CASCADE,
    version       INTEGER NOT NULL CHECK (version >= 1),
    summary       TEXT NOT NULL CHECK (summary <> ''),
    is_approved   BOOLEAN NOT NULL DEFAULT FALSE,
    approved_by   TEXT REFERENCES users(email),
    approved_at   TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, version),
    CHECK (is_approved = (approved_by IS NOT NULL AND approved_at IS NOT NULL))
);

COMMENT ON TABLE approved_understandings IS
    'HUMAN-APPROVED technical summaries only, versioned in step with '
    'feature_decompositions. Written only by workflow/approvals.py, inside the same '
    'transaction as the decomposition it was approved beside.';

-- ---------------------------------------------------------------------------
-- Regeneration requests (POST /api/runs/{id}/understanding/regenerate)
-- ---------------------------------------------------------------------------
--
-- A REQUEST to re-analyse the invention, not a re-analysis and not an approval.
-- The API records the row and returns 202; the **worker** carries it out under
-- the run's lease, inside the graph, calling the same node that made the first
-- proposal. Generating inside the HTTP handler would put an LLM call on a
-- request thread with no lease, no fencing and no recovery -- the same reason
-- manual candidates and cell reruns are requests rather than work.
--
-- Claimed by stamping `processed_at` under the lease, so the at-least-once
-- execution the lease provides cannot regenerate twice for one request. Claimed
-- rows are KEPT: the row is the evidence that a person asked and the worker
-- acted, and the audit appendix needs it.
CREATE TABLE understanding_regeneration_requests (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id         UUID NOT NULL REFERENCES verification_runs(id) ON DELETE CASCADE,
    from_version   INTEGER NOT NULL CHECK (from_version >= 1),
    feedback       TEXT NOT NULL DEFAULT '',
    requested_by   TEXT NOT NULL REFERENCES users(email),
    requested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at   TIMESTAMPTZ,
    -- One regeneration per version. Asking twice about the same version is the
    -- same request; asking about the version that regeneration produced is a
    -- different one, and takes its own row.
    UNIQUE (run_id, from_version)
);

COMMENT ON TABLE understanding_regeneration_requests IS
    'A REQUEST to re-analyse the invention. Carried out by the worker under the '
    'run''s lease, never in the HTTP handler. A regeneration produces a NEW '
    'understanding version and never edits the version it was asked to improve on.';

COMMENT ON COLUMN understanding_regeneration_requests.feedback IS
    'What the inventor said the system misunderstood. Optional -- an empty string '
    'is a bare "try again", which is a real request and not a missing one.';

CREATE INDEX understanding_regeneration_pending_idx
    ON understanding_regeneration_requests (run_id) WHERE processed_at IS NULL;
