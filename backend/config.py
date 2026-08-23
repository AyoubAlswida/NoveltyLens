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
    lens_api_key: str = os.getenv("LENS_API_KEY", "")
    """Bearer token for the Lens.org Patent API (api.lens.org), created from
    the Subscriptions tab of a Lens user profile. Currently a 14-day
    non-commercial trial token - see search/lens.py's module docstring."""
    llm_api_url: str = os.getenv("LLM_API_URL", "https://router.huggingface.co/v1/chat/completions")
    llm_model: str = os.getenv("LLM_MODEL", "meta-llama/Llama-3.1-8B-Instruct:novita")
    llm_api_key: str = os.getenv("LLM_API_KEY", os.getenv("HF_TOKEN", ""))
    """Bearer token for ``llm_api_url``. Defaults to HF_TOKEN so the HuggingFace
    Router keeps working unchanged; set LLM_API_KEY separately when pointing
    llm_api_url at a different provider (e.g. an internal OpenAI-compatible
    endpoint) that needs its own credential."""
    uspto_api_key: str = os.getenv("PATENTSVIEW_API_KEY", "")
    qdrant_url: str = os.getenv("QDRANT_URL", "http://127.0.0.1:6333")
    qdrant_api_key: str = os.getenv("QDRANT_API_KEY", "")
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "BAAI/bge-large-en-v1.5")
    embedding_api_url: str = os.getenv("EMBEDDING_API_URL", "")
    """OpenAI-compatible ``/v1/embeddings`` endpoint. Required - there is no
    default, because embeddings.py speaks the OpenAI request/response shape
    unconditionally (``{"input": [...]}`` -> ``{"data": [{"embedding": [...]}]}``),
    which the HuggingFace Router's feature-extraction pipeline this project used
    to call does not speak. Point this at whatever OpenAI-compatible embeddings
    endpoint backs ``embedding_model``."""
    embedding_api_key: str = os.getenv("EMBEDDING_API_KEY", os.getenv("LLM_API_KEY", os.getenv("HF_TOKEN", "")))
    """Bearer token for ``embedding_api_url``. Defaults to LLM_API_KEY (falling
    further back to HF_TOKEN) since both calls typically go through the same
    gateway and key; set EMBEDDING_API_KEY separately only if the embeddings
    provider needs its own credential."""
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

    server_host: str = os.getenv("SERVER_HOST", "127.0.0.1")
    """Interface uvicorn binds to. Defaults to loopback so a laptop running this
    does not expose an unauthenticated-by-default backend to its whole network.

    **A container must override this to 0.0.0.0.** Inside a pod, loopback is
    reachable only from that pod, so a container left on the default answers its
    own health probe and nothing else - the symptom is a Service with no ready
    endpoints, not a crash. The OpenShift Deployment sets it explicitly."""

    server_port: int = int(os.getenv("SERVER_PORT", "8001"))
    """Port uvicorn listens on. Kept above 1024 because OpenShift runs
    containers as an arbitrary non-root UID that cannot bind privileged ports."""


settings = Settings()