-- depends:

-- Every patent that has ever appeared in a stored report. Keyed by the same
-- normalized ID search/search.py's merge_patents() dedupes on
-- (patent.id.strip().upper()), so a stored row and a live dedup can never
-- disagree about identity.
--
-- This table exists so search_run_matches can reference a patent by ID and
-- still render a title months later, without re-hitting a provider. It is
-- deliberately NOT a search cache: nothing reads it to skip a live call. The
-- query_cache table that used to do that was not restored -- see CLAUDE.md.
--
-- serpapi_id/full_text mirror the two fields PatentRecord carries for Google
-- Patents full-text enrichment (backend/search/google.py's
-- enrich_with_full_text), so a stored row is a faithful record of what was
-- actually compared rather than a lossy summary of it.
CREATE TABLE patents (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    abstract TEXT NOT NULL DEFAULT '',
    inventors TEXT[] NOT NULL DEFAULT '{}',
    assignee TEXT NOT NULL DEFAULT '',
    publication_date TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    serpapi_id TEXT NOT NULL DEFAULT '',
    full_text TEXT NOT NULL DEFAULT '',
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE patents IS
    'Every patent referenced by a stored report. Not a search cache -- nothing reads '
    'it to skip a provider call.';
COMMENT ON COLUMN patents.id IS
    'Must equal patent.id.strip().upper() exactly, matching search/search.py merge_patents().';
