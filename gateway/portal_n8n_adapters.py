"""Captain-authorized n8n adapters for the self-service portal."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from agenten.agent_factory.contracts import FactoryJob, FactoryLease, FactoryRole
from agenten.agent_factory.gitea_template_contracts import GiteaTemplateReleaseV1
from agenten.agent_factory.gitea_templates import VerifiedTemplatePayload
from agenten.agent_factory.integration_setup import (
    CredentialVerificationReceiptV1,
    IntegrationCredentialRequirementV1,
    N8nCredentialMetadataV1,
)
from agenten.agent_factory.integration_verification import seal_provider_verification
from agenten.agent_factory.integration_verification_workflow import (
    materialize_verification_workflow,
)
from agenten.agent_factory.leases import FactoryLeaseDenied, validate_factory_lease
from agenten.agent_runtime.contracts import IntegrationIntent
from agenten.agent_runtime.n8n_endpoint import N8nEndpoint
from agenten.targets.n8n import (
    N8nDeployment,
    N8nExecutionEvidence,
    SealedArtifact,
    ValidationCase,
)


class PortalN8nLeaseSource(Protocol):
    def active_n8n(
        self,
        *,
        job_id: UUID,
        correlation_id: UUID,
        now: datetime,
    ) -> FactoryLease: ...


class FactoryJobEnvelope(Protocol):
    job: FactoryJob
    leases: tuple[FactoryLease, ...]


class FactoryJobReader(Protocol):
    def factory_job(self, job_id: UUID) -> FactoryJobEnvelope: ...


class GatewayPortalN8nLeaseSource:
    def __init__(self, reader: FactoryJobReader) -> None:
        self._reader = reader

    def active_n8n(
        self,
        *,
        job_id: UUID,
        correlation_id: UUID,
        now: datetime,
    ) -> FactoryLease:
        projection = self._reader.factory_job(job_id)
        job = projection.job
        candidates = sorted(
            (
                lease
                for lease in projection.leases
                if lease.job_id == job_id
                and lease.correlation_id == correlation_id
                and lease.subject_version == job.subject_version
                and lease.role is FactoryRole.TOOL_INTEGRATOR
                and lease.integration_intent is IntegrationIntent.N8N
                and "mcp.n8n" in lease.capabilities
            ),
            key=lambda lease: lease.issued_at,
            reverse=True,
        )
        for lease in candidates:
            try:
                return validate_factory_lease(
                    lease,
                    job=job,
                    role=FactoryRole.TOOL_INTEGRATOR,
                    attempt=lease.attempt,
                    now=now,
                )
            except FactoryLeaseDenied:
                continue
        raise PermissionError("credential operation requires an active Captain n8n lease")


class N8nMetadataClient(Protocol):
    async def discover(
        self,
        *,
        lease: FactoryLease,
        endpoint: N8nEndpoint,
        requirement: IntegrationCredentialRequirementV1,
        now: datetime,
        timeout_seconds: float,
    ) -> tuple[N8nCredentialMetadataV1, ...]: ...


class PortalN8nCredentialMetadataSource:
    """Sync Gateway facade around the lease-bound asynchronous MCP client."""

    def __init__(
        self,
        *,
        leases: PortalN8nLeaseSource,
        client: N8nMetadataClient,
        endpoint: N8nEndpoint,
        timeout_seconds: float = 10.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("credential discovery timeout must be positive")
        self._leases = leases
        self._client = client
        self._endpoint = endpoint
        self._timeout_seconds = timeout_seconds

    def list_credentials(
        self,
        *,
        requirement: IntegrationCredentialRequirementV1,
        job_id: UUID,
        correlation_id: UUID,
        now: datetime,
    ) -> tuple[N8nCredentialMetadataV1, ...]:
        lease = self._leases.active_n8n(
            job_id=job_id,
            correlation_id=correlation_id,
            now=now,
        )
        return asyncio.run(
            self._client.discover(
                lease=lease,
                endpoint=self._endpoint,
                requirement=requirement,
                now=now,
                timeout_seconds=self._timeout_seconds,
            )
        )


class VerificationReleaseSource(Protocol):
    def by_sha256(self, sha256: str) -> GiteaTemplateReleaseV1: ...


class VerifiedTemplateSource(Protocol):
    async def fetch_verified_payload(
        self,
        release: GiteaTemplateReleaseV1,
    ) -> VerifiedTemplatePayload: ...


class VerificationN8nTarget(Protocol):
    async def deploy(self, artifact: SealedArtifact) -> N8nDeployment: ...

    async def execute(
        self,
        deployment: N8nDeployment,
        case: ValidationCase,
    ) -> N8nExecutionEvidence: ...


class PortalN8nCredentialVerificationSource:
    """Run one lease-authorized, Gitea-pinned provider probe in n8n."""

    def __init__(
        self,
        *,
        leases: PortalN8nLeaseSource,
        releases: VerificationReleaseSource,
        templates: VerifiedTemplateSource,
        target: VerificationN8nTarget,
    ) -> None:
        self._leases = leases
        self._releases = releases
        self._templates = templates
        self._target = target

    def verify_credential(
        self,
        *,
        requirement: IntegrationCredentialRequirementV1,
        credential: N8nCredentialMetadataV1,
        job_id: UUID,
        correlation_id: UUID,
        expected_content_sha256: str,
        expected_revision: int,
        expected_workflow_content_sha256: str,
        probe_id: UUID | None = None,
        now: datetime,
    ) -> CredentialVerificationReceiptV1:
        if requirement.verification_workflow_sha256 != expected_workflow_content_sha256:
            raise ValueError("verification workflow release changed")
        self._leases.active_n8n(
            job_id=job_id,
            correlation_id=correlation_id,
            now=now,
        )
        return asyncio.run(
            self._verify(
                requirement=requirement,
                credential=credential,
                job_id=job_id,
                correlation_id=correlation_id,
                expected_content_sha256=expected_content_sha256,
                expected_revision=expected_revision,
                probe_id=probe_id,
                now=now,
            )
        )

    async def _verify(
        self,
        *,
        requirement: IntegrationCredentialRequirementV1,
        credential: N8nCredentialMetadataV1,
        job_id: UUID,
        correlation_id: UUID,
        expected_content_sha256: str,
        expected_revision: int,
        probe_id: UUID | None,
        now: datetime,
    ) -> CredentialVerificationReceiptV1:
        assert requirement.verification_workflow_sha256 is not None
        release = self._releases.by_sha256(requirement.verification_workflow_sha256)
        template = await self._templates.fetch_verified_payload(release)
        bound = materialize_verification_workflow(
            template=template,
            requirement=requirement,
            credential=credential,
        )
        deployment = await self._target.deploy(bound.artifact)
        if probe_id is None:
            probe_id = uuid5(
                NAMESPACE_URL,
                (
                    "captain:portal-verification:"
                    f"{job_id}:{correlation_id}:{expected_revision}:{credential.credential_id}"
                ),
            )
        execution = await self._target.execute(
            deployment,
            ValidationCase(
                case_id=str(probe_id),
                correlation_id=str(correlation_id),
                input_payload={
                    "setup_content_sha256": expected_content_sha256,
                    "setup_revision": expected_revision,
                },
            ),
        )
        return seal_provider_verification(
            requirement=requirement,
            credential=credential,
            template_ref=bound.template_ref,
            template_release=release,
            workflow_artifact=bound.artifact,
            deployment=deployment,
            execution=execution,
            expected_correlation_id=str(correlation_id),
            expected_probe_id=probe_id,
            occurred_at=now,
        )
