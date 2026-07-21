"""Pure, fail-closed lifecycle transitions for Captain agent-factory jobs."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .contracts import AgentFactoryJobV2, AgentFactoryJobV3, FactoryEvidenceBlock, FactoryJob, FactoryPhase
from .outcome_contracts import FactoryTerminalDecision, FactoryTerminalState
from .release_gate import (
    FactoryPolicyFinding,
    FactoryReleaseDecision,
    evaluation_requires_improvement,
    evaluation_tool_gaps,
    factory_evaluation_block_reason,
    factory_release_decision_block_reason,
    factory_workflow_release_decision_block_reason,
    factory_workflow_validation_decision_block_reason,
)
from .skill_evaluation import ToolGapMarker
from .skill_store import StoredSkillEvaluation
from .skill_workflow_contracts import (
    FactoryFeedbackRecommendation,
    FactoryFeedbackV1,
    TeamEvaluationV1,
)
from agenten.agent_runtime.contracts import ArtifactRef


class FactoryLifecycleError(ValueError):
    """A block cannot advance the current authoritative factory projection."""


class FactoryLifecycleStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    INFRASTRUCTURE_BLOCKED = "infrastructure_blocked"
    READY_TO_USE = "ready_to_use"
    ESCALATED = "escalated"
    BLOCKED = "blocked"
    REJECTED = "rejected"


class FactoryActionKind(str, Enum):
    APPEND_FORGE_REQUESTED = "append_forge_requested"
    DISPATCH_AGENT_ARCHITECT = "dispatch_agent_architect"
    DISPATCH_TOOL_INTEGRATOR = "dispatch_tool_integrator"
    SUBMIT_FORGE_JOB = "submit_forge_job"
    EMIT_AGENT_CODE_EVIDENCE = "emit_agent_code_evidence"
    DISPATCH_BUILD_VALIDATOR = "dispatch_build_validator"
    DISPATCH_REAL_CASE_TESTER = "dispatch_real_case_tester"
    DISPATCH_QUALITY_WARDEN = "dispatch_quality_warden"
    APPEND_IMPROVEMENT_REQUESTED = "append_improvement_requested"
    VALIDATE_FOR_PROMOTION = "validate_for_promotion"
    APPEND_ESCALATED = "append_escalated"
    WAIT_INFRASTRUCTURE = "wait_infrastructure"
    COMPLETE = "complete"
    RECORD_ESCALATION = "record_escalation"
    NO_ACTION = "no_action"


class FactoryAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: FactoryActionKind
    attempt: int = Field(ge=1, le=5)
    job_id: UUID | None = None


class FactoryProjection(BaseModel):
    """Derived state only; gateway persistence remains the authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    job: FactoryJob
    status: FactoryLifecycleStatus
    phase: FactoryPhase | None = None
    attempt: int = Field(ge=1, le=5)
    observed_assertion_ids: tuple[str, ...] = ()
    block_ids: tuple[UUID, ...] = ()
    evidence_refs: tuple[ArtifactRef, ...] = ()
    evaluation_id: UUID | None = None
    evaluation_ref: ArtifactRef | None = None
    workflow_evaluation_ref: ArtifactRef | None = None
    feedback_ref: ArtifactRef | None = None
    feedback_recommendation: FactoryFeedbackRecommendation | None = None
    tool_gaps: tuple[ToolGapMarker, ...] = ()
    policy_findings: tuple[FactoryPolicyFinding, ...] = ()
    terminal_decision: FactoryTerminalDecision | None = None

    @classmethod
    def from_job(cls, job: FactoryJob) -> "FactoryProjection":
        return cls(job=job, status=FactoryLifecycleStatus.PENDING, attempt=1)


