from typing import Any

import pytest

from agenten.evaluation import cli as evaluation_cli
from config.llm_provider import NEBIUS_BASE_URL, ProviderConfigurationError, ProviderProfile

_NEBIUS = ProviderProfile("nebius", "nvidia/Nemotron-3-Super-120b-a12b", NEBIUS_BASE_URL, "nb-test", True, True)
_OPENAI = ProviderProfile("openai", "gpt-5.6", None, "sk-test", True, True)


def _capture(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        evaluation_cli, "OpenAIChatCompletionClient", lambda **kw: captured.update(kw) or object()
    )
    return captured


def test_nebius_evaluation_client_uses_token_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture(monkeypatch)

    evaluation_cli.build_evaluation_model_client(profile=_NEBIUS)

    assert captured["base_url"] == NEBIUS_BASE_URL
    assert captured["api_key"] == "nb-test"
    assert captured["model"] == "nvidia/Nemotron-3-Super-120b-a12b"
    assert captured["model_info"]["function_calling"] is True
    assert "reasoning_effort" not in captured  # OpenAI-only parameter


def test_openai_evaluation_client_keeps_gpt5_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture(monkeypatch)

    evaluation_cli.build_evaluation_model_client(profile=_OPENAI)

    assert "base_url" not in captured
    assert captured["reasoning_effort"] == "none"
    assert captured["model_info"]["family"] == "gpt-5"


def test_model_argument_overrides_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture(monkeypatch)

    evaluation_cli.build_evaluation_model_client(profile=_NEBIUS, model="nvidia/other")

    assert captured["model"] == "nvidia/other"


def test_evaluation_requires_a_tool_capable_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture(monkeypatch)
    shim = ProviderProfile("custom", "claude-code", "http://127.0.0.1:8114/v1", None, False, True)

    with pytest.raises(ProviderConfigurationError, match="tool calling"):
        evaluation_cli.build_evaluation_model_client(profile=shim)


def test_nebius_evaluation_without_key_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture(monkeypatch)
    keyless = ProviderProfile("nebius", "nvidia/x", NEBIUS_BASE_URL, None, True, True)

    with pytest.raises(ProviderConfigurationError, match="NEBIUS_API_KEY"):
        evaluation_cli.build_evaluation_model_client(profile=keyless)
