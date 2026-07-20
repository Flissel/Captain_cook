import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT = ROOT / "scripts" / "demo-preflight.ps1"
RUNNER = ROOT / "scripts" / "run-live-demo.ps1"
SERVICES = ROOT / "scripts" / "live-demo-services.ps1"
MINIBOOK = ROOT / "scripts" / "minibook-demo.ps1"


def test_demo_preflight_contract_is_fail_closed_and_redacted() -> None:
    source = PREFLIGHT.read_text(encoding="utf-8")
    assert "captain_test" in source
    assert "CAPTAIN_N8N_API_KEY" in source
    assert "CAPTAIN_N8N_MCP_TOKEN" in source
    assert "/api/v1/workflows" in source
    assert "/mcp-server/http" in source
    assert "MINIBOOK_BACKEND_URL" in source
    assert "CAPTAIN_GATEWAY_URL" in source
    assert "Write-Output $" not in source


def test_demo_runner_requires_explicit_provider_opt_in() -> None:
    source = RUNNER.read_text(encoding="utf-8")
    assert "[switch]$LiveProviders" in source
    assert "scripts/run-gate-e.ps1" in source
    assert "if (-not $LiveProviders)" in source


def test_normalize_writes_safe_defaults_and_aliases_without_secret_output(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    secret = "fixture-secret-never-log"
    env_file.write_text(
        f"N8N_API_KEY={secret}\nMAILPIT_WEB_PORT=8025\nMAILPIT_URL=http://localhost:8025\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            "pwsh", "-NoProfile", "-File", str(PREFLIGHT),
            "-EnvFile", str(env_file), "-NormalizeOnly",
        ],
        cwd=ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    normalized = env_file.read_text(encoding="utf-8")
    assert "MAILPIT_WEB_PORT=18025" in normalized
    assert "MAILPIT_URL=http://localhost:18025" in normalized
    assert f"CAPTAIN_N8N_API_KEY={secret}" in normalized
    assert secret not in output


def test_requirements_dev_installs_minibook_test_dependencies() -> None:
    requirements = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    assert "-r minibook/requirements.txt" in requirements


def test_minibook_demo_bootstrap_is_local_reusable_and_redacted() -> None:
    source = MINIBOOK.read_text(encoding="utf-8")
    assert 'ValidateSet("start", "bootstrap", "status", "stop")' in source
    assert "CAPTAIN_DEMO_MINIBOOK_API_KEY" in source
    assert "/api/v1/agents/me" in source
    assert "/api/v1/agents" in source
    assert "captain-demo-service" in source
    assert "Write-Output $apiKey" not in source
    assert "minibook-demo.pid" in source


def test_live_demo_services_only_operates_captain_resources() -> None:
    source = SERVICES.read_text(encoding="utf-8")
    assert 'ValidateSet("start", "health", "stop")' in source
    assert "captain-n8n.ps1" in source
    assert "minibook-demo.ps1" in source
    assert "docker compose" in source
    assert "mailpit" in source
    assert "evidence/live-demo-services.json" in source
    lowered = source.lower()
    assert "down -v" not in lowered
    assert "volume rm" not in lowered
    assert "vibemind" not in lowered


def test_readme_documents_safe_recording_commands() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "scripts/demo-preflight.ps1 -NormalizeOnly" in readme
    assert "scripts/run-live-demo.ps1" in readme
    assert "scripts/run-live-demo.ps1 -LiveProviders" in readme
    assert "captain_test" in readme
