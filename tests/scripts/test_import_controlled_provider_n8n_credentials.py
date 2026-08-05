from pathlib import Path


def test_import_is_idempotent_project_bound_and_uses_stdin() -> None:
    script = Path(
        "scripts/import-controlled-provider-n8n-credentials.ps1"
    ).read_text(encoding="utf-8")

    assert "CAPTAIN_N8N_OAUTH2_CREDENTIAL_ID" in script
    assert "n8n import:credentials --input=/dev/stdin" in script
    assert "--projectId=$ProjectId" in script
    assert "ConvertTo-Json -Depth 8 -Compress -AsArray" in script
    assert "secrets_emitted = $false" in script
    assert "clientSecret=$values['CAPTAIN_N8N_OAUTH2_CLIENT_SECRET']" in script
    assert "Write-Host $payload" not in script


def test_import_restricts_the_source_and_persists_generated_identity() -> None:
    script = Path(
        "scripts/import-controlled-provider-n8n-credentials.ps1"
    ).read_text(encoding="utf-8")

    assert ".env.n8n-credentials" in script
    assert "Refusing to read credentials outside" in script
    assert ".incoming" in script
    assert "Move-Item" in script
    assert "icacls.exe" in script
    assert "[guid]::NewGuid().ToString('N').Substring(0, 24)" in script

