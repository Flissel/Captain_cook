"""Configuration for LLM-backed workflows.

Values come from the process environment, falling back to a `.env` in the
working directory. Provider selection lives in `config.llm_provider`; the
module-level aliases below stay for existing callers.
"""
import os
from pathlib import Path

from dotenv import dotenv_values

from config.llm_provider import ProviderProfile, resolve_provider

_LOCAL_VALUES = dotenv_values(Path.cwd() / ".env")

PROFILE: ProviderProfile = resolve_provider({**_LOCAL_VALUES, **os.environ})
API_KEY = PROFILE.api_key
BASE_URL = PROFILE.base_url
MODEL = PROFILE.model
