from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import jwt
from fastapi.testclient import TestClient

from portal_provider.app import ProviderSettings, create_app, load_settings


NOW = datetime(2026, 8, 5, 18, tzinfo=timezone.utc)
CORRELATION_ID = "50000000-0000-4000-8000-000000000001"
PROBE_ID = "60000000-0000-4000-8000-000000000001"


def _settings(database: Path) -> ProviderSettings:
    return ProviderSettings(
        database_path=database,
        issuer="https://provider.example.test",
        audience="captain-n8n-verification",
        bearer_token="bearer-secret-value-long-enough",
        oauth_client_id="captain-n8n",
        oauth_client_secret="oauth-secret-value-long-enough",
        oauth_signing_secret="signing-secret-value-long-enough",
        audit_token="audit-secret-value-long-enough",
    )


def _probe() -> dict[str, str]:
    return {
        "probe_id": PROBE_ID,
        "correlation_id": CORRELATION_ID,
        "setup_content_sha256": "a" * 64,
    }


def test_bearer_probe_is_authenticated_idempotent_and_secret_free(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "provider.sqlite3")
    with TestClient(create_app(settings=settings, clock=lambda: NOW)) as client:
        denied = client.post("/v1/bearer/probes", json=_probe())
        first = client.post(
            "/v1/bearer/probes",
            headers={"Authorization": f"Bearer {settings.bearer_token}"},
            json=_probe(),
        )
        replay = client.post(
            "/v1/bearer/probes",
            headers={"Authorization": f"Bearer {settings.bearer_token}"},
            json=_probe(),
        )

    assert denied.status_code == 401
    assert first.status_code == 200
    assert replay.json() == first.json()
    assert first.json()["kind"] == "bearer"
    assert first.json()["correlation_id"] == CORRELATION_ID
    assert UUID(first.json()["trace_id"])
    assert len(first.json()["proof_sha256"]) == 64
    serialized = first.text + replay.text
    for secret in (
        settings.bearer_token,
        settings.oauth_client_secret,
        settings.oauth_signing_secret,
        settings.audit_token,
    ):
        assert secret not in serialized


def test_oauth_client_credentials_token_authorizes_probe(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "provider.sqlite3")
    with TestClient(create_app(settings=settings, clock=lambda: NOW)) as client:
        token_response = client.post(
            "/oauth/token",
            auth=(settings.oauth_client_id, settings.oauth_client_secret),
            data={"grant_type": "client_credentials", "scope": "probe:read"},
        )
        assert token_response.status_code == 200
        access_token = token_response.json()["access_token"]
        claims = jwt.decode(
            access_token,
            settings.oauth_signing_secret,
            algorithms=["HS256"],
            audience=settings.audience,
            issuer=settings.issuer,
            options={"verify_exp": False, "verify_iat": False},
        )
        assert claims["scope"] == "probe:read"
        assert claims["exp"] - claims["iat"] == 300

        denied = client.post("/v1/oauth2/probes", json=_probe())
        accepted = client.post(
            "/v1/oauth2/probes",
            headers={"Authorization": f"Bearer {access_token}"},
            json=_probe(),
        )

    assert denied.status_code == 401
    assert accepted.status_code == 200
    assert accepted.json()["kind"] == "oauth2"
    assert UUID(accepted.json()["oauth_exchange_id"]) == UUID(claims["jti"])
    assert access_token not in accepted.text

    with TestClient(
        create_app(settings=settings, clock=lambda: NOW + timedelta(seconds=1))
    ) as restarted:
        replay = restarted.post(
            "/v1/oauth2/probes",
            headers={"Authorization": f"Bearer {access_token}"},
            json=_probe(),
        )

    assert replay.status_code == 200
    assert replay.json() == accepted.json()


def test_audit_receipt_survives_restart_and_requires_distinct_token(tmp_path: Path) -> None:
    database = tmp_path / "provider.sqlite3"
    settings = _settings(database)
    with TestClient(create_app(settings=settings, clock=lambda: NOW)) as client:
        created = client.post(
            "/v1/bearer/probes",
            headers={"Authorization": f"Bearer {settings.bearer_token}"},
            json=_probe(),
        ).json()

    with TestClient(
        create_app(settings=settings, clock=lambda: NOW + timedelta(seconds=1))
    ) as restarted:
        denied = restarted.get(
            f"/v1/audit/traces/{created['trace_id']}",
            headers={"Authorization": f"Bearer {settings.bearer_token}"},
        )
        audit = restarted.get(
            f"/v1/audit/traces/{created['trace_id']}",
            headers={"Authorization": f"Bearer {settings.audit_token}"},
        )

    assert denied.status_code == 401
    assert audit.status_code == 200
    assert audit.json() == created


def test_environment_loader_is_strict_and_secret_safe(tmp_path: Path) -> None:
    environment = {
        "CAPTAIN_PROVIDER_DATABASE_PATH": str(tmp_path / "provider.sqlite3"),
        "CAPTAIN_PROVIDER_ISSUER": "https://provider.example.test",
        "CAPTAIN_PROVIDER_AUDIENCE": "captain-n8n-verification",
        "CAPTAIN_PROVIDER_BEARER_TOKEN": "bearer-secret-value-long-enough",
        "CAPTAIN_PROVIDER_OAUTH_CLIENT_ID": "captain-n8n",
        "CAPTAIN_PROVIDER_OAUTH_CLIENT_SECRET": "oauth-secret-value-long-enough",
        "CAPTAIN_PROVIDER_OAUTH_SIGNING_SECRET": "signing-secret-value-long-enough",
        "CAPTAIN_PROVIDER_AUDIT_TOKEN": "audit-secret-value-long-enough",
    }

    settings = load_settings(environment)

    assert settings.database_path == tmp_path / "provider.sqlite3"
    serialized = repr(settings)
    for name in (
        "CAPTAIN_PROVIDER_BEARER_TOKEN",
        "CAPTAIN_PROVIDER_OAUTH_CLIENT_SECRET",
        "CAPTAIN_PROVIDER_OAUTH_SIGNING_SECRET",
        "CAPTAIN_PROVIDER_AUDIT_TOKEN",
    ):
        assert environment[name] not in serialized
