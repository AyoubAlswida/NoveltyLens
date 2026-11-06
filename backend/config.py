"""Application configuration sourced exclusively from environment variables."""

from dataclasses import dataclass, field
import os

from dotenv import load_dotenv

# override=True: .env is the intended source of truth for local dev config.
# Without it, a stray `export SERPAPI_KEY=...` (or any var already sitting in
# the shell) silently wins over .env forever in that shell session, no
# matter how many times the file is edited or the process restarted.
#
# DEPLOYMENT_PROFILE IS THE ONE EXCEPTION, and it is exempted deliberately. It is
# not a config value like an API key or a URL -- it is a process-level
# declaration of WHICH ENVIRONMENT THIS PROCESS IS, and the thing it gates is
# whether a capability qualification performed elsewhere may be reused. A `.env`
# sitting on a developer's disk must not be able to move a run into another
# profile behind an explicit `DEPLOYMENT_PROFILE=... python ...`.
#
# This was found the moment it mattered: `backend/conftest.py` pins the test
# suite to the `test` profile (whose stub provider has no credential, so an
# unstubbed boundary raises instead of reaching a paid API), and adding
# `DEPLOYMENT_PROFILE=local` to a real .env silently moved the whole suite into
# `local`.
_explicit_profile = os.environ.get("DEPLOYMENT_PROFILE")
load_dotenv(override=True)
if _explicit_profile is not None:
    os.environ["DEPLOYMENT_PROFILE"] = _explicit_profile

