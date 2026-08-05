from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import SecretStr

from gateway.portal_control_auth import PortalControlRole, authorize_portal_control
from gateway.settings import GatewaySettings


def _settings() -> GatewaySettings:
    return GatewaySettings(
        ledger_dsn=SecretStr("mariadb://captain:private@localhost/captain_test"),
        captain_gateway_token=SecretStr("captain-token"),
        worker_gateway_token=SecretStr("worker-token"),
        portal_provider_control_token=SecretStr("provider-token"),
        portal_evidence_token=SecretStr("evidence-token"),
        portal_restart_control_token=SecretStr("restart-token"),
    )


def test_control_capabilities_are_not_interchangeable() -> None:
    provider = HTTPAuthorizationCredentials(
        scheme="Bearer",
        credentials="provider-token",
    )
    assert (
        authorize_portal_control(provider, _settings(), PortalControlRole.PROVIDER)
        is PortalControlRole.PROVIDER
    )

    with pytest.raises(HTTPException) as denied:
        authorize_portal_control(provider, _settings(), PortalControlRole.EVIDENCE)
    assert denied.value.status_code == 401


def test_unconfigured_control_plane_fails_closed() -> None:
    settings = GatewaySettings(
        ledger_dsn=SecretStr("mariadb://captain:private@localhost/captain_test"),
        captain_gateway_token=SecretStr("captain-token"),
        worker_gateway_token=SecretStr("worker-token"),
    )
    credentials = HTTPAuthorizationCredentials(
        scheme="Bearer",
        credentials="provider-token",
    )

    with pytest.raises(HTTPException) as unavailable:
        authorize_portal_control(credentials, settings, PortalControlRole.PROVIDER)
    assert unavailable.value.status_code == 503
