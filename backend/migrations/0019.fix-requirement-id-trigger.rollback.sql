-- Restores migration 0010's version of the function BYTE-FOR-BYTE, including the
-- CASE-over-NEW defect that made every insert into decomposition_elements and
-- decomposition_relationships raise.
--
-- That is deliberate. A rollback that silently improved on the migration it
-- reverses would make the two directions disagree about what this schema version
-- was, and "rolled back" would no longer mean "back to the previous state".

CREATE OR REPLACE FUNCTION sync_decomposition_requirement_ids() RETURNS trigger AS $$
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
