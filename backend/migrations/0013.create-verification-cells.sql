-- depends: 0012.create-evidence-passages

-- The matrix cells and their immutable machine results.
CREATE TABLE verification_cells (
    cell_id                TEXT PRIMARY KEY,   -- "{run_id}:{feature_id}:{publication_number}"
    run_id                 UUID NOT NULL REFERENCES verification_runs(id) ON DELETE CASCADE,
    feature_id             TEXT NOT NULL,
    feature_version        TEXT NOT NULL,
    publication_number     TEXT NOT NULL,
    decomposition_version  INTEGER NOT NULL,
    candidate_set_version  INTEGER NOT NULL,
    stale                  BOOLEAN NOT NULL DEFAULT FALSE,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, feature_id, publication_number, decomposition_version, candidate_set_version),
    -- The target of cell_waivers' identity FK. Every column a waiver claims about
    -- its cell is checked by the database rather than by a hopeful application if.
    UNIQUE (cell_id, run_id, feature_id, feature_version, publication_number)
);

COMMENT ON TABLE verification_cells IS
    'One approved feature x one approved publication. An EXCLUDED publication has no '
    'row here at all -- not an empty one, not a greyed one.';

CREATE TABLE verification_results (
    cell_id               TEXT NOT NULL REFERENCES verification_cells(cell_id) ON DELETE CASCADE,
    result_version        INTEGER NOT NULL CHECK (result_version >= 1),
    state                 cell_state NOT NULL,
    machine_result        JSONB,          -- verbatim, immutable; NULL unless VERIFIED
    outcome               TEXT,
    failed_check          TEXT,
    failure_reason        TEXT,
    raw_response          TEXT NOT NULL DEFAULT '',
    not_evaluated_reason  TEXT,
    model                 TEXT NOT NULL,
    model_revision        TEXT NOT NULL DEFAULT '',
    prompt_version        TEXT NOT NULL,
    passage_chunk_ids     TEXT[] NOT NULL DEFAULT '{}',
    passage_text_hashes   TEXT[] NOT NULL DEFAULT '{}',
    retrieval_scores      JSONB NOT NULL DEFAULT '{}'::jsonb,
    attempts              INTEGER NOT NULL DEFAULT 0,
    source_result_version INTEGER,        -- the version this re-run came from
    verified_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (cell_id, result_version),
    CHECK ((state = 'VERIFIED') = (machine_result IS NOT NULL)),
    CHECK ((state = 'VALIDATION_FAILURE') = (failed_check IS NOT NULL)),
    CHECK ((state = 'NOT_EVALUATED') = (not_evaluated_reason IS NOT NULL))
);

COMMENT ON TABLE verification_results IS
    'A rejected response is a diagnostic artifact and can never become the cell''s '
    'machine result. retrieval_scores lives HERE and never in a prompt. '
    'passage_text_hashes is what makes a changed source text detectable.';
