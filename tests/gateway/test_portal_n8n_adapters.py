from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from uuid import UUID
from types import SimpleNamespace

import pytest

from agenten.agent_factory.contracts import FactoryRole
from agenten.agent_factory.integration_setup import N8nCredentialMetadataV1
from agenten.agent_factory.gitea_template_contracts import GiteaTemplateReleaseV1
from agenten.agent_factory.gitea_templates import VerifiedTemplatePayload
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_runtime.contracts import IntegrationIntent
from agenten.agent_runtime.contracts import ArtifactRef
from agenten.targets.n8n import (
    N8nDeployment,
    N8nExecutionEvidence,
    N8nProviderEvidence,
)
from gateway.portal_n8n_adapters import (
    PortalN8nCredentialMetadataSource,
    PortalN8nCredentialVerificationSource,
    GatewayPortalN8nLeaseSource,
)
from tests.agent_factory.test_integration_setup_n8n import endpoint, requirement
from tests.agent_factory.test_state_machine import job


NOW = datetime(2026, 8, 5, 15, tzinfo=timezone.utc)


class StaticLeaseSource:
    def __init__(self) -> None:
        factory_job = job()
        self.lease = issue_factory_lease(
            job=factory_job,
            role=FactoryRole.TOOL_INTEGRATOR,
            attempt=1,
            workspace_ref="workspace://portal/n8n-discovery",
            now=NOW,
            integration_intent=IntegrationIntent.N8N,
        )

    def active_n8n(self, *, job_id: UUID, correlation_id: UUID, now: datetime):
        assert job_id == self.lease.job_id
        assert correlation_id == self.lease.correlation_id
        assert now == NOW
        return self.lease


class RecordingMetadataClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def discover(self, **kwargs):
        self.calls.append(kwargs)
        return (
            N8nCredentialMetadataV1(
                credential_id="cred-prod",
                credential_name="CRM production",
                credential_type="httpBearerAuth",
                project_id="captain-production",
            ),
        )


def test_discovery_adapter_uses_exact_captain_lease_and_endpoint() -> None:
    leases = StaticLeaseSource()
    client = RecordingMetadataClient()
    source = PortalN8nCredentialMetadataSource(
        leases=leases,
        client=client,
        endpoint=endpoint(),
        timeout_seconds=5.0,
    )

    result = source.list_credentials(
        requirement=requirement(),
        job_id=leases.lease.job_id,
        correlation_id=leases.lease.correlation_id,
        now=NOW,
    )

    assert tuple(item.credential_id for item in result) == ("cred-prod",)
    assert client.calls == [
        {
            "lease": leases.lease,
            "endpoint": endpoint(),
            "requirement": requirement(),
            "now": NOW,
            "timeout_seconds": 5.0,
        }
    ]


def test_gateway_lease_source_returns_only_exact_active_n8n_authority() -> None:
    leases = StaticLeaseSource()

    class Reader:
        def factory_job(self, job_id: UUID):
            assert job_id == leases.lease.job_id
            return SimpleNamespace(job=job(), leases=(leases.lease,))

    source = GatewayPortalN8nLeaseSource(Reader())

    resolved = source.active_n8n(
        job_id=leases.lease.job_id,
        correlation_id=leases.lease.correlation_id,
        now=NOW,
    )
    assert resolved == leases.lease

    with pytest.raises(PermissionError, match="active Captain n8n lease"):
        source.active_n8n(
            job_id=leases.lease.job_id,
            correlation_id=UUID("ffffffff-ffff-4fff-8fff-ffffffffffff"),
            now=NOW,
        )


class StaticReleaseSource:
    def __init__(self, release: GiteaTemplateReleaseV1) -> None:
        self.release = release

    def by_sha256(self, sha256: str) -> GiteaTemplateReleaseV1:
        assert sha256 == self.release.sha256
        return self.release


class StaticTemplateClient:
    def __init__(self, payload: VerifiedTemplatePayload) -> None:
        self.payload = payload

    async def fetch_verified_payload(self, release: GiteaTemplateReleaseV1):
        assert release.sha256 == self.payload.ref.sha256
        return self.payload


class RecordingTarget:
    def __init__(self) -> None:
        self.artifact = None
        self.case = None

    async def deploy(self, artifact):
        self.artifact = artifact
        return N8nDeployment(
            workflow_id="workflow-1",
            workflow_name="captain::verification",
            webhook_path="captain-verification",
            artifact_digest=artifact.artifact_digest,
        )

    async def execute(self, deployment, case):
        self.case = case
        return N8nExecutionEvidence(
            execution_id="execution-1",
            workflow_id=deployment.workflow_id,
            artifact_digest=deployment.artifact_digest,
            correlation_id=case.correlation_id,
            status="success",
            provider=N8nProviderEvidence(
                trace_id="40000000-0000-4000-8000-000000000001",
                proof_sha256="f" * 64,
                kind="bearer",
                probe_id=case.case_id,
            ),
        )


def test_verification_adapter_seals_template_and_bound_workflow_digests() -> None:
    workflow = {
        "nodes": [
            {
                "name": "Provider probe",
                "type": "n8n-nodes-base.httpRequest",
                "parameters": {"url": "https://provider.example/health"},
                "credentials": {
                    "httpBearerAuth": {
                        "id": "{{CAPTAIN_CREDENTIAL_ID}}",
                        "name": "{{CAPTAIN_CREDENTIAL_NAME}}",
                    }
                },
            }
        ],
        "connections": {},
    }
    content = json.dumps(workflow, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(content).hexdigest()
    payload = VerifiedTemplatePayload(
        ref=ArtifactRef(
            uri=f"artifact://gitea/{digest}",
            sha256=digest,
            media_type="application/json",
        ),
        content=content,
    )
    release = GiteaTemplateReleaseV1(
        repository="captain/templates",
        revision="1" * 40,
        path="verification/bearer.json",
        contents_url=(
            "https://gitea.example/captain/templates/raw/commit/"
            + "1" * 40
            + "/verification/bearer.json"
        ),
        sha256=digest,
    )
    leases = StaticLeaseSource()
    target = RecordingTarget()
    source = PortalN8nCredentialVerificationSource(
        leases=leases,
        releases=StaticReleaseSource(release),
        templates=StaticTemplateClient(payload),
        target=target,
    )
    expected_requirement = requirement().model_copy(
        update={"verification_workflow_sha256": digest}
    )
    selected = N8nCredentialMetadataV1(
        credential_id="cred-prod",
        credential_name="CRM production",
        credential_type="httpBearerAuth",
        project_id="captain-production",
    )

    receipt = source.verify_credential(
        requirement=expected_requirement,
        credential=selected,
        job_id=leases.lease.job_id,
        correlation_id=leases.lease.correlation_id,
        expected_content_sha256="c" * 64,
        expected_revision=7,
        expected_workflow_content_sha256=digest,
        now=NOW,
    )

    assert receipt.template_content_sha256 == digest
    assert receipt.workflow_content_sha256 == target.artifact.artifact_digest
    assert receipt.workflow_content_sha256 != digest
    assert target.case.input_payload == {
        "setup_content_sha256": "c" * 64,
        "setup_revision": 7,
    }
