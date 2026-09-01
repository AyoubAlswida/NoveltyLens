-- depends: 0009.create-idempotency-records

-- The APPROVED feature decomposition (Workflow Gate A).
--
-- The CANDIDATE decomposition an LLM produced is NOT stored here. It lives in the
-- checkpointed graph state and in the Gate A payload. Only an approval
-- transaction (workflow/approvals.py, built in a later gate) inserts a row here,
-- and only with all three approval columns set. There is no other writer, no
-- default, and no auto-approval.
CREATE TABLE feature_decompositions (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id            UUID NOT NULL REFERENCES verification_runs(id) ON DELETE CASCADE,
    version           INTEGER NOT NULL CHECK (version >= 1),
    feature_id        TEXT NOT NULL,
    feature_version   TEXT NOT NULL,
    feature_text      TEXT NOT NULL CHECK (feature_text <> ''),
    is_approved       BOOLEAN NOT NULL DEFAULT FALSE,
    approved_by       TEXT REFERENCES users(email),
    approved_at       TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, feature_id, version),
    -- Approval is all-or-nothing and cannot be half-stamped. is_approved exists
    -- so a foreign key elsewhere can require it; the CHECK keeps the three
    -- columns honest.
    CHECK (is_approved = (approved_by IS NOT NULL AND approved_at IS NOT NULL))
);

COMMENT ON TABLE feature_decompositions IS
    'HUMAN-APPROVED decompositions only. Machine output is not stored here -- a row '
    'exists only because an approval transaction wrote it with a real approved_by and '
    'approved_at.';

CREATE TABLE decomposition_elements (
    decomposition_id  UUID NOT NULL REFERENCES feature_decompositions(id) ON DELETE CASCADE,
    element_id        TEXT NOT NULL,
    text              TEXT NOT NULL CHECK (text <> ''),
    core              BOOLEAN NOT NULL,
    ord               INTEGER NOT NULL CHECK (ord >= 1),
    PRIMARY KEY (decomposition_id, element_id),
    UNIQUE (decomposition_id, ord)
);

CREATE TABLE decomposition_relationships (
    decomposition_id  UUID NOT NULL REFERENCES feature_decompositions(id) ON DELETE CASCADE,
    relationship_id   TEXT NOT NULL,
    text              TEXT NOT NULL CHECK (text <> ''),
    ord               INTEGER NOT NULL CHECK (ord >= 1),
    PRIMARY KEY (decomposition_id, relationship_id),
    UNIQUE (decomposition_id, ord)
);

CREATE TABLE decomposition_requirement_ids (
    decomposition_id  UUID NOT NULL REFERENCES feature_decompositions(id) ON DELETE CASCADE,
    requirement_id    TEXT NOT NULL,
    requirement_type  TEXT NOT NULL CHECK (requirement_type IN ('ELEMENT', 'RELATIONSHIP')),
    PRIMARY KEY (decomposition_id, requirement_id)
);
-- Requirement IDs must be unique ACROSS elements and relationships, not within
-- each: verification/review.py's reviewed_outcome() merges both into one map keyed
-- by requirement id, so a collision would silently drop one requirement's
-- reviewed status. This table is that uniqueness, declaratively; the triggers
-- below keep it in step with the two tables above.

COMMENT ON TABLE decomposition_requirement_ids IS
    'Cross-table uniqueness of requirement IDs, maintained by trigger. A collision '
    'between an element ID and a relationship ID would silently drop a reviewed '
    'status when the two are merged into one map.';

CREATE FUNCTION sync_decomposition_requirement_ids() RETURNS trigger AS $$
DECLARE
    v_type TEXT := CASE TG_TABLE_NAME
                       WHEN 'decomposition_elements' THEN 'ELEMENT'
                       ELSE 'RELATIONSHIP'
                   END;
    v_old  TEXT;
    v_new  TEXT;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        v_old := CASE v_type WHEN 'ELEMENT' THEN OLD.element_id ELSE OLD.relationship_id END;
    END IF;
    IF TG_OP <> 'DELETE' THEN
        v_new := CASE v_type WHEN 'ELEMENT' THEN NEW.element_id ELSE NEW.relationship_id END;
    END IF;

    IF TG_OP = 'DELETE' THEN
        -- A no-op when the parent decomposition is being cascaded away and the
        -- requirement-id rows have already gone with it.
        DELETE FROM decomposition_requirement_ids
         WHERE decomposition_id = OLD.decomposition_id AND requirement_id = v_old;
        RETURN OLD;
    END IF;

    IF TG_OP = 'UPDATE' AND v_old IS DISTINCT FROM v_new THEN
        DELETE FROM decomposition_requirement_ids
         WHERE decomposition_id = OLD.decomposition_id AND requirement_id = v_old;
    END IF;

    -- A duplicate raises on the primary key, which is the whole point: an element
    -- and a relationship may not share an ID.
    INSERT INTO decomposition_requirement_ids (decomposition_id, requirement_id, requirement_type)
    VALUES (NEW.decomposition_id, v_new, v_type);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER decomposition_elements_requirement_ids
    AFTER INSERT OR UPDATE OR DELETE ON decomposition_elements
    FOR EACH ROW EXECUTE FUNCTION sync_decomposition_requirement_ids();

CREATE TRIGGER decomposition_relationships_requirement_ids
    AFTER INSERT OR UPDATE OR DELETE ON decomposition_relationships
    FOR EACH ROW EXECUTE FUNCTION sync_decomposition_requirement_ids();
