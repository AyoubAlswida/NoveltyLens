"""Apply pending yoyo migrations (backend/migrations/) against settings.database_url.

Usage, from backend/ with requirements installed and the Compose Postgres up:
    python migrate.py

**This is not the whole schema.** LangGraph's checkpoint tables -- ``checkpoints``,
``checkpoint_blobs``, ``checkpoint_writes`` and ``checkpoint_migrations`` -- are
created and migrated by ``PostgresSaver.setup()``, which tracks its own schema
version in the last of them. yoyo deliberately does not own them: two migration
systems on the same tables is a guaranteed conflict the first time either side
changes a column. Run that step separately, once, after this one::

    python checkpointer.py setup

See ``backend/checkpointer.py`` for the full reasoning.
"""

from yoyo import get_backend, read_migrations

from config import settings

_PSYCOPG2_SCHEMES = ("postgresql://", "postgres://")


def _yoyo_database_uri(database_url: str) -> str:
    """yoyo picks its backend from the URI scheme, and postgresql:// resolves
    to psycopg2 -- not installed, since backend/requirements.txt pins psycopg3
    instead. Rewrite the scheme to postgresql+psycopg:// so yoyo loads the
    psycopg3 backend, without needing a second, duplicate connection string."""
    for scheme in _PSYCOPG2_SCHEMES:
        if database_url.startswith(scheme):
            return f"postgresql+psycopg://{database_url[len(scheme):]}"
    return database_url


def main() -> None:
    backend = get_backend(_yoyo_database_uri(settings.database_url))
    migrations = read_migrations("migrations")
    with backend.lock():
        backend.apply_migrations(backend.to_apply(migrations))


if __name__ == "__main__":
    main()
