from __future__ import annotations

import os
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SCRIPT = ROOT / "scripts" / "deploy-portal-mini-pc.ps1"
PREFLIGHT_SCRIPT = ROOT / "scripts" / "portal-preflight.ps1"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_portal_deploy_refuses_missing_env_and_requires_apply() -> None:
    source = _source(DEPLOY_SCRIPT)

    assert "required portal environment is missing" in source
    assert "[switch]$Apply" in source
    assert "No changes applied" in source


def test_portal_deploy_validates_both_configs_before_bounded_up() -> None:
    source = _source(DEPLOY_SCRIPT)
    first_config = source.index('"config"')
    second_config = source.index('"config"', first_config + 1)
    first_up = source.index('"up"')

    assert first_config < second_config < first_up
    assert '"portal"' in source
    assert '"mini-pc-wireguard"' in source
    assert '"mini-pc-portal-link"' in source
    assert "--project-name" in source
    assert "captain-mini-pc-portal" in source
    assert "captain-mini-pc-portal-link" in source


def test_portal_deploy_never_operates_on_adjacent_services_or_volumes() -> None:
    source = _source(DEPLOY_SCRIPT).lower()

    for forbidden in (
        "down",
        "volume rm",
        "system prune",
        "n8n",
        "mariadb",
        "supabase start",
        "supabase stop",
    ):
        assert forbidden not in source


def test_portal_deploy_requires_only_public_config_and_local_link_secrets() -> None:
    source = _source(DEPLOY_SCRIPT)

    for name in (
        "CAPTAIN_PORTAL_URL",
        "CAPTAIN_PORTAL_SUPABASE_URL",
        "CAPTAIN_PORTAL_SUPABASE_ANON_KEY",
        "CAPTAIN_PORTAL_GITEA_URL",
    ):
        assert name in source
    assert "CAPTAIN_PORTAL_GATEWAY_URL" not in source
    for forbidden in (
        "SUPABASE_SERVICE_ROLE",
        "CAPTAIN_GATEWAY_TOKEN",
        "N8N_MCP_TOKEN",
        "GITEA_TOKEN",
    ):
        assert forbidden not in source
    assert ".secrets/mini-pc/wireguard/mini-pc.conf" in source
    assert ".secrets/mini-pc/mini-pc-client.key" in source


def test_portal_container_is_static_non_root_read_only_and_same_origin() -> None:
    dockerfile = _source(ROOT / "portal" / "Dockerfile")
    dockerignore = _source(ROOT / "portal" / ".dockerignore")
    web_config = _source(ROOT / "portal" / "nginx.conf")
    compose = _source(ROOT / "deploy" / "portal" / "compose.portal.yml")

    assert dockerfile.lower().count("from ") == 2
    assert "npm run build" in dockerfile
    assert "USER " in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "location /v1/portal/" in web_config
    assert "proxy_pass http://127.0.0.1:8443" in web_config
    assert "CAPTAIN_PORTAL_GATEWAY" not in dockerfile + web_config + compose
    assert "network_mode: host" in compose
    assert "read_only: true" in compose
    assert "no-new-privileges:true" in compose
    assert "VITE_SUPABASE_URL" in compose
    assert "VITE_SUPABASE_ANON_KEY" in compose
    assert "node_modules" in dockerignore
    assert "dist" in dockerignore
    assert ".env" in dockerignore
    for forbidden in (
        "SUPABASE_SERVICE_ROLE",
        "CAPTAIN_GATEWAY_TOKEN",
        "N8N_MCP_TOKEN",
        "GITEA_TOKEN",
        "provider_secret",
    ):
        assert forbidden.lower() not in compose.lower()


def test_preflight_is_read_only_bounded_and_emits_redacted_json_schema() -> None:
    source = _source(PREFLIGHT_SCRIPT)

    assert "ConvertTo-Json" in source
    assert "TimeoutSec" in source
    assert "Authorization" not in source
    assert "Invoke-WebRequest" in source
    assert "portal" in source
    assert "portal_link" in source
    assert "supabase_auth" in source
    assert "gitea" in source
    for safe_field in ("host", "status", "version", "readiness"):
        assert safe_field in source
    for unsafe_field in ("body", "token", "key", "query"):
        assert f'"{unsafe_field}"' not in source.lower()


def test_deploy_missing_environment_fails_before_docker() -> None:
    env = {
        **os.environ,
        "CAPTAIN_PORTAL_URL": "",
        "CAPTAIN_PORTAL_SUPABASE_URL": "",
        "CAPTAIN_PORTAL_SUPABASE_ANON_KEY": "",
        "CAPTAIN_PORTAL_GITEA_URL": "",
    }
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(DEPLOY_SCRIPT), "-Apply"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    assert "required portal environment is missing" in result.stderr
    assert "docker" not in result.stdout.lower()


def test_preflight_returns_only_redacted_schema_when_endpoints_are_unreachable() -> None:
    env = {
        **os.environ,
        "CAPTAIN_PORTAL_URL": "http://127.0.0.1:9/private/path?secret=canary",
        "CAPTAIN_PORTAL_SUPABASE_URL": "http://127.0.0.1:9/hidden",
        "CAPTAIN_PORTAL_GITEA_URL": "http://127.0.0.1:9/hidden",
    }
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(PREFLIGHT_SCRIPT), "-TimeoutSeconds", "1"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert set(report) == {"schema", "readiness", "checks"}
    assert report["schema"] == "captain.portal-preflight.v1"
    assert report["readiness"] is False
    assert [check["name"] for check in report["checks"]] == [
        "portal",
        "portal_link",
        "supabase_auth",
        "gitea",
    ]
    assert all(set(check) == {"name", "host", "status", "version", "readiness"} for check in report["checks"])
    assert "private" not in result.stdout
    assert "secret" not in result.stdout
    assert "canary" not in result.stdout
