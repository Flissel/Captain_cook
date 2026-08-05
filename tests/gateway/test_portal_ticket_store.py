from __future__ import annotations

import os
import hashlib
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from blockchain.mariadb_storage import MariaDBStorage
from gateway.portal_contracts import PortalPrincipalV1, PortalTicketFenceV1
from gateway.portal_store import PortalTicketStore
from tests.support.mariadb import assert_isolated_test_database


TEST_DSN = os.getenv("TEST_MARIADB_DSN")
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="TEST_MARIADB_DSN is not configured")
NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
JOB_ID = UUID("10000000-0000-0000-0000-000000000001")
ORG_A = PortalPrincipalV1(subject_id="user-a", organization_id="org-a")
ORG_B = PortalPrincipalV1(subject_id="user-b", organization_id="org-b")


def fence(*, revision: int = 1, selected: str | None = None) -> PortalTicketFenceV1:
    return PortalTicketFenceV1(
        revision=revision,
        content_sha256=("a" if revision == 1 else "b") * 64,
        correlation_id=UUID("30000000-0000-0000-0000-000000000001"),
        credential_alias="CRM_PRIMARY",
        credential_type="hubspotApi",
        requirement_project_id=None,
        selected_credential_id=selected,
        expected_verification_workflow_sha256="d" * 64,
        verification_workflow_sha256=None,
    )


@pytest.fixture
def storage() -> Iterator[MariaDBStorage]:
    assert TEST_DSN is not None
    assert_isolated_test_database(TEST_DSN)
    value = MariaDBStorage(TEST_DSN)
    PortalTicketStore(value)
    with value.transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM portal_setup_tickets")
            cursor.execute("DELETE FROM portal_setup_bindings")
    value.clear()
    yield value
    with value.transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM portal_setup_tickets")
            cursor.execute("DELETE FROM portal_setup_bindings")
    value.clear()


def test_ticket_is_hashed_expires_and_can_only_be_used_once(storage: MariaDBStorage) -> None:
    store = PortalTicketStore(storage)
    assert store.provision_organization(JOB_ID, "org-a") is True
    assert store.provision_organization(JOB_ID, "org-a") is False
    ticket = store.issue(
        job_id=JOB_ID,
        principal=ORG_A,
        credential_alias="CRM_PRIMARY",
        action="discover",
        fence=fence(),
        now=NOW,
    )
    with storage.transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT token_sha256 FROM portal_setup_tickets WHERE ticket_id = %s",
                (str(ticket.ticket_id),),
            )
            row = cursor.fetchone()
    assert row == {"token_sha256": hashlib.sha256(ticket.ticket.encode()).hexdigest()}
    assert ticket.ticket not in str(row)

    store.consume(
        job_id=JOB_ID,
        principal=ORG_A,
        ticket_id=ticket.ticket_id,
        raw_ticket=ticket.ticket,
        credential_alias="CRM_PRIMARY",
        action="discover",
        current_fence=fence(),
        now=NOW + timedelta(minutes=1),
    )
    with pytest.raises(PermissionError, match="invalid portal setup ticket"):
        store.consume(
            job_id=JOB_ID,
            principal=ORG_A,
            ticket_id=ticket.ticket_id,
            raw_ticket=ticket.ticket,
            credential_alias="CRM_PRIMARY",
            action="discover",
            current_fence=fence(),
            now=NOW + timedelta(minutes=1),
        )


def test_cross_tenant_and_expired_ticket_fail_with_fixed_error(storage: MariaDBStorage) -> None:
    store = PortalTicketStore(storage)
    store.provision_organization(JOB_ID, "org-a")
    ticket = store.issue(
        job_id=JOB_ID,
        principal=ORG_A,
        credential_alias="CRM_PRIMARY",
        action="select",
        fence=fence(),
        now=NOW,
    )

    for principal, current in (
        (ORG_B, NOW + timedelta(minutes=1)),
        (ORG_A, NOW + timedelta(minutes=10)),
    ):
        with pytest.raises(PermissionError, match="^invalid portal setup ticket$"):
            store.consume(
                job_id=JOB_ID,
                principal=principal,
                ticket_id=ticket.ticket_id,
                raw_ticket=ticket.ticket,
                credential_alias="CRM_PRIMARY",
                action="select",
                current_fence=fence(),
                now=current,
            )


def test_concurrent_consumers_cannot_both_use_one_ticket(storage: MariaDBStorage) -> None:
    store = PortalTicketStore(storage)
    store.provision_organization(JOB_ID, "org-a")
    ticket = store.issue(
        job_id=JOB_ID,
        principal=ORG_A,
        credential_alias="CRM_PRIMARY",
        action="discover",
        fence=fence(),
        now=NOW,
    )

    def consume() -> str:
        try:
            store.consume(
                job_id=JOB_ID,
                principal=ORG_A,
                ticket_id=ticket.ticket_id,
                raw_ticket=ticket.ticket,
                credential_alias="CRM_PRIMARY",
                action="discover",
                current_fence=fence(),
                now=NOW + timedelta(minutes=1),
            )
        except PermissionError:
            return "denied"
        return "accepted"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _: consume(), range(2)))

    assert sorted(results) == ["accepted", "denied"]


