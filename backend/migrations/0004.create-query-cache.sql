-- depends: 0001.create-patents

-- Caches which patents a given (already-.strip()ped) query string returned,
-- so an exact repeat of a generated search query can skip Google
-- Patents/EPO entirely and read from Postgres instead. Added in P2.2
-- alongside the read/write wiring, not in P2.1: search providers have no
-- ID-only lookup, only search-by-query, so a cache keyed purely by patent
-- ID (the `patents` table) cannot by itself avoid a live provider call.
--
-- Patent bibliographic data barely changes once published, but which
-- patents match a QUERY does change as new patents are published and
-- indexed -- so this cache uses its own, shorter TTL than the patents
-- table itself (see backend/cache.py's QUERY_TTL vs PATENT_TTL).
--
-- No foreign key on patent_ids: Postgres cannot FK into an array column.
-- backend/cache.py enforces that invariant at read time instead, and
-- raises loudly rather than silently degrading if it's ever violated.
CREATE TABLE query_cache (
    query TEXT PRIMARY KEY,
    patent_ids TEXT[] NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE query_cache IS
    'Maps a generated search query to the patent IDs it returned last time. '
    'See backend/cache.py for the TTL and the read-then-fallback-to-live logic.';