def apply_block(
    projection: FactoryProjection,
    block: FactoryEvidenceBlock,
    *,
    evaluation: StoredSkillEvaluation | None = None,
    release_decision: FactoryReleaseDecision | None = None,
    workflow_evaluation: TeamEvaluationV1 | None = None,
    feedback: FactoryFeedbackV1 | None = None,
    now: datetime | None = None,
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
    if projection.terminal_decision is not None or projection.status in {
        FactoryLifecycleStatus.READY_TO_USE,
        FactoryLifecycleStatus.ESCALATED,
        FactoryLifecycleStatus.BLOCKED,
        FactoryLifecycleStatus.REJECTED,
    }:
        raise FactoryLifecycleError("terminal factory projection cannot accept blocks")

    if (
        isinstance(projection.job, AgentFactoryJobV2)
        and now is not None
        and now >= projection.job.deadline_at
    ):
        if block.phase is not FactoryPhase.ESCALATED or block.status.value != "succeeded":
            raise FactoryLifecycleError(
                "v2 factory deadline permits only the derived Captain escalation"
            )
        return projection.model_copy(
            update={
                "status": FactoryLifecycleStatus.ESCALATED,
                "phase": block.phase,
                "block_ids": (*projection.block_ids, block.event_id),
                "evidence_refs": _append_evidence_refs(projection, block),
            }
        )

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
                "evidence_refs": _append_evidence_refs(projection, block),
            }
        )

    assertions = tuple(dict.fromkeys((*projection.observed_assertion_ids, *block.assertion_ids)))
    status = FactoryLifecycleStatus.RUNNING
    attempt = projection.attempt
    evaluation_update: dict[str, object] = {}
    if block.phase is FactoryPhase.IMPROVEMENT_REQUESTED:
        if projection.attempt >= projection.job.max_behavioral_iterations:
            raise FactoryLifecycleError("behavioral iteration ceiling reached")
        attempt += 1
    elif block.phase is FactoryPhase.CAPABILITY_PROMOTED:
        required = set(projection.job.acceptance_assertion_ids)
        if not required.issubset(assertions):
            raise FactoryLifecycleError("promotion is missing required assertions")
        status = FactoryLifecycleStatus.READY_TO_USE
        if isinstance(projection.job, AgentFactoryJobV3):
            _validate_workflow_feedback(
                projection,
                workflow_evaluation=workflow_evaluation,
                feedback=feedback,
            )
            decision_reason = factory_workflow_release_decision_block_reason(
                projection.job,
                workflow_evaluation,
                release_decision,
            )
            if decision_reason is not None:
                raise FactoryLifecycleError(decision_reason)
            assert workflow_evaluation is not None
            assert feedback is not None
            evaluation_update = {
                "workflow_evaluation_ref": workflow_evaluation.artifact_ref,
                "feedback_ref": feedback.artifact_ref,
                "feedback_recommendation": feedback.recommendation,
            }
        else:
            evaluation_reason = factory_evaluation_block_reason(
                projection.job, evaluation
            )
            if evaluation_reason is not None:
                raise FactoryLifecycleError(evaluation_reason)
            decision_reason = factory_release_decision_block_reason(
                projection.job,
                evaluation,
                release_decision,
            )
            if decision_reason is not None:
                raise FactoryLifecycleError(decision_reason)
            assert evaluation is not None
            evaluation_update = {
                "evaluation_id": evaluation.evidence.evidence_id,
                "evaluation_ref": evaluation.evidence_ref,
                "tool_gaps": evaluation_tool_gaps(evaluation),
            }
    elif block.phase is FactoryPhase.ESCALATED:
        status = FactoryLifecycleStatus.ESCALATED
    elif block.phase is FactoryPhase.QUALITY_REVIEWED and (
        workflow_evaluation is not None or feedback is not None
    ):
        _validate_workflow_feedback(
            projection,
            workflow_evaluation=workflow_evaluation,
            feedback=feedback,
        )
        assert workflow_evaluation is not None
        assert feedback is not None
        if (
            workflow_evaluation.artifact_ref not in block.artifact_refs
            or feedback.artifact_ref not in block.artifact_refs
        ):
            raise FactoryLifecycleError(
                "quality review block is missing workflow feedback artifacts"
            )
        evaluation_update = {
            "workflow_evaluation_ref": workflow_evaluation.artifact_ref,
            "feedback_ref": feedback.artifact_ref,
            "feedback_recommendation": feedback.recommendation,
        }

    return projection.model_copy(
        update={
            "status": status,
            "phase": block.phase,
            "attempt": attempt,
            "observed_assertion_ids": assertions,
            "block_ids": (*projection.block_ids, block.event_id),
            "evidence_refs": _append_evidence_refs(projection, block),
            **evaluation_update,
        }
    )