def test_portal_identity_cannot_create_or_rebind_tenant_binding(storage: MariaDBStorage) -> None:
    store = PortalTicketStore(storage)

    with pytest.raises(LookupError, match="portal integration setup not found"):
        store.issue(
            job_id=JOB_ID,
            principal=ORG_B,
            credential_alias="CRM_PRIMARY",
            action="discover",
            fence=fence(),
            now=NOW,
        )
    assert store.organization_owns_setup(JOB_ID, "org-b") is False

    store.provision_organization(JOB_ID, "org-a")
    with pytest.raises(ValueError, match="portal tenant binding conflict"):
        store.provision_organization(JOB_ID, "org-b")
    assert store.organization_owns_setup(JOB_ID, "org-a") is True
    assert store.organization_owns_setup(JOB_ID, "org-b") is False


@pytest.mark.parametrize("action", ["select", "rotation_requested", "revoked"])
def test_ticket_action_is_bound_and_single_use_for_every_mutating_action(
    storage: MariaDBStorage,
    action: str,
) -> None:
    store = PortalTicketStore(storage)
    store.provision_organization(JOB_ID, "org-a")
    ticket = store.issue(
        job_id=JOB_ID,
        principal=ORG_A,
        credential_alias="CRM_PRIMARY",
        action=action,
        fence=fence(),
        now=NOW,
    )
    wrong_action = "revoked" if action != "revoked" else "rotation_requested"

    with pytest.raises(PermissionError, match="^invalid portal setup ticket$"):
        store.consume(
            job_id=JOB_ID,
            principal=ORG_A,
            ticket_id=ticket.ticket_id,
            raw_ticket=ticket.ticket,
            credential_alias="CRM_PRIMARY",
            action=wrong_action,
            current_fence=fence(),
            now=NOW + timedelta(minutes=1),
        )
    store.consume(
        job_id=JOB_ID,
        principal=ORG_A,
        ticket_id=ticket.ticket_id,
        raw_ticket=ticket.ticket,
        credential_alias="CRM_PRIMARY",
        action=action,
        current_fence=fence(),
        now=NOW + timedelta(minutes=1),
    )
    with pytest.raises(PermissionError, match="^invalid portal setup ticket$"):
        store.consume(
            job_id=JOB_ID,
            principal=ORG_A,
            ticket_id=ticket.ticket_id,
            raw_ticket=ticket.ticket,
            credential_alias="CRM_PRIMARY",
            action=action,
            current_fence=fence(),
            now=NOW + timedelta(minutes=1),
        )


@pytest.mark.parametrize(
    "action",
    ["discover", "select", "verify", "rotation_requested", "revoked"],
)
def test_every_ticket_action_fails_closed_when_setup_fence_changes(
    storage: MariaDBStorage,
    action: str,
) -> None:
    store = PortalTicketStore(storage)
    store.provision_organization(JOB_ID, "org-a")
    ticket = store.issue(
        job_id=JOB_ID,
        principal=ORG_A,
        credential_alias="CRM_PRIMARY",
        action=action,
        fence=fence(),
        now=NOW,
    )

    with pytest.raises(PermissionError, match="^invalid portal setup ticket$"):
        store.consume(
            job_id=JOB_ID,
            principal=ORG_A,
            ticket_id=ticket.ticket_id,
            raw_ticket=ticket.ticket,
            credential_alias="CRM_PRIMARY",
            action=action,
            current_fence=fence(revision=2),
            now=NOW + timedelta(minutes=1),
        )


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("content_sha256", "c" * 64),
        ("correlation_id", UUID("30000000-0000-0000-0000-000000000002")),
        ("credential_alias", "CRM_SECONDARY"),
        ("credential_type", "oauth2Api"),
        ("requirement_project_id", "project-2"),
        ("selected_credential_id", "credential-2"),
        ("expected_verification_workflow_sha256", "e" * 64),
        ("verification_workflow_sha256", "d" * 64),
    ],
)
def test_ticket_fence_compares_every_authorized_target_field(
    storage: MariaDBStorage,
    field: str,
    changed: object,
) -> None:
    store = PortalTicketStore(storage)
    store.provision_organization(JOB_ID, "org-a")
    ticket = store.issue(
        job_id=JOB_ID,
        principal=ORG_A,
        credential_alias="CRM_PRIMARY",
        action="discover",
        fence=fence(),
        now=NOW,
    )

    with pytest.raises(PermissionError, match="^invalid portal setup ticket$"):
        store.consume(
            job_id=JOB_ID,
            principal=ORG_A,
            ticket_id=ticket.ticket_id,
            raw_ticket=ticket.ticket,
            credential_alias="CRM_PRIMARY",
            action="discover",
            current_fence=fence().model_copy(update={field: changed}),
            now=NOW + timedelta(minutes=1),
        )
