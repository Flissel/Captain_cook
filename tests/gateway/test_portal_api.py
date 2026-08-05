from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from gateway.app import create_app
from gateway.portal_auth import PortalTokenVerifier
from gateway.portal_contracts import PortalPrincipalV1, PortalSetupTicketV1
from gateway.settings import GatewaySettings
from tests.gateway.test_integration_setup_api import setup_payload
from tests.agent_factory.test_state_machine import job
from gateway.integration_setup_contracts import (
    IntegrationSetupSubmissionV1,
    PersistedIntegrationSetupV1,
    build_integration_setup_surface,
)
from blockchain.mariadb_storage import MariaDBStorage
from tests.support.mariadb import assert_isolated_test_database
from agenten.agent_factory.integration_setup import N8nCredentialMetadataV1


NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
JOB_ID = UUID("10000000-0000-0000-0000-000000000001")
TEST_DSN = os.getenv("TEST_MARIADB_DSN")


class StaticVerifier(PortalTokenVerifier):
    def __init__(self, principal: PortalPrincipalV1) -> None:
        self.principal = principal

    def verify(self, token: str, settings: GatewaySettings) -> PortalPrincipalV1:
        del token, settings
        return self.principal


class NullMirror:
    def enqueue_nowait(self, block: dict[str, Any]) -> None:
        del block


class StaticCredentialSource:
    def list_credentials(self, requirement):
        del requirement
        return (
            N8nCredentialMetadataV1(
                credential_id="credential-1",
                credential_name="HubSpot One",
                credential_type="hubspotApi",
            ),
            N8nCredentialMetadataV1(
                credential_id="credential-2",
                credential_name="HubSpot Two",
                credential_type="hubspotApi",
            ),
        )


class FakePortalGatewayStore:
    def __init__(self) -> None:
        factory_job = job().model_copy(update={"job_id": JOB_ID})
        submission = IntegrationSetupSubmissionV1.model_validate(setup_payload(factory_job))
        self.persisted = PersistedIntegrationSetupV1(
            submission=submission,
            content_sha256="a" * 64,
        )
        self.owner = "org-a"
        self.used: list[tuple[str, str]] = []
        self.ticket_principal: PortalPrincipalV1 | None = None

    def portal_integration_setup(self, job_id: UUID, organization_id: str):
        if job_id != JOB_ID or organization_id != self.owner:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="integration setup not found")
        return self.persisted

    def issue_portal_ticket(self, *, job_id, principal, credential_alias, action, now):
        self.portal_integration_setup(job_id, principal.organization_id)
        self.ticket_principal = principal
        return PortalSetupTicketV1(
            ticket_id=UUID("20000000-0000-0000-0000-000000000001"),
            ticket="opaque-ticket",
            job_id=job_id,
            credential_alias=credential_alias,
            expires_at=now + timedelta(minutes=10),
        )

    def portal_discover(self, *, job_id, principal, request, now):
        if principal != self.ticket_principal:
            from fastapi import HTTPException

            raise HTTPException(status_code=403, detail="invalid portal setup ticket")
        self.portal_integration_setup(job_id, principal.organization_id)
        self.used.append((principal.organization_id, "discover"))
        return self.persisted

    def portal_select(self, *, job_id, principal, request, now):
        self.portal_integration_setup(job_id, principal.organization_id)
        self.used.append((principal.organization_id, "select"))
        return self.persisted

    def portal_mutate(self, *, job_id, principal, request, now):
        self.portal_integration_setup(job_id, principal.organization_id)
        self.used.append((principal.organization_id, request.action))
        return self.persisted


def settings() -> GatewaySettings:
    return GatewaySettings(
        ledger_dsn=SecretStr("mysql://unused:unused@127.0.0.1:3306/captain_test"),
        captain_gateway_token=SecretStr("captain"),
        worker_gateway_token=SecretStr("worker"),
        portal_supabase_issuer="https://identity.example.test/auth/v1",
        portal_supabase_audience="portal",
        portal_supabase_jwks_url="https://identity.example.test/auth/v1/.well-known/jwks.json",
    )


