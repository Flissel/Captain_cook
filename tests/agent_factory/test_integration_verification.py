from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agenten.agent_factory.integration_setup import (
    IntegrationCredentialRequirementV1,
    N8nCredentialMetadataV1,
)
from agenten.agent_factory.integration_verification import seal_provider_verification
from agenten.targets.n8n import (
    N8nDeployment,
    N8nExecutionEvidence,
    SealedArtifact,
)


NOW = datetime(2026, 8, 5, 8, tzinfo=timezone.utc)
CORRELATION_ID = "20000000-0000-4000-8000-000000000001"


def requirement() -> IntegrationCredentialRequirementV1:
    return IntegrationCredentialRequirementV1(
        integration_key="crm",
        credential_alias="CRM_API_KEY",
        credential_type="httpBearerAuth",
        required=True,
        setup_method="n8n_ui",
        setup_label="Bearer Auth",
        project_id="captain-production",
    )


def credential() -> N8nCredentialMetadataV1:
    return N8nCredentialMetadataV1(
        credential_id="cred-prod",
        credential_name="CRM production",
        credential_type="httpBearerAuth",
        project_id="captain-production",
        project_name="Captain production",
    )


def artifact() -> SealedArtifact:
    return SealedArtifact(
        artifact_id="crm-auth-probe",
        artifact_digest="a" * 64,
        namespace="integration-verification",
        workflow={"nodes": [], "connections": {}},
    )


def deployment() -> N8nDeployment:
    return N8nDeployment(
        workflow_id="workflow-1",
        workflow_name="captain::integration-verification::crm-auth-probe",
        webhook_path="captain-integration-verification-crm-auth-probe",
        artifact_digest="a" * 64,
    )


def evidence() -> N8nExecutionEvidence:
    return N8nExecutionEvidence(
        execution_id="execution-1",
        workflow_id="workflow-1",
        artifact_digest="a" * 64,
        correlation_id=CORRELATION_ID,
        status="success",
    )


def test_matching_provider_execution_seals_secret_free_gateway_receipt() -> None:
    receipt = seal_provider_verification(
        requirement=requirement(),
        credential=credential(),
        workflow_artifact=artifact(),
        deployment=deployment(),
        execution=evidence(),
        expected_correlation_id=CORRELATION_ID,
        occurred_at=NOW,
        valid_until=NOW + timedelta(hours=1),
    )

    assert receipt.integration_key == "crm"
    assert receipt.credential_alias == "CRM_API_KEY"
    assert receipt.credential_id == "cred-prod"
    assert receipt.project_id == "captain-production"
    assert receipt.workflow_content_sha256 == "a" * 64
    assert receipt.workflow_ref.uri == "artifact://n8n-workflow/" + "a" * 64
    assert receipt.execution_ref.uri.startswith("artifact://n8n-execution/")
    assert receipt.status == "passed"
    rendered = receipt.model_dump_json()
    for forbidden in ("token", "password", "authorization", "client_secret"):
        assert forbidden not in rendered.lower()


@pytest.mark.parametrize(
    "changed",
    (
        {"workflow_id": "foreign-workflow"},
        {"artifact_digest": "b" * 64},
        {"correlation_id": "foreign-correlation"},
    ),
)
def test_mismatched_provider_execution_cannot_be_sealed(
    changed: dict[str, str],
) -> None:
    with pytest.raises(ValueError, match="execution evidence"):
        seal_provider_verification(
            requirement=requirement(),
            credential=credential(),
            workflow_artifact=artifact(),
            deployment=deployment(),
            execution=evidence().model_copy(update=changed),
            expected_correlation_id=CORRELATION_ID,
            occurred_at=NOW,
        )


def test_foreign_project_or_workflow_digest_cannot_be_sealed() -> None:
    with pytest.raises(ValueError, match="credential metadata"):
        seal_provider_verification(
            requirement=requirement(),
            credential=credential().model_copy(update={"project_id": "foreign-project"}),
            workflow_artifact=artifact(),
            deployment=deployment(),
            execution=evidence(),
            expected_correlation_id=CORRELATION_ID,
            occurred_at=NOW,
        )

    with pytest.raises(ValueError, match="workflow deployment"):
        seal_provider_verification(
            requirement=requirement(),
            credential=credential(),
            workflow_artifact=artifact(),
            deployment=deployment().model_copy(update={"artifact_digest": "b" * 64}),
            execution=evidence(),
            expected_correlation_id=CORRELATION_ID,
            occurred_at=NOW,
        )
