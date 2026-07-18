from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_lifecycle_script_scopes_every_compose_call() -> None:
    source = (ROOT / "scripts" / "captain-n8n.ps1").read_text(encoding="utf-8")

    assert "-p captain-n8n-builder" in source
    assert "--env-file $EnvFile" in source
    assert "-f $ComposeFile" in source
    assert "down -v" not in source.lower()
    assert "docker volume" not in source.lower()
    assert "vibemind-n8n" not in source.lower()
    assert "captain@local.test" in source
    assert "ConvertTo-SecureString" in source


def test_lifecycle_script_uses_secure_secrets_and_safe_operations() -> None:
    source = (ROOT / "scripts" / "captain-n8n.ps1").read_text(encoding="utf-8")

    assert "RandomNumberGenerator]::Create()" in source
    assert "System.Net.Sockets.TcpListener" in source
    assert "up -d --wait" in source
    assert " stop" in source
    assert "com.docker.compose.project=captain-n8n-builder" in source
    assert "/rest/owner/setup" in source
    assert "/rest/login" in source
    assert "/rest/api-keys/scopes" in source
    assert "/rest/api-keys" in source
    assert "rawApiKey" in source
    assert "CAPTAIN_N8N_API_KEY" in source
    assert "psql" not in source.lower()
    assert "/var/lib/postgresql" not in source.lower()
    assert "$IsWindows" not in source


def test_bootstrap_never_echoes_secret_values() -> None:
    source = (ROOT / "scripts" / "captain-n8n.ps1").read_text(encoding="utf-8")

    assert "Write-Host $ApiKey" not in source
    assert "Write-Output $OwnerPassword" not in source
    assert "Write-Host $OwnerPassword" not in source
    assert "Write-Output $ApiKey" not in source


def test_verifier_is_project_scoped_and_uses_authenticated_harmless_read() -> None:
    source = (ROOT / "scripts" / "verify_captain_n8n.ps1").read_text(
        encoding="utf-8"
    )

    assert "com.docker.compose.project=captain-n8n-builder" in source
    assert "/healthz" in source
    assert "/api/v1/workflows" in source
    assert "X-N8N-API-KEY" in source
    assert "vibemind-n8n" not in source.lower()
    assert "Write-Host $ApiKey" not in source
    assert "Write-Output $ApiKey" not in source


def test_readme_documents_only_captain_builder_lifecycle() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    for action in ("init", "start", "bootstrap", "status", "stop"):
        assert f"scripts/captain-n8n.ps1 -Action {action}" in readme
    assert "captain@local.test" in readme
    assert "VibeMind remains untouched" in readme
