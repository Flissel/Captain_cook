"""The spike env generator writes fresh secrets once and never prints them."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from gateway.settings import GatewaySettings
from scripts.openclaw_spike import (
    SpikeEnvExistsError,
    init_env,
    read_env,
)


def test_init_env_writes_consistent_isolated_settings(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / ".env"

    init_env(path)

    values = read_env(path)
    digests = json.loads(values["WORKER_AGENT_TOKEN_SHA256_JSON"])
    assert digests == {
        "openclaw": hashlib.sha256(values["CAPTAIN_MCP_GATEWAY_TOKEN"].encode()).hexdigest()
    }
    tokens = [
        values[name]
        for name in (
            "OPENCLAW_GATEWAY_TOKEN",
            "CAPTAIN_MCP_TOKEN",
            "CAPTAIN_MCP_GATEWAY_TOKEN",
            "CAPTAIN_GATEWAY_TOKEN",
            "WORKER_GATEWAY_TOKEN",
            "CAPTAIN_RUNTIME_TOKEN",
            "MARIADB_PASSWORD",
            "MARIADB_ROOT_PASSWORD",
        )
    ]
    assert len(set(tokens)) == len(tokens)
    assert all(len(token) >= 32 for token in tokens)
    # A throwaway ledger on its own port, never the shared 3306 one.
    assert values["MARIADB_PORT"] == "3307"
    assert "@127.0.0.1:3307/" in values["LEDGER_DSN"]
    assert values["GATEWAY_REQUIRE_AGENT_IDENTITY"] == "true"
    # The gateway accepts the generated settings.
    settings = GatewaySettings.from_env(values)
    assert set(settings.worker_agent_token_sha256) == {"openclaw"}
    # Nothing secret reaches the terminal.
    printed = capsys.readouterr().out
    assert all(token not in printed for token in tokens)


def test_init_env_never_overwrites(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("KEEP=1\n", encoding="utf-8")

    with pytest.raises(SpikeEnvExistsError):
        init_env(path)
    assert path.read_text(encoding="utf-8") == "KEEP=1\n"


def test_read_env_ignores_comments_and_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("# note\n\nA=1\nB=two=parts\n", encoding="utf-8")

    assert read_env(path) == {"A": "1", "B": "two=parts"}
