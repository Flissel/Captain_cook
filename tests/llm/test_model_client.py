from typing import Any

import pytest

from agenten.llm import model_client as model_client_module


def test_openai_client_disables_sdk_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    sentinel = object()

    def build_stub(**kwargs: Any) -> object:
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(
        model_client_module,
        "OpenAIChatCompletionClient",
        build_stub,
    )

    # The endpoint is read from config, which reads the repo .env, so this
    # case pins "nothing configured" rather than depending on the file.
    monkeypatch.setattr(model_client_module, "BASE_URL", None)

    result = model_client_module.build_model_client(
        api_key="test-api-key",
        model="test-model",
    )

    assert result is sentinel
    assert captured == {
        "model": "test-model",
        "api_key": "test-api-key",
        "max_retries": 0,
        "parallel_tool_calls": False,
    }


def test_configured_endpoint_is_passed_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured endpoint travels as an argument, not as ambient env."""

    captured: dict[str, Any] = {}

    def build_stub(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(
        model_client_module,
        "OpenAIChatCompletionClient",
        build_stub,
    )
    monkeypatch.setattr(
        model_client_module,
        "BASE_URL",
        "http://127.0.0.1:8114/v1",
    )
    monkeypatch.setattr(model_client_module, "API_KEY", None)

    model_client_module.build_model_client(model="claude-code")

    assert captured["base_url"] == "http://127.0.0.1:8114/v1"
    # Local shims ignore the key, but the SDK still requires one.
    assert captured["api_key"] == "captain-local-endpoint"
