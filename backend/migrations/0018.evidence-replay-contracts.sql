-- depends: 0017.persist-sanitized-failure-detail

-- Replay contracts for the evidence layer, enforced in the database.
--
-- TWO DEFECTS, one shape. Recovery re-runs a node, so every evidence write is
-- replayed sooner or later. Neither write handled that:
--
--   * `record_exclusion` was a plain INSERT into a table with **no natural-key
--     constraint at all**. A replay did not raise -- it silently wrote a SECOND
--     exclusion row for the same publication. Two rows, possibly with different
--     reasons, both claiming to be why that publication was excluded.
--
--   * `record_passages` upserted, but its SET list touched only `canonical_text`,
--     `text_sha256`, `section_text_sha256` and `retrieved_at`. A replay carrying
--     genuinely different evidence would overwrite the text while leaving the
--     text-version date, the locator, the section, the language and the
--     provenance from the ORIGINAL passage. The result is a row whose text says
--     one thing and whose provenance says another -- immutable machine evidence
--     mutated in place, and a stored verdict citing it now pointing at text that
--     was never validated against it.
--
-- ONE RULE for both: an **identical** replay is a no-op, and a **different**
-- payload under the same identity RAISES `FN003` rather than overwriting. That
-- is the same contract `_terminal_replay_or_null` already applies to terminal
-- events, and for the same reason: a replay and a conflicting reuse of an
-- identity look alike from the outside, and only the stored content can tell
-- them apart.
--
-- `retrieved_at` is deliberately **excluded from the identity comparison and
-- never updated**. Two identical re-fetches legitimately differ in clock time,
-- so including it would make every replay a conflict; and the first retrieval is
-- the one that produced the stored evidence, so overwriting it would misdate the
-- provenance.
--
-- Both functions are fenced through `_lock_run_and_require_lease`, taking
-- `verification_runs` then `run_leases` in the mandatory order and holding both
-- to commit. A fenced worker replays nothing.

-- The natural key that was missing. Without it a replay wrote a duplicate row
-- rather than colliding, which is why the defect was silent.
ALTER TABLE publication_exclusions
    ADD CONSTRAINT publication_exclusions_run_publication_key
    UNIQUE (run_id, requested_number);

COMMENT ON CONSTRAINT publication_exclusions_run_publication_key
    ON publication_exclusions IS
    'One exclusion per publication per run. Its absence let a replay write a '
    'second row silently.';


-- Exclusion: replay-safe, fenced, and mutually exclusive with evidence.
CREATE FUNCTION record_publication_exclusion(
    p_run_id           UUID,
    p_owner_id         TEXT,
    p_fence_token      BIGINT,
    p_requested        TEXT,
    p_normalized       TEXT,
    p_served           TEXT,
    p_state            exclusion_state,
    p_reason           TEXT,
    p_failing_check    TEXT,
    p_language         TEXT,
    p_available        TEXT[],
    p_retrieved        TEXT[],
    p_passages_built   INTEGER,
    p_provider_detail  TEXT
) RETURNS UUID AS $$
DECLARE
    v_existing publication_exclusions%ROWTYPE;
    v_id       UUID;
BEGIN
    PERFORM _lock_run_and_require_lease(p_run_id, p_owner_id, p_fence_token);

    -- A publication is excluded OR it has evidence. Never both: an exclusion
    -- means no comparison was attempted, and citable passages attached to one
    -- would be a contradiction a reviewer could only resolve by guessing.
    IF EXISTS (
        SELECT 1 FROM evidence_passages
         WHERE run_id = p_run_id AND publication_number = p_normalized
    ) THEN
        RAISE EXCEPTION
            'run % already holds evidence passages for %; it cannot also be excluded',
            p_run_id, p_normalized USING ERRCODE = 'FN003';
    END IF;

    SELECT * INTO v_existing FROM publication_exclusions
     WHERE run_id = p_run_id AND requested_number = p_requested
     FOR UPDATE;

    IF FOUND THEN
        -- A true replay changes nothing. A conflicting one is a different
        -- statement about the same publication and must not be absorbed.
        IF v_existing.state             IS DISTINCT FROM p_state
        OR v_existing.reason            IS DISTINCT FROM p_reason
        OR v_existing.normalized_number IS DISTINCT FROM p_normalized
        OR v_existing.served_number     IS DISTINCT FROM NULLIF(p_served, '')
        OR v_existing.failing_check     IS DISTINCT FROM p_failing_check
        OR v_existing.language_attempted IS DISTINCT FROM COALESCE(p_language, '')
        OR v_existing.sections_available IS DISTINCT FROM COALESCE(p_available, '{}')
        OR v_existing.sections_retrieved IS DISTINCT FROM COALESCE(p_retrieved, '{}')
        OR v_existing.passages_built    IS DISTINCT FROM COALESCE(p_passages_built, 0)
        OR v_existing.provider_detail   IS DISTINCT FROM COALESCE(p_provider_detail, '')
        THEN
            RAISE EXCEPTION
                'exclusion for % in run % already exists with different content '
                '(stored state %, reason %); a conflicting replay is not a replay',
                p_requested, p_run_id, v_existing.state, left(v_existing.reason, 60)
                USING ERRCODE = 'FN003';
        END IF;
        RETURN v_existing.id;
    END IF;

    INSERT INTO publication_exclusions
        (run_id, requested_number, normalized_number, served_number, state, reason,
         failing_check, language_attempted, sections_available, sections_retrieved,
         passages_built, provider_detail)
    VALUES (p_run_id, p_requested, p_normalized, NULLIF(p_served, ''), p_state, p_reason,
            p_failing_check, COALESCE(p_language, ''), COALESCE(p_available, '{}'),
            COALESCE(p_retrieved, '{}'), COALESCE(p_passages_built, 0),
            COALESCE(p_provider_detail, ''))
    RETURNING id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql;


