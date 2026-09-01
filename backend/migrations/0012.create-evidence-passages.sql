-- depends: 0011.create-candidates-and-exclusions

-- The passages of record. Only CLAIMS and DESCRIPTION can be stored at all, so no
-- application bug can route an abstract, a title, bibliographic metadata or a
-- search snippet into a verifier prompt.
CREATE TABLE evidence_passages (
    run_id                 UUID NOT NULL REFERENCES verification_runs(id) ON DELETE CASCADE,
    chunk_id               TEXT NOT NULL,
    publication_number     TEXT NOT NULL,
    source_document_id     TEXT NOT NULL CHECK (source_document_id <> ''),
    source                 TEXT NOT NULL CHECK (source <> ''),
    section                TEXT NOT NULL CHECK (section IN ('CLAIMS', 'DESCRIPTION')),
    locator                TEXT NOT NULL CHECK (locator <> ''),
    locator_kind           locator_type NOT NULL,
    locator_source         TEXT NOT NULL CHECK (locator_source IN
                               ('PROVIDER_CLAIM_NUMBER', 'PROVIDER_ELEMENT_ID',
                                'PROVIDER_NUMBER_ATTR', 'DERIVED_SOURCE_OFFSET')),
    char_start             INTEGER,          -- SOURCE_RANGE only
    char_end               INTEGER,          -- SOURCE_RANGE only
    section_text_sha256    TEXT NOT NULL,    -- the EXACT provider text the offsets index into
    segmenter_version      TEXT,             -- SOURCE_RANGE only; a bump is visible, not silent
    language               TEXT NOT NULL CHECK (language <> ''),
    text_version_original  TEXT NOT NULL,
    text_version_date      DATE NOT NULL,
    text_version_src       text_version_source NOT NULL,
    retrieved_at           TIMESTAMPTZ NOT NULL,
    canonical_text         TEXT NOT NULL CHECK (canonical_text <> ''),
    translation            TEXT,
    text_sha256            TEXT NOT NULL,
    PRIMARY KEY (run_id, chunk_id),
    CHECK ((locator_kind = 'SOURCE_RANGE') =
           (char_start IS NOT NULL AND char_end IS NOT NULL AND segmenter_version IS NOT NULL)),
    CHECK (char_start IS NULL OR char_end > char_start),
    -- Retrieval time is NEVER the text version. NONE cannot be stored, because
    -- verification.Passage requires a non-empty text_version_date: a publication
    -- with no trustworthy version produces no passage and is excluded instead.
    CHECK (text_version_src <> 'NONE')
);
CREATE INDEX evidence_passages_pub_idx ON evidence_passages (run_id, publication_number);

COMMENT ON TABLE evidence_passages IS
    'The passage of record. The CHECK on section is the schema-level half of the '
    'eligible-full-text rule: an ABSTRACT, a title, bibliographic metadata or a search '
    'snippet cannot be stored as evidence at all.';
COMMENT ON COLUMN evidence_passages.canonical_text IS
    'Post-canonicalization and the passage of record: every exact_quote is a plain '
    'substring of THIS column.';
COMMENT ON COLUMN evidence_passages.section_text_sha256 IS
    'Over the PRE-canonicalization provider text, because that is what char_start and '
    'char_end index into and what makes a SOURCE_RANGE locator reproducible.';
COMMENT ON COLUMN evidence_passages.retrieved_at IS
    'When the text was fetched. NEVER substituted for a text_version_* column.';
