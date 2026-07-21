import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT = ROOT / "scripts" / "demo-preflight.ps1"
RUNNER = ROOT / "scripts" / "run-live-demo.ps1"
SERVICES = ROOT / "scripts" / "live-demo-services.ps1"
MINIBOOK = ROOT / "scripts" / "minibook-demo.ps1"
SANDBOX_DOCKERFILE = ROOT / "Dockerfile.capability-sandbox"


def test_demo_preflight_contract_is_fail_closed_and_redacted() -> None:
    source = PREFLIGHT.read_text(encoding="utf-8")
    assert "captain_test" in source
    assert "CAPTAIN_N8N_API_KEY" in source
    assert "CAPTAIN_N8N_MCP_TOKEN" in source
    assert "/api/v1/workflows" in source
    assert "/mcp-server/http" in source
    assert "MINIBOOK_BACKEND_URL" in source
    assert "MINIBOOK_API_KEY" in source
    assert "MINIBOOK_PROJECTION_API_KEY" in source
    assert "CAPTAIN_CAPABILITY_SANDBOX_IMAGE" in source
    assert "Assert-CaptainSandboxImage" in source
    assert "docker image inspect $Reference" in source
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
    opaque_provider = "provider-value-must-remain-opaque"
    minibook_key = "minibook-key-must-remain-opaque"
    projection_key = "projection-key-must-remain-opaque"
    env_file.write_text(
        f"OPENAI_API_KEY={opaque_provider}\n"
        f"N8N_API_KEY={secret}\n"
        f"MINIBOOK_API_KEY={minibook_key}\n"
        f"MINIBOOK_PROJECTION_API_KEY={projection_key}\n"
        "MAILPIT_WEB_PORT=8025\nMAILPIT_URL=http://localhost:8025\n",
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
    assert f"MINIBOOK_API_KEY={minibook_key}" in normalized
    assert f"MINIBOOK_PROJECTION_API_KEY={projection_key}" in normalized
    assert f"OPENAI_API_KEY={opaque_provider}" in normalized
    assert secret not in output
    assert minibook_key not in output
    assert projection_key not in output
    assert opaque_provider not in output
    assert "$AllowedNames -notcontains $name" in PREFLIGHT.read_text(encoding="utf-8")


def test_preflight_rejects_mutable_sandbox_reference_before_network(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    secrets = {
        "CAPTAIN_N8N_API_KEY": "n8n-rest-secret",
        "CAPTAIN_N8N_MCP_TOKEN": "n8n-mcp-secret",
        "MINIBOOK_API_KEY": "minibook-secret",
        "MINIBOOK_PROJECTION_API_KEY": "projection-secret",
    }
    values = {
        **secrets,
        "TEST_MARIADB_DSN": "mariadb://captain:secret@127.0.0.1:9/captain_test",
        "CAPTAIN_CAPABILITY_SANDBOX_IMAGE": "captain-capability-sandbox:latest",
    }
    env_file.write_text(
        "\n".join(f"{name}={value}" for name, value in values.items()) + "\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(PREFLIGHT), "-EnvFile", str(env_file)],
        cwd=ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "Captain-owned digest-pinned capability sandbox image" in output
    assert "Captain n8n REST" not in output
    for secret in secrets.values():
        assert secret not in output


def test_capability_sandbox_image_is_minimal_and_non_root() -> None:
    source = SANDBOX_DOCKERFILE.read_text(encoding="utf-8")
    assert 'org.opencontainers.image.title="captain-capability-sandbox"' in source
    assert "WORKDIR /workspace" in source
    assert "USER 65532:65532" in source
    assert 'ENTRYPOINT ["python", "-I"]' in source
    assert "COPY " not in source
    assert "ADD " not in source


def test_requirements_dev_installs_minibook_test_dependencies() -> None:
    requirements = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    assert "-r minibook/requirements.txt" in requirements


def test_runtime_dependencies_install_hermes_streamable_http_transport() -> None:
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "mcp==1.26.0" in requirements
    assert "starlette==1.0.1" in requirements


def test_minibook_demo_bootstrap_is_local_reusable_and_redacted() -> None:
    source = MINIBOOK.read_text(encoding="utf-8")
    assert 'ValidateSet("start", "bootstrap", "status", "stop")' in source
    assert "CAPTAIN_DEMO_MINIBOOK_API_KEY" in source
    assert "MINIBOOK_API_KEY" in source
    assert "MINIBOOK_PROJECTION_API_KEY" in source
    assert "Projection credential is required before starting Minibook" in source
    assert "/api/v1/agents/me" in source
    assert "/api/v1/agents" in source
    assert "captain-demo-service" in source
    assert "[switch]$RecoverDemoCredentials" in source
    assert "Minibook demo service credential recovered locally" in source
    assert "SELECT api_key FROM agents WHERE name" in source
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
    assert "docker-compose.test.yml" in source
    assert "captain-cook-live-demo" in source
    assert "mariadb-test" in source
    assert "python" in source and "gateway.app" in source
    assert "gateway-demo.pid" in source
    assert "Gateway port is occupied by a non-demo process" in source
    assert "stale local Gateway process stopped" in source
    assert "Get-CimInstance Win32_Process" in source
    assert ".env.captain-n8n" in source
    assert "CAPTAIN_N8N_API_KEY" in source
    assert "CAPTAIN_N8N_MCP_TOKEN" in source
    assert "TEST_MARIADB_DSN" in source
    assert "captain_test" in source
    assert "CAPTAIN_GATEWAY_TOKEN" in source
    assert "WORKER_GATEWAY_TOKEN" in source
    assert "MINIBOOK_API_KEY" in source
    assert "MINIBOOK_PROJECTION_API_KEY" in source
    assert "CAPTAIN_CAPABILITY_SANDBOX_IMAGE" in source
    assert "Set-Missing $values 'MINIBOOK_PROJECTION_API_KEY'" in source
    assert "RandomNumberGenerator" in source
    assert "[switch]$RecoverDemoCredentials" in source
    assert "[string]$CredentialSourceEnv" in source
    assert "N8N_API_KEY" in source
    assert "N8N_MCP_TOKEN" in source
    assert "http://127.0.0.1:5679" in source
    assert "/api/v1/workflows?limit=1" in source
    assert "/mcp-server/http" in source
    assert "X-N8N-API-KEY" in source
    assert "Authorization" in source
    assert "application/json, text/event-stream" in source
    assert "docker ps -a" in source
    assert "docker stop" in source
    assert "com.docker.compose.project=captain-n8n-builder" in source
    assert "application/json, text/event-stream" in PREFLIGHT.read_text(encoding="utf-8")
    assert "bootstrap -RecoverDemoCredentials:$RecoverDemoCredentials" in source
    assert "OPENAI_API_KEY" in source
    assert "MINIBOOK_CREATION_DB" in source
    assert "MINIBOOK_CREATION_ARTIFACTS" in source
    assert "CAPTAIN_RUNTIME_ARTIFACT_ROOT" in source
    assert "OPENAI_MODEL" in source and "gpt-4o-mini" in source
    assert "CAPTAIN_FACTORY_MAX_COST_USD" in source
    assert "CAPTAIN_FACTORY_MAX_COST_PER_CALL_USD" in source
    assert "Captain Runtime and Minibook capability artifact roots differ" in source
    assert "Write-Output $values" not in source
    assert source.index("Initialize-CaptainN8n $values") < source.index("mariadb-test")
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
    assert ".env.captain-n8n" in readme
    assert "-RecoverDemoCredentials" in readme
    assert "-CredentialSourceEnv" in readme
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    for name in ("MARIADB_TEST_PORT", "MARIADB_TEST_PASSWORD", "MARIADB_TEST_ROOT_PASSWORD", "TEST_MARIADB_DSN"):
        assert f"{name}=" in env
