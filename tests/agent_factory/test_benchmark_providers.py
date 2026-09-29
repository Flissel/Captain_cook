from typing import Any

import pytest

from agenten.agent_factory import benchmark_providers
from agenten.agent_factory.benchmark_providers import (
    benchmark_provider_secret_name,
    build_benchmark_model_client,
    require_benchmark_provider,
)
from agenten.agent_factory.business_benchmark_production_ports import (
    OpenAIBusinessBenchmarkModelClientBuilder,
)
from config.llm_provider import NEBIUS_BASE_URL


def test_nebius_and_openai_are_allowlisted():
    assert require_benchmark_provider("nebius") == "nebius"
    assert require_benchmark_provider("openai") == "openai"


def test_other_providers_are_rejected():
    with pytest.raises(ValueError, match="nebius, openai"):
        require_benchmark_provider("anthropic")


def test_secret_names_follow_the_provider():
    assert benchmark_provider_secret_name("nebius") == "NEBIUS_API_KEY"
    assert benchmark_provider_secret_name("openai") == "OPENAI_API_KEY"


def test_nebius_benchmark_client_targets_token_factory(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        benchmark_providers, "OpenAIChatCompletionClient", lambda **kw: captured.update(kw) or object()
    )

    build_benchmark_model_client(provider="nebius", api_key="nb-test", model="nvidia/x")

    assert captured["base_url"] == NEBIUS_BASE_URL
    assert captured["temperature"] == 0
    assert captured["max_retries"] == 0
    assert captured["model_info"]["function_calling"] is True


def test_openai_benchmark_client_is_unchanged(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        benchmark_providers, "OpenAIChatCompletionClient", lambda **kw: captured.update(kw) or object()
    )

    build_benchmark_model_client(provider="openai", api_key="sk-test", model="gpt-5.6")

    assert captured == {"api_key": "sk-test", "model": "gpt-5.6", "max_retries": 0, "temperature": 0}


def test_builder_reads_the_nebius_secret():
    builder = OpenAIBusinessBenchmarkModelClientBuilder.from_environment(
        {"CAPTAIN_BENCHMARK_PROVIDER": "nebius", "CAPTAIN_BENCHMARK_MODEL": "nvidia/x", "NEBIUS_API_KEY": "nb-test"}
    )
    assert builder.provider == "nebius"


def test_builder_does_not_accept_the_openai_secret_for_nebius():
    with pytest.raises(ValueError, match="NEBIUS_API_KEY"):
        OpenAIBusinessBenchmarkModelClientBuilder.from_environment(
            {"CAPTAIN_BENCHMARK_PROVIDER": "nebius", "CAPTAIN_BENCHMARK_MODEL": "nvidia/x", "OPENAI_API_KEY": "sk-old"}
        )


def test_nebius_benchmark_capabilities_come_from_the_provider_config(
    monkeypatch: pytest.MonkeyPatch,
):
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        benchmark_providers, "OpenAIChatCompletionClient", lambda **kw: captured.update(kw) or object()
    )
    monkeypatch.setattr(benchmark_providers, "NEMOTRON_TOOL_CALLING", False)
    monkeypatch.setattr(benchmark_providers, "NEMOTRON_STRUCTURED_OUTPUT", False)

    build_benchmark_model_client(provider="nebius", api_key="nb-test", model="nvidia/x")

    assert captured["model_info"]["function_calling"] is False
    assert captured["model_info"]["structured_output"] is False
