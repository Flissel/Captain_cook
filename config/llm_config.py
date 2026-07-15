"""Configuration for LLMs."""
import os

API_KEY = os.environ.get("OPENAI_API_KEY")
MODEL = os.environ.get("CAPTAIN_MODEL", "gpt-5.6")
