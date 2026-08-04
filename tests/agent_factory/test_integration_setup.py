from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from agenten.agent_factory.input_contracts import RequestedIntegration
from agenten.agent_factory.integration_setup import (
    CredentialVerificationReceiptV1,
    IntegrationCredentialRequirementV1,
    IntegrationSetupPlanner,
    IntegrationSetupStatus,
    N8nCredentialMetadataV1,
    require_required_integrations_ready,
)
from agenten.agent_runtime.contracts import ArtifactRef


NOW = datetime(2026, 8, 4, 10, tzinfo=timezone.utc)


def integration(*, required: bool = True) -> RequestedIntegration:
    return RequestedIntegration(
        integration_key="crm",
        purpose="Read customer records",
        trigger="A released agent requests customer context",
        operation="Read one customer record",
        required=required,
        credential_aliases=("CRM_API_KEY",),
        success_behavior="Return the typed customer record",
        failure_behavior="Escalate without inventing customer data",
    )


def requirement(*, required: bool = True) -> IntegrationCredentialRequirementV1:
    return IntegrationCredentialRequirementV1(
        integration_key="crm",
        credential_alias="CRM_API_KEY",
        credential_type="httpBearerAuth",
        required=required,
        setup_method="n8n_ui",
        setup_label="Bearer Auth",
    )


def credential(
    credential_id: str = "cred-prod",
    *,
    name: str = "CRM production",
) -> N8nCredentialMetadataV1:
    return N8nCredentialMetadataV1(
        credential_id=credential_id,
        credential_name=name,
        credential_type="httpBearerAuth",
        project_id="captain-production",
        project_name="Captain production",
    )


def receipt(
    credential_id: str = "cred-prod",
    *,
    status: str = "passed",
) -> CredentialVerificationReceiptV1:
    return CredentialVerificationReceiptV1(
        integration_key="crm",
        credential_alias="CRM_API_KEY",
        credential_id=credential_id,
        credential_type="httpBearerAuth",
        status=status,
        occurred_at=NOW,
        workflow_ref=ArtifactRef(
            uri="artifact://n8n-workflow/" + "a" * 64,
            sha256="a" * 64,
            media_type="application/json",
        ),
        execution_ref=ArtifactRef(
            uri="artifact://n8n-execution/" + "b" * 64,
            sha256="b" * 64,
            media_type="application/json",
        ),
    )


def test_required_missing_credential_blocks_readiness_without_secret_fields() -> None:
    plan = IntegrationSetupPlanner().plan(
        integrations=(integration(),),
        requirements=(requirement(),),
        credentials=(),
    )

    assert plan.connections[0].status is IntegrationSetupStatus.MISSING
    assert plan.connections[0].candidate_credentials == ()
    with pytest.raises(PermissionError, match="CRM_API_KEY:missing"):
        require_required_integrations_ready(plan)
    assert "API_KEY" not in plan.model_dump_json().replace("CRM_API_KEY", "")


def test_one_exact_metadata_match_still_requires_provider_verification() -> None:
    plan = IntegrationSetupPlanner().plan(
        integrations=(integration(),),
        requirements=(requirement(),),
        credentials=(credential(),),
    )

    connection = plan.connections[0]
    assert connection.status is IntegrationSetupStatus.VERIFICATION_REQUIRED
    assert connection.selected_credential == credential()
    with pytest.raises(PermissionError, match="verification_required"):
        require_required_integrations_ready(plan)


def test_multiple_matches_require_explicit_selection() -> None:
    credentials = (
        credential("cred-dev", name="CRM development"),
        credential("cred-prod", name="CRM production"),
    )

    unresolved = IntegrationSetupPlanner().plan(
        integrations=(integration(),),
        requirements=(requirement(),),
        credentials=credentials,
    )
    selected = IntegrationSetupPlanner().plan(
        integrations=(integration(),),
        requirements=(requirement(),),
        credentials=credentials,
        selected_credential_ids={"CRM_API_KEY": "cred-prod"},
    )

    assert unresolved.connections[0].status is IntegrationSetupStatus.SELECTION_REQUIRED
    assert unresolved.connections[0].selected_credential is None
    assert selected.connections[0].status is IntegrationSetupStatus.VERIFICATION_REQUIRED
    assert selected.connections[0].selected_credential == credential()


def test_matching_passed_probe_is_the_only_path_to_ready() -> None:
    plan = IntegrationSetupPlanner().plan(
        integrations=(integration(),),
        requirements=(requirement(),),
        credentials=(credential(),),
        verification_receipts=(receipt(),),
    )

    assert plan.connections[0].status is IntegrationSetupStatus.READY
    assert require_required_integrations_ready(plan) == plan


def test_failed_probe_remains_blocking_and_visible() -> None:
    plan = IntegrationSetupPlanner().plan(
        integrations=(integration(),),
        requirements=(requirement(),),
        credentials=(credential(),),
        verification_receipts=(receipt(status="failed"),),
    )

    assert plan.connections[0].status is IntegrationSetupStatus.VERIFICATION_FAILED
    with pytest.raises(PermissionError, match="verification_failed"):
        require_required_integrations_ready(plan)


def test_optional_missing_credential_remains_visible_but_does_not_block() -> None:
    plan = IntegrationSetupPlanner().plan(
        integrations=(integration(required=False),),
        requirements=(requirement(required=False),),
        credentials=(),
    )

    assert plan.connections[0].status is IntegrationSetupStatus.MISSING
    assert require_required_integrations_ready(plan) == plan


def test_requirement_and_receipt_must_match_released_input_exactly() -> None:
    wrong_requirement = requirement().model_copy(
        update={"credential_alias": "UNKNOWN_API_KEY"}
    )
    with pytest.raises(ValueError, match="exactly match"):
        IntegrationSetupPlanner().plan(
            integrations=(integration(),),
            requirements=(wrong_requirement,),
            credentials=(),
        )

    with pytest.raises(ValueError, match="selected credential"):
        IntegrationSetupPlanner().plan(
            integrations=(integration(),),
            requirements=(requirement(),),
            credentials=(credential(),),
            verification_receipts=(receipt("cred-other"),),
        )


def test_credential_metadata_rejects_secret_data() -> None:
    with pytest.raises(ValidationError):
        N8nCredentialMetadataV1.model_validate(
            credential().model_dump() | {"token": "must-not-enter-captain"}
        )
