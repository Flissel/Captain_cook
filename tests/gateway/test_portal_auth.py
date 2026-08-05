from __future__ import annotations

from datetime import datetime, timedelta, timezone
from http.client import IncompleteRead
import json
from threading import Event, Lock, Thread

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, generate_private_key
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from gateway.auth import GatewayRole, require_actor
from gateway import portal_auth as portal_auth_module
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


class JwksResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self.read_limits: list[int] = []

    def __enter__(self) -> "JwksResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, limit: int = -1) -> bytes:
        self.read_limits.append(limit)
        return self._body if limit < 0 else self._body[:limit]


class InterruptedJwksResponse(JwksResponse):
    def read(self, limit: int = -1) -> bytes:
        del limit
        raise IncompleteRead(b"partial", 10)


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


def default_portal_client(settings: GatewaySettings) -> TestClient:
    app = FastAPI()
    app.state.gateway_settings = settings
    app.state.gateway_settings_lock = Lock()

    @app.get("/portal")
    async def portal(principal: PortalPrincipalV1 = Depends(require_portal_principal)) -> dict[str, str]:
        return principal.model_dump()

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
    remove = overrides.pop("_remove", ())
    assert isinstance(remove, tuple)
    claims.update(overrides)
    for claim_name in remove:
        assert isinstance(claim_name, str)
        claims.pop(claim_name, None)
    headers = claims.pop("_headers", {"kid": KID})
    assert isinstance(headers, dict)
    return jwt.encode(claims, private_key, algorithm="RS256", headers=headers)


def bearer(value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}"}


def jwks_document(private_key: RSAPrivateKey, **overrides: object) -> bytes:
    key = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    key.update({"kid": KID, "alg": "RS256", "use": "sig"})
    key.update(overrides)
    return json.dumps({"keys": [key]}).encode("utf-8")


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


def test_default_portal_dependency_reuses_one_key_then_refreshes_after_ttl(
    private_key: RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    fetch_count = 0
    document = jwks_document(private_key)

    def build_resolver() -> CachingJwksKeyResolver:
        return CachingJwksKeyResolver(ttl_seconds=10, clock=lambda: now[0])

    def fetch(*_: object, **__: object) -> JwksResponse:
        nonlocal fetch_count
        fetch_count += 1
        return JwksResponse(document)

    monkeypatch.setattr(portal_auth_module, "CachingJwksKeyResolver", build_resolver)
    monkeypatch.setattr(portal_auth_module, "urlopen", fetch)
    client = default_portal_client(configured_settings())
    supplied = bearer(token(private_key))

    assert client.get("/portal", headers=supplied).status_code == 200
    assert client.get("/portal", headers=supplied).status_code == 200
    assert fetch_count == 1
    now[0] = 11.0
    assert client.get("/portal", headers=supplied).status_code == 200
    assert fetch_count == 2


def test_jwks_resolver_requires_public_rsa_signing_metadata(
    private_key: RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = JwksResponse(jwks_document(private_key, use="enc"))
    monkeypatch.setattr(portal_auth_module, "urlopen", lambda *_args, **_kwargs: response)

    with pytest.raises(PortalTokenVerificationError):
        CachingJwksKeyResolver().get_key(kid=KID, jwks_url=JWKS_URL)


def test_jwks_resolver_caps_response_before_json_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = JwksResponse(b"x" * 1_048_577)
    monkeypatch.setattr(portal_auth_module, "urlopen", lambda *_args, **_kwargs: response)

    with pytest.raises(PortalTokenVerificationError):
        CachingJwksKeyResolver().get_key(kid=KID, jwks_url=JWKS_URL)

    assert response.read_limits == [1_048_577]


def test_cached_valid_key_does_not_wait_for_an_unknown_kid_refresh(
    private_key: RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    resolver = CachingJwksKeyResolver(clock=lambda: now[0])
    document = jwks_document(private_key)
    refresh_started = Event()
    release_refresh = Event()
    valid_completed = Event()
    calls = 0

    def fetch(*_: object, **__: object) -> JwksResponse:
        nonlocal calls
        calls += 1
        if calls == 2:
            refresh_started.set()
            assert release_refresh.wait(timeout=2)
        return JwksResponse(document)

    monkeypatch.setattr(portal_auth_module, "urlopen", fetch)
    cached_key = resolver.get_key(kid=KID, jwks_url=JWKS_URL)
    now[0] = 1.0

    def read_unknown_key() -> None:
        with pytest.raises(PortalTokenVerificationError):
            resolver.get_key(kid="unknown-key", jwks_url=JWKS_URL)

    def read_cached_key() -> None:
        assert resolver.get_key(kid=KID, jwks_url=JWKS_URL) is cached_key
        valid_completed.set()

    unknown = Thread(target=read_unknown_key)
    unknown.start()
    assert refresh_started.wait(timeout=1)
    valid = Thread(target=read_cached_key)
    valid.start()
    try:
        assert valid_completed.wait(timeout=0.2)
    finally:
        release_refresh.set()
    unknown.join(timeout=1)
    valid.join(timeout=1)
    assert not unknown.is_alive()
    assert not valid.is_alive()


def test_unknown_kid_refresh_is_throttled_between_bounded_attempts(
    private_key: RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    resolver = CachingJwksKeyResolver(
        clock=lambda: now[0],
        refresh_cooldown_seconds=10,
    )
    calls = 0

    def fetch(*_: object, **__: object) -> JwksResponse:
        nonlocal calls
        calls += 1
        return JwksResponse(jwks_document(private_key))

    monkeypatch.setattr(portal_auth_module, "urlopen", fetch)

    with pytest.raises(PortalTokenVerificationError):
        resolver.get_key(kid="unknown-key", jwks_url=JWKS_URL)
    with pytest.raises(PortalTokenVerificationError):
        resolver.get_key(kid="unknown-key", jwks_url=JWKS_URL)
    assert calls == 1
    now[0] = 10.0
    with pytest.raises(PortalTokenVerificationError):
        resolver.get_key(kid="unknown-key", jwks_url=JWKS_URL)
    assert calls == 2


def test_incomplete_jwks_body_read_is_normalized_to_portal_identity_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        portal_auth_module,
        "urlopen",
        lambda *_args, **_kwargs: InterruptedJwksResponse(b""),
    )

    with pytest.raises(PortalTokenVerificationError):
        CachingJwksKeyResolver().get_key(kid=KID, jwks_url=JWKS_URL)


def test_configured_organization_claim_maps_to_the_portal_principal(
    private_key: RSAPrivateKey,
    verifier: PyJwtPortalVerifier,
) -> None:
    settings = configured_settings().model_copy(
        update={"portal_organization_claim": "tenant_id"}
    )

    response = portal_client(settings, verifier).get(
        "/portal",
        headers=bearer(token(private_key, tenant_id="tenant-7")),
    )

    assert response.status_code == 200
    assert response.json() == {"subject_id": "user-1", "organization_id": "tenant-7"}


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
        {"_remove": ("exp",)},
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
