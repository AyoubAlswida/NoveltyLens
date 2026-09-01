-- depends: 0018.evidence-replay-contracts

-- `sync_decomposition_requirement_ids` has never worked, and nothing noticed
-- because nothing inserted a decomposition until Implementation Gate C.
--
-- THE DEFECT. The function is one trigger shared by two tables, so it picked the
-- id column with a CASE over the record:
--
--     v_new := CASE v_type WHEN 'ELEMENT' THEN NEW.element_id
--                          ELSE NEW.relationship_id END;
--
-- plpgsql resolves record field references when the expression is *evaluated*,
-- not when the branch is taken. Inserting into `decomposition_elements` therefore
-- fails with `record "new" has no field "relationship_id"`, and inserting into
-- `decomposition_relationships` fails the mirror image. Both branches referenced
-- a column that exists on only one of the two tables, so EVERY insert raised.
--
-- Why it matters beyond "the insert fails": the trigger is the mechanism that
-- makes requirement IDs unique ACROSS elements and relationships.
-- `verification/review.py`'s `reviewed_outcome()` merges both into one map keyed
-- by requirement id, so an element E1 and a relationship E1 would silently drop
-- one requirement's reviewed status -- a human's review of a requirement
-- disappearing with no error anywhere. A trigger that cannot run enforces
-- nothing.
--
-- THE FIX. Branch on `TG_TABLE_NAME` with real IF blocks, so each branch names
-- only the column its own table has. Behaviour is otherwise identical: the same
-- rows are written to `decomposition_requirement_ids`, and the same primary-key
-- collision raises when an element and a relationship share an id.
--
-- Migration 0010 is applied and is NOT rewritten. This replaces the function
-- forward; the rollback restores 0010's version byte-for-byte, defect included,
-- because a rollback that silently improved on the migration it reverses would
-- make the two directions disagree about what version 0018 of this schema was.

CREATE OR REPLACE FUNCTION sync_decomposition_requirement_ids() RETURNS trigger AS $$
DECLARE
    v_is_element BOOLEAN := TG_TABLE_NAME = 'decomposition_elements';
    v_type       TEXT    := CASE WHEN TG_TABLE_NAME = 'decomposition_elements'
                                 THEN 'ELEMENT' ELSE 'RELATIONSHIP' END;
    v_old        TEXT;
    v_new        TEXT;
    v_parent     UUID;
BEGIN
    -- Each branch names only the column its own table actually has. That is the
    -- whole fix: a CASE expression over NEW/OLD resolves every field reference
    -- it contains, including the one in the branch not taken.
    IF TG_OP = 'DELETE' THEN
        IF v_is_element THEN
            v_old := OLD.element_id;
        ELSE
            v_old := OLD.relationship_id;
        END IF;
        -- A no-op when the parent decomposition is being cascaded away and the
        -- requirement-id rows have already gone with it.
        DELETE FROM decomposition_requirement_ids
         WHERE decomposition_id = OLD.decomposition_id AND requirement_id = v_old;
        RETURN OLD;
    END IF;

    IF v_is_element THEN
        v_new := NEW.element_id;
    ELSE
        v_new := NEW.relationship_id;
    END IF;
    v_parent := NEW.decomposition_id;

    IF TG_OP = 'UPDATE' THEN
        IF v_is_element THEN
            v_old := OLD.element_id;
        ELSE
            v_old := OLD.relationship_id;
        END IF;
        IF v_old IS DISTINCT FROM v_new THEN
            DELETE FROM decomposition_requirement_ids
             WHERE decomposition_id = OLD.decomposition_id AND requirement_id = v_old;
        END IF;
    END IF;

    -- A duplicate raises on the primary key, which is the whole point: an element
    -- and a relationship may not share an ID.
    INSERT INTO decomposition_requirement_ids (decomposition_id, requirement_id, requirement_type)
    VALUES (v_parent, v_new, v_type);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION sync_decomposition_requirement_ids IS
    'Branches on TG_TABLE_NAME with IF blocks, not a CASE over NEW/OLD: plpgsql '
    'resolves every record field reference in an expression, including the branch '
    'not taken, so 0010''s version raised on every insert into either table.';
