"""Configuration for LLM-backed workflows."""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path.cwd() / ".env")

API_KEY = os.environ.get("OPENAI_API_KEY")
MODEL = os.environ.get("CAPTAIN_MODEL", "gpt-5.6")
