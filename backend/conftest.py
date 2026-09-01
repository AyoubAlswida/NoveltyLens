"""Root conftest: pin the deployment profile before anything imports `config`.

`DEPLOYMENT_PROFILE` has **no default** — `backend/deployment.py` raises
`ConfigError` at import when it is unset, because a default would silently choose
which provider the application talks to and which capability qualifications may be
reused.

The test suite therefore has to declare its own profile, and it declares `test`:
the profile whose provider is a stub with no address and no model. That is the
correct answer rather than a convenience. Under `test`:

* no real provider is configured, so a boundary somebody forgot to stub raises
  `MissingCredential` instead of reaching a paid API;
* capability artifacts carry `deployment_profile: test`, and
  `verification/capability.py` refuses those under `local` and `aramco`, so a
  fixture can never qualify a real runtime.

`setdefault`, not assignment: a developer running the suite against a real profile
on purpose (`DEPLOYMENT_PROFILE=local python -m pytest`) keeps their choice. What
they cannot do is run it with *no* choice.

This file must stay at the backend root rather than in `tests/`. pytest loads the
rootdir conftest before collecting any test module, and collection is what
triggers `import config`.

**`OPENAI_REASONING_EFFORT` is cleared, not defaulted.** It is read from the
environment on every request, and a developer's `backend/.env` sets it to `high`
for the real local runtime. Left in place it would leak into the suite, where a
test constructing a classic-model target (`gpt-4o-2024-08-06`) would hit the
adapter's deliberate refusal to send a setting that family cannot honour — a test
outcome decided by a file that is not part of the test. Tests that care about the
setting set it themselves with `monkeypatch`, in both directions, which is the
only way a test can assert on it honestly.

Cleared rather than set to a value, because "unset" is itself one of the
configurations under test: `tests/test_inference_identity.py` asserts that an
artifact measured with the effort unset cannot qualify a `high` runtime.

It is cleared **per test**, not once at import, and that is not belt-and-braces:
`config.py` calls `load_dotenv()`, which reads `backend/.env` into `os.environ`
when `config` is first imported — *after* this module runs. A single pop here is
undone by that import, silently, and the leak reappears.
"""

import os

import pytest

os.environ.setdefault("DEPLOYMENT_PROFILE", "test")
os.environ.pop("OPENAI_REASONING_EFFORT", None)


@pytest.fixture(autouse=True)
def hermetic_inference_settings(monkeypatch):
    """Clear inference settings a developer's `.env` may carry, before every test.

    Autouse and function-scoped, so a test that wants a setting declares it with
    `monkeypatch` in its own body and gets it — a test's behaviour must never be
    decided by a file that is not part of the test.
    """
    monkeypatch.delenv("OPENAI_REASONING_EFFORT", raising=False)


# --- the outbound-call guard ----------------------------------------------
#
# The `test` profile already makes a paid call *structurally* impossible: its
# provider is a stub with no address, and `require_llm_credentials` raises for it
# rather than returning a key. That is an argument, though, and the claim being
# made is a measurement: **no test in this suite contacts an external host.**
#
# So it is enforced rather than asserted. `socket.socket.connect` is wrapped for
# the whole session and refuses any address that is not loopback. Loopback stays
# open because the Compose PostgreSQL genuinely is a service this suite talks to,
# and pretending otherwise would mean stubbing the one thing the `postgres`-marked
# tests exist to exercise.
#
# It is the same idea as `run_mock.py`'s guard and deliberately a different
# implementation: that one wraps `requests.Session.request`, which sees only HTTP
# through `requests`. This sits under *every* client -- `requests`, the OpenAI
# SDK's httpx, `qdrant-client`, psycopg -- because the property is about the
# socket, not about a library.

import socket as _socket


class OutboundCallAttempted(RuntimeError):
    """A test tried to open a connection to a non-loopback host.

    Never "just a warning": a suite that quietly reaches a provider spends real
    quota, and a test whose result depends on a live third party is not a test.
    Stub the boundary instead.
    """


_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost", "0.0.0.0", ""})

_REAL_CONNECT = _socket.socket.connect


def _guarded_connect(self, address):
    host = address[0] if isinstance(address, tuple) else None
    if host is not None and str(host) not in _LOOPBACK:
        raise OutboundCallAttempted(
            f"a test tried to connect to {host!r}, which is not loopback. Nothing "
            "in this suite may contact an external host: it would spend real "
            "quota and make the result depend on a third party. Stub the boundary."
        )
    return _REAL_CONNECT(self, address)


@pytest.fixture(autouse=True, scope="session")
def no_outbound_calls():
    _socket.socket.connect = _guarded_connect
    try:
        yield
    finally:
        _socket.socket.connect = _REAL_CONNECT
