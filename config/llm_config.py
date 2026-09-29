"""Configuration for LLM-backed workflows.

Values come from the process environment, falling back to a `.env` in the
working directory. Provider selection lives in `config.llm_provider`.

Resolution is lazy: importing this module never reads `.env` or validates the
provider, so a stale or contradictory `.env` cannot break the offline path.
A contradiction surfaces when a caller asks for the profile, e.g. while a real
model client is being built. The module-level aliases `PROFILE`, `API_KEY`,
`BASE_URL` and `MODEL` stay for existing callers and resolve on access.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import dotenv_values

from config.llm_provider import ProviderProfile, resolve_provider

def load_environment() -> dict[str, str | None]:
    """Merge the working directory's `.env` with the process environment.

    The process environment wins. Nothing is written back to `os.environ`.
    """
    return {**dotenv_values(Path.cwd() / ".env"), **os.environ}


@lru_cache(maxsize=1)
def get_profile() -> ProviderProfile:
    """Resolve and cache the configured provider profile.

    Raises `ProviderConfigurationError` for a contradictory configuration; a
    failed resolution is not cached.
    """
    return resolve_provider(load_environment())


def reset_profile_cache() -> None:
    """Forget the cached profile so the next access re-reads the environment."""
    get_profile.cache_clear()


def __getattr__(name: str) -> object:
    if name == "PROFILE":
        return get_profile()
    if name == "API_KEY":
        return get_profile().api_key
    if name == "BASE_URL":
        return get_profile().base_url
    if name == "MODEL":
        return get_profile().model
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
