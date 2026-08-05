"""Dedicated, non-interchangeable bearer capabilities for portal controls."""

from __future__ import annotations

import secrets
from enum import Enum

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from gateway.auth import get_gateway_settings
from gateway.settings import GatewaySettings


class PortalControlRole(str, Enum):
    PROVIDER = "provider"
    EVIDENCE = "evidence"
    RESTART = "restart"


_bearer = HTTPBearer(auto_error=False)


def authorize_portal_control(
    credentials: HTTPAuthorizationCredentials | None,
    settings: GatewaySettings,
    role: PortalControlRole,
) -> PortalControlRole:
    if not settings.portal_control_configured:
        raise HTTPException(status_code=503, detail="portal control unavailable")
    expected_setting = {
        PortalControlRole.PROVIDER: settings.portal_provider_control_token,
        PortalControlRole.EVIDENCE: settings.portal_evidence_token,
        PortalControlRole.RESTART: settings.portal_restart_control_token,
    }[role]
    assert expected_setting is not None
    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or not secrets.compare_digest(
            credentials.credentials,
            expected_setting.get_secret_value(),
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return role


def require_provider_control(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: GatewaySettings = Depends(get_gateway_settings),
) -> PortalControlRole:
    return authorize_portal_control(credentials, settings, PortalControlRole.PROVIDER)


def require_evidence_control(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: GatewaySettings = Depends(get_gateway_settings),
) -> PortalControlRole:
    return authorize_portal_control(credentials, settings, PortalControlRole.EVIDENCE)


def require_restart_control(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: GatewaySettings = Depends(get_gateway_settings),
) -> PortalControlRole:
    return authorize_portal_control(credentials, settings, PortalControlRole.RESTART)
