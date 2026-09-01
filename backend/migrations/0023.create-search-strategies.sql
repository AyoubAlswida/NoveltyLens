-- depends: 0022.add-strategy-workflow-gate

-- The SEARCH STRATEGY stage: what will be searched for, before anything is.
--
-- This stage sits between the approved invention understanding and the first
-- provider call. Its shape is deliberately the same shape as the understanding
-- stage's, because it is the same kind of thing: a machine PROPOSES, a human
-- EDITS and APPROVES, and a rejection is a durable work request the worker
-- carries out -- never an approval, and never work done on a request thread.
--
-- **The one thing this stage adds is coverage.** Each proposed query names the
-- approved element ids it is meant to retrieve prior art for
-- (covered_element_ids), and a core element that no enabled query covers BLOCKS
-- approval. That is what stops a run from searching confidently while leaving
-- part of the invention unsearched -- an omission that would otherwise surface
-- as "no prior art found" about something nobody looked for.

-- ---------------------------------------------------------------------------
-- Machine proposals and human edits (NOT approvals)
-- ---------------------------------------------------------------------------
--
-- **No is_approved column and no approver columns exist here**, exactly as in
-- understanding_proposals: approved_search_strategies is the approved artifact,
-- and a row in this table cannot be mistaken for one because there is no field
-- in which an approval could be recorded.
--
-- understanding_version is on the proposal because a strategy is a proposal
-- ABOUT a specific approved understanding. Its coverage claims name that
-- understanding's element ids, so a proposal that does not say which version it
-- was made against cannot be checked against anything.
CREATE TABLE search_strategy_proposals (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id                UUID NOT NULL REFERENCES verification_runs(id) ON DELETE CASCADE,
    version               INTEGER NOT NULL CHECK (version >= 1),
    understanding_version INTEGER NOT NULL CHECK (understanding_version >= 1),
    source                TEXT NOT NULL CHECK (source IN ('LLM', 'USER_EDIT')),
    queries               JSONB NOT NULL,
    feedback              TEXT,
    created_by            TEXT NOT NULL REFERENCES users(email),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (jsonb_typeof(queries) = 'array'),
    -- One machine proposal and one human edit per version. A regeneration is a
    -- NEW version, never an overwrite of this one.
    UNIQUE (run_id, version, source)
);

COMMENT ON TABLE search_strategy_proposals IS
    'MACHINE OUTPUT and HUMAN EDITS, never approvals. No is_approved column and no '
    'approver columns exist here on purpose: approved_search_strategies is the '
    'approved artifact, and a row in this table cannot record an approval because '
    'there is no field in which one could be recorded.';
COMMENT ON COLUMN search_strategy_proposals.queries IS
    'The proposed queries whole: query_id, query_text, rationale, enabled and '
    'covered_element_ids. Stored as proposed -- a proposal is reviewed as a whole, '
    'and one that needed repairing to be stored is not the proposal that was made.';
COMMENT ON COLUMN search_strategy_proposals.understanding_version IS
    'The approved understanding this strategy was proposed against. Coverage is '
    'claimed over that version element ids, so a strategy that cannot name it '
    'cannot be checked for coverage at all.';

CREATE INDEX search_strategy_proposals_run_idx
    ON search_strategy_proposals (run_id, version DESC);

-- ---------------------------------------------------------------------------
-- The APPROVED search strategy
-- ---------------------------------------------------------------------------
--
-- Same approval CHECK and the same single writer (workflow/approvals.py) as
-- every other approved artifact. A strategy that names no approver is not an
-- approval.
--
-- understanding_version is recorded here too, and it is not decoration: it is
-- what makes a strategy STALE when the understanding is regenerated and
-- re-approved. A strategy approved against version 1 claims coverage of version
-- 1 elements, and silently reusing it for version 2 would report coverage of
-- requirements it was never checked against.
CREATE TABLE approved_search_strategies (
    run_id                UUID NOT NULL REFERENCES verification_runs(id) ON DELETE CASCADE,
    version               INTEGER NOT NULL CHECK (version >= 1),
    understanding_version INTEGER NOT NULL CHECK (understanding_version >= 1),
    is_approved           BOOLEAN NOT NULL DEFAULT FALSE,
    approved_by           TEXT REFERENCES users(email),
    approved_at           TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, version),
    CHECK (is_approved = (approved_by IS NOT NULL AND approved_at IS NOT NULL))
);

