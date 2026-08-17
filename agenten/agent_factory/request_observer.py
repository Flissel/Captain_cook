"""Captain validation and dual durable projection of Factory observations."""

from __future__ import annotations

from datetime import datetime
from collections.abc import Callable
from typing import Protocol
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field

from agenten.agent_factory.forge_contracts import (
    ArtifactRef,
    CreationJobV3,
    FactoryActionRequestV1,
)


class FactoryCredentialRequiredObservation(BaseModel):
    """Sanitized Hermes/Minibook observation; never carries credential bytes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    factory_job_id: UUID
    creation_job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1)
    integration_key: str
    required_scope: str = Field(min_length=1, max_length=200)
    safe_checkpoint_ref: ArtifactRef
    message: str = Field(min_length=1, max_length=500)
    observed_at: datetime


class FactoryActionMinibookProjectionPort(Protocol):
    def project_action_request(
        self, request: FactoryActionRequestV1, *, expected_version: int
    ) -> None: ...


class FactoryActionInboxProjectionPort(Protocol):
    def create(self, request: FactoryActionRequestV1, *, tenant_id: str) -> object: ...


class FactoryActionProgressPort(Protocol):
    @property
    def version(self) -> int: ...


class CaptainFactoryActionRequestCoordinator:
    """Create one request only after binding an observation to Captain policy."""

    def __init__(
        self,
        *,
        creation_job: CreationJobV3,
        minibook_projection: FactoryActionMinibookProjectionPort,
        action_inbox: FactoryActionInboxProjectionPort,
        progress_for: Callable[[UUID], FactoryActionProgressPort],
        tenant_id: str,
        deadline_for: Callable[[datetime], datetime],
    ) -> None:
        self._job = creation_job
        self._minibook = minibook_projection
        self._inbox = action_inbox
        self._progress_for = progress_for
        self._tenant_id = tenant_id
        self._deadline_for = deadline_for

    def observe(
        self, observation: FactoryCredentialRequiredObservation
    ) -> FactoryActionRequestV1:
        job = self._job
        if (
            observation.factory_job_id != job.factory_job_id
            or observation.creation_job_id != job.creation_job_id
            or observation.correlation_id != job.correlation_id
            or observation.subject_version != job.subject_version
        ):
            raise ValueError("Factory action observation identity changed")
        if "credential_required" not in job.clarification_policy.allowed_request_types:
            raise ValueError("Captain policy does not allow credential requests")
        if observation.integration_key not in {
            item.tool_id for item in job.tool_requirements if item.required
        }:
            raise ValueError("credential observation is not for a required tool")
        progress = self._progress_for(job.creation_job_id)
        request = FactoryActionRequestV1(
            schema_name="captain.factory-action-request.v1",
            request_id=uuid5(
                job.creation_job_id,
                f"credential:{observation.integration_key}:{observation.safe_checkpoint_ref.sha256}",
            ),
            factory_job_id=job.factory_job_id,
            creation_job_id=job.creation_job_id,
            correlation_id=job.correlation_id,
            expected_subject_version=job.subject_version,
            request_type="credential_required",
            safe_checkpoint_ref=observation.safe_checkpoint_ref,
            safe_checkpoint_sha256=observation.safe_checkpoint_ref.sha256,
            required_scope=observation.required_scope,
            allowed_decisions=("approve", "deny"),
            message=observation.message,
            deadline_at=self._deadline_for(observation.observed_at),
            status="pending",
        )
        self._minibook.project_action_request(
            request,
            expected_version=progress.version,
        )
        self._inbox.create(request, tenant_id=self._tenant_id)
        return request


__all__ = [
    "CaptainFactoryActionRequestCoordinator",
    "FactoryCredentialRequiredObservation",
]
