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
    """Model provider settings (V1: no real provider calls yet)."""

    model_config = SettingsConfigDict(
        env_prefix="LLM_",
        env_file=".env",
        extra="ignore",
    )

    provider: Literal["openai", "compatible", "null"] = "null"
    model: str = Field(default="gpt-4o-mini")
    api_key: str = Field(default="")
    base_url: str | None = None
    timeout_seconds: float = Field(default=60.0)


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
    app: ApplicationSettings = Field(default_factory=ApplicationSettings)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
