"""Role-scoped bearer authentication for the ledger gateway."""

from __future__ import annotations

import secrets
from enum import Enum

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from gateway.settings import GatewayConfigurationError, GatewaySettings


class GatewayRole(str, Enum):
    CAPTAIN = "captain"
    WORKER = "worker"


_bearer = HTTPBearer(auto_error=False)


def load_gateway_settings(request_or_app: Request | object) -> GatewaySettings:
    application = (
        request_or_app.app
        if isinstance(request_or_app, Request)
        else request_or_app
    )
    current = getattr(application.state, "gateway_settings", None)
    if isinstance(current, GatewaySettings):
        return current

    lock = application.state.gateway_settings_lock
    with lock:
        current = getattr(application.state, "gateway_settings", None)
        if not isinstance(current, GatewaySettings):
            current = GatewaySettings.from_env()
            application.state.gateway_settings = current
    return current


def get_gateway_settings(request: Request) -> GatewaySettings:
    try:
        return load_gateway_settings(request)
    except GatewayConfigurationError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="gateway unavailable",
        ) from None


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid or missing bearer token",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_actor(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: GatewaySettings = Depends(get_gateway_settings),
) -> GatewayRole:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized()

    presented = credentials.credentials
    captain_match = secrets.compare_digest(
        presented,
        settings.captain_gateway_token.get_secret_value(),
    )
    worker_match = secrets.compare_digest(
        presented,
        settings.worker_gateway_token.get_secret_value(),
    )
    if captain_match:
        return GatewayRole.CAPTAIN
    if worker_match:
        return GatewayRole.WORKER
    raise _unauthorized()


def _require_role(actor: GatewayRole, expected: GatewayRole) -> GatewayRole:
    if actor is not expected:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="insufficient gateway role",
        )
    return actor


def require_captain(actor: GatewayRole = Depends(require_actor)) -> GatewayRole:
    return _require_role(actor, GatewayRole.CAPTAIN)


def require_worker(actor: GatewayRole = Depends(require_actor)) -> GatewayRole:
    return _require_role(actor, GatewayRole.WORKER)


def require_reader(actor: GatewayRole = Depends(require_actor)) -> GatewayRole:
    return actor