class ConfigError(RuntimeError):
    """Raised at import time when the environment is configured incorrectly."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Every credential-bearing field is declared ``repr=False``.

    Not cosmetic. ``repr(settings)`` used to print five live credentials --
    ``HF_TOKEN``, ``SERPAPI_KEY``, ``LLM_API_KEY``, ``EMBEDDING_API_KEY`` and
    ``AUTH_SECRET`` -- so anything that rendered this object carried them: a
    pytest failure report, a log line, an f-string in an exception message, a
    debugger session someone screenshots. It was found by a test failure whose
    output printed the real keys.

    ``workflow/redaction.py`` scrubs credentials at the persistence boundary; this
    keeps them out of the *object's own* string form, which redaction never sees.
    A new secret field must be declared the same way -- ``tests/test_secrets_do_not_leak.py``
    asserts it.
    """

    fuse_api_key: str = field(default=os.getenv("FUSE_API_KEY", ""), repr=False)
    hf_token: str = field(default=os.getenv("HF_TOKEN", ""), repr=False)
    serpapi_key: str = field(default=os.getenv("SERPAPI_KEY", ""), repr=False)
    epo_key: str = field(
        default=os.getenv("EPO_KEY", os.getenv("EPO_CONSUMER_KEY", "")), repr=False
    )
    epo_secret: str = field(
        default=os.getenv("EPO_SECRET", os.getenv("EPO_CONSUMER_SECRET", "")), repr=False
    )
    lens_api_key: str = field(default=os.getenv("LENS_API_KEY", ""), repr=False)
    """Bearer token for the Lens.org Patent API (api.lens.org), created from
    the Subscriptions tab of a Lens user profile. Currently a 14-day
    non-commercial trial token - see search/lens.py's module docstring."""
    llm_api_url: str = os.getenv("LLM_API_URL", "https://router.huggingface.co/v1/chat/completions")
    llm_model: str = os.getenv("LLM_MODEL", "meta-llama/Llama-3.1-8B-Instruct:novita")
    llm_api_key: str = field(default=os.getenv("LLM_API_KEY", os.getenv("HF_TOKEN", "")), repr=False)
    """Bearer token for ``llm_api_url``. Defaults to HF_TOKEN so the HuggingFace
    Router keeps working unchanged; set LLM_API_KEY separately when pointing
    llm_api_url at a different provider (e.g. an internal OpenAI-compatible
    endpoint) that needs its own credential."""
    uspto_api_key: str = field(default=os.getenv("PATENTSVIEW_API_KEY", ""), repr=False)
    qdrant_url: str = os.getenv("QDRANT_URL", "http://127.0.0.1:6333")
    qdrant_api_key: str = field(default=os.getenv("QDRANT_API_KEY", ""), repr=False)
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "BAAI/bge-large-en-v1.5")
    embedding_api_url: str = os.getenv("EMBEDDING_API_URL", "")
    """Hosted embedding endpoint. Required because there is no silent local or
    zero-vector fallback. ``embedding_api_format`` declares its wire protocol."""
    embedding_api_format: str = os.getenv("EMBEDDING_API_FORMAT", "openai").strip()
    """Embedding wire protocol: ``openai`` or ``huggingface``. Declared rather
    than inferred from the URL so changing hosts cannot silently change the
    request/response contract."""
    embedding_api_key: str = field(default=os.getenv("EMBEDDING_API_KEY", os.getenv("LLM_API_KEY", os.getenv("HF_TOKEN", ""))), repr=False)
    """Bearer token for ``embedding_api_url``. Defaults to LLM_API_KEY (falling
    further back to HF_TOKEN) since both calls typically go through the same
    gateway and key; set EMBEDDING_API_KEY separately only if the embeddings
    provider needs its own credential."""
    database_url: str = field(
        repr=False,
        default=os.getenv(
            "DATABASE_URL", "postgresql://agenticai:agenticai@127.0.0.1:5432/agenticai"
        ),
    )
    """PostgreSQL connection string. The default matches docker-compose.yml's
    `postgres` service, whose credentials are local-development-only and
    deliberately committed."""

    auth_secret: str = field(default=os.getenv("AUTH_SECRET", "novelty-lens-local-development-secret"), repr=False)
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

    # --- Durable workflow execution -------------------------------------
    #
    # These configure the *host process* and the lease protocol, not the
    # implementation: there is ONE executor, deployed two ways. See
    # backend/workflow/executor.py and CLAUDE.md's "Durable workflow execution".

    workflow_executor: str = os.getenv("WORKFLOW_EXECUTOR", "worker")
    """Which process hosts the executor loop: ``worker`` or ``inprocess``.

    ``worker`` is the production topology -- a second workload
    (``python -m workflow.worker``) alongside the API. ``inprocess`` runs the
    identical loop on a daemon thread inside the API process for local
    development; it is still lease-protected, still database-driven, and still
    recovers after a restart.

    **This is not the old PIPELINE flag pattern.** That flag chose between two
    parallel implementations. This chooses where one implementation runs."""

    workflow_graph_adapter: str = os.getenv("WORKFLOW_GRAPH_ADAPTER", "")
    """``module:attribute`` of the :class:`workflow.executor.GraphAdapter` the
    executor drives. Empty by default and deliberately without a fallback: an
    executor with no graph to run refuses to start rather than admitting runs it
    cannot advance. ``python -m workflow.worker --mock`` selects the built-in
    deterministic adapter instead, which makes no outbound HTTP calls."""

    lease_ttl_seconds: int = int(os.getenv("LEASE_TTL_SECONDS", "60"))
    """How long an acquired lease stays active. Validated positive at startup."""

    lease_heartbeat_seconds: int = int(os.getenv("LEASE_HEARTBEAT_SECONDS", "20"))
    """How often the heartbeat thread renews the lease. Must be strictly below
    ``lease_ttl_seconds``: a heartbeat interval at or above the TTL guarantees the
    lease lapses between beats and the worker fences itself mid-run."""

    workflow_poll_interval_seconds: float = float(
        os.getenv("WORKFLOW_POLL_INTERVAL_SECONDS", "2.0")
    )
    """How often the executor polls for a claimable run when it found none."""

    workflow_event_poll_ms: int = int(os.getenv("WORKFLOW_EVENT_POLL_MS", "500"))
    """How often an SSE reader polls ``workflow_events`` for new rows.
    Correctness is polling; LISTEN/NOTIFY may only ever be a latency
    optimisation on top of it."""

    sse_client_buffer: int = int(os.getenv("SSE_CLIENT_BUFFER", "256"))
    """Frames a single SSE connection may buffer before it is closed. Nothing is
    dropped -- every frame is still in ``workflow_events`` and the client
    reconnects with its last id."""

    sse_write_timeout_seconds: int = int(os.getenv("SSE_WRITE_TIMEOUT_SECONDS", "30"))
    """How long a stalled SSE client may block a write before it is disconnected.
    A slow or vanished client never stalls, cancels, pauses or fails the run."""

    workflow_event_retention_days: int = int(
        os.getenv("WORKFLOW_EVENT_RETENTION_DAYS", "30")
    )
    """How long a finished run's ``progress`` and ``verification_progress`` rows
    are kept by ``python -m workflow.cleanup``. Lifecycle events -- review
    required, paused, resumed, and the terminal -- are kept for as long as the run
    row exists, whatever this says."""

    # --- Evidence-layer policy (Implementation Gate B) -------------------
    #
    # These encode rules with LEGAL consequences, so neither has a working
    # default and neither may be chosen by whoever writes the code. Admission
    # refuses a run whose policy is unset or whose decision is unrecorded, which
    # is the same shape as VERIFIER_GENERIC_JSON_DECISION below: one pattern for
    # "a decision a human must record before the machine may act".

    date_screening_policy: str = os.getenv("DATE_SCREENING_POLICY", "")
    """Which date screens prior art, and where the cutoff edge sits.

    ``publication_lt_cutoff``  - the evaluated publication's own publication
                                 date, STRICTLY BEFORE the cutoff.
    ``publication_lte_cutoff`` - the same date, on or before the cutoff.

    No default. Which date governs and whether the cutoff is inclusive are legal
    questions about what counts as prior art, not engineering preferences, and a
    default here would answer them on the user's behalf without anyone noticing.
    See backend/evidence/dates.py."""

    date_screening_decision: str = os.getenv("DATE_SCREENING_DECISION", "")
    """The recorded human decision behind ``date_screening_policy``: owner, date,
    and rationale. Free text, because a config file cannot check any of that -
    what it CAN do is refuse to run until somebody writes it down.

    Empty while a policy is set is a policy nobody approved, and admission
    refuses it."""

    text_version_policy: str = os.getenv("TEXT_VERSION_POLICY", "")
    """What a passage's ``text_version_date`` may be when EPO supplies no
    document version date.

    ``provider_publication_date`` - the served document's publication date from
                                    EPO biblio, recorded as
                                    PROVIDER_PUBLICATION_DATE. Provider metadata,
                                    and honest that it identifies the version only
                                    as coarsely as the publication does.
    ``exclude``                   - no trustworthy version means no passage:
                                    EXCLUDED_INELIGIBLE for that publication.

    **Retrieval time is never an option.** ``retrieved_at`` says when we fetched,
    not which version we fetched, and substituting it would date a stored verdict
    against a document version nobody can identify."""

    text_version_decision: str = os.getenv("TEXT_VERSION_DECISION", "")
    """The recorded human decision behind ``text_version_policy``. Same shape and
    same reason as ``date_screening_decision``."""

    verifier_structured_output: str = os.getenv("VERIFIER_STRUCTURED_OUTPUT", "json_schema")
    """Output mode for the feature-disclosure verifier: ``json_schema`` or ``json_object``.

    Spec section 14.1 makes JSON Schema-constrained generation an **eligibility
    requirement**, not a preference, so this defaults to ``json_schema`` and a
    model or adapter that cannot serve it fails the stage with a capability error
    rather than degrading. There is no prompt-only value and no automatic
    fallback: `json_object` guarantees only syntactically valid JSON - it does not
    enforce required fields, does not close enums, and does not reject additional
    properties.

    Section 14.2.2 point 3 does allow the team to accept generic-JSON mode plus
    full application-side rejection for the MVP. That is a genuine decision with a
    real cost - under `json_object` every schema violation becomes a rejected
    response, a failed cell, and a spent gateway call - so selecting it here also
    requires ``VERIFIER_GENERIC_JSON_DECISION`` below. It must never be settled
    silently at runtime."""

    verifier_capability_enforcement: str = os.getenv(
        "VERIFIER_CAPABILITY_ENFORCEMENT", "required"
    )
    """Whether a verified run requires an **eligible** §14.2.1 capability artifact.

    ``required`` (the default) is the specification's position: §14.1 makes
    JSON Schema-constrained generation an eligibility requirement, and admitting a
    run without one spends quota producing verdicts nobody can stand behind.

    ``deferred`` admits the run anyway, and is **not a bypass**. The difference is
    the whole point:

    * a bypass proceeds *as though qualified*, and nothing downstream can tell;
    * a deferral proceeds while **recording that this runtime is not qualified**,
      carries that into the run and into the report as a stated limitation, and
      still enforces every one of the eighteen §9 checks on every response.

    It requires :attr:`verifier_capability_deferral_decision` to be non-empty --
    the same shape as every other owner decision in this file -- so "we chose to
    run unqualified" is a recorded sentence with a name and a date on it rather
    than an environment variable somebody set once.

    **Nothing here relaxes validation.** A model whose output does not conform
    still fails the cell with the failing check named, and the run keeps its
    state. That is the behaviour a deferral relies on: it is safe to run
    unqualified precisely because an incompatible response fails loudly instead
    of being repaired.

    Any other value raises at admission rather than being treated as one of the
    two -- a typo must not silently become the permissive reading.
    """

    verifier_capability_deferral_decision: str = os.getenv(
        "VERIFIER_CAPABILITY_DEFERRAL_DECISION", ""
    )
    """Who deferred capability qualification, when, and why.

    Required whenever :attr:`verifier_capability_enforcement` is ``deferred``.
    Same reason as ``date_screening_decision``: a policy chosen with nobody
    recorded as having chosen it is indistinguishable from a default nobody
    noticed.
    """

    verifier_generic_json_decision: str = os.getenv("VERIFIER_GENERIC_JSON_DECISION", "")
    """The recorded decision permitting ``verifier_structured_output=json_object``.

    Spec section 14.2.2 point 4: record the owner, the date, the model and adapter
    revisions it applies to, and the measured malformed-response rate on the
    section 15 regression suite. Free text, because a config file cannot check any
    of that - what it *can* do is refuse to run in the weaker mode until somebody
    writes the decision down, which is the difference between an accepted
    trade-off and a silent downgrade.

    Empty by default. Leaving it empty while asking for ``json_object`` is a
    capability error, not a warning."""


