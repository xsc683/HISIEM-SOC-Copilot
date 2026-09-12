"""Typed application settings — the single configuration entry point.

Per ``python-package-boundary.md``:
- ``config.py`` is the only configuration entry point.
- typed settings split into the documented config domains.
- Secrets never enter Graph State, Domain Events, Commands, Tool Result or logs.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .domain.knowledge.value_objects import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_OVERLAP_TOKENS,
    DEFAULT_TARGET_TOKENS,
    MAX_TOKENS_CEILING,
    ChunkerProfile,
)


class DatabaseSettings(BaseSettings):
    """Copilot-owned ``copilot`` schema persistence (SQLAlchemy Async)."""

    model_config = SettingsConfigDict(
        env_prefix="COPILOT_",
        env_file=".env",
        extra="ignore",
    )

    database_url: str = Field(
        # Local-dev contract: 5433 is Copilot's PostgreSQL (5432 is HISIEM's).
        default="postgresql+psycopg://copilot:copilot@127.0.0.1:5433/copilot"
    )


class LangGraphSettings(BaseSettings):
    """LangGraph-owned ``langgraph_checkpoint`` schema persistence."""

    model_config = SettingsConfigDict(
        env_prefix="LANGGRAPH_",
        env_file=".env",
        extra="ignore",
    )

    database_url: str = Field(
        # Local-dev contract: 5433 is Copilot's PostgreSQL (5432 is HISIEM's).
        default="postgresql+psycopg://copilot:copilot@127.0.0.1:5433/copilot"
    )
    schema_name: str = Field(
        default="langgraph_checkpoint", validation_alias="schema"
    )


class HisiemSettings(BaseSettings):
    """HISIEM platform read/authority access."""

    model_config = SettingsConfigDict(
        env_prefix="HISIEM_",
        env_file=".env",
        extra="ignore",
    )

    base_url: str = Field(default="http://127.0.0.1:8080")
    bearer_token: str = Field(default="")
    timeout_seconds: float = Field(default=10.0)
    # Default tenant used when no authenticated tenant context is available.
    # Runtime TenantContext must normally come from the authenticated HISIEM caller.
    tenant_header: str = Field(default="X-Tenant-ID")


class LLMSettings(BaseSettings):
    """Model provider settings.

    V1 default is ``scripted`` (deterministic fake, no network) so the graph and
    tests run offline. Set ``llm.provider = openai_compatible`` (plus a CMD_API_KEY
    in the environment) to instantiate the real OpenAI-compatible Command Code
    adapter. Secrets are read ONLY from the environment variable named by
    ``api_key_env`` — never from config defaults, git, prompts, domain/checkpoint
    state, logs, or telemetry.
    """

    model_config = SettingsConfigDict(
        env_prefix="LLM_",
        env_file=".env",
        extra="ignore",
    )

    provider: Literal["scripted", "openai_compatible"] = "scripted"
    model: str = Field(default="deepseek/deepseek-v4-flash")
    base_url: str = Field(default="https://api.commandcode.ai/provider/v1")
    # Name of the environment variable holding the API key. The secret itself is
    # never a config default.
    api_key_env: str = Field(default="CMD_API_KEY")
    timeout_seconds: float = Field(default=60.0)
    max_retries: int = Field(default=2, ge=0)
    # Command Code data-residency/zero-data-retention flag → ``x-cmd-zdr: 1``.
    zdr: bool = Field(default=True)
    # structured-output strategy: auto (probe json_schema → json_object → JSON-only),
    # or pin json_schema / json_object / json_only.
    structured_output_mode: str = Field(default="auto")


class AgentBudgetSettings(BaseSettings):
    """Agent autonomy bounds (execution steps, tool calls, tokens)."""

    model_config = SettingsConfigDict(
        env_prefix="AGENT_",
        env_file=".env",
        extra="ignore",
    )

    max_steps: int = Field(default=20, ge=1)
    max_tool_calls: int = Field(default=30, ge=1)
    max_tool_calls_per_step: int = Field(default=4, ge=1)
    max_llm_calls: int = Field(default=20, ge=1)
    # Token ceiling is reserved for a real provider's accounting; the deterministic
    # ceiling enforced at runtime is max_llm_calls (no token metering exists yet).
    max_llm_tokens: int = Field(default=20_000, ge=1)
    max_duration_seconds: int = Field(default=600, ge=1)


class ObservabilitySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OBS_",
        env_file=".env",
        extra="ignore",
    )

    service_name: str = "hisiem-soc-copilot"
    tracing_enabled: bool = False


class ApplicationSettings(BaseSettings):
    """Application runtime settings (API transport)."""

    model_config = SettingsConfigDict(
        env_prefix="COPILOT_APP_",
        env_file=".env",
        extra="ignore",
    )

    debug: bool = False
    # Start command still validates tenant/alert against authoritative HISIEM.
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    # Run the durable outbox dispatcher worker in-process. Disabled by default so
    # tests never start a rogue background worker; a deployment enables it.
    enable_dispatcher: bool = False
    # Run the durable RESPONSE-execution worker in-process (submit approved
    # actions through the SOAR port). Separate flag so a deployment can run graph
    # dispatch without the response worker (and tests stay deterministic).
    enable_response_worker: bool = False
    # Reconciliation cadence for a SUBMITTED SOAR execution. A non-terminal
    # execution is re-observed on this interval by DURABLE scheduling (a future
    # ``available_at``), never by a retry exception, so a playbook may legitimately
    # run for minutes or hours without exhausting the outbox attempt budget. Tests
    # set it to 0 to drive reconciliation deterministically.
    response_observe_interval_seconds: float = 15.0


class SoarSettings(BaseSettings):
    """Copilot → HISIEM SOAR execution boundary (server-to-server).

    The bearer credential is a server-only secret read from the environment
    (``SOAR_BEARER_TOKEN``); it is never a config default, never logged, and never
    persisted on a proposal/execution row. The adapter fails closed when it is
    blank (a real provider is never built without credentials).
    """

    model_config = SettingsConfigDict(
        env_prefix="SOAR_",
        env_file=".env",
        extra="ignore",
    )

    base_url: str = Field(default="http://127.0.0.1:8080")
    bearer_token: str = Field(default="")
    timeout_seconds: float = Field(default=10.0)


class AuthSettings(BaseSettings):
    """Trusted-context provider selection.

    ``hisiem_bearer`` is the integrated/production boundary: it authenticates the
    HISIEM service caller with a shared server-only bearer credential before
    trusting the tenant/actor HISIEM asserts. ``header`` is a development/test
    adapter only and must not be selected for a production/integrated runtime;
    ``none`` (default) fails closed — no request can be trusted.
    """

    model_config = SettingsConfigDict(
        env_prefix="COPILOT_AUTH_",
        env_file=".env",
        extra="ignore",
    )

    trusted_context_provider: Literal["none", "header", "hisiem_bearer"] = "none"
    # Name of the environment variable holding the HISIEM→Copilot service
    # credential. The secret itself is never a config default; it is resolved from
    # the environment at runtime (the same pattern as ``llm.api_key_env``).
    hisiem_service_token_env: str = Field(default="HISIEM_COPILOT_SERVICE_TOKEN")


class EvaluationSettings(BaseSettings):
    """GP-01 evaluation dataset materializer settings (E1-B.3/E1-B.4).

    Safe defaults only — never secrets. The HISIEM control surface (base_url /
    bearer token) is reused from :class:`HisiemSettings`; these settings only
    cover the SSH TCP injection target and the local run-artifact directory.
    """

    model_config = SettingsConfigDict(
        env_prefix="EVAL_",
        env_file=".env",
        extra="ignore",
    )

    # The SSH TCP syslog input the materializer writes to (E1-B.3 §2).
    ssh_tcp_host: str = "127.0.0.1"
    ssh_tcp_port: int = 5007
    # Evaluation tenant id (the materializer resolves within this tenant).
    tenant_id: str = "default"
    # Directory holding mutable materialization.json + sealed manifest.json.
    runs_dir: str = ".eval-runs"
    # Directory holding Evaluation Execution Records (.eval-executions/gp-01/...).
    executions_dir: str = ".eval-executions"
    # Bounded resolution deadline for event/alert polling (seconds).
    resolve_deadline_seconds: int = 300
    # Poll interval while waiting for events/alerts to appear (seconds).
    poll_interval: float = 2.0


class KnowledgeSettings(BaseSettings):
    """Knowledge ingestion + retrieval bounds (brief sections 22, 25, 50, 75).

    Every bound here is a REJECTION threshold, never a truncation budget: an
    oversized document is refused rather than silently shortened, because a
    shortened version's content hash would no longer describe what the operator
    supplied.
    """

    model_config = SettingsConfigDict(
        env_prefix="KNOWLEDGE_",
        env_file=".env",
        extra="ignore",
    )

    max_document_bytes: int = Field(default=2_000_000, ge=1)
    max_normalized_chars: int = Field(default=2_000_000, ge=1)
    # The canonical name is the derived ``KNOWLEDGE_MAX_CHUNKS_PER_DOCUMENT``; the
    # shorter operator spelling is accepted as well so the variable an operator
    # reaches for out of the brief actually binds. Both are full names because a
    # ``validation_alias`` opts the field out of ``env_prefix``.
    max_chunks_per_document: int = Field(
        default=512,
        ge=1,
        validation_alias=AliasChoices(
            "KNOWLEDGE_MAX_CHUNKS_PER_DOCUMENT", "KNOWLEDGE_MAX_CHUNKS"
        ),
    )
    max_chunk_chars: int = Field(default=8_000, ge=1)

    # The FROZEN chunker configuration. Changing these without changing
    # ``chunker_version`` would silently rewrite the retrieval projection of
    # content that did not change, so the version travels with every chunk.
    chunk_target_tokens: int = Field(
        default=DEFAULT_TARGET_TOKENS,
        ge=1,
        validation_alias=AliasChoices(
            "KNOWLEDGE_CHUNK_TARGET_TOKENS", "KNOWLEDGE_CHUNK_TARGET"
        ),
    )
    chunk_max_tokens: int = Field(
        default=DEFAULT_MAX_TOKENS,
        ge=1,
        validation_alias=AliasChoices(
            "KNOWLEDGE_CHUNK_MAX_TOKENS", "KNOWLEDGE_CHUNK_MAX"
        ),
    )
    chunk_overlap_tokens: int = Field(
        default=DEFAULT_OVERLAP_TOKENS,
        ge=0,
        validation_alias=AliasChoices(
            "KNOWLEDGE_CHUNK_OVERLAP_TOKENS", "KNOWLEDGE_CHUNK_OVERLAP"
        ),
    )

    # Retrieval profile (brief section 50). The defaults ARE the frozen profile
    # ``hybrid-v1``; changing them changes every ranking, which is why they are
    # recorded on every result.
    lexical_candidate_limit: int = Field(default=20, ge=1)
    vector_candidate_limit: int = Field(default=20, ge=1)
    rrf_k: int = Field(default=60, ge=1)
    max_hits_per_document: int = Field(default=2, ge=1)

    #: Where evaluation artifacts are written (brief section 58).
    evaluation_output_dir: str = Field(default=".eval-runs/knowledge")

    @model_validator(mode="after")
    def _validate_chunker_profile(self) -> KnowledgeSettings:
        # Validate the cross-field chunker rules by building the domain value
        # object, so the domain stays the single source of truth for what a legal
        # chunker configuration is.
        try:
            ChunkerProfile(
                target_tokens=self.chunk_target_tokens,
                max_tokens=self.chunk_max_tokens,
                overlap_tokens=self.chunk_overlap_tokens,
            )
        except Exception as exc:  # noqa: BLE001 - re-raised as a config error
            raise ValueError(f"invalid chunker configuration: {exc}") from exc
        if self.chunk_max_tokens > MAX_TOKENS_CEILING:
            raise ValueError(
                f"chunk_max_tokens must be <= {MAX_TOKENS_CEILING}"
            )
        return self

    def chunker_profile(self) -> ChunkerProfile:
        """The frozen chunker profile this configuration describes."""
        return ChunkerProfile(
            target_tokens=self.chunk_target_tokens,
            max_tokens=self.chunk_max_tokens,
            overlap_tokens=self.chunk_overlap_tokens,
        )


class EmbeddingSettings(BaseSettings):
    """Embedding provider configuration, INDEPENDENT of the chat LLM (section 18).

    Deliberately separate from :class:`LLMSettings`: the model that reasons about
    an investigation and the model that produces retrieval vectors are different
    services with different contracts, and assuming the chat endpoint can embed
    is exactly the mistake this separation prevents.

    Defaults to ``unconfigured``. That is the honest default: without a real
    provider there is no vector retrieval, and the system says so instead of
    substituting fake vectors (brief section 87).
    """

    model_config = SettingsConfigDict(
        env_prefix="EMBEDDING_",
        env_file=".env",
        extra="ignore",
    )

    provider: Literal["unconfigured", "openai_compatible"] = "unconfigured"
    base_url: str = Field(default="")
    model: str = Field(default="")
    # 0 means "not configured"; the provider must declare a real dimension.
    dimension: int = Field(default=0, ge=0, le=8192)
    # Name of the environment variable holding the API key. The secret itself is
    # never a config default, never logged, never echoed by ``doctor``.
    api_key_env: str = Field(default="EMBEDDING_API_KEY")
    normalization: Literal["NONE", "L2"] = Field(default="NONE")
    distance_metric: Literal["COSINE"] = Field(default="COSINE")
    profile_version: int = Field(default=1, ge=1)
    timeout_seconds: float = Field(default=30.0, gt=0)
    max_retries: int = Field(default=2, ge=0)

    @property
    def is_configured(self) -> bool:
        return (
            self.provider != "unconfigured"
            and bool(self.base_url.strip())
            and bool(self.model.strip())
            and self.dimension > 0
        )


class Settings(BaseSettings):
    """Aggregate settings root for Composition Root wiring."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    langgraph: LangGraphSettings = Field(default_factory=LangGraphSettings)
    hisiem: HisiemSettings = Field(default_factory=HisiemSettings)
    soar: SoarSettings = Field(default_factory=SoarSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    agent_budget: AgentBudgetSettings = Field(default_factory=AgentBudgetSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    evaluation: EvaluationSettings = Field(default_factory=EvaluationSettings)
    knowledge: KnowledgeSettings = Field(default_factory=KnowledgeSettings)
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    app: ApplicationSettings = Field(default_factory=ApplicationSettings)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
