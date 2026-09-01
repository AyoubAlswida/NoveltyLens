-- depends: 0008.create-workflow-events

-- Transactional idempotency for every mutating workflow endpoint.
--
-- A unique index is NOT idempotency. It prevents a duplicate row; it does not
-- make a multi-statement mutation replay-safe, it cannot return the original
-- response body, and it cannot tell a retry from a conflicting reuse of the same
-- key. This table is one half of the protocol in workflow/idempotency.py -- the
-- insert-then-mutate-then-record transaction with a blocking read for the loser
-- of a race. The table alone guarantees nothing.
CREATE TABLE idempotency_records (
    actor_email      TEXT NOT NULL REFERENCES users(email),
    scope            TEXT NOT NULL,   -- 'gate_a_approve', 'cell_review', 'cell_waiver', ...
    resource_id      TEXT NOT NULL,   -- run_id, or cell_id, whichever the operation mutates
    idempotency_key  TEXT NOT NULL,
    request_sha256   TEXT NOT NULL,
    http_status      INTEGER,         -- NULL while the mutation is still in flight
    response_body    JSONB,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at     TIMESTAMPTZ,
    PRIMARY KEY (actor_email, scope, resource_id, idempotency_key),
    CHECK ((http_status IS NULL) = (completed_at IS NULL))
);

COMMENT ON TABLE idempotency_records IS
    'Keyed by the AUTHENTICATED USER as well as the resource, so one user''s '
    'idempotency key can never return another user''s stored response. The mechanism '
    'is the transaction in workflow/idempotency.py, not this table on its own.';
