"""Seal provider-backed n8n execution evidence into a secret-free receipt."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from agenten.agent_factory.integration_setup import (
    CredentialVerificationReceiptV1,
    IntegrationCredentialRequirementV1,
    N8nCredentialMetadataV1,
)
from agenten.agent_runtime.contracts import ArtifactRef
from agenten.targets.n8n import (
    N8nDeployment,
    N8nExecutionEvidence,
    SealedArtifact,
)


def seal_provider_verification(
    *,
    requirement: IntegrationCredentialRequirementV1,
    credential: N8nCredentialMetadataV1,
    workflow_artifact: SealedArtifact,
    deployment: N8nDeployment,
    execution: N8nExecutionEvidence,
    expected_correlation_id: str,
    occurred_at: datetime,
    valid_until: datetime | None = None,
) -> CredentialVerificationReceiptV1:
    """Fail closed unless credential, workflow and execution bindings agree."""

    if (
        credential.credential_type != requirement.credential_type
        or credential.project_id != requirement.project_id
    ):
        raise ValueError("credential metadata does not match setup requirement")
    if deployment.artifact_digest != workflow_artifact.artifact_digest:
        raise ValueError("workflow deployment does not match sealed artifact")
    if (
        execution.workflow_id != deployment.workflow_id
        or execution.artifact_digest != deployment.artifact_digest
        or execution.correlation_id != expected_correlation_id
        or execution.status != "success"
    ):
        raise ValueError("execution evidence does not match verified deployment")

    execution_payload = execution.model_dump(mode="json")
    execution_digest = hashlib.sha256(
        json.dumps(
            execution_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    workflow_digest = workflow_artifact.artifact_digest
    return CredentialVerificationReceiptV1(
        integration_key=requirement.integration_key,
        credential_alias=requirement.credential_alias,
        credential_id=credential.credential_id,
        credential_type=credential.credential_type,
        project_id=credential.project_id,
        status="passed",
        occurred_at=occurred_at,
        workflow_ref=ArtifactRef(
            uri=f"artifact://n8n-workflow/{workflow_digest}",
            sha256=workflow_digest,
            media_type="application/json",
        ),
        workflow_content_sha256=workflow_digest,
        execution_ref=ArtifactRef(
            uri=f"artifact://n8n-execution/{execution_digest}",
            sha256=execution_digest,
            media_type="application/json",
        ),
        valid_until=valid_until,
    )
