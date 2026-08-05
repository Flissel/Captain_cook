from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from gateway.portal_contracts import (
    PortalPrincipalV1,
    PortalTenantBindingV1,
    PortalSetupSelectionRequestV1,
    PortalSetupTicketIssueV1,
    PortalSetupTicketUseV1,
    PortalSetupActionRequestV1,
    PortalSetupTicketRequestV1,
    PortalSetupTicketV1,
)


def test_portal_tenant_binding_is_strict_and_secret_free() -> None:
    binding = PortalTenantBindingV1(job_id=JOB_ID, organization_id="org-a")

    assert binding.organization_id == "org-a"
    with pytest.raises(ValidationError):
        PortalTenantBindingV1.model_validate(
            {"job_id": str(JOB_ID), "organization_id": "org-a", "api_key": "no"}
        )


def test_portal_operation_requests_reject_secret_shaped_fields() -> None:
    issue = PortalSetupTicketIssueV1(
        credential_alias="CRM_PRIMARY",
        action="discover",
    )
    use = PortalSetupTicketUseV1(
        ticket_id=UUID("10000000-0000-0000-0000-000000000010"),
        ticket="opaque-ticket",
        credential_alias="CRM_PRIMARY",
    )
    selection = PortalSetupSelectionRequestV1(
        ticket_id=use.ticket_id,
        ticket=use.ticket,
        credential_alias=use.credential_alias,
        credential_id="credential-1",
    )

    assert issue.action == "discover"
    assert selection.credential_id == "credential-1"
    with pytest.raises(ValidationError):
        PortalSetupTicketIssueV1.model_validate(
            {
                "credential_alias": "CRM_PRIMARY",
                "action": "discover",
                "api_key": "must-not-enter",
            }
        )


NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
JOB_ID = UUID("10000000-0000-0000-0000-000000000001")
TICKET_ID = UUID("20000000-0000-0000-0000-000000000001")


def ticket_request_payload() -> dict[str, object]:
    return {
        "job_id": JOB_ID,
        "organization_id": "org-a",
        "subject_id": "user-a",
        "credential_alias": "CRM_PRIMARY",
        "issued_at": NOW,
        "expires_at": NOW + timedelta(minutes=10),
    }


def test_portal_contracts_accept_a_valid_ten_minute_ticket() -> None:
    principal = PortalPrincipalV1(subject_id="user-a", organization_id="org-a")
    request = PortalSetupTicketRequestV1(**ticket_request_payload())
    ticket = PortalSetupTicketV1(
        ticket_id=TICKET_ID,
        ticket="opaque-setup-ticket",
        job_id=JOB_ID,
        credential_alias="CRM_PRIMARY",
        expires_at=NOW + timedelta(minutes=10),
    )
    action = PortalSetupActionRequestV1(
        ticket_id=TICKET_ID,
        ticket="opaque-setup-ticket",
        credential_alias="CRM_PRIMARY",
        action="rotation_requested",
    )

    assert principal.organization_id == "org-a"
    assert request.expires_at == NOW + timedelta(minutes=10)
    assert ticket.job_id == JOB_ID
    assert action.action == "rotation_requested"


def test_ticket_rejects_expiry_longer_than_ten_minutes() -> None:
    with pytest.raises(ValidationError, match="portal ticket expiry must be at most ten minutes"):
        PortalSetupTicketRequestV1(
            **(ticket_request_payload() | {"expires_at": NOW + timedelta(minutes=11)})
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("issued_at", datetime(2026, 8, 5, 12, 0)),
        ("expires_at", datetime(2026, 8, 5, 12, 10, tzinfo=timezone(timedelta(hours=1)))),
    ],
)
def test_ticket_rejects_naive_or_non_utc_timestamps(field: str, value: datetime) -> None:
    with pytest.raises(ValidationError, match="UTC"):
        PortalSetupTicketRequestV1(**(ticket_request_payload() | {field: value}))


def test_contracts_reject_invalid_identifiers_and_secret_extras_without_echoing_values() -> None:
    with pytest.raises(ValidationError):
        PortalPrincipalV1(subject_id="bad id", organization_id="org-a")

    with pytest.raises(ValidationError) as raised:
        PortalSetupTicketRequestV1(
            **(ticket_request_payload() | {"client_secret": "must-not-appear"})
        )

    assert "must-not-appear" not in str(raised.value)


def test_contracts_are_frozen_and_dumps_do_not_include_undeclared_secret_fields() -> None:
    request = PortalSetupTicketRequestV1(**ticket_request_payload())

    with pytest.raises(ValidationError):
        request.credential_alias = "OTHER"  # type: ignore[misc]

    dumped = request.model_dump(mode="json")
    assert "client_secret" not in dumped
    assert "refresh_token" not in dumped
