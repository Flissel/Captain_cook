"""Fail-closed Supabase JWT authentication for future portal-only routes."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from http.client import HTTPException as HttpClientError
from time import monotonic
from threading import Lock
from typing import Protocol
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError

from gateway.auth import get_gateway_settings
from gateway.portal_contracts import PortalPrincipalV1
from gateway.settings import GatewaySettings


MAX_JWKS_RESPONSE_BYTES = 1_048_576
_application_initialization_lock = Lock()


class PortalTokenVerificationError(Exception):
    """A token is invalid without retaining its raw contents or failure cause."""


class PortalPublicKeyResolver(Protocol):
    """Resolves one public key for the untrusted JWT header's key identifier."""

    def get_key(self, *, kid: str, jwks_url: str) -> object: ...


class PortalTokenVerifier(Protocol):
    """Injectable boundary for portal identity verification."""

    def verify(self, token: str, settings: GatewaySettings) -> PortalPrincipalV1: ...


class CachingJwksKeyResolver:
    """Fetches JWKS documents while retaining only selected public keys by ``kid``."""

    def __init__(
        self,
        *,
        ttl_seconds: int = 300,
        refresh_cooldown_seconds: int = 1,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if ttl_seconds < 1:
            raise ValueError("JWKS cache TTL must be positive")
        if refresh_cooldown_seconds < 1:
            raise ValueError("JWKS refresh cooldown must be positive")
        self._ttl_seconds = ttl_seconds
        self._refresh_cooldown_seconds = refresh_cooldown_seconds
        self._clock = clock
        self._keys: dict[str, tuple[float, object]] = {}
        self._lock = Lock()
        self._refresh_lock = Lock()
        self._next_refresh_at = 0.0

    def get_key(self, *, kid: str, jwks_url: str) -> object:
        with self._lock:
            cached = self._keys.get(kid)
            now = self._clock()
            if cached is not None and cached[0] > now:
                return cached[1]
            if now < self._next_refresh_at:
                raise PortalTokenVerificationError()

        if not self._refresh_lock.acquire(blocking=False):
            raise PortalTokenVerificationError()
        try:
            with self._lock:
                cached = self._keys.get(kid)
                now = self._clock()
                if cached is not None and cached[0] > now:
                    return cached[1]
                if now < self._next_refresh_at:
                    raise PortalTokenVerificationError()
                self._next_refresh_at = now + self._refresh_cooldown_seconds
            try:
                request = UrlRequest(jwks_url, headers={"Accept": "application/json"})
                with urlopen(request, timeout=5.0) as response:  # noqa: S310 - configured JWKS URL
                    body = response.read(MAX_JWKS_RESPONSE_BYTES + 1)
                if len(body) > MAX_JWKS_RESPONSE_BYTES:
                    raise ValueError("JWKS response exceeds maximum size")
                decoded = json.loads(body.decode("utf-8"))
                key = self._select_key(decoded, kid)
            except (
                HttpClientError,
                OSError,
                UnicodeDecodeError,
                json.JSONDecodeError,
                ValueError,
                TypeError,
            ):
                raise PortalTokenVerificationError() from None

            with self._lock:
                self._keys[kid] = (now + self._ttl_seconds, key)
            return key
        finally:
            self._refresh_lock.release()

    @staticmethod
    def _select_key(document: object, kid: str) -> object:
        if not isinstance(document, Mapping):
            raise PortalTokenVerificationError()
        keys = document.get("keys")
        if not isinstance(keys, list):
            raise PortalTokenVerificationError()
        for candidate in keys:
            if not isinstance(candidate, dict) or candidate.get("kid") != kid:
                continue
            if (
                candidate.get("kty") != "RSA"
                or candidate.get("use") != "sig"
                or candidate.get("alg") != "RS256"
                or "d" in candidate
            ):
                raise PortalTokenVerificationError()
            try:
                return jwt.PyJWK.from_dict(candidate).key
            except (TypeError, ValueError, jwt.PyJWTError):
                raise PortalTokenVerificationError() from None
        raise PortalTokenVerificationError()


class PyJwtPortalVerifier:
    """Validates a Supabase portal JWT without retaining or logging it."""

    def __init__(self, *, key_resolver: PortalPublicKeyResolver | None = None) -> None:
        self._key_resolver = key_resolver or CachingJwksKeyResolver()

    def verify(self, token: str, settings: GatewaySettings) -> PortalPrincipalV1:
        if not settings.portal_identity_configured:
            raise PortalTokenVerificationError()

        issuer = settings.portal_supabase_issuer
        audience = settings.portal_supabase_audience
        jwks_url = settings.portal_supabase_jwks_url
        if issuer is None or audience is None or jwks_url is None:
            raise PortalTokenVerificationError()

        try:
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if header.get("alg") != "RS256" or not isinstance(kid, str) or not kid.strip():
                raise PortalTokenVerificationError()
            key = self._key_resolver.get_key(kid=kid, jwks_url=jwks_url)
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=audience,
                issuer=issuer,
                options={"require": ["exp", "sub", settings.portal_organization_claim]},
            )
            subject_id = claims.get("sub")
            organization_id = claims.get(settings.portal_organization_claim)
            if not isinstance(subject_id, str) or not subject_id.strip():
                raise PortalTokenVerificationError()
            if not isinstance(organization_id, str) or not organization_id.strip():
                raise PortalTokenVerificationError()
            return PortalPrincipalV1(
                subject_id=subject_id,
                organization_id=organization_id,
            )
        except (jwt.PyJWTError, PortalTokenVerificationError, ValidationError):
            raise PortalTokenVerificationError() from None


_portal_bearer = HTTPBearer(auto_error=False)


def initialize_portal_auth(application: object) -> None:
    """Install one verifier per FastAPI application before portal dependencies run."""

    state = getattr(application, "state")
    with _application_initialization_lock:
        lock = getattr(state, "portal_token_verifier_lock", None)
        if lock is None:
            lock = Lock()
            state.portal_token_verifier_lock = lock
    with lock:
        current = getattr(state, "portal_token_verifier", None)
        if current is None or not callable(getattr(current, "verify", None)):
            state.portal_token_verifier = PyJwtPortalVerifier()


def get_portal_token_verifier(request: Request) -> PortalTokenVerifier:
    initialize_portal_auth(request.app)
    current = getattr(request.app.state, "portal_token_verifier", None)
    if current is not None and callable(getattr(current, "verify", None)):
        return current
    raise PortalTokenVerificationError()


def _invalid_portal_identity() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid portal identity",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_portal_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(_portal_bearer),
    settings: GatewaySettings = Depends(get_gateway_settings),
    verifier: PortalTokenVerifier = Depends(get_portal_token_verifier),
) -> PortalPrincipalV1:
    """Return the portal tenant identity; no existing gateway route consumes this yet."""

    if not settings.portal_identity_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="portal identity unavailable",
        )
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _invalid_portal_identity()
    try:
        return verifier.verify(credentials.credentials, settings)
    except PortalTokenVerificationError:
        raise _invalid_portal_identity() from None
