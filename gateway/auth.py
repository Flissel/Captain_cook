"""Role-scoped bearer authentication for the ledger gateway."""

from __future__ import annotations

import hashlib
import secrets
from enum import Enum

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from gateway.settings import GatewayConfigurationError, GatewaySettings


class GatewayRole(str, Enum):
    CAPTAIN = "captain"
    WORKER = "worker"
    PROVIDER_RECEIPT = "provider_receipt"


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


def _registered_agent(presented: str, settings: GatewaySettings) -> str | None:
    """Return the agent whose own worker token was presented, if any."""

    digest = hashlib.sha256(presented.encode("utf-8")).hexdigest()
    matched: str | None = None
    for agent_id, expected in settings.worker_agent_token_sha256.items():
        if secrets.compare_digest(digest, expected):
            matched = agent_id
    return matched


def require_actor(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: GatewaySettings = Depends(get_gateway_settings),
) -> GatewayRole:
    request.state.gateway_agent_id = None
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized()

    presented = credentials.credentials
    agent_id = _registered_agent(presented, settings)
    if agent_id is not None:
        request.state.gateway_agent_id = agent_id
        return GatewayRole.WORKER
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


def current_agent_id(
    request: Request,
    _: GatewayRole = Depends(require_actor),
) -> str | None:
    """The registered agent behind the request; None for shared role tokens."""

    return getattr(request.state, "gateway_agent_id", None)


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


def require_provider_receipt_authority(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: GatewaySettings = Depends(get_gateway_settings),
) -> GatewayRole:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized()
    presented = credentials.credentials
    provider = settings.provider_receipt_gateway_token
    if provider is not None and secrets.compare_digest(
        presented, provider.get_secret_value()
    ):
        return GatewayRole.PROVIDER_RECEIPT
    if _registered_agent(presented, settings) is not None or any(
        secrets.compare_digest(presented, token.get_secret_value())
        for token in (
            settings.captain_gateway_token,
            settings.worker_gateway_token,
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="insufficient gateway role",
        )
    raise _unauthorized()


def require_reader(actor: GatewayRole = Depends(require_actor)) -> GatewayRole:
    return actor
