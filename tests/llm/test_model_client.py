from typing import Any

import pytest

from agenten.llm import model_client as model_client_module
from config.llm_provider import (
    CUSTOM_ENDPOINT_PLACEHOLDER_KEY,
    NEBIUS_BASE_URL,
    ProviderConfigurationError,
    ProviderProfile,
)

_OPENAI = ProviderProfile("openai", "gpt-5.6", None, "sk-test", True, True)
_NEBIUS = ProviderProfile(
    "nebius", "nvidia/Nemotron-3-Super-120b-a12b", NEBIUS_BASE_URL, "nb-test", True, True
)
_SHIM = ProviderProfile("custom", "claude-code", "http://127.0.0.1:8114/v1", None, False, True)


def _capture(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def build_stub(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(model_client_module, "OpenAIChatCompletionClient", build_stub)
    return captured


def test_openai_client_disables_sdk_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture(monkeypatch)

    model_client_module.build_model_client(profile=_OPENAI)

    assert captured == {
        "model": "gpt-5.6",
        "api_key": "sk-test",
        "max_retries": 0,
        "parallel_tool_calls": False,
    }


def test_nebius_profile_targets_token_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture(monkeypatch)

    model_client_module.build_model_client(profile=_NEBIUS)

    assert captured["base_url"] == NEBIUS_BASE_URL
    assert captured["api_key"] == "nb-test"
    assert captured["model"] == "nvidia/Nemotron-3-Super-120b-a12b"
    assert captured["model_info"]["function_calling"] is True
    assert captured["model_info"]["structured_output"] is True


def test_nebius_without_key_fails_at_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture(monkeypatch)
    keyless = ProviderProfile("nebius", "nvidia/x", NEBIUS_BASE_URL, None, True, True)

    with pytest.raises(ProviderConfigurationError, match="NEBIUS_API_KEY"):
        model_client_module.build_model_client(profile=keyless)


def test_configured_endpoint_is_passed_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured endpoint travels as an argument, not as ambient env."""
    captured = _capture(monkeypatch)

    model_client_module.build_model_client(profile=_SHIM)

    assert captured["base_url"] == "http://127.0.0.1:8114/v1"
    # Local shims ignore the key, but the SDK still requires one.
    assert captured["api_key"] == CUSTOM_ENDPOINT_PLACEHOLDER_KEY
    assert captured["model_info"]["function_calling"] is False


def test_explicit_arguments_override_the_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture(monkeypatch)

    model_client_module.build_model_client(api_key="nb-other", model="nvidia/other", profile=_NEBIUS)

    assert captured["api_key"] == "nb-other"
    assert captured["model"] == "nvidia/other"


def test_default_profile_comes_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture(monkeypatch)
    monkeypatch.setattr(model_client_module, "PROFILE", _NEBIUS)

    model_client_module.build_model_client()

    assert captured["base_url"] == NEBIUS_BASE_URL
