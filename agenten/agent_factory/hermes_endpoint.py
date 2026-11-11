"""Translate Captain's Hermes endpoint settings into Hermes child-process env.

Hermes resolves a custom provider endpoint from `OPENAI_BASE_URL` and
`OPENAI_API_KEY` in its own process environment. Those names are provider
specific, so Captain carries the endpoint as `CAPTAIN_HERMES_BASE_URL` and this
module -- shared by every Hermes composition root -- computes the overrides for
the Hermes child only. It never mutates `os.environ`, so a Nebius key cannot
leak into later OpenAI clients or other child processes of the Captain process.

`hermes_provider_settings` is the single place that picks Hermes' provider and
model. Without any Hermes setting it selects the Claude Code subscription shim
(Hermes' `custom` provider, model `claude-code`).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from config.llm_provider import (
    CLAUDE_CODE_DEFAULT_MODEL,
    CLAUDE_CODE_SHIM_BASE_URL,
    NEBIUS_BASE_URL,
)

# Local OpenAI-compatible shims ignore the key, but the OpenAI client inside
# Hermes refuses to start without one.
HERMES_LOCAL_ENDPOINT_PLACEHOLDER_KEY = "captain-hermes-local-endpoint"


def _value(environ: Mapping[str, str], name: str) -> str:
    raw = environ.get(name, "")
    return raw.strip() if isinstance(raw, str) else ""


def hermes_endpoint_overrides(environ: Mapping[str, str]) -> Mapping[str, str]:
    """Return the Hermes child environment overrides for the configured endpoint.

    - no `CAPTAIN_HERMES_BASE_URL`: no overrides (Hermes keeps its own config);
    - the Nebius Token Factory URL: that URL plus `NEBIUS_API_KEY`, which is
      required (`ValueError` otherwise, naming the variable, never its value);
    - any other URL: that URL plus `CAPTAIN_HERMES_API_KEY` when configured,
      otherwise a placeholder. Ambient `OPENAI_API_KEY`/`NEBIUS_API_KEY`
      values are never forwarded to such an endpoint.
    """
    base_url = _value(environ, "CAPTAIN_HERMES_BASE_URL")
    if not base_url:
        return MappingProxyType({})
    if base_url.rstrip("/") == NEBIUS_BASE_URL:
        nebius_key = _value(environ, "NEBIUS_API_KEY")
        if not nebius_key:
            raise ValueError(
                "NEBIUS_API_KEY is required for the Token Factory Hermes endpoint"
            )
        api_key = nebius_key
    else:
        api_key = (
            _value(environ, "CAPTAIN_HERMES_API_KEY")
            or HERMES_LOCAL_ENDPOINT_PLACEHOLDER_KEY
        )
    return MappingProxyType({"OPENAI_BASE_URL": base_url, "OPENAI_API_KEY": api_key})


HERMES_DEFAULT_PROVIDER = "custom"
HERMES_DEFAULT_MODEL = CLAUDE_CODE_DEFAULT_MODEL


@dataclass(frozen=True)
class HermesProviderSettings:
    """Provider, model and child-only environment for one Hermes process."""

    provider: str | None
    model: str | None
    environment_overrides: Mapping[str, str] = field(repr=False)


def hermes_provider_settings(environ: Mapping[str, str]) -> HermesProviderSettings:
    """Pick Hermes' provider, model and child environment.

    - nothing configured: the Claude Code subscription shim through Hermes'
      `custom` provider with model `claude-code`;
    - otherwise the configured provider and model as given (a half-configured
      pair is left for `HermesCliSettings` to reject) plus the endpoint
      overrides above;
    - provider `anthropic`: `ANTHROPIC_API_KEY` is required and forwarded to
      the child, because Hermes subprocesses inherit no secrets.
    """

    provider = _value(environ, "CAPTAIN_HERMES_PROVIDER") or None
    model = _value(environ, "CAPTAIN_HERMES_MODEL") or None
    if provider is None and model is None and not _value(environ, "CAPTAIN_HERMES_BASE_URL"):
        return HermesProviderSettings(
            provider=HERMES_DEFAULT_PROVIDER,
            model=HERMES_DEFAULT_MODEL,
            environment_overrides=MappingProxyType(
                {
                    "OPENAI_BASE_URL": CLAUDE_CODE_SHIM_BASE_URL,
                    "OPENAI_API_KEY": HERMES_LOCAL_ENDPOINT_PLACEHOLDER_KEY,
                }
            ),
        )
    overrides = dict(hermes_endpoint_overrides(environ))
    if provider == "anthropic":
        anthropic_key = _value(environ, "ANTHROPIC_API_KEY")
        if not anthropic_key:
            raise ValueError("ANTHROPIC_API_KEY is required for the anthropic Hermes provider")
        overrides["ANTHROPIC_API_KEY"] = anthropic_key
    return HermesProviderSettings(
        provider=provider,
        model=model,
        environment_overrides=MappingProxyType(overrides),
    )
