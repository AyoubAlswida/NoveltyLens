-- depends: 0010.create-feature-decompositions

-- The APPROVED candidate set (Workflow Gate B), the per-publication date record,
-- and the publication-scoped exclusions.
--
-- The RANKED candidate list is machine output and is not an approved set. Same
-- rule as feature_decompositions: only an approval transaction writes a row here.
CREATE TABLE candidate_sets (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id       UUID NOT NULL REFERENCES verification_runs(id) ON DELETE CASCADE,
    version      INTEGER NOT NULL CHECK (version >= 1),
    is_approved  BOOLEAN NOT NULL DEFAULT FALSE,
    approved_by  TEXT REFERENCES users(email),
    approved_at  TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, version),
    CHECK (is_approved = (approved_by IS NOT NULL AND approved_at IS NOT NULL))
);

COMMENT ON TABLE candidate_sets IS
    'HUMAN-APPROVED candidate sets only. A ranked list is machine output and is not '
    'one of these.';

CREATE TABLE candidate_set_members (
    candidate_set_id    UUID NOT NULL REFERENCES candidate_sets(id) ON DELETE CASCADE,
    publication_number  TEXT NOT NULL,
    normalized_number   TEXT NOT NULL,
    ord                 INTEGER NOT NULL CHECK (ord >= 0),
    added_manually      BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (candidate_set_id, publication_number)
);

CREATE TABLE publication_dates (
    run_id              UUID NOT NULL REFERENCES verification_runs(id) ON DELETE CASCADE,
    publication_number  TEXT NOT NULL,
    date_original       TEXT NOT NULL,     -- provider string, VERBATIM
    date_normalized     DATE,              -- NULL only when certainty is UNCERTAIN
    date_source         TEXT NOT NULL,     -- which provider field it came from
    date_certainty      date_certainty NOT NULL,
    priority_date       DATE,              -- displayed separately; never silently screens
    screened_at_stage   TEXT NOT NULL CHECK (screened_at_stage IN ('DISCOVERY', 'ENRICHMENT')),
    PRIMARY KEY (run_id, publication_number),
    CHECK (date_certainty <> 'CERTAIN' OR date_normalized IS NOT NULL)
);

COMMENT ON TABLE publication_dates IS
    'Patent dates are date-only and carry NO timezone: date_normalized is a plain DATE '
    'and no timezone shift is ever applied. An UNCERTAIN date never silently passes '
    'screening.';

CREATE TABLE publication_exclusions (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id                UUID NOT NULL REFERENCES verification_runs(id) ON DELETE CASCADE,
    requested_number      TEXT NOT NULL,
    normalized_number     TEXT NOT NULL,
    served_number         TEXT,
    state                 exclusion_state NOT NULL,
    reason                TEXT NOT NULL CHECK (reason <> ''),
    failing_check         TEXT,
    language_attempted    TEXT NOT NULL DEFAULT '',
    sections_available    TEXT[] NOT NULL DEFAULT '{}',
    sections_retrieved    TEXT[] NOT NULL DEFAULT '{}',
    passages_built        INTEGER NOT NULL DEFAULT 0,
    provider_detail       TEXT NOT NULL DEFAULT '',   -- REDACTED before insert
    excluded_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX publication_exclusions_run_idx ON publication_exclusions (run_id, state);

COMMENT ON TABLE publication_exclusions IS
    'Publication-scoped exclusions. NOT a matrix cell, and never joined into one. '
    'passages_built is what makes derived eligibility auditable: an EXCLUDED_INELIGIBLE '
    'row with sections_retrieved={DESCRIPTION} and passages_built=0 says exactly why -- '
    'text was there, a citable locator was not.';
COMMENT ON COLUMN publication_exclusions.provider_detail IS
    'REDACTED before insert. No credential, no URL carrying a key, no raw provider body.';
