from __future__ import annotations

import os
import hashlib
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from blockchain.mariadb_storage import MariaDBStorage
from gateway.portal_contracts import PortalPrincipalV1
from gateway.portal_store import PortalTicketStore
from tests.support.mariadb import assert_isolated_test_database


TEST_DSN = os.getenv("TEST_MARIADB_DSN")
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="TEST_MARIADB_DSN is not configured")
NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
JOB_ID = UUID("10000000-0000-0000-0000-000000000001")
ORG_A = PortalPrincipalV1(subject_id="user-a", organization_id="org-a")
ORG_B = PortalPrincipalV1(subject_id="user-b", organization_id="org-b")


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
    ticket = store.issue(
        job_id=JOB_ID,
        principal=ORG_A,
        credential_alias="CRM_PRIMARY",
        action="discover",
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
            now=NOW + timedelta(minutes=1),
        )


def test_cross_tenant_and_expired_ticket_fail_with_fixed_error(storage: MariaDBStorage) -> None:
    store = PortalTicketStore(storage)
    ticket = store.issue(
        job_id=JOB_ID,
        principal=ORG_A,
        credential_alias="CRM_PRIMARY",
        action="select",
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
                now=current,
            )


def test_concurrent_consumers_cannot_both_use_one_ticket(storage: MariaDBStorage) -> None:
    store = PortalTicketStore(storage)
    ticket = store.issue(
        job_id=JOB_ID,
        principal=ORG_A,
        credential_alias="CRM_PRIMARY",
        action="discover",
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
                now=NOW + timedelta(minutes=1),
            )
        except PermissionError:
            return "denied"
        return "accepted"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _: consume(), range(2)))

    assert sorted(results) == ["accepted", "denied"]
