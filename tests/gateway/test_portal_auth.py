from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Lock

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, generate_private_key
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from gateway.auth import GatewayRole, require_actor
from gateway.portal_auth import (
    CachingJwksKeyResolver,
    PortalTokenVerificationError,
    PyJwtPortalVerifier,
    require_portal_principal,
)
from gateway.portal_contracts import PortalPrincipalV1
from gateway.settings import GatewaySettings


ISSUER = "https://project.supabase.co/auth/v1"
AUDIENCE = "authenticated"
JWKS_URL = "https://project.supabase.co/auth/v1/.well-known/jwks.json"
KID = "portal-key-1"
CAPTAIN_TOKEN = "captain-test-token"
WORKER_TOKEN = "worker-test-token"
NOW = datetime.now(timezone.utc)


class StaticKeyResolver:
    def __init__(self, key: object) -> None:
        self.key = key
        self.requests: list[tuple[str, str]] = []

    def get_key(self, *, kid: str, jwks_url: str) -> object:
        self.requests.append((kid, jwks_url))
        if kid != KID:
            raise PortalTokenVerificationError()
        return self.key


def configured_settings() -> GatewaySettings:
    return GatewaySettings(
        ledger_dsn=SecretStr("mariadb://captain:database-secret@127.0.0.1/captain"),
        captain_gateway_token=SecretStr(CAPTAIN_TOKEN),
        worker_gateway_token=SecretStr(WORKER_TOKEN),
        portal_supabase_issuer=ISSUER,
        portal_supabase_audience=AUDIENCE,
        portal_supabase_jwks_url=JWKS_URL,
    )


def portal_client(
    settings: GatewaySettings,
    verifier: PyJwtPortalVerifier,
) -> TestClient:
    app = FastAPI()
    app.state.gateway_settings = settings
    app.state.gateway_settings_lock = Lock()
    app.state.portal_token_verifier = verifier

    @app.get("/portal")
    async def portal(principal: PortalPrincipalV1 = Depends(require_portal_principal)) -> dict[str, str]:
        return principal.model_dump()

    @app.get("/existing")
    async def existing(actor: GatewayRole = Depends(require_actor)) -> dict[str, str]:
        return {"actor": actor.value}

    return TestClient(app)


@pytest.fixture
def private_key() -> RSAPrivateKey:
    return generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def resolver(private_key: RSAPrivateKey) -> StaticKeyResolver:
    return StaticKeyResolver(private_key.public_key())


@pytest.fixture
def verifier(resolver: StaticKeyResolver) -> PyJwtPortalVerifier:
    return PyJwtPortalVerifier(key_resolver=resolver)


def token(private_key: RSAPrivateKey, **overrides: object) -> str:
    claims: dict[str, object] = {
        "sub": "user-1",
        "organization_id": "org-1",
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": NOW,
        "exp": NOW + timedelta(minutes=5),
    }
    claims.update(overrides)
    headers = claims.pop("_headers", {"kid": KID})
    assert isinstance(headers, dict)
    return jwt.encode(claims, private_key, algorithm="RS256", headers=headers)


def bearer(value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}"}


def test_valid_rs256_token_maps_subject_and_organization(
    private_key: RSAPrivateKey,
    resolver: StaticKeyResolver,
    verifier: PyJwtPortalVerifier,
) -> None:
    response = portal_client(configured_settings(), verifier).get(
        "/portal",
        headers=bearer(token(private_key)),
    )

    assert response.status_code == 200
    assert response.json() == {"subject_id": "user-1", "organization_id": "org-1"}
    assert resolver.requests == [(KID, JWKS_URL)]


@pytest.mark.parametrize(
    "replacement",
    (
        {"aud": "wrong-audience"},
        {"iss": "https://wrong.example.test"},
        {"_headers": {"typ": "JWT"}},
    ),
)
def test_wrong_audience_issuer_or_missing_kid_is_rejected(
    private_key: RSAPrivateKey,
    verifier: PyJwtPortalVerifier,
    replacement: dict[str, object],
) -> None:
    response = portal_client(configured_settings(), verifier).get(
        "/portal",
        headers=bearer(token(private_key, **replacement)),
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid portal identity"}
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("kind", ("signature", "algorithm"))
def test_wrong_signature_or_algorithm_is_rejected(
    private_key: RSAPrivateKey,
    verifier: PyJwtPortalVerifier,
    kind: str,
) -> None:
    if kind == "signature":
        supplied = token(generate_private_key(public_exponent=65537, key_size=2048))
    else:
        supplied = jwt.encode(
            {
                "sub": "user-1",
                "organization_id": "org-1",
                "iss": ISSUER,
                "aud": AUDIENCE,
                "exp": NOW + timedelta(minutes=5),
            },
            "algorithm-secret-canary-with-at-least-thirty-two-bytes",
            algorithm="HS256",
            headers={"kid": KID},
        )

    response = portal_client(configured_settings(), verifier).get("/portal", headers=bearer(supplied))

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid portal identity"}
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "replacement",
    (
        {"exp": NOW - timedelta(seconds=1)},
        {"sub": None},
        {"organization_id": None},
        {"sub": "   "},
        {"organization_id": "   "},
    ),
)
def test_expired_or_missing_required_claims_are_rejected(
    private_key: RSAPrivateKey,
    verifier: PyJwtPortalVerifier,
    replacement: dict[str, object],
) -> None:
    response = portal_client(configured_settings(), verifier).get(
        "/portal",
        headers=bearer(token(private_key, **replacement)),
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid portal identity"}
    assert response.headers["www-authenticate"] == "Bearer"


def test_missing_or_partial_portal_configuration_fails_closed(
    private_key: RSAPrivateKey,
    verifier: PyJwtPortalVerifier,
) -> None:
    unavailable = configured_settings().model_copy(
        update={
            "portal_supabase_issuer": None,
            "portal_supabase_audience": None,
            "portal_supabase_jwks_url": None,
        }
    )

    response = portal_client(unavailable, verifier).get(
        "/portal",
        headers=bearer(token(private_key)),
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "portal identity unavailable"}
    with pytest.raises(ValidationError):
        GatewaySettings(
            ledger_dsn=SecretStr("mariadb://captain:database-secret@127.0.0.1/captain"),
            captain_gateway_token=SecretStr(CAPTAIN_TOKEN),
            worker_gateway_token=SecretStr(WORKER_TOKEN),
            portal_supabase_issuer=ISSUER,
        )


def test_portal_errors_never_echo_raw_token_or_secret_canary(
    verifier: PyJwtPortalVerifier,
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw_token = "invalid.portal-token-secret-canary.value"

    response = portal_client(configured_settings(), verifier).get(
        "/portal",
        headers=bearer(raw_token),
    )

    captured = caplog.text + response.text
    assert response.status_code == 401
    assert raw_token not in captured
    assert "portal-token-secret-canary" not in captured


def test_existing_captain_and_worker_bearer_auth_remains_unaffected(
    verifier: PyJwtPortalVerifier,
) -> None:
    client = portal_client(configured_settings(), verifier)

    captain = client.get("/existing", headers=bearer(CAPTAIN_TOKEN))
    worker = client.get("/existing", headers=bearer(WORKER_TOKEN))

    assert captain.status_code == 200
    assert captain.json() == {"actor": "captain"}
    assert worker.status_code == 200
    assert worker.json() == {"actor": "worker"}


def test_jwks_key_cache_rejects_an_unbounded_ttl() -> None:
    with pytest.raises(ValueError):
        CachingJwksKeyResolver(ttl_seconds=0)
