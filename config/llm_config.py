"""Configuration for LLM-backed workflows."""
import os
from pathlib import Path

from dotenv import dotenv_values

_LOCAL_VALUES = dotenv_values(Path.cwd() / ".env")

API_KEY = os.environ.get("OPENAI_API_KEY", _LOCAL_VALUES.get("OPENAI_API_KEY"))
MODEL = os.environ.get(
    "CAPTAIN_MODEL",
    _LOCAL_VALUES.get("CAPTAIN_MODEL") or "gpt-5.6",
)
