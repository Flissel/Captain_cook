from __future__ import annotations

import json

import pytest

from gateway.settings import GatewayConfigurationError, GatewaySettings
from tests.gateway.test_gateway_settings import valid_environment


def _verification_releases_json() -> str:
    return json.dumps(
        [
            {
                "repository": "captain/templates",
                "revision": "1" * 40,
                "path": "verification/bearer.json",
                "contents_url": (
                    "https://gitea.example.test/captain/templates/raw/commit/"
                    + "1" * 40
                    + "/verification/bearer.json"
                ),
                "sha256": "a" * 64,
            }
        ]
    )


def test_portal_settings_are_optional_only_as_a_complete_group() -> None:
    absent = GatewaySettings.from_env(valid_environment())
    configured = GatewaySettings.from_env(
        valid_environment(
            PORTAL_SUPABASE_ISSUER="https://project.supabase.co/auth/v1",
            PORTAL_SUPABASE_AUDIENCE="authenticated",
            PORTAL_SUPABASE_JWKS_URL="https://project.supabase.co/auth/v1/.well-known/jwks.json",
            PORTAL_ORGANIZATION_CLAIM="tenant_id",
        )
    )

    assert absent.portal_supabase_issuer is None
    assert configured.portal_supabase_audience == "authenticated"
    assert configured.portal_organization_claim == "tenant_id"
    with pytest.raises(GatewayConfigurationError):
        GatewaySettings.from_env(
            valid_environment(PORTAL_SUPABASE_ISSUER="https://project.supabase.co/auth/v1")
        )


@pytest.mark.parametrize(
    "jwks_url",
    (
        "http://project.supabase.co/auth/v1/.well-known/jwks.json",
        "https://user:password@project.supabase.co/jwks.json",
        "https://project.supabase.co/jwks.json?next=https://unsafe.example.test",
        "https://project.supabase.co/jwks.json#fragment",
    ),
)
def test_portal_jwks_url_must_be_safe_https(jwks_url: str) -> None:
    with pytest.raises(GatewayConfigurationError):
        GatewaySettings.from_env(
            valid_environment(
                PORTAL_SUPABASE_ISSUER="https://project.supabase.co/auth/v1",
                PORTAL_SUPABASE_AUDIENCE="authenticated",
                PORTAL_SUPABASE_JWKS_URL=jwks_url,
            )
        )


def test_portal_n8n_adapters_require_one_complete_explicit_group() -> None:
    absent = GatewaySettings.from_env(valid_environment())
    configured = GatewaySettings.from_env(
        valid_environment(
            CAPTAIN_PORTAL_N8N_ADAPTERS_ENABLED="true",
            CAPTAIN_N8N_API_KEY="api-test-secret",
            CAPTAIN_N8N_MCP_TOKEN="mcp-test-secret",
            CAPTAIN_PORTAL_GITEA_ORIGIN="https://gitea.example.test",
            CAPTAIN_PORTAL_VERIFICATION_RELEASES_JSON=_verification_releases_json(),
        )
    )

    assert absent.portal_n8n_adapters_configured is False
    assert configured.portal_n8n_adapters_configured is True
    assert configured.portal_n8n_api_key is not None
    assert configured.portal_n8n_api_key.get_secret_value() == "api-test-secret"
    assert len(configured.portal_verification_releases) == 1
    assert "api-test-secret" not in repr(configured)
    assert "mcp-test-secret" not in repr(configured)


@pytest.mark.parametrize(
    "missing_name",
    (
        "CAPTAIN_N8N_API_KEY",
        "CAPTAIN_N8N_MCP_TOKEN",
        "CAPTAIN_PORTAL_GITEA_ORIGIN",
        "CAPTAIN_PORTAL_VERIFICATION_RELEASES_JSON",
    ),
)
def test_portal_n8n_adapter_configuration_fails_closed_when_incomplete(
    missing_name: str,
) -> None:
    environment = dict(
        valid_environment(
            CAPTAIN_PORTAL_N8N_ADAPTERS_ENABLED="true",
            CAPTAIN_N8N_API_KEY="api-test-secret",
            CAPTAIN_N8N_MCP_TOKEN="mcp-test-secret",
            CAPTAIN_PORTAL_GITEA_ORIGIN="https://gitea.example.test",
            CAPTAIN_PORTAL_VERIFICATION_RELEASES_JSON=_verification_releases_json(),
        )
    )
    del environment[missing_name]

    with pytest.raises(GatewayConfigurationError):
        GatewaySettings.from_env(environment)