settings = Settings()


def validate_embedding_configuration(api_format: str) -> None:
    """Refuse an unknown embedding protocol before the first paid request."""
    if api_format not in {"openai", "huggingface"}:
        raise ConfigError(
            "EMBEDDING_API_FORMAT must be 'openai' or 'huggingface', got "
            f"{api_format!r}"
        )


validate_embedding_configuration(settings.embedding_api_format)


def validate_lease_configuration(ttl_seconds: float, heartbeat_seconds: float) -> None:
    """Refuse a lease configuration that cannot work, rather than defaulting it.

    Three rules, and the third is the one that bites silently:

    * ``LEASE_TTL_SECONDS`` must be positive -- a non-positive TTL produces a
      lease that is expired the instant it is taken.
    * ``LEASE_HEARTBEAT_SECONDS`` must be positive.
    * The heartbeat must be **strictly below** the TTL. At or above it, the lease
      lapses between beats and the worker fences itself mid-run, which presents as
      a run that mysteriously stalls and is reclaimed by a replacement.

    An invalid value **stops the worker from starting**. It is never silently
    replaced with a default, because a default that quietly overrides a deliberate
    setting is how a misconfiguration survives a deploy unnoticed. The SQL
    functions validate the TTL again at their own boundary
    (``invalid_parameter_value``), because a function is not entitled to trust its
    caller.

    Raises:
        ConfigError: naming the offending setting and its value.
    """
    if ttl_seconds <= 0:
        raise ConfigError(
            f"LEASE_TTL_SECONDS must be positive, got {ttl_seconds!r}. A non-positive "
            "TTL produces a lease that is expired the moment it is acquired."
        )
    if heartbeat_seconds <= 0:
        raise ConfigError(
            f"LEASE_HEARTBEAT_SECONDS must be positive, got {heartbeat_seconds!r}."
        )
    if heartbeat_seconds >= ttl_seconds:
        raise ConfigError(
            f"LEASE_HEARTBEAT_SECONDS ({heartbeat_seconds!r}) must be strictly below "
            f"LEASE_TTL_SECONDS ({ttl_seconds!r}). At or above it the lease lapses "
            "between beats and the worker fences itself mid-run."
        )


validate_lease_configuration(settings.lease_ttl_seconds, settings.lease_heartbeat_seconds)
"""Checked at import, so a misconfigured deployment fails at startup rather than
at the first lease acquisition -- which would be minutes into a paid run."""


# --- Deployment profile (amendment 2026-08-28) -----------------------------
#
# The profile decides which LLM provider is live and which capability
# qualifications may be reused. It is DECLARED, never inferred from a URL, a
# hostname, a key or a model name -- see backend/deployment.py for why that
# distinction is load-bearing rather than fussy.
#
# Resolved at import so an unknown, missing, or internally inconsistent profile
# stops the process here instead of at the first LLM call, which on this codebase
# is minutes into a run that has already spent SerpAPI quota.

from deployment import resolve_target as _resolve_target  # noqa: E402

llm_target = _resolve_target(settings)
"""The active LLM target: profile, provider, sanitized endpoint and model.

Carries no credential by construction. `deployment.require_llm_credentials()`
fetches that, on the call path rather than here, so `migrate.py` and the test
suite keep working without one.
"""
