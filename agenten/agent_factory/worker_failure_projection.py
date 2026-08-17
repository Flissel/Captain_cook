"""Production coordination of durable Hermes failure observations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from typing import Callable, Protocol
from uuid import uuid5

from agenten.agent_factory.contracts import (
    AgentFactoryJobV3,
    FactoryBlockStatus,
    FactoryEvidenceBlock,
    FactoryLease,
    FactoryPhase,
    FactoryRole,
)
from agenten.agent_factory.forge_contracts import (
    ArtifactRef,
    FactoryBuildAssignmentV2,
)
from agenten.agent_factory.hermes_factory_worker import HermesFactoryWorkerCheckpoint
from agenten.agent_runtime.contracts import ArtifactRef as RuntimeArtifactRef


class FactoryWorkerFailureMinibookProjectionPort(Protocol):
    def block_worker_failure(
        self,
        assignment: FactoryBuildAssignmentV2,
        *,
        failure_code: str,
        failure_summary: str,
    ) -> None: ...


class FactoryWorkerFailureCaptainProjectionPort(Protocol):
    def record(self, block: FactoryEvidenceBlock) -> FactoryEvidenceBlock: ...


class FactoryFailureArtifactPort(Protocol):
    def put_failure(self, content: bytes) -> ArtifactRef: ...


@dataclass(frozen=True, slots=True)
class FactoryProjectedFailure:
    failure_code: str
    failure_summary: str


class CaptainFactoryWorkerFailureCoordinator:
    """Validate a persisted checkpoint, then project Minibook and Captain state."""

    def __init__(
        self,
        *,
        minibook: FactoryWorkerFailureMinibookProjectionPort,
        captain: FactoryWorkerFailureCaptainProjectionPort,
        artifacts: FactoryFailureArtifactPort,
        factory_job: AgentFactoryJobV3,
        lease: FactoryLease,
        clock: Callable[[], datetime],
    ) -> None:
        self._minibook = minibook
        self._captain = captain
        self._artifacts = artifacts
        self._job = factory_job
        self._lease = lease
        self._clock = clock
        self.last_failure: FactoryProjectedFailure | None = None

    def report(
        self,
        assignment: FactoryBuildAssignmentV2,
        checkpoint: HermesFactoryWorkerCheckpoint,
        error: Exception,
    ) -> None:
        if (
            checkpoint.assignment_id != assignment.assignment_id
            or checkpoint.effect_key != assignment.effect_key
            or checkpoint.state != "failed"
            or checkpoint.failure_kind is None
        ):
            raise ValueError("Hermes worker failure checkpoint is not authoritative")
        failure_code = getattr(error, "failure_code", None)
        if not isinstance(failure_code, str):
            failure_code = {
                "timeout": "codex_timeout",
                "null_or_untyped": "codex_null_output",
            }.get(checkpoint.failure_kind, "codex_failed")
        summary = str(error) or type(error).__name__
        evidence_ref = self._artifacts.put_failure(
            json.dumps(
                {
                    "schema": "hermes.factory-build-failure.v1",
                    "assignment_id": str(assignment.assignment_id),
                    "failure_code": failure_code,
                    "failure_kind": checkpoint.failure_kind,
                    "status": "failed",
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        self._minibook.block_worker_failure(
            assignment,
            failure_code=failure_code,
            failure_summary=summary,
        )
        self._captain.record(
            self._failure_block(
                assignment=assignment,
                failure_code=failure_code,
                evidence_ref=evidence_ref,
                event_suffix=f"worker:{checkpoint.revision}",
            )
        )
        self.last_failure = FactoryProjectedFailure(failure_code, summary)

    def report_boundary_failure(
        self,
        assignment: FactoryBuildAssignmentV2,
        *,
        failure_code: str,
        failure_summary: str,
    ) -> None:
        evidence_ref = self._artifacts.put_failure(
            json.dumps(
                {
                    "schema": "captain.factory-boundary-failure.v1",
                    "assignment_id": str(assignment.assignment_id),
                    "failure_code": failure_code,
                    "status": "failed",
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        self._captain.record(
            self._failure_block(
                assignment=assignment,
                failure_code=failure_code,
                evidence_ref=evidence_ref,
                event_suffix="boundary",
            )
        )
        self.last_failure = FactoryProjectedFailure(failure_code, failure_summary)

    def _failure_block(
        self,
        *,
        assignment: FactoryBuildAssignmentV2,
        failure_code: str,
        evidence_ref: ArtifactRef,
        event_suffix: str,
    ) -> FactoryEvidenceBlock:
        runtime_ref = RuntimeArtifactRef.model_validate(
            evidence_ref.model_dump(mode="json")
        )
        return FactoryEvidenceBlock(
            schema_name="captain.agent-factory-block.v1",
            event_id=uuid5(
                assignment.assignment_id,
                f"factory-failed:{failure_code}:{event_suffix}",
            ),
            job_id=self._job.job_id,
            correlation_id=self._job.correlation_id,
            causation_id=self._job.event_id,
            occurred_at=self._clock(),
            producer="hermes",
            subject_version=self._job.subject_version,
            attempt=assignment.attempt,
            phase=FactoryPhase.AGENT_CODE_CREATED,
            role=FactoryRole.TOOL_INTEGRATOR,
            status=FactoryBlockStatus.FAILED,
            artifact_refs=(runtime_ref,),
            evidence_refs=(runtime_ref,),
            lease_id=self._lease.lease_id,
        )


__all__ = ["CaptainFactoryWorkerFailureCoordinator", "FactoryProjectedFailure"]
