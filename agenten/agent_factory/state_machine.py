"""Pure, fail-closed lifecycle transitions for Captain agent-factory jobs."""

from __future__ import annotations

from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .contracts import AgentFactoryJob, FactoryEvidenceBlock, FactoryPhase


class FactoryLifecycleError(ValueError):
    """A block cannot advance the current authoritative factory projection."""


class FactoryLifecycleStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    INFRASTRUCTURE_BLOCKED = "infrastructure_blocked"
    READY_TO_USE = "ready_to_use"
    ESCALATED = "escalated"


class FactoryActionKind(str, Enum):
    APPEND_FORGE_REQUESTED = "append_forge_requested"
    DISPATCH_AGENT_ARCHITECT = "dispatch_agent_architect"
    DISPATCH_TOOL_INTEGRATOR = "dispatch_tool_integrator"
    SUBMIT_FORGE_JOB = "submit_forge_job"
    DISPATCH_BUILD_VALIDATOR = "dispatch_build_validator"
    DISPATCH_REAL_CASE_TESTER = "dispatch_real_case_tester"
    DISPATCH_QUALITY_WARDEN = "dispatch_quality_warden"
    APPEND_IMPROVEMENT_REQUESTED = "append_improvement_requested"
    VALIDATE_FOR_PROMOTION = "validate_for_promotion"
    APPEND_ESCALATED = "append_escalated"
    WAIT_INFRASTRUCTURE = "wait_infrastructure"
    COMPLETE = "complete"


class FactoryAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: FactoryActionKind
    attempt: int = Field(ge=1, le=5)
    job_id: UUID | None = None


