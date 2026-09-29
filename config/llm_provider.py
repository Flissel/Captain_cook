"""Resolve which OpenAI-compatible LLM endpoint Captain talks to.

One place decides provider, endpoint, key, and model so no caller hard-codes
a vendor. Nebius Token Factory with an NVIDIA Nemotron model is the default;
OpenAI and a custom local endpoint (the Claude Code shim) are opt-in.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

ProviderName = Literal["nebius", "openai", "custom"]

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
    {"nebius": "NEBIUS_API_KEY", "openai": "OPENAI_API_KEY"}
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
        requested = "custom" if custom_url else "nebius"
    model = _value(environ, "CAPTAIN_MODEL")

    if requested in ("nebius", "openai") and custom_url:
        raise ProviderConfigurationError(
            "CAPTAIN_LLM_BASE_URL is only valid with CAPTAIN_LLM_PROVIDER=custom"
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
        f"unknown CAPTAIN_LLM_PROVIDER={requested!r}; use nebius, openai, or custom"
    )
