"""The authorize step must fail closed when the pinned authority bundle drifts."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agenten.agent_factory.authority_adapter_bundle import AuthorityAdapterBundleError
from gateway.auth import GatewayRole, require_captain
from gateway.authority_resume_api import build_authority_resume_router

NOW = datetime(2026, 8, 17, 9, 0, tzinfo=timezone.utc)
ASSEMBLY = "a" * 64


class _StubRecord:
    def __init__(self) -> None:
        self.authorization_id = uuid4()
        self.expires_at = NOW + timedelta(minutes=10)


class _StubStore:
    """Records whether the router reached the store at all."""

    def __init__(self) -> None:
        self.authorize_calls = 0

    def authorize(self, assembly_id: str, *, now: datetime):
        self.authorize_calls += 1
        return _StubRecord(), "raw-token"


def _client(verifier, store: _StubStore) -> TestClient:
    app = FastAPI()
    app.include_router(
        build_authority_resume_router(
            lambda: store,
            clock=lambda: NOW,
            verify_authority_bundle=verifier,
        )
    )
    app.dependency_overrides[require_captain] = lambda: GatewayRole.CAPTAIN
    return TestClient(app)


def test_authorize_is_denied_when_the_bundle_does_not_verify():
    def refuse() -> None:
        raise AuthorityAdapterBundleError("adapter digest mismatch for role gateway")

    store = _StubStore()
    response = _client(refuse, store).post(
        f"/v1/authority/assemblies/{ASSEMBLY}/resume-authorizations"
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "authority_bundle_unverified"
    assert "digest mismatch" not in response.text
    assert store.authorize_calls == 0


def test_authorize_proceeds_when_the_bundle_verifies():
    store = _StubStore()
    response = _client(lambda: None, store).post(
        f"/v1/authority/assemblies/{ASSEMBLY}/resume-authorizations"
    )

    assert response.status_code == 201
    assert response.json()["token"] == "raw-token"
    assert store.authorize_calls == 1


def test_the_default_verifier_accepts_the_committed_bundle():
    """The shipped pin must actually verify, or the guard bricks every resume."""
    from gateway.authority_resume_api import _verify_pinned_authority_bundle

    _verify_pinned_authority_bundle()
