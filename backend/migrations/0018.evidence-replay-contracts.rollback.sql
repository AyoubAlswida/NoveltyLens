DROP FUNCTION record_evidence_passage(UUID, TEXT, BIGINT, TEXT, TEXT, TEXT, TEXT, TEXT,
    TEXT, locator_type, TEXT, INTEGER, INTEGER, TEXT, TEXT, TEXT, TEXT, DATE,
    text_version_source, TIMESTAMPTZ, TEXT, TEXT, TEXT);
DROP FUNCTION record_publication_exclusion(UUID, TEXT, BIGINT, TEXT, TEXT, TEXT,
    exclusion_state, TEXT, TEXT, TEXT, TEXT[], TEXT[], INTEGER, TEXT);
ALTER TABLE publication_exclusions
    DROP CONSTRAINT publication_exclusions_run_publication_key;
