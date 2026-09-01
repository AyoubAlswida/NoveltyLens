"""A negative control on the session-wide outbound-call guard.

**A guard nobody tests is indistinguishable from a guard that stopped working.**
`backend/conftest.py` wraps `socket.socket.connect` for the whole session and
refuses any address that is not loopback, so "the suite makes no external calls"
is enforced rather than argued. But a suite that makes no external calls and a
guard that has quietly become a no-op produce the *same* green run — the only way
to tell them apart is to try a connection that must fail.

So this file does exactly that, once, and then checks that loopback still works,
because a guard that blocked everything would also pass the first test while
breaking every `postgres`-marked test in the suite for an unrelated reason.

Neither test contacts an external host: the first is refused before the socket is
opened, and the second reaches the local Compose PostgreSQL.
"""

import socket

import pytest

from conftest import OutboundCallAttempted


def test_the_guard_blocks_a_connection_to_a_real_external_host():
    """`api.openai.com` deliberately — it is the host a forgotten stub would reach,
    and reaching it would spend real quota against the configured account.

    The refusal names the **resolved address**, not the hostname: `create_connection`
    resolves first and hands `connect` an IP, and the guard sits on `connect`. That
    is the right place for it — a name lookup carries no payload and spends no
    quota, while the connect is the moment a request could actually leave. A guard
    on the lookup would also have to understand every way a client can be handed an
    address it already resolved.
    """
    with pytest.raises(OutboundCallAttempted) as refused:
        socket.create_connection(("api.openai.com", 443), timeout=5)
    assert "not loopback" in str(refused.value)


def test_the_guard_leaves_loopback_open():
    """The Compose PostgreSQL is a service this suite genuinely talks to, and the
    `postgres`-marked tests exist precisely to exercise the real SQL. Blocking it
    would make the guard a different, wrong thing."""
    sock = socket.socket()
    try:
        sock.settimeout(2)
        sock.connect(("127.0.0.1", 5432))
    except OSError as unreachable:  # pragma: no cover - environment dependent
        pytest.skip(f"the Compose PostgreSQL is not up: {unreachable}")
    finally:
        sock.close()
