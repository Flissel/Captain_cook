"""Configuration for LLM-backed workflows."""
import os
from pathlib import Path

from dotenv import dotenv_values

_LOCAL_VALUES = dotenv_values(Path.cwd() / ".env")

API_KEY = os.environ.get("OPENAI_API_KEY", _LOCAL_VALUES.get("OPENAI_API_KEY"))
# Endpoint for the planning pipeline (decompose/align/enrich/judge). Unset
# means the SDK default, i.e. the metered provider. Point it at the local
# OpenAI-compatible shim to bill planning to a Claude Code subscription.
BASE_URL = os.environ.get(
    "CAPTAIN_LLM_BASE_URL",
    _LOCAL_VALUES.get("CAPTAIN_LLM_BASE_URL"),
)
MODEL = os.environ.get(
    "CAPTAIN_MODEL",
    _LOCAL_VALUES.get("CAPTAIN_MODEL") or "gpt-5.6",
)
