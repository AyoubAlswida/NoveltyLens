-- depends: 0025.add-patent-family-to-matches

ALTER TABLE verification_runs
    DROP COLUMN advanced_search_flow_version;