-- Passage: replay-safe, fenced, and never mutated in place.
CREATE FUNCTION record_evidence_passage(
    p_run_id                UUID,
    p_owner_id              TEXT,
    p_fence_token           BIGINT,
    p_chunk_id              TEXT,
    p_publication_number    TEXT,
    p_source_document_id    TEXT,
    p_source                TEXT,
    p_section               TEXT,
    p_locator               TEXT,
    p_locator_kind          locator_type,
    p_locator_source        TEXT,
    p_char_start            INTEGER,
    p_char_end              INTEGER,
    p_section_text_sha256   TEXT,
    p_segmenter_version     TEXT,
    p_language              TEXT,
    p_text_version_original TEXT,
    p_text_version_date     DATE,
    p_text_version_src      text_version_source,
    p_retrieved_at          TIMESTAMPTZ,
    p_canonical_text        TEXT,
    p_translation           TEXT,
    p_text_sha256           TEXT
) RETURNS BOOLEAN AS $$
DECLARE
    v_existing evidence_passages%ROWTYPE;
BEGIN
    PERFORM _lock_run_and_require_lease(p_run_id, p_owner_id, p_fence_token);

    IF EXISTS (
        SELECT 1 FROM publication_exclusions
         WHERE run_id = p_run_id AND normalized_number = p_publication_number
    ) THEN
        RAISE EXCEPTION
            'publication % is excluded in run %; an excluded publication has no '
            'evidence', p_publication_number, p_run_id USING ERRCODE = 'FN003';
    END IF;

    SELECT * INTO v_existing FROM evidence_passages
     WHERE run_id = p_run_id AND chunk_id = p_chunk_id
     FOR UPDATE;

    IF FOUND THEN
        -- EVERY content and provenance column is compared, not just the text.
        -- The old upsert compared nothing and updated four columns, which is how
        -- a row ended up with new text and old provenance.
        --
        -- `retrieved_at` is excluded: two identical re-fetches differ in clock
        -- time, and the first retrieval is the one that produced this evidence.
        IF v_existing.publication_number    IS DISTINCT FROM p_publication_number
        OR v_existing.source_document_id    IS DISTINCT FROM p_source_document_id
        OR v_existing.source                IS DISTINCT FROM p_source
        OR v_existing.section               IS DISTINCT FROM p_section
        OR v_existing.locator               IS DISTINCT FROM p_locator
        OR v_existing.locator_kind          IS DISTINCT FROM p_locator_kind
        OR v_existing.locator_source        IS DISTINCT FROM p_locator_source
        OR v_existing.char_start            IS DISTINCT FROM p_char_start
        OR v_existing.char_end              IS DISTINCT FROM p_char_end
        OR v_existing.section_text_sha256   IS DISTINCT FROM p_section_text_sha256
        OR v_existing.segmenter_version     IS DISTINCT FROM p_segmenter_version
        OR v_existing.language              IS DISTINCT FROM p_language
        OR v_existing.text_version_original IS DISTINCT FROM p_text_version_original
        OR v_existing.text_version_date     IS DISTINCT FROM p_text_version_date
        OR v_existing.text_version_src      IS DISTINCT FROM p_text_version_src
        OR v_existing.canonical_text        IS DISTINCT FROM p_canonical_text
        OR v_existing.translation           IS DISTINCT FROM p_translation
        OR v_existing.text_sha256           IS DISTINCT FROM p_text_sha256
        THEN
            RAISE EXCEPTION
                'passage % in run % already exists with different content or '
                'provenance; a validated passage is immutable, so this is a '
                'version conflict rather than an update',
                p_chunk_id, p_run_id USING ERRCODE = 'FN003';
        END IF;
        RETURN FALSE;   -- a true replay: nothing written, nothing changed
    END IF;

    INSERT INTO evidence_passages
        (run_id, chunk_id, publication_number, source_document_id, source, section,
         locator, locator_kind, locator_source, char_start, char_end,
         section_text_sha256, segmenter_version, language, text_version_original,
         text_version_date, text_version_src, retrieved_at, canonical_text,
         translation, text_sha256)
    VALUES (p_run_id, p_chunk_id, p_publication_number, p_source_document_id, p_source,
            p_section, p_locator, p_locator_kind, p_locator_source, p_char_start,
            p_char_end, p_section_text_sha256, p_segmenter_version, p_language,
            p_text_version_original, p_text_version_date, p_text_version_src,
            p_retrieved_at, p_canonical_text, p_translation, p_text_sha256);
    RETURN TRUE;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION record_publication_exclusion IS
    'Fenced, replay-safe. Identical replay returns the existing id; a conflicting '
    'one raises FN003. Refuses a publication that already has evidence passages.';
COMMENT ON FUNCTION record_evidence_passage IS
    'Fenced, replay-safe. Identical replay is a no-op; different content or '
    'provenance under the same chunk_id raises FN003 rather than mutating '
    'immutable evidence. Refuses a publication that is already excluded.';
