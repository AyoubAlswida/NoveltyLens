-- depends: 0003.create-search-run-matches

-- Login accounts for the API. There is no signup endpoint and no self-service
-- registration by design: accounts are created here, by migration, or by an
-- INSERT run against the database. Adding a user is a row, not a code change.
--
-- password_hash holds the full self-describing string produced by
-- backend/auth.py's hash_password() -- "pbkdf2_sha256$<iterations>$<salt>$<key>"
-- -- so the iteration count and salt travel with the hash and can be raised
-- later without invalidating existing rows.
CREATE TABLE users (
    email TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE users IS
    'Login accounts. Populated by migration or by hand -- the API exposes no signup.';
COMMENT ON COLUMN users.email IS
    'Stored lowercased; backend/auth.py lowercases the submitted email before lookup.';

-- The seeded test account: testnovelty@gmail.com / ABC123.
--
-- This password is public -- it is written in the repository, in this comment,
-- and on the login screen's hint. The account is a shared demo login, not a
-- credential: treat any backend reachable from outside localhost as having no
-- access control at all until this row is deleted and a real one inserted with
-- backend/auth.py's hash_password().
INSERT INTO users (email, password_hash) VALUES (
    'testnovelty@gmail.com',
    'pbkdf2_sha256$260000$7f3c9a1d4e2b8065af31cd57e90b264a$6f61bf309a99965530c58b10610046bdd9e6d4073ba647abf8cf07b9fc41f3a8'
);