def client_for(principal: PortalPrincipalV1, store: FakePortalGatewayStore) -> TestClient:
    application = create_app(
        gateway_store=store,
        mirror=NullMirror(),
        settings=settings(),
        portal_clock=lambda: NOW,
    )
    application.state.portal_token_verifier = StaticVerifier(principal)
    return TestClient(application)


def auth() -> dict[str, str]:
    return {"Authorization": "Bearer portal-jwt"}


def test_org_b_cannot_read_org_a_setup_or_issue_org_a_ticket() -> None:
    store = FakePortalGatewayStore()
    principal = PortalPrincipalV1(subject_id="user-b", organization_id="org-b")
    with client_for(principal, store) as client:
        read = client.get(f"/v1/portal/integration-setups/{JOB_ID}", headers=auth())
        issued = client.post(
            f"/v1/portal/integration-setups/{JOB_ID}/tickets",
            headers=auth(),
            json={"credential_alias": "CRM_PRIMARY", "action": "discover"},
        )

    assert read.status_code == 404
    assert issued.status_code == 404


def test_org_b_cannot_consume_org_a_ticket() -> None:
    store = FakePortalGatewayStore()
    org_a = PortalPrincipalV1(subject_id="user-a", organization_id="org-a")
    with client_for(org_a, store) as client:
        issued = client.post(
            f"/v1/portal/integration-setups/{JOB_ID}/tickets",
            headers=auth(),
            json={"credential_alias": "CRM_PRIMARY", "action": "discover"},
        )
    org_b = PortalPrincipalV1(subject_id="user-b", organization_id="org-b")
    with client_for(org_b, store) as client:
        consumed = client.post(
            f"/v1/portal/integration-setups/{JOB_ID}/discover",
            headers=auth(),
            json={
                "ticket_id": issued.json()["ticket_id"],
                "ticket": issued.json()["ticket"],
                "credential_alias": "CRM_PRIMARY",
            },
        )

    assert consumed.status_code == 403
    assert consumed.json() == {"detail": "invalid portal setup ticket"}


def test_portal_routes_return_only_secret_free_setup_data() -> None:
    store = FakePortalGatewayStore()
    principal = PortalPrincipalV1(subject_id="user-a", organization_id="org-a")
    with client_for(principal, store) as client:
        issued = client.post(
            f"/v1/portal/integration-setups/{JOB_ID}/tickets",
            headers=auth(),
            json={"credential_alias": "CRM_PRIMARY", "action": "discover"},
        )
        discovered = client.post(
            f"/v1/portal/integration-setups/{JOB_ID}/discover",
            headers=auth(),
            json={
                "ticket_id": issued.json()["ticket_id"],
                "ticket": issued.json()["ticket"],
                "credential_alias": "CRM_PRIMARY",
            },
        )
        surface = client.get(f"/v1/portal/integration-setups/{JOB_ID}", headers=auth())

    assert issued.status_code == 201
    assert discovered.status_code == 200
    assert surface.status_code == 200
    assert surface.json() == build_integration_setup_surface(
        store.persisted,
        n8n_ui_base_url=settings().captain_n8n_ui_url,
    ).model_dump(mode="json")
    assert "opaque-ticket" not in discovered.text
    assert store.used == [("org-a", "discover")]


def test_select_rotation_and_revoke_routes_are_portal_authenticated() -> None:
    store = FakePortalGatewayStore()
    principal = PortalPrincipalV1(subject_id="user-a", organization_id="org-a")
    with client_for(principal, store) as client:
        selected = client.post(
            f"/v1/portal/integration-setups/{JOB_ID}/select",
            headers=auth(),
            json={
                "ticket_id": "20000000-0000-0000-0000-000000000001",
                "ticket": "opaque-ticket",
                "credential_alias": "CRM_PRIMARY",
                "credential_id": "credential-1",
            },
        )
        rotated = client.post(
            f"/v1/portal/integration-setups/{JOB_ID}/actions",
            headers=auth(),
            json={
                "ticket_id": "20000000-0000-0000-0000-000000000001",
                "ticket": "opaque-ticket",
                "credential_alias": "CRM_PRIMARY",
                "action": "rotation_requested",
            },
        )

    assert selected.status_code == 200
    assert rotated.status_code == 200
    assert store.used == [("org-a", "select"), ("org-a", "rotation_requested")]


