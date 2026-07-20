from __future__ import annotations

import importlib
from pathlib import Path


def test_swarm_credentials_are_kept_under_the_checked_out_repository(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    constants = importlib.import_module("minibook.swarm.constants")
    repository_root = Path(__file__).resolve().parents[2]

    assert constants.CREDS_FILE == repository_root / "config" / "swarm_agents.json"
