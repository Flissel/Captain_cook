"""Static guarantees of the OpenClaw spike deployment files."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

DEPLOY = Path(__file__).parents[2] / "deploy" / "captain-openclaw"


def compose() -> dict[str, Any]:
    return yaml.safe_load((DEPLOY / "compose.captain-openclaw.yml").read_text(encoding="utf-8"))


def seed_config() -> dict[str, Any]:
    text = (DEPLOY / "openclaw.json").read_text(encoding="utf-8")
    without_comments = "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("//")
    )
    return json.loads(without_comments)


def test_gateway_is_published_on_loopback_only() -> None:
    ports = compose()["services"]["captain-openclaw-gateway"]["ports"]

    assert ports and all(str(port).startswith("127.0.0.1:") for port in ports)


def test_project_and_volume_names_are_captain_owned() -> None:
    document = compose()

    assert document["name"] == "captain-openclaw"
    assert set(document["volumes"]) == {"captain-openclaw-home"}
    assert document["volumes"]["captain-openclaw-home"]["name"] == "captain-openclaw-home"
    assert all(name.startswith("captain-openclaw-") for name in document["services"])


def test_container_home_is_persisted_as_a_whole() -> None:
    """Claude Code lives in /home/node/.local and its login in /home/node/.claude*."""

    for name in ("captain-openclaw-gateway", "captain-openclaw-cli"):
        assert "captain-openclaw-home:/home/node" in compose()["services"][name]["volumes"]


def test_cli_shares_gateway_network_and_pid_namespace() -> None:
    """OpenClaw verifies its state-database owner by PID (GatewayLockNamespaceError)."""

    cli = compose()["services"]["captain-openclaw-cli"]
    assert cli["network_mode"] == "service:captain-openclaw-gateway"
    assert cli["pid"] == "service:captain-openclaw-gateway"


def test_no_service_mounts_the_docker_socket_or_restarts_on_boot() -> None:
    rendered = (DEPLOY / "compose.captain-openclaw.yml").read_text(encoding="utf-8")
    assert "docker.sock" not in rendered
    for service in compose()["services"].values():
        assert service.get("restart", "no") == "no"


def test_seed_config_holds_no_literal_secrets() -> None:
    config = seed_config()

    assert config["gateway"]["auth"]["token"] == "${OPENCLAW_GATEWAY_TOKEN}"
    captain = config["mcp"]["servers"]["captain"]
    assert captain["transport"] == "streamable-http"
    assert captain["headers"]["Authorization"] == "Bearer ${CAPTAIN_MCP_TOKEN}"
    values = json.dumps(config)
    assert not re.search(r"(sk-ant|Bearer [A-Za-z0-9_-]{16,})", values)


def test_secrets_are_required_not_defaulted() -> None:
    rendered = (DEPLOY / "compose.captain-openclaw.yml").read_text(encoding="utf-8")

    assert "${OPENCLAW_GATEWAY_TOKEN:?" in rendered
    assert "${CAPTAIN_MCP_TOKEN:?" in rendered


def test_container_seed_files_are_lf_on_every_checkout() -> None:
    """The seed service copies these bytes verbatim into the Linux container."""

    for path in DEPLOY.rglob("*"):
        if path.is_file() and path.suffix in {".md", ".json", ".yml"}:
            assert b"\r" not in path.read_bytes(), path
