"""Typed application settings — the single configuration entry point.

Per ``python-package-boundary.md``:
- ``config.py`` is the only configuration entry point.
- typed settings split into the documented config domains.
- Secrets never enter Graph State, Domain Events, Commands, Tool Result or logs.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    """Copilot-owned ``copilot`` schema persistence (SQLAlchemy Async)."""

    model_config = SettingsConfigDict(
        env_prefix="COPILOT_",
        env_file=".env",
        extra="ignore",
    )

    database_url: str = Field(
        default="postgresql+psycopg://copilot:copilot@127.0.0.1:5432/copilot"
    )


class LangGraphSettings(BaseSettings):
    """LangGraph-owned ``langgraph_checkpoint`` schema persistence."""

    model_config = SettingsConfigDict(
        env_prefix="LANGGRAPH_",
        env_file=".env",
        extra="ignore",
    )

    database_url: str = Field(
        default="postgresql+psycopg://copilot:copilot@127.0.0.1:5432/copilot"
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


class AuthSettings(BaseSettings):
    """Trusted-context provider selection.

    Production must wire a real authenticator. ``header`` is a development/test
    adapter only and must not be the production default; ``none`` (default) fails
    closed — no request can be trusted.
    """

    model_config = SettingsConfigDict(
        env_prefix="COPILOT_AUTH_",
        env_file=".env",
        extra="ignore",
    )

    trusted_context_provider: Literal["none", "header"] = "none"


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
    # Bounded resolution deadline for event/alert polling (seconds).
    resolve_deadline_seconds: int = 300
    # Poll interval while waiting for events/alerts to appear (seconds).
    poll_interval: float = 2.0


class Settings(BaseSettings):
    """Aggregate settings root for Composition Root wiring."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    langgraph: LangGraphSettings = Field(default_factory=LangGraphSettings)
    hisiem: HisiemSettings = Field(default_factory=HisiemSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    agent_budget: AgentBudgetSettings = Field(default_factory=AgentBudgetSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    evaluation: EvaluationSettings = Field(default_factory=EvaluationSettings)
    app: ApplicationSettings = Field(default_factory=ApplicationSettings)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
