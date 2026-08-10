-- depends: 0004.create-users

-- The "key overlap" tags analyze_all_matches returns per match, added to the
-- result payload in 3dad9f6b. Without this column a stored run loses them, and
-- a report reopened from History would render with no overlap pills while the
-- same run rendered with them seconds earlier -- the two paths must agree.
--
-- DEFAULT '{}' rather than NOT NULL with no default: runs stored before this
-- migration genuinely have no tags recorded, and an empty array says exactly
-- that. Back-filling them with anything would be inventing model output for
-- runs no model ever analysed that way.
ALTER TABLE search_run_matches
    ADD COLUMN key_overlap TEXT[] NOT NULL DEFAULT '{}';

COMMENT ON COLUMN search_run_matches.key_overlap IS
    'Short LLM-written overlap tags for this match. Empty for runs stored before '
    'this column existed -- an honest "none recorded", not "none found".';
