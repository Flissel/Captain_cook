"""Captain-only release gate for the required E2E and recovery evidence."""

from __future__ import annotations

from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agenten.agent_factory.contracts import AgentFactoryJob
from agenten.agent_runtime.contracts import ArtifactRef


class E2EKind(str, Enum):
    NORMAL = "normal"
    RECOVERY = "recovery"


class E2EOutcome(str, Enum):
    SUCCEEDED = "succeeded"
    EXPECTED_FAILURE = "expected_failure"
    FAILED = "failed"


class E2ERunEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_number: int = Field(ge=1, strict=True)
    correlation_id: UUID
    kind: E2EKind
    outcome: E2EOutcome
    evidence_ref: ArtifactRef


class FactoryReleaseDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: UUID
    correlation_id: UUID
    status: str
    reasons: tuple[str, ...]


def evaluate_factory_release(
    job: AgentFactoryJob, evidence: tuple[E2ERunEvidence, ...]
) -> FactoryReleaseDecision:
    """Require a post-recovery streak of three successful normal E2E runs."""

    if any(item.correlation_id != job.correlation_id for item in evidence):
        return _blocked(job, "E2E evidence correlation does not match the factory job")
    ordered = tuple(sorted(evidence, key=lambda item: item.run_number))
    if len({item.run_number for item in ordered}) != len(ordered):
        return _blocked(job, "E2E run numbers must be unique")
    recovery_runs = [
        item for item in ordered
        if item.kind is E2EKind.RECOVERY and item.outcome is E2EOutcome.EXPECTED_FAILURE
    ]
    if not recovery_runs:
        return _blocked(job, "missing intentionally failing recovery scenario")
    tail = ordered[-3:]
    if len(tail) != 3 or any(
        item.kind is not E2EKind.NORMAL or item.outcome is not E2EOutcome.SUCCEEDED
        for item in tail
    ):
        return _blocked(job, "missing three consecutive successful normal E2E runs")
    if [item.run_number for item in tail] != list(range(tail[0].run_number, tail[0].run_number + 3)):
        return _blocked(job, "successful E2E run numbers are not consecutive")
    if max(item.run_number for item in recovery_runs) >= tail[0].run_number:
        return _blocked(job, "recovery scenario must precede the successful E2E streak")
    return FactoryReleaseDecision(
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        status="ready",
        reasons=("three consecutive successful E2E runs and recovery evidence verified",),
    )


def _blocked(job: AgentFactoryJob, reason: str) -> FactoryReleaseDecision:
    return FactoryReleaseDecision(
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        status="blocked",
        reasons=(reason,),
    )
