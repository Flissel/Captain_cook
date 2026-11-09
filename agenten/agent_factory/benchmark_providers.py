"""Allowlisted model providers for Captain business benchmarks."""
from __future__ import annotations

from autogen_core.models import ChatCompletionClient, ModelFamily, ModelInfo
from autogen_ext.models.anthropic import AnthropicChatCompletionClient
from autogen_ext.models.openai import OpenAIChatCompletionClient

from agenten.llm.model_client import anthropic_model_info

from config.llm_provider import (
    NEBIUS_BASE_URL,
    NEMOTRON_STRUCTURED_OUTPUT,
    NEMOTRON_TOOL_CALLING,
    PROVIDER_SECRET_NAMES,
)

# Benchmarks are metered: every provider needs its own API key secret and a
# per-call cost ceiling, so the subscription shim (claude-code) is not listed.
BENCHMARK_PROVIDERS = frozenset({"anthropic", "nebius", "openai"})


def require_benchmark_provider(provider: str) -> str:
    if provider not in BENCHMARK_PROVIDERS:
        raise ValueError(
            "business benchmark provider must be one of: " + ", ".join(sorted(BENCHMARK_PROVIDERS))
        )
    return provider


def benchmark_provider_secret_name(provider: str) -> str:
    return PROVIDER_SECRET_NAMES[require_benchmark_provider(provider)]


def build_benchmark_model_client(*, provider: str, api_key: str, model: str) -> ChatCompletionClient:
    if require_benchmark_provider(provider) == "anthropic":
        return AnthropicChatCompletionClient(
            api_key=api_key,
            model=model,
            max_retries=0,
            temperature=0,
            model_info=anthropic_model_info(model),
        )
    kwargs: dict[str, object] = {
        "api_key": api_key,
        "model": model,
        "max_retries": 0,
        "temperature": 0,
    }
    if require_benchmark_provider(provider) == "nebius":
        kwargs["base_url"] = NEBIUS_BASE_URL
        kwargs["model_info"] = ModelInfo(
            vision=False,
            function_calling=NEMOTRON_TOOL_CALLING,
            json_output=True,
            family=ModelFamily.UNKNOWN,
            structured_output=NEMOTRON_STRUCTURED_OUTPUT,
        )
    return OpenAIChatCompletionClient(**kwargs)
