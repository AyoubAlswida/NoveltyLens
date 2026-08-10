"""Password verification and stateless session tokens.

Scope, stated plainly so nobody mistakes this for more than it is: this gates the
API behind a single shared, seeded demo account (``testnovelty@gmail.com``,
password ``ABC123``, inserted by ``migrations/0004.create-users.sql``). There is
no signup endpoint, no password reset, no rate limiting, and no revocation. It
keeps a casual visitor off a backend that spends metered SerpAPI and HuggingFace
quota per request; it is not a security boundary for anything of value.

Two halves, deliberately independent:

**Passwords** live in Postgres, hashed with PBKDF2-HMAC-SHA256 from the standard
library -- no bcrypt/argon2 dependency for a demo login. The stored string is
self-describing (``pbkdf2_sha256$<iterations>$<salt_hex>$<key_hex>``) so
``ITERATIONS`` can be raised later without invalidating existing rows.

**Tokens** are stateless: an HMAC-SHA256 signature over ``{"sub", "exp"}``, keyed
by ``settings.auth_secret``. Verifying one therefore touches no database, which
is why ``require_user`` can guard the SSE endpoint without holding a connection
open for the length of a 60-second run. The cost of statelessness is that a
token cannot be revoked before ``exp`` -- ``settings.auth_session_hours`` is the
only bound on a leaked one.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Optional

import psycopg
from fastapi import Header, HTTPException

from config import settings

ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 260_000
"""PBKDF2 rounds for new hashes. Existing rows carry their own count in the
stored string, so raising this does not invalidate them -- it only applies to
passwords hashed after the change."""


class AuthError(RuntimeError):
    """Raised when a token is absent, malformed, forged, or expired."""


# --- Passwords -------------------------------------------------------------


def hash_password(password: str, *, iterations: int = ITERATIONS) -> str:
    """Hash `password` into the self-describing string the `users` table stores.

    Use this to add an account:

        python -c "from auth import hash_password; print(hash_password('...'))"
    """
    salt = secrets.token_bytes(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"{ALGORITHM}${iterations}${salt.hex()}${key.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Check `password` against a stored hash, in constant time.

    A stored string this function cannot parse returns False rather than
    raising: a corrupt row must not be distinguishable from a wrong password by
    the shape of the failure.
    """
    try:
        algorithm, iterations, salt_hex, key_hex = stored.split("$")
        if algorithm != ALGORITHM:
            return False
        expected = bytes.fromhex(key_hex)
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iterations))
    return hmac.compare_digest(candidate, expected)


def authenticate(conn: psycopg.Connection, email: str, password: str) -> Optional[str]:
    """Return the canonical email if the credentials are valid, else None.

    Emails are matched lowercased and stripped, so the account is not sensitive
    to how it was typed. The password is not.

    A missing user and a wrong password both return None, and the caller reports
    one message for both -- telling an unauthenticated caller which of the two
    it got would confirm whether an address is registered.
    """
    normalized = email.strip().lower()
    with conn.cursor() as cur:
        cur.execute("SELECT email, password_hash FROM users WHERE email = %s", (normalized,))
        row = cur.fetchone()
    if row is None:
        return None
    stored_email, password_hash = row
    return stored_email if verify_password(password, password_hash) else None


# --- Tokens ----------------------------------------------------------------


def _b64encode(raw: bytes) -> str:
    """URL-safe base64 with the padding stripped, so a token is header-safe."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: bytes) -> str:
    return _b64encode(hmac.new(settings.auth_secret.encode(), payload, hashlib.sha256).digest())


def issue_token(email: str, *, now: Optional[float] = None) -> str:
    """Mint a signed session token for `email`, valid for auth_session_hours."""
    issued_at = time.time() if now is None else now
    payload = json.dumps(
        {"sub": email, "exp": int(issued_at + settings.auth_session_hours * 3600)},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return f"{_b64encode(payload)}.{_sign(payload)}"


def verify_token(token: str, *, now: Optional[float] = None) -> str:
    """Return the email a valid token belongs to.

    The signature is checked BEFORE the payload is parsed as JSON, so a forged
    token never reaches the parser at all.

    Raises:
        AuthError: on any malformed, forged, or expired token.
    """
    try:
        encoded_payload, signature = token.split(".")
        payload = _b64decode(encoded_payload)
    except (ValueError, base64.binascii.Error) as error:  # type: ignore[attr-defined]
        raise AuthError("Malformed session token.") from error

    if not hmac.compare_digest(_sign(payload), signature):
        raise AuthError("Session token signature does not verify.")

    try:
        claims = json.loads(payload)
        email, expires_at = claims["sub"], float(claims["exp"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise AuthError("Session token payload is unreadable.") from error

    if (time.time() if now is None else now) >= expires_at:
        raise AuthError("Session expired. Sign in again.")
    return str(email)


# --- FastAPI dependency ----------------------------------------------------


def require_user(authorization: Optional[str] = Header(default=None)) -> str:
    """FastAPI dependency: the signed-in user's email, or HTTP 401.

    401 rather than 403 throughout, including for an expired token, because the
    frontend treats exactly one status as "your session is gone, show the login
    screen again" (``services/auth.ts``).
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Sign in to use Novelty Lens.")
    try:
        return verify_token(authorization[len("bearer "):].strip())
    except AuthError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error
