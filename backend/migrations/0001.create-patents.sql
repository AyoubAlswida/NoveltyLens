-- depends:

-- Cache of PatentRecord (backend/models.py). Keyed by the same normalized ID
-- search/search.py's merge_patents() dedupes on (patent.id.strip().upper()),
-- so a cache lookup and a live dedup can never disagree about identity.
--
-- serpapi_id/full_text mirror the two fields Phase 3 added to PatentRecord
-- for Google Patents full-text enrichment (backend/search/google.py's
-- enrich_with_full_text). Storing them means a cached patent is exactly as
-- useful to compare.py's embedding-based ranking as a freshly-fetched one --
-- without them, a cache hit would silently downgrade ranking quality by
-- reverting to title+abstract for patents that had already been enriched.
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
    'Cache of PatentRecord results. fetched_at backs a cache TTL (see backend/cache.py). '
    'full_text/serpapi_id mirror Phase 3''s Google Patents full-text enrichment fields.';
COMMENT ON COLUMN patents.id IS
    'Must equal patent.id.strip().upper() exactly, matching search/search.py merge_patents().';
