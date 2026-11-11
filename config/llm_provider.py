"""Resolve which LLM endpoint Captain talks to.

One place decides provider, endpoint, key, and model so no caller hard-codes
a vendor. The default is ``claude-code``: Anthropic models on the operator's
Claude subscription through the local OpenAI-compatible shim
(``scripts/claude_code_openai_shim.py``), which supports tool calling. The
``anthropic`` provider uses the metered Messages API with an API key. Nebius
Token Factory, OpenAI and any other OpenAI-compatible endpoint (``custom``)
stay available on request.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

ProviderName = Literal["claude-code", "anthropic", "nebius", "openai", "custom"]

CLAUDE_CODE_SHIM_BASE_URL = "http://127.0.0.1:8114/v1"
CLAUDE_CODE_DEFAULT_MODEL = "claude-code"
# The ids the shim maps to a model tier; anything else would silently fall
# back to the account default, so it is rejected here instead.
CLAUDE_CODE_MODELS = frozenset(
    {CLAUDE_CODE_DEFAULT_MODEL, "claude-code-opus", "claude-code-sonnet", "claude-code-haiku"}
)
ANTHROPIC_DEFAULT_MODEL = "claude-opus-5-5"
NEBIUS_BASE_URL = "https://api.tokenfactory.nebius.com/v1"
# Model ids and capability flags are pending confirmation by the S1 spike
# (GET /v1/models); the controller approved these values in the meantime.
NEMOTRON_DEFAULT_MODEL = "nvidia/Nemotron-3-Super-120b-a12b"
NEMOTRON_SMALL_MODEL = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"
NEMOTRON_TOOL_CALLING = True
NEMOTRON_STRUCTURED_OUTPUT = True
OPENAI_DEFAULT_MODEL = "gpt-5.6"
CUSTOM_ENDPOINT_PLACEHOLDER_KEY = "captain-local-endpoint"

PROVIDER_SECRET_NAMES: Mapping[str, str] = MappingProxyType(
    {
        "anthropic": "ANTHROPIC_API_KEY",
        "nebius": "NEBIUS_API_KEY",
        "openai": "OPENAI_API_KEY",
    }
)

_OPENAI_ONLY_MODEL_PREFIXES = ("gpt-", "o1", "o3", "o4")


class ProviderConfigurationError(ValueError):
    """The environment names an unusable or contradictory LLM provider."""


@dataclass(frozen=True)
class ProviderProfile:
    name: ProviderName
    model: str
    base_url: str | None
    api_key: str | None = field(repr=False)
    tool_calling: bool
    structured_output: bool


def _value(environ: Mapping[str, str | None], name: str) -> str | None:
    raw = environ.get(name)
    if not isinstance(raw, str):
        return None
    stripped = raw.strip()
    return stripped or None


def resolve_provider(environ: Mapping[str, str | None]) -> ProviderProfile:
    custom_url = _value(environ, "CAPTAIN_LLM_BASE_URL")
    requested = (_value(environ, "CAPTAIN_LLM_PROVIDER") or "").lower()
    if not requested:
        requested = "custom" if custom_url else "claude-code"
    model = _value(environ, "CAPTAIN_MODEL")

    if requested in ("anthropic", "nebius", "openai") and custom_url:
        raise ProviderConfigurationError(
            "CAPTAIN_LLM_BASE_URL is only valid with CAPTAIN_LLM_PROVIDER=claude-code or custom"
        )
    if requested == "claude-code":
        if model is not None and model not in CLAUDE_CODE_MODELS:
            raise ProviderConfigurationError(
                f"CAPTAIN_MODEL={model} is not a Claude Code shim model; use one of "
                + ", ".join(sorted(CLAUDE_CODE_MODELS))
            )
        # The shim runs on the subscription and ignores keys; none is read so
        # an ambient API key is never sent to it.
        return ProviderProfile(
            name="claude-code",
            model=model or CLAUDE_CODE_DEFAULT_MODEL,
            base_url=custom_url or CLAUDE_CODE_SHIM_BASE_URL,
            api_key=None,
            tool_calling=True,
            structured_output=True,
        )
    if requested == "anthropic":
        if model is not None and not model.lower().startswith("claude-"):
            raise ProviderConfigurationError(
                f"CAPTAIN_MODEL={model} is not an Anthropic model id"
            )
        return ProviderProfile(
            name="anthropic",
            model=model or ANTHROPIC_DEFAULT_MODEL,
            base_url=None,
            api_key=_value(environ, "ANTHROPIC_API_KEY"),
            tool_calling=True,
            # AutoGen 0.7.5's Anthropic client refuses typed (Pydantic)
            # structured output; JSON mode still works.
            structured_output=False,
        )
    if requested == "nebius":
        if model is not None and model.lower().startswith(_OPENAI_ONLY_MODEL_PREFIXES):
            raise ProviderConfigurationError(
                f"CAPTAIN_MODEL={model} is an OpenAI model; Nebius needs a Token Factory model id"
            )
        return ProviderProfile(
            name="nebius",
            model=model or NEMOTRON_DEFAULT_MODEL,
            base_url=NEBIUS_BASE_URL,
            api_key=_value(environ, "NEBIUS_API_KEY"),
            tool_calling=NEMOTRON_TOOL_CALLING,
            structured_output=NEMOTRON_STRUCTURED_OUTPUT,
        )
    if requested == "openai":
        return ProviderProfile(
            name="openai",
            model=model or OPENAI_DEFAULT_MODEL,
            base_url=None,
            api_key=_value(environ, "OPENAI_API_KEY"),
            tool_calling=True,
            structured_output=True,
        )
    if requested == "custom":
        if custom_url is None:
            raise ProviderConfigurationError(
                "CAPTAIN_LLM_PROVIDER=custom requires CAPTAIN_LLM_BASE_URL"
            )
        return ProviderProfile(
            name="custom",
            model=model or OPENAI_DEFAULT_MODEL,
            base_url=custom_url,
            api_key=_value(environ, "OPENAI_API_KEY"),
            tool_calling=False,
            structured_output=True,
        )
    raise ProviderConfigurationError(
        f"unknown CAPTAIN_LLM_PROVIDER={requested!r}; "
        "use claude-code, anthropic, nebius, openai, or custom"
    )