def test_portal_rejects_arbitrary_secret_fields_before_store_use() -> None:
    store = FakePortalGatewayStore()
    principal = PortalPrincipalV1(subject_id="user-a", organization_id="org-a")
    with client_for(principal, store) as client:
        response = client.post(
            f"/v1/portal/integration-setups/{JOB_ID}/tickets",
            headers=auth(),
            json={
                "credential_alias": "CRM_PRIMARY",
                "action": "discover",
                "api_key": "must-not-enter",
            },
        )

    assert response.status_code == 422
    assert "must-not-enter" not in response.text


@pytest.mark.skipif(not TEST_DSN, reason="TEST_MARIADB_DSN is not configured")
def test_real_gateway_portal_flow_is_tenant_scoped_and_digest_fenced() -> None:
    assert TEST_DSN is not None
    assert_isolated_test_database(TEST_DSN)
    storage = MariaDBStorage(TEST_DSN)
    from gateway.portal_store import PortalTicketStore

    PortalTicketStore(storage)
    with storage.transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM portal_setup_tickets")
            cursor.execute("DELETE FROM portal_setup_bindings")
    storage.clear()
    configured = settings().model_copy(update={"ledger_dsn": SecretStr(TEST_DSN)})
    application = create_app(
        storage=storage,
        mirror=NullMirror(),
        settings=configured,
        portal_credential_source=StaticCredentialSource(),
        portal_clock=lambda: NOW,
    )
    org_a = PortalPrincipalV1(subject_id="user-a", organization_id="org-a")
    application.state.portal_token_verifier = StaticVerifier(org_a)
    factory_job = job().model_copy(update={"job_id": JOB_ID})

    try:
        with TestClient(application) as client:
            assert client.post(
                "/v1/factory/jobs",
                headers={"Authorization": "Bearer captain"},
                json=factory_job.model_dump(mode="json", by_alias=True),
            ).status_code == 202
            assert client.post(
                "/v1/factory/integration-setups",
                headers={"Authorization": "Bearer captain"},
                json=setup_payload(factory_job),
            ).status_code == 201
            issued = client.post(
                f"/v1/portal/integration-setups/{JOB_ID}/tickets",
                headers=auth(),
                json={"credential_alias": "CRM_PRIMARY", "action": "discover"},
            )
            discovered = client.post(
                f"/v1/portal/integration-setups/{JOB_ID}/discover",
                headers=auth(),
                json={
                    "ticket_id": issued.json()["ticket_id"],
                    "ticket": issued.json()["ticket"],
                    "credential_alias": "CRM_PRIMARY",
                },
            )
            replay = client.post(
                f"/v1/portal/integration-setups/{JOB_ID}/discover",
                headers=auth(),
                json={
                    "ticket_id": issued.json()["ticket_id"],
                    "ticket": issued.json()["ticket"],
                    "credential_alias": "CRM_PRIMARY",
                },
            )
            application.state.portal_token_verifier = StaticVerifier(
                PortalPrincipalV1(subject_id="user-b", organization_id="org-b")
            )
            cross_read = client.get(
                f"/v1/portal/integration-setups/{JOB_ID}", headers=auth()
            )

        assert issued.status_code == 201
        assert discovered.status_code == 200
        assert discovered.json()["overall_status"] == "selection_required"
        assert discovered.json()["revision"] == 2
        assert replay.status_code == 403
        assert cross_read.status_code == 404
    finally:
        with storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM portal_setup_tickets")
                cursor.execute("DELETE FROM portal_setup_bindings")
        storage.clear()
