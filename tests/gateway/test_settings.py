from __future__ import annotations

import pytest

from gateway.settings import GatewayConfigurationError, GatewaySettings
from tests.gateway.test_gateway_settings import valid_environment


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
