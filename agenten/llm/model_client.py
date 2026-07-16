"""Factories for building `autogen_core.models.ChatCompletionClient`
instances.

Two entry points:

- `build_model_client`: a real OpenAI-backed client
  (`autogen_ext.models.openai.OpenAIChatCompletionClient`), defaulting
  `api_key`/`model` from the existing `config.llm_config` module (`API_KEY`,
  `MODEL`) so callers don't have to duplicate that env-var wiring. SDK-level
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

from config.llm_config import API_KEY, MODEL


def build_model_client(api_key: Optional[str] = None, model: Optional[str] = None) -> ChatCompletionClient:
    """Build a real OpenAI-backed `ChatCompletionClient`.

    `api_key`/`model` default to `config.llm_config.API_KEY`/`MODEL` (which
    in turn reads `OPENAI_API_KEY` from the environment) when not given
    explicitly. If the resolved api key is still `None`, it is simply
    omitted from the underlying client's kwargs rather than passed through
    as an explicit `None` — this lets the OpenAI SDK make its own attempt
    to read `OPENAI_API_KEY` from the environment rather than failing
    immediately at construction time on a redundant lookup.
    """
    resolved_model = model if model is not None else MODEL
    resolved_api_key = api_key if api_key is not None else API_KEY

    kwargs = {"model": resolved_model, "max_retries": 0}
    if resolved_api_key is not None:
        kwargs["api_key"] = resolved_api_key

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
