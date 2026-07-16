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

    result = model_client_module.build_model_client(
        api_key="test-api-key",
        model="test-model",
    )

    assert result is sentinel
    assert captured == {
        "model": "test-model",
        "api_key": "test-api-key",
        "max_retries": 0,
    }