def next_action(
    projection: FactoryProjection,
    *,
    evaluation: StoredSkillEvaluation | None = None,
    workflow_evaluation: TeamEvaluationV1 | None = None,
    feedback: FactoryFeedbackV1 | None = None,
    workflow_release_decision: FactoryReleaseDecision | None = None,
    now: datetime | None = None,
) -> FactoryAction:
    """Return the one allowed next side effect for a derived projection."""

    if projection.terminal_decision is not None:
        return FactoryAction(kind=FactoryActionKind.NO_ACTION, attempt=projection.attempt)
    if projection.status in {
        FactoryLifecycleStatus.BLOCKED,
        FactoryLifecycleStatus.REJECTED,
    }:
        return FactoryAction(kind=FactoryActionKind.NO_ACTION, attempt=projection.attempt)
    if (
        isinstance(projection.job, AgentFactoryJobV2)
        and now is not None
        and now >= projection.job.deadline_at
    ):
        return FactoryAction(kind=FactoryActionKind.RECORD_ESCALATION, attempt=projection.attempt)
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
        if (
            phase is FactoryPhase.QUALITY_REVIEWED
            and isinstance(projection.job, AgentFactoryJobV3)
        ):
            _validate_workflow_feedback(
                projection,
                workflow_evaluation=workflow_evaluation,
                feedback=feedback,
            )
            assert feedback is not None
            if (
                feedback.recommendation
                is FactoryFeedbackRecommendation.PROMOTE_CANDIDATE
            ):
                required = set(projection.job.acceptance_assertion_ids)
                if not required.issubset(projection.observed_assertion_ids):
                    raise FactoryLifecycleError(
                        "workflow promotion recommendation is missing Captain assertions"
                    )
                decision_reason = factory_workflow_validation_decision_block_reason(
                    projection.job,
                    workflow_evaluation,
                    workflow_release_decision,
                )
                if decision_reason is None:
                    return FactoryAction(
                        kind=FactoryActionKind.VALIDATE_FOR_PROMOTION,
                        attempt=projection.attempt,
                    )
                return FactoryAction(
                    kind=FactoryActionKind.APPEND_ESCALATED,
                    attempt=projection.attempt,
                )
            if feedback.recommendation is FactoryFeedbackRecommendation.RETRY_BUILD:
                if projection.attempt < projection.job.max_behavioral_iterations:
                    return FactoryAction(
                        kind=FactoryActionKind.APPEND_IMPROVEMENT_REQUESTED,
                        attempt=projection.attempt,
                    )
            return FactoryAction(
                kind=FactoryActionKind.APPEND_ESCALATED,
                attempt=projection.attempt,
            )
        required = set(projection.job.acceptance_assertion_ids)
        if phase is FactoryPhase.QUALITY_REVIEWED and required.issubset(projection.observed_assertion_ids):
            if evaluation_requires_improvement(projection.job, evaluation):
                if projection.attempt < projection.job.max_behavioral_iterations:
                    return FactoryAction(
                        kind=FactoryActionKind.APPEND_IMPROVEMENT_REQUESTED,
                        attempt=projection.attempt,
                    )
                return FactoryAction(
                    kind=FactoryActionKind.APPEND_ESCALATED,
                    attempt=projection.attempt,
                )
            return FactoryAction(kind=FactoryActionKind.VALIDATE_FOR_PROMOTION, attempt=projection.attempt)
        if projection.attempt < projection.job.max_behavioral_iterations:
            return FactoryAction(kind=FactoryActionKind.APPEND_IMPROVEMENT_REQUESTED, attempt=projection.attempt)
        return FactoryAction(kind=FactoryActionKind.APPEND_ESCALATED, attempt=projection.attempt)
    raise FactoryLifecycleError(f"no next action for phase {phase!r}")


