"""Factories for building `autogen_core.models.ChatCompletionClient`
instances.

Two entry points:

- `build_model_client`: a real OpenAI-backed client
  (`autogen_ext.models.openai.OpenAIChatCompletionClient`), defaulting
  provider, endpoint, key and model from `config.llm_config.PROFILE` so
  callers don't have to duplicate that env-var wiring. SDK-level
  retries are disabled so the Captain stage wrapper remains the sole retry
  owner.
- `build_replay_model_client`: a deterministic, offline client
  (`autogen_ext.models.replay.ReplayChatCompletionClient`) for tests — it
  replays a fixed list of responses instead of calling any API, and never
  needs network access or an API key.
"""
from typing import List, Optional

from autogen_core.models import ChatCompletionClient, ModelFamily, ModelInfo
from autogen_ext.models.openai import OpenAIChatCompletionClient
from autogen_ext.models.replay import ReplayChatCompletionClient

from config.llm_config import PROFILE
from config.llm_provider import (
    CUSTOM_ENDPOINT_PLACEHOLDER_KEY,
    ProviderConfigurationError,
    ProviderProfile,
)


def build_model_client(
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    *,
    profile: Optional[ProviderProfile] = None,
) -> ChatCompletionClient:
    """Build a real `ChatCompletionClient` for the configured provider.

    Defaults come from `config.llm_config.PROFILE` (Nebius Token Factory with
    Nemotron unless the environment opts into OpenAI or a custom endpoint).
    Explicit arguments override the profile. SDK retries stay disabled so the
    Captain stage wrapper remains the sole retry owner.
    """
    active = profile if profile is not None else PROFILE
    resolved_model = model if model is not None else active.model
    resolved_api_key = api_key if api_key is not None else active.api_key
    resolved_base_url = base_url if base_url is not None else active.base_url

    if active.name == "nebius" and resolved_api_key is None:
        raise ProviderConfigurationError(
            "NEBIUS_API_KEY is not configured for the Nebius Token Factory provider"
        )

    kwargs: dict[str, object] = {
        "model": resolved_model,
        "max_retries": 0,
        "parallel_tool_calls": False,
    }
    if resolved_api_key is not None:
        kwargs["api_key"] = resolved_api_key

    # A configured endpoint is passed explicitly rather than left to the SDK's
    # OPENAI_BASE_URL lookup, so which path this client takes is visible here.
    if resolved_base_url:
        kwargs["base_url"] = resolved_base_url
        kwargs.setdefault("api_key", CUSTOM_ENDPOINT_PLACEHOLDER_KEY)
        # Non-OpenAI model names are unknown to the SDK, which refuses to
        # construct without declared capabilities. They come from the profile
        # so they match what the endpoint verifiably does.
        kwargs["model_info"] = ModelInfo(
            vision=False,
            function_calling=active.tool_calling,
            json_output=True,
            family=ModelFamily.UNKNOWN,
            structured_output=active.structured_output,
        )

    return OpenAIChatCompletionClient(**kwargs)


def build_replay_model_client(responses: List[str]) -> ChatCompletionClient:
    """Build a deterministic, offline `ChatCompletionClient` for tests.

    Replays `responses` in order, one per `create()`/`create_stream()` call
    (see `autogen_ext.models.replay.ReplayChatCompletionClient`). Advertises
    `json_output=True`/`structured_output=True` model info so it is usable
    both as an `AssistantAgent(..., output_content_type=...)` backend and
    for direct `client.create(..., json_output=SomeModel)` calls — the two
    patterns used by `agenten.llm.decompose` and `agenten.llm.judge`
    respectively.
    """
    return ReplayChatCompletionClient(
        responses,
        model_info=ModelInfo(
            vision=False,
            function_calling=False,
            json_output=True,
            family=ModelFamily.UNKNOWN,
            structured_output=True,
        ),
    )
