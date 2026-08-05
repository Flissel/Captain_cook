from pathlib import Path


def test_sync_is_allowlisted_atomic_and_never_emits_secret_values() -> None:
    script = Path("scripts/sync-controlled-provider-credentials.ps1").read_text(
        encoding="utf-8"
    )

    assert ".env.n8n-credentials" in script
    assert "CAPTAIN_PROVIDER_BEARER_TOKEN" in script
    assert "CAPTAIN_PROVIDER_OAUTH_CLIENT_ID" in script
    assert "CAPTAIN_PROVIDER_OAUTH_CLIENT_SECRET" in script
    assert ".incoming" in script
    assert "[System.Collections.Generic.List[string]]::new()" in script
    assert "$lines.AddRange" in script
    assert "icacls.exe" in script
    assert "secrets_emitted = $false" in script
    assert "CAPTAIN_PROVIDER_AUDIT_TOKEN" not in script
    assert "CAPTAIN_PROVIDER_OAUTH_SIGNING_SECRET" not in script


def test_sync_configures_only_the_supported_client_credentials_endpoint() -> None:
    script = Path("scripts/sync-controlled-provider-credentials.ps1").read_text(
        encoding="utf-8"
    )

    assert 'CAPTAIN_N8N_OAUTH2_AUTH_URL = \'\'' in script
    assert 'CAPTAIN_N8N_OAUTH2_ACCESS_TOKEN_URL = "$ProviderOrigin/oauth/token"' in script
    assert "CAPTAIN_N8N_OAUTH2_GRANT_TYPE = 'clientCredentials'" in script
    assert "CAPTAIN_N8N_OAUTH2_SCOPE = 'probe:read'" in script
    assert "CAPTAIN_N8N_OAUTH2_AUTHENTICATION = 'header'" in script
    assert "/oauth/authorize" not in script
