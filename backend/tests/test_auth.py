"""Password hashing, session tokens, and the endpoint gate.

What this file does NOT claim: that the login is secure. It is a single shared
demo account with a published password and no rate limiting (see
``backend/auth.py``'s module docstring). These tests pin the properties the
implementation does promise - a wrong password is rejected, a forged or expired
token is rejected, and no endpoint but ``/api/auth/login`` answers without one.
"""

import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import server
from auth import (
    AuthError,
    authenticate,
    hash_password,
    issue_token,
    verify_password,
    verify_token,
)

SEEDED_EMAIL = "testnovelty@gmail.com"
SEEDED_PASSWORD = "ABC123"
SEEDED_HASH = (
    "pbkdf2_sha256$260000$7f3c9a1d4e2b8065af31cd57e90b264a$"
    "6f61bf309a99965530c58b10610046bdd9e6d4073ba647abf8cf07b9fc41f3a8"
)


class _FakeCursor:
    """Answers the one SELECT ``authenticate`` issues, from a dict of rows."""

    def __init__(self, rows: dict[str, str]) -> None:
        self._rows = rows
        self._result: tuple[str, str] | None = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql: str, params: tuple) -> None:
        email = params[0]
        self._result = (email, self._rows[email]) if email in self._rows else None

    def fetchone(self):
        return self._result


class _FakeConnection:
    def __init__(self, rows: dict[str, str]) -> None:
        self._rows = rows

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._rows)


@pytest.fixture
def seeded_connection() -> _FakeConnection:
    """A database holding exactly the row migration 0004 inserts."""
    return _FakeConnection({SEEDED_EMAIL: SEEDED_HASH})


# --- Passwords -------------------------------------------------------------


def test_hash_and_verify_round_trip():
    stored = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", stored)
    assert not verify_password("Correct horse battery staple", stored)


def test_each_hash_uses_a_fresh_salt():
    """Two rows with the same password must not look the same, or the table
    leaks which accounts share a password."""
    assert hash_password("ABC123") != hash_password("ABC123")


def test_an_unparseable_stored_hash_is_a_failed_login_not_a_crash():
    assert not verify_password("ABC123", "not-a-hash")
    assert not verify_password("ABC123", "bcrypt$12$whatever")


def test_the_seeded_migration_hash_matches_the_documented_password():
    """Pins migrations/0004.create-users.sql to the credentials the login screen
    and CLAUDE.md both advertise. If someone regenerates that hash without
    updating the docs, this is what catches it."""
    assert verify_password(SEEDED_PASSWORD, SEEDED_HASH)


# --- authenticate() --------------------------------------------------------


def test_correct_credentials_return_the_canonical_email(seeded_connection):
    assert authenticate(seeded_connection, SEEDED_EMAIL, SEEDED_PASSWORD) == SEEDED_EMAIL


def test_email_matching_ignores_case_and_surrounding_space(seeded_connection):
    assert authenticate(seeded_connection, "  TestNovelty@Gmail.com ", SEEDED_PASSWORD) == SEEDED_EMAIL


def test_password_matching_does_not_ignore_case(seeded_connection):
    assert authenticate(seeded_connection, SEEDED_EMAIL, "abc123") is None


def test_unknown_user_and_wrong_password_are_indistinguishable(seeded_connection):
    """Both None, so the endpoint cannot be used to enumerate accounts."""
    assert authenticate(seeded_connection, "nobody@example.com", SEEDED_PASSWORD) is None
    assert authenticate(seeded_connection, SEEDED_EMAIL, "wrong") is None


# --- Tokens ----------------------------------------------------------------


def test_token_round_trips_to_the_email_it_was_issued_for():
    assert verify_token(issue_token(SEEDED_EMAIL)) == SEEDED_EMAIL


def test_a_tampered_payload_is_rejected():
    """The signature covers the payload, so swapping the subject invalidates it."""
    token = issue_token(SEEDED_EMAIL)
    payload, signature = token.split(".")
    forged = f"{payload[:-4]}AAAA.{signature}"
    with pytest.raises(AuthError):
        verify_token(forged)


def test_a_token_signed_with_another_secret_is_rejected():
    """``Settings`` is frozen, so the whole object is swapped rather than the
    one field - ``patch("auth.settings.auth_secret", ...)`` raises
    FrozenInstanceError."""
    from dataclasses import replace

    from config import settings

    with patch("auth.settings", replace(settings, auth_secret="someone-elses-secret")):
        foreign = issue_token(SEEDED_EMAIL)
    with pytest.raises(AuthError):
        verify_token(foreign)


def test_an_expired_token_is_rejected():
    """Issued far enough in the past that auth_session_hours has elapsed."""
    stale = issue_token(SEEDED_EMAIL, now=time.time() - 3600 * 24 * 365)
    with pytest.raises(AuthError):
        verify_token(stale)


@pytest.mark.parametrize("token", ["", "nonsense", "a.b.c", "onlyonepart"])
def test_malformed_tokens_are_rejected_without_raising_something_else(token):
    with pytest.raises(AuthError):
        verify_token(token)


# --- The endpoint gate -----------------------------------------------------


@pytest.fixture
def client(seeded_connection) -> TestClient:
    """A client whose ``db.connect()`` yields the seeded users table.

    ``conftest.py``'s autouse ``report_store`` fixture already patches
    ``db.connect``; this narrows it further for the login path, which is the
    only endpoint that actually queries.
    """
    from contextlib import contextmanager

    @contextmanager
    def connect():
        yield seeded_connection

    with patch("db.connect", connect):
        yield TestClient(server.app)


def test_login_returns_a_token_that_the_gate_accepts(client):
    response = client.post(
        "/api/auth/login", json={"email": SEEDED_EMAIL, "password": SEEDED_PASSWORD}
    )
    assert response.status_code == 200
    token = response.json()["token"]
    assert response.json()["email"] == SEEDED_EMAIL

    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json() == {"email": SEEDED_EMAIL}


def test_login_with_a_wrong_password_is_401_and_says_nothing_specific(client):
    response = client.post(
        "/api/auth/login", json={"email": SEEDED_EMAIL, "password": "nope"}
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Incorrect email or password."


def test_login_with_an_unknown_email_gives_the_identical_response(client):
    wrong_password = client.post(
        "/api/auth/login", json={"email": SEEDED_EMAIL, "password": "nope"}
    )
    unknown_user = client.post(
        "/api/auth/login", json={"email": "nobody@example.com", "password": "nope"}
    )
    assert unknown_user.status_code == wrong_password.status_code
    assert unknown_user.json() == wrong_password.json()


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("post", "/api/analyze", {"description": "a widget"}),
        ("post", "/api/analyze/stream", {"description": "a widget"}),
        ("get", "/api/history", None),
        ("get", "/api/auth/me", None),
    ],
)
def test_every_endpoint_but_login_rejects_an_anonymous_request(client, method, path, body):
    """The gate is what keeps a passer-by from spending the SerpAPI and
    HuggingFace quota a single run costs."""
    response = getattr(client, method)(path, **({"json": body} if body else {}))
    assert response.status_code == 401


def test_a_garbage_authorization_header_is_401_not_500(client):
    response = client.get("/api/history", headers={"Authorization": "Bearer not-a-token"})
    assert response.status_code == 401


def test_a_non_bearer_scheme_is_rejected(client):
    response = client.get("/api/history", headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert response.status_code == 401
