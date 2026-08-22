import os
import socket
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT = ROOT / "scripts" / "demo-preflight.ps1"
RUNNER = ROOT / "scripts" / "run-live-demo.ps1"
SERVICES = ROOT / "scripts" / "live-demo-services.ps1"
MINIBOOK = ROOT / "scripts" / "minibook-demo.ps1"
BENCHMARK_COMPOSE = ROOT / "docker-compose.benchmark.yml"
MANAGED_PROCESS = ROOT / "scripts" / "managed-process-identity.ps1"


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
    opaque_provider = "provider-value-must-remain-opaque"
    env_file.write_text(
        f"OPENAI_API_KEY={opaque_provider}\nN8N_API_KEY={secret}\nMAILPIT_WEB_PORT=8025\nMAILPIT_URL=http://localhost:8025\n",
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
    # 127.0.0.1, not localhost: Mailpit listens on IPv4 only and Invoke-WebRequest
    # resolves localhost to ::1 first, burning the full probe timeout.
    assert "MAILPIT_URL=http://127.0.0.1:18025" in normalized
    assert f"CAPTAIN_N8N_API_KEY={secret}" in normalized
    assert f"OPENAI_API_KEY={opaque_provider}" in normalized
    assert secret not in output
    assert opaque_provider not in output
    assert "$AllowedNames -notcontains $name" in PREFLIGHT.read_text(encoding="utf-8")


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
    assert "CAPTAIN_DEMO_MINIBOOK_PROJECTION_API_KEY" in source
    assert "RandomNumberGenerator]::GetBytes(32)" in source
    assert "$env:MINIBOOK_PROJECTION_API_KEY" in source
    assert "/api/v1/agents/me" in source
    assert "/api/v1/agents" in source
    assert "captain-demo-service" in source
    assert "[switch]$RecoverDemoCredentials" in source
    assert "Minibook demo service credential recovered locally" in source
    assert "SELECT api_key FROM agents WHERE name" in source
    assert "Write-Output $apiKey" not in source
    assert "Write-Output $projectionApiKey" not in source
    assert "minibook-demo.pid" in source
    assert "$baseUrl = 'http://127.0.0.1:8080'" in source
    assert "Get-Command python.exe -CommandType Application" in source


def test_live_demo_services_only_operates_captain_resources() -> None:
    source = SERVICES.read_text(encoding="utf-8")
    assert "$global:LASTEXITCODE = 0" in source
    assert 'ValidateSet("start", "status", "portal-start", "gateway-restart", "benchmark-start"' in source
    assert "captain-n8n.ps1" in source
    assert "minibook-demo.ps1" in source
    assert "docker compose" in source
    assert "function Test-CaptainN8nCredentials" in source
    assert "Captain n8n stored REST/MCP credentials failed verification" in source
    assert "mailpit" in source
    assert "evidence/live-demo-services.json" in source
    assert "docker-compose.test.yml" in source
    assert "$project = 'captain-cook-test'" in source
    assert "captain-cook-live-demo" not in source
    assert "mariadb-test" in source
    assert "python" in source and "gateway.app" in source
    assert "gateway-demo.pid" in source
    assert "Gateway port is occupied without the exact managed process and ledger identity" in source
    assert "verified stale local Gateway process stopped" in source
    assert "verified exited legacy Gateway identity removed" in source
    assert "legacy Gateway identity still refers to a running process" in source
    assert "Gateway listener process is not an exact child of the managed launcher" in source
    assert "Write-ManagedProcessIdentity -Process $listenerProcess" in source
    assert "Get-ManagedProcessIdentity" in source
    assert ".env.captain-n8n" in source
    assert "CAPTAIN_N8N_API_KEY" in source
    assert "CAPTAIN_N8N_MCP_TOKEN" in source
    assert "TEST_MARIADB_DSN" in source
    assert "captain_test" in source
    assert "CAPTAIN_GATEWAY_TOKEN" in source
    assert "WORKER_GATEWAY_TOKEN" in source
    for portal_name in (
        "PORTAL_SUPABASE_ISSUER",
        "PORTAL_SUPABASE_AUDIENCE",
        "PORTAL_SUPABASE_JWKS_URL",
        "PORTAL_ORGANIZATION_CLAIM",
        "PORTAL_PROVIDER_CONTROL_TOKEN",
        "PORTAL_EVIDENCE_TOKEN",
        "PORTAL_RESTART_CONTROL_TOKEN",
        "SSL_CERT_FILE",
    ):
        assert portal_name in source
    assert "captain.gateway.configuration.v2" in source
    assert "function Invoke-PortalStart" in source
    portal_start = source.split("function Invoke-PortalStart", 1)[1].split(
        "function ", 1
    )[0]
    assert "Start-Gateway $values" in portal_start
    assert "Start-CaptainN8nBroker $values" in portal_start
    assert "Assert-RuntimeConfiguration" not in portal_start
    assert "Start-Runtime" not in portal_start
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
    assert "& $MinibookCommand start" in source
    assert "OPENAI" not in source.upper()
    assert source.index("Initialize-CaptainN8n $values") < source.index("mariadb-test")
    lowered = source.lower()
    assert "down -v" not in lowered
    assert "volume rm" not in lowered
    assert "vibemind" not in lowered


def test_business_benchmark_uses_a_dedicated_persistent_database() -> None:
    source = SERVICES.read_text(encoding="utf-8")
    compose = BENCHMARK_COMPOSE.read_text(encoding="utf-8")

    benchmark_start = source[source.index("function Invoke-BenchmarkStart"):]
    assert "Start-CaptainN8nBroker $benchmarkValues" in benchmark_start
    assert benchmark_start.index("Start-Gateway $benchmarkValues") < benchmark_start.index(
        "Start-CaptainN8nBroker $benchmarkValues"
    )

    assert "docker-compose.benchmark.yml" in source
    assert "captain-cook-business-benchmark" in source
    assert "mariadb-benchmark" in source
    assert "business-benchmark-runtime.env" in source
    assert "MARIADB_BENCHMARK_PORT" in source
    assert "CAPTAIN_BENCHMARK_GATEWAY_PORT" in source
    assert BENCHMARK_COMPOSE.is_file()
    assert "mariadb-benchmark:" in compose
    assert "captain-benchmark-mariadb:/var/lib/mysql" in compose
    assert "captain-benchmark-mariadb:" in compose
    assert "tmpfs:" not in compose
    assert "managed-process-identity.ps1" in source
    assert "Get-ManagedProcessIdentity" in source
    assert "Get-GatewayConfigurationSha256" in source


def test_live_demo_services_can_restart_only_the_managed_benchmark_gateway() -> None:
    source = SERVICES.read_text(encoding="utf-8")

    assert '"benchmark-restart"' in source
    restart_block = source.split("benchmark-restart {", 1)[1].split("}", 1)[0]
    assert "Stop-ManagedGateway -PidPath $benchmarkGatewayPid" in restart_block
    assert "Invoke-BenchmarkStart" in restart_block
    assert "docker compose down" not in restart_block
    assert "Stop-CaptainN8nContainers" not in restart_block


def test_live_demo_services_can_restart_only_the_verified_portal_gateway() -> None:
    source = SERVICES.read_text(encoding="utf-8")

    assert '"gateway-restart"' in source
    restart_block = source.split("gateway-restart {", 1)[1].split("}", 1)[0]
    assert "Initialize-LocalEnvironment" in restart_block
    assert "Restart-Gateway $values" in restart_block
    assert "docker compose" not in restart_block
    assert "Stop-CaptainN8nContainers" not in restart_block
    restart_function = source.split("function Restart-Gateway", 1)[1].split(
        "function ", 1
    )[0]
    assert "Get-ManagedListenerIdentity" in restart_function
    assert "Get-ManagedListenerIdentityForConfigurationReplacement" in restart_function
    assert "taskkill.exe" in restart_function
    assert "Start-Gateway $Values" in restart_function


def test_managed_process_identity_rejects_wrong_listener_and_configuration(
    tmp_path: Path,
) -> None:
    identity = tmp_path / "gateway.pid"
    harness = tmp_path / "verify.ps1"
    harness.write_text(
        "\n".join(
            (
                "$ErrorActionPreference = 'Stop'",
                f". '{MANAGED_PROCESS.as_posix()}'",
                "$process = Get-Process -Id $PID",
                f"$path = '{identity.as_posix()}'",
                "Write-ManagedProcessIdentity -Process $process -Path $path -ConfigurationSha256 ('a' * 64)",
                "$matched = Get-ManagedProcessIdentity -Path $path -ListenerPid $PID -ConfigurationSha256 ('a' * 64)",
                "if ($matched.Id -ne $PID) { throw 'identity mismatch' }",
                "$wrongListenerRejected = $false",
                "try { Get-ManagedProcessIdentity -Path $path -ListenerPid ($PID + 1) -ConfigurationSha256 ('a' * 64) } catch { $wrongListenerRejected = $true }",
                "if (-not $wrongListenerRejected) { throw 'wrong listener accepted' }",
                "$wrongConfigRejected = $false",
                "try { Get-ManagedProcessIdentity -Path $path -ListenerPid $PID -ConfigurationSha256 ('b' * 64) } catch { $wrongConfigRejected = $true }",
                "if (-not $wrongConfigRejected) { throw 'wrong config accepted' }",
            )
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(harness)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_managed_listener_identity_binds_a_real_listener_to_its_recorded_process(
    tmp_path: Path,
) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    identity = tmp_path / "listener.pid"
    harness = tmp_path / "listener.ps1"
    harness.write_text(
        "\n".join(
            (
                "$ErrorActionPreference = 'Stop'",
                f". '{MANAGED_PROCESS.as_posix()}'",
                f"$listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, {port})",
                "$listener.Start()",
                "$other = Start-Process pwsh -ArgumentList '-NoProfile','-Command','Start-Sleep -Seconds 20' -WindowStyle Hidden -PassThru",
                "try {",
                f"  Write-ManagedProcessIdentity -Process (Get-Process -Id $PID) -Path '{identity.as_posix()}' -ConfigurationSha256 ('a' * 64)",
                f"  $matched = Get-ManagedListenerIdentity -Path '{identity.as_posix()}' -Port {port} -ConfigurationSha256 ('a' * 64)",
                "  if ($matched.Id -ne $PID) { throw 'listener identity mismatch' }",
                f"  Write-ManagedProcessIdentity -Process $other -Path '{identity.as_posix()}' -ConfigurationSha256 ('a' * 64)",
                "  $wrongOwnerRejected = $false",
                f"  try {{ Get-ManagedListenerIdentity -Path '{identity.as_posix()}' -Port {port} -ConfigurationSha256 ('a' * 64) }} catch {{ $wrongOwnerRejected = $true }}",
                "  if (-not $wrongOwnerRejected) { throw 'foreign listener accepted' }",
                "} finally {",
                "  $listener.Stop()",
                "  if (Get-Process -Id $other.Id -ErrorAction SilentlyContinue) { Stop-Process -Id $other.Id -Force }",
                "}",
            )
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(harness)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_managed_listener_identity_allows_only_exact_configuration_replacement(
    tmp_path: Path,
) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    identity = tmp_path / "listener.pid"
    harness = tmp_path / "replacement.ps1"
    harness.write_text(
        "\n".join(
            (
                "$ErrorActionPreference = 'Stop'",
                f". '{MANAGED_PROCESS.as_posix()}'",
                f"$listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, {port})",
                "$listener.Start()",
                "try {",
                f"  Write-ManagedProcessIdentity -Process (Get-Process -Id $PID) -Path '{identity.as_posix()}' -ConfigurationSha256 ('a' * 64)",
                f"  $matched = Get-ManagedListenerIdentityForConfigurationReplacement -Path '{identity.as_posix()}' -Port {port} -ReplacementConfigurationSha256 ('b' * 64)",
                "  if ($matched.Id -ne $PID) { throw 'replacement identity mismatch' }",
                "  $sameConfigRejected = $false",
                f"  try {{ Get-ManagedListenerIdentityForConfigurationReplacement -Path '{identity.as_posix()}' -Port {port} -ReplacementConfigurationSha256 ('a' * 64) }} catch {{ $sameConfigRejected = $true }}",
                "  if (-not $sameConfigRejected) { throw 'same configuration accepted as replacement' }",
                "} finally {",
                "  $listener.Stop()",
                "}",
            )
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(harness)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


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
