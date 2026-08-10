-- depends: 0002.create-search-runs

-- One row per entry in the "result" SSE event's patents[] array
-- (backend/graph/ranking.py's format_matches), ordered by rank (0 = highest
-- pct). title/num come from the patents table via patent_id; pct/risk/rationale
-- are per-run, not per-patent, since they depend on the invention text compared
-- against (backend/compare.py, backend/llm.py analyze_all_matches), so they
-- cannot live on the patents row.
CREATE TABLE search_run_matches (
    run_id UUID NOT NULL REFERENCES search_runs(id) ON DELETE CASCADE,
    rank INTEGER NOT NULL CHECK (rank >= 0),
    patent_id TEXT NOT NULL REFERENCES patents(id),
    pct INTEGER NOT NULL CHECK (pct BETWEEN 0 AND 100),
    risk TEXT NOT NULL CHECK (risk IN ('High', 'Medium', 'Low')),
    rationale TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, rank)
);

CREATE INDEX search_run_matches_patent_id_idx ON search_run_matches (patent_id);

COMMENT ON TABLE search_run_matches IS
    'Per-run patent matches; joins search_runs to patents to reconstruct patents[] '
    'in the "result" SSE payload, in original rank order.';
COMMENT ON COLUMN search_run_matches.source IS
    'Snapshot of patent.source (or "live") at analysis time, kept separate from '
    'patents.source so a later refresh cannot silently rewrite a past report.';
