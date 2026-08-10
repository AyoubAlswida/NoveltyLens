-- depends: 0001.create-patents

-- One row per completed pipeline run (backend/graph/pipeline.py). Carries every
-- field the "result" SSE event's data object needs other than the per-patent
-- rows, which live in search_run_matches. "title" and "date" in the SSE payload
-- are cheap derived/placeholder values ("Today" is literally hardcoded today)
-- rather than real state, so this table stores the description/claims that
-- produced the run and lets the History screen re-derive a title and a real
-- timestamp from created_at instead.
--
-- query_count has no CHECK: the pipeline can legitimately finish a run that
-- found nothing, and pinning a lower bound here would turn an honest empty
-- report into a failed insert.
CREATE TABLE search_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    description TEXT NOT NULL,
    claims TEXT,
    concepts TEXT[] NOT NULL DEFAULT '{}',
    query_count INTEGER NOT NULL CHECK (query_count >= 0),
    top_score INTEGER NOT NULL CHECK (top_score BETWEEN 0 AND 100),
    verdict TEXT NOT NULL CHECK (verdict IN ('Strong', 'Moderate', 'Weak')),
    recommendations TEXT[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX search_runs_created_at_idx ON search_runs (created_at DESC);

COMMENT ON TABLE search_runs IS
    'One row per completed analyze pipeline run, enough to rebuild the "result" '
    'SSE event''s concepts/query_count/top_score/verdict/recommendations fields.';