class FactoryProjection(BaseModel):
    """Derived state only; gateway persistence remains the authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    job: AgentFactoryJob
    status: FactoryLifecycleStatus
    phase: FactoryPhase | None = None
    attempt: int = Field(ge=1, le=5)
    observed_assertion_ids: tuple[str, ...] = ()
    block_ids: tuple[UUID, ...] = ()

    @classmethod
    def from_job(cls, job: AgentFactoryJob) -> "FactoryProjection":
        return cls(job=job, status=FactoryLifecycleStatus.PENDING, attempt=1)


def apply_block(
    projection: FactoryProjection,
    block: FactoryEvidenceBlock,
) -> FactoryProjection:
    """Apply one new immutable block after enforcing lifecycle ordering."""

    if block.job_id != projection.job.job_id:
        raise FactoryLifecycleError("block job does not match projection")
    if block.correlation_id != projection.job.correlation_id:
        raise FactoryLifecycleError("block correlation does not match projection")
    if block.subject_version != projection.job.subject_version:
        raise FactoryLifecycleError("block subject version does not match projection")
    if block.event_id in projection.block_ids:
        return projection
    if block.attempt != projection.attempt:
        raise FactoryLifecycleError("block attempt does not match projection")
    if projection.status in {FactoryLifecycleStatus.READY_TO_USE, FactoryLifecycleStatus.ESCALATED}:
        raise FactoryLifecycleError("terminal factory projection cannot accept blocks")

    allowed = _allowed_next_phases(projection)
    if block.phase not in allowed:
        raise FactoryLifecycleError(
            f"illegal phase {block.phase.value!r} after {projection.phase.value if projection.phase else 'initial'!r}"
        )

    if block.status.value == "infrastructure_failed":
        return projection.model_copy(
            update={
                "status": FactoryLifecycleStatus.INFRASTRUCTURE_BLOCKED,
                "phase": block.phase,
                "block_ids": (*projection.block_ids, block.event_id),
            }
        )

    assertions = tuple(dict.fromkeys((*projection.observed_assertion_ids, *block.assertion_ids)))
    status = FactoryLifecycleStatus.RUNNING
    attempt = projection.attempt
    if block.phase is FactoryPhase.IMPROVEMENT_REQUESTED:
        if projection.attempt >= projection.job.max_behavioral_iterations:
            raise FactoryLifecycleError("behavioral iteration ceiling reached")
        attempt += 1
    elif block.phase is FactoryPhase.CAPABILITY_PROMOTED:
        required = set(projection.job.acceptance_assertion_ids)
        if not required.issubset(assertions):
            raise FactoryLifecycleError("promotion is missing required assertions")
        status = FactoryLifecycleStatus.READY_TO_USE
    elif block.phase is FactoryPhase.ESCALATED:
        status = FactoryLifecycleStatus.ESCALATED

    return projection.model_copy(
        update={
            "status": status,
            "phase": block.phase,
            "attempt": attempt,
            "observed_assertion_ids": assertions,
            "block_ids": (*projection.block_ids, block.event_id),
        }
    )


def next_action(projection: FactoryProjection) -> FactoryAction:
    """Return the one allowed next side effect for a derived projection."""

    if projection.status is FactoryLifecycleStatus.INFRASTRUCTURE_BLOCKED:
        return FactoryAction(kind=FactoryActionKind.WAIT_INFRASTRUCTURE, attempt=projection.attempt)
    if projection.status in {FactoryLifecycleStatus.READY_TO_USE, FactoryLifecycleStatus.ESCALATED}:
        return FactoryAction(kind=FactoryActionKind.COMPLETE, attempt=projection.attempt)
    if projection.status is FactoryLifecycleStatus.PENDING:
        return FactoryAction(kind=FactoryActionKind.APPEND_FORGE_REQUESTED, attempt=projection.attempt)

    phase = projection.phase
    if phase is FactoryPhase.FORGE_REQUESTED or phase is FactoryPhase.IMPROVEMENT_REQUESTED:
        return FactoryAction(kind=FactoryActionKind.DISPATCH_AGENT_ARCHITECT, attempt=projection.attempt)
    if phase is FactoryPhase.BLUEPRINT_CREATED:
        return FactoryAction(kind=FactoryActionKind.DISPATCH_TOOL_INTEGRATOR, attempt=projection.attempt)
    if phase is FactoryPhase.TOOL_CANDIDATE_TESTED:
        return FactoryAction(kind=FactoryActionKind.SUBMIT_FORGE_JOB, attempt=projection.attempt)
    if phase is FactoryPhase.AGENT_CODE_CREATED:
        return FactoryAction(kind=FactoryActionKind.DISPATCH_BUILD_VALIDATOR, attempt=projection.attempt)
    if phase is FactoryPhase.BUILD_PASSED:
        return FactoryAction(kind=FactoryActionKind.DISPATCH_REAL_CASE_TESTER, attempt=projection.attempt)
    if phase is FactoryPhase.REAL_CASE_EVIDENCE:
        return FactoryAction(kind=FactoryActionKind.DISPATCH_QUALITY_WARDEN, attempt=projection.attempt)
    if phase in {FactoryPhase.BUILD_FAILED, FactoryPhase.QUALITY_REVIEWED}:
        required = set(projection.job.acceptance_assertion_ids)
        if phase is FactoryPhase.QUALITY_REVIEWED and required.issubset(projection.observed_assertion_ids):
            return FactoryAction(kind=FactoryActionKind.VALIDATE_FOR_PROMOTION, attempt=projection.attempt)
        if projection.attempt < projection.job.max_behavioral_iterations:
            return FactoryAction(kind=FactoryActionKind.APPEND_IMPROVEMENT_REQUESTED, attempt=projection.attempt)
        return FactoryAction(kind=FactoryActionKind.APPEND_ESCALATED, attempt=projection.attempt)
    raise FactoryLifecycleError(f"no next action for phase {phase!r}")


def _allowed_next_phases(projection: FactoryProjection) -> frozenset[FactoryPhase]:
    if projection.status is FactoryLifecycleStatus.PENDING:
        return frozenset({FactoryPhase.FORGE_REQUESTED})
    phase = projection.phase
    transitions: dict[FactoryPhase, frozenset[FactoryPhase]] = {
        FactoryPhase.FORGE_REQUESTED: frozenset({FactoryPhase.BLUEPRINT_CREATED}),
        FactoryPhase.IMPROVEMENT_REQUESTED: frozenset({FactoryPhase.BLUEPRINT_CREATED}),
        FactoryPhase.BLUEPRINT_CREATED: frozenset({FactoryPhase.TOOL_CANDIDATE_TESTED}),
        FactoryPhase.TOOL_CANDIDATE_TESTED: frozenset({FactoryPhase.AGENT_CODE_CREATED}),
        FactoryPhase.AGENT_CODE_CREATED: frozenset({FactoryPhase.BUILD_PASSED, FactoryPhase.BUILD_FAILED}),
        FactoryPhase.BUILD_PASSED: frozenset({FactoryPhase.REAL_CASE_EVIDENCE}),
        FactoryPhase.REAL_CASE_EVIDENCE: frozenset({FactoryPhase.QUALITY_REVIEWED}),
        FactoryPhase.BUILD_FAILED: frozenset({FactoryPhase.IMPROVEMENT_REQUESTED, FactoryPhase.ESCALATED}),
        FactoryPhase.QUALITY_REVIEWED: frozenset(
            {FactoryPhase.IMPROVEMENT_REQUESTED, FactoryPhase.CAPABILITY_PROMOTED, FactoryPhase.ESCALATED}
        ),
    }
    try:
        return transitions[phase]  # type: ignore[index]
    except KeyError as exc:
        raise FactoryLifecycleError(f"no legal transition from {phase!r}") from exc