def with_terminal_decision(
    projection: FactoryProjection,
    decision: FactoryTerminalDecision,
) -> FactoryProjection:
    """Seal a projection with one Captain-authored terminal decision."""

    if (
        decision.job_id != projection.job.job_id
        or decision.correlation_id != projection.job.correlation_id
        or decision.subject_version != projection.job.subject_version
    ):
        raise FactoryLifecycleError("terminal decision does not match projection")
    existing = projection.terminal_decision
    if existing is not None:
        if existing == decision:
            return projection
        raise FactoryLifecycleError("terminal decision conflicts with sealed projection")
    status = {
        FactoryTerminalState.READY_TO_USE: FactoryLifecycleStatus.READY_TO_USE,
        FactoryTerminalState.BLOCKED: FactoryLifecycleStatus.BLOCKED,
        FactoryTerminalState.ESCALATED: FactoryLifecycleStatus.ESCALATED,
        FactoryTerminalState.REJECTED: FactoryLifecycleStatus.REJECTED,
    }[decision.state]
    return projection.model_copy(update={"status": status, "terminal_decision": decision})


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


def _append_evidence_refs(
    projection: FactoryProjection,
    block: FactoryEvidenceBlock,
) -> tuple[ArtifactRef, ...]:
    references = (*projection.evidence_refs, *block.artifact_refs, *block.evidence_refs)
    return tuple(dict.fromkeys(references))


def _validate_workflow_feedback(
    projection: FactoryProjection,
    *,
    workflow_evaluation: TeamEvaluationV1 | None,
    feedback: FactoryFeedbackV1 | None,
) -> None:
    if workflow_evaluation is None or feedback is None:
        raise FactoryLifecycleError("missing validated workflow feedback")
    job = projection.job
    if (
        workflow_evaluation.job_id != job.job_id
        or workflow_evaluation.correlation_id != job.correlation_id
        or workflow_evaluation.subject_version != job.subject_version
        or workflow_evaluation.attempt != projection.attempt
        or workflow_evaluation.acceptance_assertion_ids
        != job.acceptance_assertion_ids
        or feedback.job_id != job.job_id
        or feedback.correlation_id != job.correlation_id
        or feedback.subject_version != job.subject_version
        or feedback.attempt != projection.attempt
        or feedback.acceptance_assertion_ids != job.acceptance_assertion_ids
        or feedback.assertion_ids != job.acceptance_assertion_ids
        or feedback.invocation.input_ref != workflow_evaluation.artifact_ref
        or workflow_evaluation.artifact_ref not in feedback.evidence_refs
    ):
        raise FactoryLifecycleError("workflow feedback binding does not match projection")
    required_gap = any(
        gap.severity == "required" and gap.status == "unresolved"
        for gap in feedback.tool_gaps
    )
    if (
        feedback.recommendation is FactoryFeedbackRecommendation.PROMOTE_CANDIDATE
        and (
            workflow_evaluation.failure_class is not None
            or any(
                outcome.status != "passed"
                for outcome in workflow_evaluation.assertion_outcomes
            )
            or required_gap
        )
    ):
        raise FactoryLifecycleError("workflow feedback cannot weaken Captain assertions")
    if (
        feedback.recommendation is FactoryFeedbackRecommendation.RETRY_BUILD
        and workflow_evaluation.failure_class
        not in {"behavioral_failure", "test_regression"}
    ):
        raise FactoryLifecycleError("workflow retry lacks a behavioral failure")
