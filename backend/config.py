"""Application configuration sourced exclusively from environment variables."""

from dataclasses import dataclass
import os

from dotenv import load_dotenv

# override=True: .env is the intended source of truth for local dev config.
# Without it, a stray `export SERPAPI_KEY=...` (or any var already sitting in
# the shell) silently wins over .env forever in that shell session, no
# matter how many times the file is edited or the process restarted.
load_dotenv(override=True)

class ConfigError(RuntimeError):
    """Raised at import time when the environment is configured incorrectly."""


@dataclass(frozen=True, slots=True)
class Settings:
    hf_token: str = os.getenv("HF_TOKEN", "")
    serpapi_key: str = os.getenv("SERPAPI_KEY", "")
    epo_key: str = os.getenv("EPO_KEY", os.getenv("EPO_CONSUMER_KEY", ""))
    epo_secret: str = os.getenv("EPO_SECRET", os.getenv("EPO_CONSUMER_SECRET", ""))
    llm_api_url: str = os.getenv("LLM_API_URL", "https://router.huggingface.co/v1/chat/completions")
    llm_model: str = os.getenv("LLM_MODEL", "meta-llama/Llama-3.1-8B-Instruct:novita")
    uspto_api_key: str = os.getenv("PATENTSVIEW_API_KEY", "")
    qdrant_url: str = os.getenv("QDRANT_URL", "http://127.0.0.1:6333")
    qdrant_api_key: str = os.getenv("QDRANT_API_KEY", "")
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "BAAI/bge-large-en-v1.5")
    database_url: str = os.getenv(
        "DATABASE_URL", "postgresql://agenticai:agenticai@127.0.0.1:5432/agenticai"
    )
    """PostgreSQL connection string. The default matches docker-compose.yml's
    `postgres` service, whose credentials are local-development-only and
    deliberately committed."""

    auth_secret: str = os.getenv("AUTH_SECRET", "novelty-lens-local-development-secret")
    """HMAC key the session tokens in ``backend/auth.py`` are signed with.

    The default is a known constant, so anyone can mint a valid token against a
    backend that did not set AUTH_SECRET. That is acceptable for local
    development and is exactly why it must be set to a real random value before
    this is exposed to anything but localhost."""

    auth_session_hours: int = int(os.getenv("AUTH_SESSION_HOURS", "12"))
    """How long an issued session token stays valid. Tokens are stateless, so
    this is the only thing bounding a leaked one - there is no revocation list."""


settings = Settings()