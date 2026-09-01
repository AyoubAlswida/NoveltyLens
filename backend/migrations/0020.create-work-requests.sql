-- depends: 0019.fix-requirement-id-trigger

-- Two kinds of human request the API records and the WORKER carries out.
--
-- Spec §10 gives both endpoints a `202`, and the status code is the design: the
-- API accepts the request and does none of the work. That is the same boundary
-- every gate approval already respects -- no endpoint fetches a publication, no
-- endpoint calls the graph, and no endpoint can make something eligible. It
-- records what a person asked for; the executor, under a lease, does it.
--
-- Both tables are ownership-scoped through their run and cascade with it.

-- --------------------------------------------------------------------------
-- Manually added candidate publications (POST /api/runs/{id}/candidates)
-- --------------------------------------------------------------------------
--
-- Spec case T: a publication added by hand at Workflow Gate B "passes
-- normalization, date screening, enrichment, and full-text eligibility
-- IDENTICALLY to a discovered candidate. If it fails any, it is recorded with
-- that state and cannot be approved into the set."
--
-- So this table holds a REQUEST, never a result. There is deliberately no
-- `eligible` column and no way to write one: eligibility is derived from
-- passages actually built (§3.7), by the same nodes that derive it for a
-- discovered candidate. A manual candidate that fails screening ends up in
-- `publication_exclusions` exactly like any other.
CREATE TABLE manual_candidates (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id              UUID NOT NULL REFERENCES verification_runs(id) ON DELETE CASCADE,
    requested_number    TEXT NOT NULL,
    requested_by        TEXT NOT NULL REFERENCES users(email),
    requested_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at        TIMESTAMPTZ,
    UNIQUE (run_id, requested_number)
);

COMMENT ON TABLE manual_candidates IS
    'A REQUEST to put a publication through the evidence pipeline, not a candidate '
    'and not an approval. It carries no eligibility column on purpose: eligibility '
    'is derived from passages built by the same nodes a discovered candidate goes '
    'through (spec case T, §3.7). Approving one into the candidate set is still '
    'workflow/approvals.py''s job and still needs a human.';

COMMENT ON COLUMN manual_candidates.requested_number IS
    'As typed. Normalization happens in the pipeline, where an unparseable number '
    'RAISES rather than being guessed at -- a number quietly corrected into a '
    'different real document would attach that document''s text to this candidate.';

CREATE INDEX manual_candidates_pending_idx
    ON manual_candidates (run_id) WHERE processed_at IS NULL;

-- --------------------------------------------------------------------------
-- Cell rerun requests (POST /api/runs/{id}/cells/{cell_id}/rerun)
-- --------------------------------------------------------------------------
--
-- Spec case U: "Only the named surrounding material is fetched; only that cell
-- re-runs; every other cell's result is byte-identical; new result links to the
-- prior one via `source_result_version`."
--
-- `source_result_version` is the version the reviewer was LOOKING AT when they
-- asked. It is recorded rather than derived at execution time so a rerun cannot
-- silently attach to a result that superseded the one under review while the
-- request sat in the queue.
CREATE TABLE cell_rerun_requests (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id                 UUID NOT NULL REFERENCES verification_runs(id) ON DELETE CASCADE,
    cell_id                TEXT NOT NULL,
    source_result_version  INTEGER NOT NULL,
    context_requests       JSONB NOT NULL DEFAULT '[]'::jsonb,
    requested_by           TEXT NOT NULL REFERENCES users(email),
    requested_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at           TIMESTAMPTZ,
    CHECK (jsonb_typeof(context_requests) = 'array'),
    CHECK (source_result_version >= 1),
    UNIQUE (run_id, cell_id, source_result_version)
);

COMMENT ON TABLE cell_rerun_requests IS
    'A REQUEST to re-run ONE cell with named additional context. It never edits, '
    'replaces or deletes the existing machine result: a rerun produces a NEW '
    'result_version linked to the one it came from, and the prior version stays '
    'immutable and retrievable (spec §13.1, case U).';

COMMENT ON COLUMN cell_rerun_requests.context_requests IS
    'The model''s own requests_for_more_context entries the reviewer chose to '
    'fulfil. ONLY the named material is fetched -- a rerun is not a licence to '
    'widen the passage set.';

CREATE INDEX cell_rerun_requests_pending_idx
    ON cell_rerun_requests (run_id) WHERE processed_at IS NULL;
