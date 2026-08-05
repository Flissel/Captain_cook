from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from agenten.agent_factory.integration_setup import (
    IntegrationCredentialRequirementV1,
    N8nCredentialMetadataV1,
)
from agenten.agent_factory.integration_verification import seal_provider_verification
from agenten.agent_runtime.contracts import ArtifactRef
from agenten.targets.n8n import (
    N8nDeployment,
    N8nExecutionEvidence,
    N8nProviderEvidence,
    SealedArtifact,
)


NOW = datetime(2026, 8, 5, 8, tzinfo=timezone.utc)
CORRELATION_ID = "20000000-0000-4000-8000-000000000001"
PROBE_ID = UUID("50000000-0000-4000-8000-000000000001")


def requirement() -> IntegrationCredentialRequirementV1:
    return IntegrationCredentialRequirementV1(
        integration_key="crm",
        credential_alias="CRM_API_KEY",
        credential_type="httpBearerAuth",
        required=True,
        setup_method="n8n_ui",
        setup_label="Bearer Auth",
        project_id="captain-production",
        verification_workflow_sha256="a" * 64,
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
        provider=N8nProviderEvidence(
            trace_id="30000000-0000-4000-8000-000000000001",
            proof_sha256="c" * 64,
            kind="bearer",
            probe_id=PROBE_ID,
        ),
    )


def test_provider_evidence_rejects_non_uuid_probe_identity() -> None:
    with pytest.raises(ValueError):
        N8nProviderEvidence(
            trace_id="30000000-0000-4000-8000-000000000001",
            proof_sha256="c" * 64,
            kind="bearer",
            probe_id="portal-verification-r1",
        )

    accepted = N8nProviderEvidence(
        trace_id="30000000-0000-4000-8000-000000000001",
        proof_sha256="c" * 64,
        kind="bearer",
        probe_id="50000000-0000-4000-8000-000000000001",
    )
    assert accepted.probe_id == UUID("50000000-0000-4000-8000-000000000001")


def test_matching_provider_execution_seals_secret_free_gateway_receipt() -> None:
    receipt = seal_provider_verification(
        requirement=requirement(),
        credential=credential(),
        workflow_artifact=artifact(),
        deployment=deployment(),
        execution=evidence(),
        expected_correlation_id=CORRELATION_ID,
        expected_probe_id=PROBE_ID,
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
    assert str(receipt.provider_trace_id) == "30000000-0000-4000-8000-000000000001"
    assert receipt.provider_proof_sha256 == "c" * 64
    assert receipt.provider_kind == "bearer"
    assert receipt.provider_probe_id == PROBE_ID
    rendered = receipt.model_dump_json()
    for forbidden in ("token", "password", "authorization", "client_secret"):
        assert forbidden not in rendered.lower()


def test_oauth_client_credentials_seals_provider_bound_exchange_evidence() -> None:
    exchange_id = UUID("60000000-0000-4000-8000-000000000001")
    oauth_requirement = requirement().model_copy(
        update={"credential_alias": "CRM_OAUTH", "credential_type": "oAuth2Api"}
    )
    oauth_credential = credential().model_copy(
        update={"credential_id": "cred-oauth", "credential_type": "oAuth2Api"}
    )
    oauth_evidence = evidence().model_copy(
        update={
            "provider": N8nProviderEvidence(
                trace_id="30000000-0000-4000-8000-000000000001",
                proof_sha256="c" * 64,
                kind="oauth2",
                probe_id=PROBE_ID,
                oauth_exchange_id=exchange_id,
            )
        }
    )

    receipt = seal_provider_verification(
        requirement=oauth_requirement,
        credential=oauth_credential,
        workflow_artifact=artifact(),
        deployment=deployment(),
        execution=oauth_evidence,
        expected_correlation_id=CORRELATION_ID,
        expected_probe_id=PROBE_ID,
        occurred_at=NOW,
    )

    assert receipt.oauth_grant_type == "client_credentials"
    assert receipt.oauth_exchange_id == exchange_id
    assert receipt.oauth_exchange_ref is not None
    assert receipt.oauth_exchange_ref.uri == f"artifact://oauth-token-exchange/{exchange_id}"
    rendered = receipt.model_dump_json()
    for forbidden in ("access_token", "client_secret", "authorization"):
        assert forbidden not in rendered.lower()


def test_bound_workflow_keeps_release_and_deployment_digests_distinct() -> None:
    template_ref = ArtifactRef(
        uri="artifact://gitea/" + "a" * 64,
        sha256="a" * 64,
        media_type="application/json",
    )
    bound_artifact = artifact().model_copy(update={"artifact_digest": "b" * 64})
    bound_deployment = deployment().model_copy(update={"artifact_digest": "b" * 64})
    bound_execution = evidence().model_copy(update={"artifact_digest": "b" * 64})

    receipt = seal_provider_verification(
        requirement=requirement(),
        credential=credential(),
        template_ref=template_ref,
        workflow_artifact=bound_artifact,
        deployment=bound_deployment,
        execution=bound_execution,
        expected_correlation_id=CORRELATION_ID,
        expected_probe_id=PROBE_ID,
        occurred_at=NOW,
    )

    assert receipt.template_ref == template_ref
    assert receipt.template_content_sha256 == "a" * 64
    assert receipt.workflow_ref.sha256 == "b" * 64
    assert receipt.workflow_content_sha256 == "b" * 64


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
            expected_probe_id=PROBE_ID,
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
            expected_probe_id=PROBE_ID,
            occurred_at=NOW,
        )

    foreign_artifact = artifact().model_copy(update={"artifact_digest": "b" * 64})
    with pytest.raises(ValueError, match="Captain verification workflow"):
        seal_provider_verification(
            requirement=requirement(),
            credential=credential(),
            workflow_artifact=foreign_artifact,
            deployment=deployment().model_copy(update={"artifact_digest": "b" * 64}),
            execution=evidence().model_copy(update={"artifact_digest": "b" * 64}),
            expected_correlation_id=CORRELATION_ID,
            expected_probe_id=PROBE_ID,
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
            expected_probe_id=PROBE_ID,
            occurred_at=NOW,
        )


@pytest.mark.parametrize(
    "provider",
    (
        None,
        N8nProviderEvidence(
            trace_id="30000000-0000-4000-8000-000000000001",
            proof_sha256="c" * 64,
            kind="oauth2",
            probe_id=PROBE_ID,
            oauth_exchange_id=UUID(
                "60000000-0000-4000-8000-000000000001"
            ),
        ),
        N8nProviderEvidence(
            trace_id="30000000-0000-4000-8000-000000000001",
            proof_sha256="c" * 64,
            kind="bearer",
            probe_id=UUID("50000000-0000-4000-8000-000000000002"),
        ),
    ),
)
def test_provider_verification_requires_bound_provider_proof(provider) -> None:
    with pytest.raises(ValueError, match="provider evidence"):
        seal_provider_verification(
            requirement=requirement(),
            credential=credential(),
            workflow_artifact=artifact(),
            deployment=deployment(),
            execution=evidence().model_copy(update={"provider": provider}),
            expected_correlation_id=CORRELATION_ID,
            expected_probe_id=PROBE_ID,
            occurred_at=NOW,
        )
