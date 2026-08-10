"""Synchronous psycopg3 connection helper.

One connection per request, autocommit so a write from an early stage survives
even if a later stage fails. A connection failure here MUST fail the whole
request -- callers must not catch it and fall back to a degraded, database-less
result. That is the zero-tolerance failure policy applied to persistence: a down
database is not a cache miss, and a report that was never stored must not be
reported as stored.
"""

from contextlib import contextmanager
from typing import Iterator

import psycopg

from config import settings


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        yield conn