COMMENT ON TABLE approved_search_strategies IS
    'HUMAN-APPROVED search strategies only. Written by workflow/approvals.py and '
    'nothing else. The queries themselves are rows in approved_strategy_queries.';

-- One row per approved query, in approved order.
--
-- **Disabled queries are stored, not dropped.** A reviewer switching a query off
-- is a decision about the search, and the report has to be able to say what was
-- deliberately not searched for. Execution filters on enabled; the record keeps
-- both.
--
-- covered_element_ids is the coverage claim: the approved element ids this query
-- is meant to retrieve prior art for. There is no foreign key to
-- decomposition_elements -- the same situation decomposition_relationships is in
-- -- so the coverage check in workflow/review.py is an id-set comparison against
-- the approved decomposition at understanding_version, and it says so rather
-- than implying the database is enforcing it.
CREATE TABLE approved_strategy_queries (
    run_id              UUID NOT NULL,
    version             INTEGER NOT NULL,
    query_id            TEXT NOT NULL,
    query_text          TEXT NOT NULL CHECK (query_text <> ''),
    rationale           TEXT NOT NULL DEFAULT '',
    enabled             BOOLEAN NOT NULL,
    ord                 INTEGER NOT NULL,
    covered_element_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    PRIMARY KEY (run_id, version, query_id),
    FOREIGN KEY (run_id, version)
        REFERENCES approved_search_strategies (run_id, version) ON DELETE CASCADE,
    CHECK (jsonb_typeof(covered_element_ids) = 'array')
);

COMMENT ON COLUMN approved_strategy_queries.enabled IS
    'Whether this query is dispatched. ONLY enabled queries from the approved '
    'strategy execute. A disabled row is kept because "we deliberately did not '
    'search for this" is part of what the human approved and part of what the '
    'report has to be able to state.';
COMMENT ON COLUMN approved_strategy_queries.covered_element_ids IS
    'The approved element ids this query claims to cover. Checked against the '
    'approved decomposition at the strategy understanding_version -- an id set, '
    'not a foreign key, and never a guarantee that the query actually retrieves '
    'prior art for them.';

CREATE INDEX approved_strategy_queries_enabled_idx
    ON approved_strategy_queries (run_id, version) WHERE enabled;

-- ---------------------------------------------------------------------------
-- Regeneration requests (POST /api/runs/{id}/strategy/regenerate)
-- ---------------------------------------------------------------------------
--
-- A REQUEST for fresh queries, not fresh queries and not an approval. The API
-- records the row and returns 202; the **worker** carries it out under the run
-- lease, inside the graph, calling the same node that made the first proposal --
-- for the same reason a manual candidate, a cell rerun and an understanding
-- regeneration are all requests: an LLM call on a request thread has no lease,
-- no fencing and no recovery.
CREATE TABLE strategy_regeneration_requests (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id         UUID NOT NULL REFERENCES verification_runs(id) ON DELETE CASCADE,
    from_version   INTEGER NOT NULL CHECK (from_version >= 1),
    feedback       TEXT NOT NULL DEFAULT '',
    requested_by   TEXT NOT NULL REFERENCES users(email),
    requested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at   TIMESTAMPTZ,
    -- One regeneration per version. Asking twice about the same version is the
    -- same request; asking about the version that regeneration produced is a
    -- different one and takes its own row.
    UNIQUE (run_id, from_version)
);

COMMENT ON TABLE strategy_regeneration_requests IS
    'A REQUEST to propose fresh search queries. Carried out by the worker under '
    'the run lease, never in the HTTP handler. A regeneration produces a NEW '
    'strategy version and never edits the version it was asked to improve on. The '
    'claimed row is KEPT: it is the evidence that a person asked and the worker '
    'acted.';

CREATE INDEX strategy_regeneration_pending_idx
    ON strategy_regeneration_requests (run_id) WHERE processed_at IS NULL;
