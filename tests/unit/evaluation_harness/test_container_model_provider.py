"""Composition-root provider construction (E1-C2 §3/§4) + public introspection.

``Container.model_provider()`` is the ONLY configuration-based provider
construction path; the durable runner reuses it when no model is injected. The
real provider exposes ONLY bounded public metadata (never the API key/prompt/
client internals). Tests build real providers (no network) against a local dummy
``CMD_API_KEY``.
"""

from __future__ import annotations

import pytest

from hisiem_soc_copilot.bootstrap.container import Container
from hisiem_soc_copilot.config import Settings
from hisiem_soc_copilot.contracts.llm.errors import ModelConfigurationError
from hisiem_soc_copilot.infrastructure.llm.openai_compatible import (
    OpenAICompatibleModelProvider,
)
from hisiem_soc_copilot.infrastructure.llm.scripted import ScriptedModelProvider


def test_model_provider_defaults_to_scripted() -> None:
    container = Container(Settings())
    provider = container.model_provider()
    assert isinstance(provider, ScriptedModelProvider)


def test_model_provider_builds_real_openai_provider_when_configured(monkeypatch) -> None:
    monkeypatch.setenv("CMD_API_KEY", "dummy-key-for-test-only")
    settings = Settings()
    settings.llm.provider = "openai_compatible"
    provider = Container(settings).model_provider()
    assert isinstance(provider, OpenAICompatibleModelProvider)
    assert provider.model_name == settings.llm.model
    assert provider.provider_name == "command_code"
    assert provider.protocol_name == "openai_compatible_chat_completions"
    assert provider.zdr_enabled is True
    assert provider.configured_structured_output_mode == settings.llm.structured_output_mode
    # Not yet resolved and no usage before any call.
    assert provider.resolved_structured_output_mode is None
    assert provider.usage_snapshot() == ()


def test_model_provider_missing_api_key_is_configuration_error(monkeypatch) -> None:
    monkeypatch.delenv("CMD_API_KEY", raising=False)
    settings = Settings()
    settings.llm.provider = "openai_compatible"
    with pytest.raises(ModelConfigurationError):
        Container(settings).model_provider()


def test_public_metadata_never_exposes_the_api_key(monkeypatch) -> None:
    monkeypatch.setenv("CMD_API_KEY", "super-secret-api-key-value")
    settings = Settings()
    settings.llm.provider = "openai_compatible"
    provider = Container(settings).model_provider()
    # Public surface only — no public attribute may name or carry the key.
    public = {name for name in dir(provider) if not name.startswith("_")}
    assert "api_key" not in public
    assert "usage_snapshot" in public
    assert "resolved_structured_output_mode" in public
    # The secret value never appears in the bounded public metadata.
    for attr in ("model_name", "provider_name", "protocol_name"):
        assert "super-secret-api-key-value" not in str(getattr(provider, attr))
