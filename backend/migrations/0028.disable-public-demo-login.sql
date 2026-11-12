-- depends: 0027.abstract-search

-- The credential seeded by 0004 is public and must never remain usable on a
-- public deployment. A real account is provisioned by the operator after the
-- first deployment.
UPDATE users
SET password_hash = 'disabled'
WHERE email = 'testnovelty@gmail.com'
  AND password_hash = 'pbkdf2_sha256$260000$7f3c9a1d4e2b8065af31cd57e90b264a$6f61bf309a99965530c58b10610046bdd9e6d4073ba647abf8cf07b9fc41f3a8';
