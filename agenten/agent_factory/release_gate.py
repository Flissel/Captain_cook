"""Captain-only release gate for the required E2E and recovery evidence."""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agenten.agent_factory.contracts import AgentFactoryJobV3, FactoryJob
from agenten.agent_factory.execution_budget import (
    FactoryBudgetProjection,
    FactoryUsageReceiptV1,
)
from agenten.agent_factory.execution_policy import FactoryExecutionMode
from agenten.agent_factory.skill_evaluation import ToolGapMarker
from agenten.agent_factory.skill_store import StoredSkillEvaluation
from agenten.agent_factory.skill_workflow_contracts import (
    FactoryFeedbackRecommendation,
    TeamEvaluationV1,
    TeamExecutionEvidenceV1,
)
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
    status: Literal["blocked", "demo_ready", "ready"]
    reasons: tuple[str, ...]
    evaluation_id: UUID | None = None
    evaluation_ref: ArtifactRef | None = None
    tool_gaps: tuple[ToolGapMarker, ...] = ()


def factory_release_decision_block_reason(
    job: FactoryJob,
    evaluation: StoredSkillEvaluation | None,
    decision: FactoryReleaseDecision | None,
) -> str | None:
    """Require one Captain decision bound to the accepted evaluation."""

    if decision is None:
        return "missing accepted Factory release decision"
    if decision.status != "ready":
        return "Factory release decision is blocked: " + ", ".join(decision.reasons)
    if decision.job_id != job.job_id or decision.correlation_id != job.correlation_id:
        return "Factory release decision does not match the factory job"
    if evaluation is None:
        return "missing accepted Hermes skill evaluation evidence"
    if (
        decision.evaluation_id != evaluation.evidence.evidence_id
        or decision.evaluation_ref != evaluation.evidence_ref
    ):
        return "Factory release decision does not match the accepted skill evaluation"
    if decision.tool_gaps != evaluation_tool_gaps(evaluation):
        return "Factory release decision tool gaps do not match the accepted skill evaluation"
    return None


def evaluate_factory_release(
    job: FactoryJob,
    evidence: tuple[E2ERunEvidence, ...],
    evaluation: StoredSkillEvaluation | None = None,
) -> FactoryReleaseDecision:
    """Require a post-recovery streak of three successful normal E2E runs."""

    evaluation_reason = factory_evaluation_block_reason(job, evaluation)
    if evaluation_reason is not None:
        return _blocked(job, evaluation_reason, evaluation)
    if any(item.correlation_id != job.correlation_id for item in evidence):
        return _blocked(job, "E2E evidence correlation does not match the factory job", evaluation)
    ordered = tuple(sorted(evidence, key=lambda item: item.run_number))
    if len({item.run_number for item in ordered}) != len(ordered):
        return _blocked(job, "E2E run numbers must be unique", evaluation)
    recovery_runs = [
        item for item in ordered
        if item.kind is E2EKind.RECOVERY and item.outcome is E2EOutcome.EXPECTED_FAILURE
    ]
    if not recovery_runs:
        return _blocked(job, "missing intentionally failing recovery scenario", evaluation)
    tail = ordered[-3:]
    if len(tail) != 3 or any(
        item.kind is not E2EKind.NORMAL or item.outcome is not E2EOutcome.SUCCEEDED
        for item in tail
    ):
        return _blocked(job, "missing three consecutive successful normal E2E runs", evaluation)
    if [item.run_number for item in tail] != list(range(tail[0].run_number, tail[0].run_number + 3)):
        return _blocked(job, "successful E2E run numbers are not consecutive", evaluation)
    if max(item.run_number for item in recovery_runs) >= tail[0].run_number:
        return _blocked(job, "recovery scenario must precede the successful E2E streak", evaluation)
    assert evaluation is not None
    return FactoryReleaseDecision(
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        status="ready",
        reasons=("three consecutive successful E2E runs and recovery evidence verified",),
        evaluation_id=evaluation.evidence.evidence_id,
        evaluation_ref=evaluation.evidence_ref,
        tool_gaps=evaluation_tool_gaps(evaluation),
    )


def evaluate_factory_workflow_release(
    job: AgentFactoryJobV3,
    evidence: tuple[TeamExecutionEvidenceV1, ...],
    evaluation: TeamEvaluationV1,
    *,
    budget_projection: FactoryBudgetProjection | None = None,
    usage_receipts: tuple[FactoryUsageReceiptV1, ...] = (),
) -> FactoryReleaseDecision:
    """Validate V3 live workflow evidence without granting promotion authority."""

    blocked = _workflow_evaluation_block_reason(job, evidence, evaluation)
    if blocked is not None:
        return _workflow_blocked(job, evaluation, blocked)
    if budget_projection is None:
        return _workflow_blocked(
            job,
            evaluation,
            "missing Gateway workflow budget projection",
        )
    required_runs = job.execution_policy.required_live_runs
    if len(evidence) != required_runs:
        label = "one" if required_runs == 1 else "three"
        return _workflow_blocked(
            job,
            evaluation,
            f"missing exactly {label} successful live workflow run(s)",
        )
    if (
        budget_projection.job_id != job.job_id
        or budget_projection.limit_usd != job.execution_policy.max_cost_usd
        or budget_projection.consumed_usd > job.execution_policy.max_cost_usd
        or budget_projection.reserved_usd != 0
        or budget_projection.active_reservation_ids
    ):
        return _workflow_blocked(
            job,
            evaluation,
            "workflow budget projection is not release-complete",
        )
    run_receipt_refs = tuple(
        reference
        for run in evidence
        for reference in run.usage_receipt_refs
    )
    receipt_identity_is_unique = all(
        len(values) == len(set(values))
        for values in (
            tuple(receipt.receipt_id for receipt in usage_receipts),
            tuple(receipt.reservation_id for receipt in usage_receipts),
            tuple(receipt.evidence_ref for receipt in usage_receipts),
        )
    )
    receipts_are_bound = all(
        receipt.job_id == job.job_id
        and receipt.correlation_id == job.correlation_id
        and receipt.attempt <= evaluation.attempt
        and receipt.model in job.execution_policy.allowed_models
        for receipt in usage_receipts
    )
    accepted_receipt_refs = {
        receipt.evidence_ref
        for receipt in usage_receipts
        if receipt.attempt == evaluation.attempt
    }
    if (
        not usage_receipts
        or not receipt_identity_is_unique
        or not receipts_are_bound
        or sum(
            (receipt.cost_usd for receipt in usage_receipts),
            start=Decimal("0"),
        )
        != budget_projection.consumed_usd
    ):
        return _workflow_blocked(
            job,
            evaluation,
            "workflow usage receipts do not cover the Gateway budget projection",
        )
    if (
        len(run_receipt_refs) != len(set(run_receipt_refs))
        or set(run_receipt_refs) != accepted_receipt_refs
    ):
        return _workflow_blocked(
            job,
            evaluation,
            "workflow usage receipts must exactly and uniquely cover every run",
        )
    run_numbers = tuple(item.run_number for item in evidence)
    if len(run_numbers) != len(set(run_numbers)):
        return _workflow_blocked(job, evaluation, "workflow run numbers must be unique")
    invocation_ids = tuple(item.invocation_id for item in evidence)
    idempotency_keys = tuple(item.invocation.idempotency_key for item in evidence)
    artifact_digests = tuple(item.artifact_ref.sha256 for item in evidence)
    if (
        len(invocation_ids) != len(set(invocation_ids))
        or len(idempotency_keys) != len(set(idempotency_keys))
        or len(artifact_digests) != len(set(artifact_digests))
    ):
        return _workflow_blocked(
            job,
            evaluation,
            "workflow runs must have distinct invocation and evidence identities",
        )
    candidates = {item.candidate_ref for item in evidence}
    if len(candidates) != 1:
        return _workflow_blocked(
            job,
            evaluation,
            "workflow candidate binding changed across live runs",
        )
    candidate_ref = next(iter(candidates))
    if candidate_ref not in evaluation.evidence_refs:
        return _workflow_blocked(
            job,
            evaluation,
            "workflow evaluation is missing its candidate binding",
        )
    accepted = job.acceptance_assertion_ids
    for run in evidence:
        outcome_ids = tuple(
            item.assertion_id for item in run.execution_outcome.assertion_outcomes
        )
        if (
            run.job_id != job.job_id
            or run.correlation_id != job.correlation_id
            or run.subject_version != job.subject_version
            or run.attempt != evaluation.attempt
            or run.invocation.job_id != job.job_id
            or run.invocation.correlation_id != job.correlation_id
            or run.invocation.subject_version != job.subject_version
            or run.invocation.attempt != evaluation.attempt
        ):
            return _workflow_blocked(
                job,
                evaluation,
                "workflow run identity does not match the Factory job",
            )
        if (
            run.acceptance_assertion_ids != accepted
            or outcome_ids != accepted
            or run.execution_outcome.correlation_id != job.correlation_id
        ):
            return _workflow_blocked(
                job,
                evaluation,
                "workflow run assertions do not match the Captain release",
            )
        if (
            run.status != "succeeded"
            or run.execution_outcome.status != "succeeded"
            or any(
                outcome.status != "passed"
                for outcome in run.execution_outcome.assertion_outcomes
            )
        ):
            return _workflow_blocked(
                job,
                evaluation,
                "workflow run did not succeed",
            )
        if not run.usage_receipt_refs:
            return _workflow_blocked(
                job,
                evaluation,
                "workflow run is missing provider budget receipts",
            )
        if run.artifact_ref not in evaluation.evidence_refs:
            return _workflow_blocked(
                job,
                evaluation,
                "workflow evaluation is missing exact run evidence",
            )
    status: Literal["demo_ready", "ready"] = (
        "demo_ready"
        if job.execution_policy.mode is FactoryExecutionMode.DEMO
        else "ready"
    )
    return FactoryReleaseDecision(
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        status=status,
        reasons=(
            "one successful live demo run verified"
            if status == "demo_ready"
            else "three distinct successful live workflow runs verified"
        ,),
        evaluation_id=evaluation.invocation_id,
        evaluation_ref=evaluation.artifact_ref,
        tool_gaps=(),
    )


def factory_evaluation_block_reason(
    job: FactoryJob,
    evaluation: StoredSkillEvaluation | None,
) -> str | None:
    """Return the first auditable reason that blocks Captain promotion."""

    if evaluation is None:
        return "missing accepted Hermes skill evaluation evidence"
    evidence = evaluation.evidence
    request = evidence.request
    receipt = evidence.receipt
    if any(identity != job.job_id for identity in (evidence.job_id, request.job_id, receipt.job_id)):
        return "skill evaluation job does not match the factory job"
    if any(
        identity != job.correlation_id
        for identity in (evidence.correlation_id, request.correlation_id, receipt.correlation_id)
    ):
        return "skill evaluation correlation does not match the factory job"
    if any(
        version != job.subject_version
        for version in (
            evidence.subject_version,
            request.subject_version,
            request.lease.subject_version,
        )
    ):
        return "skill evaluation subject version does not match the factory job"
    if request.released_skill.capability != job.required_capability:
        return "released skill capability does not match the factory job"
    required_assertions = set(job.acceptance_assertion_ids)
    if set(request.acceptance_assertion_ids) != required_assertions:
        return "skill evaluation request assertions do not match the factory job"
    if not _valid_usage_receipt(evaluation):
        return "skill usage receipt is not valid for the factory job"
    missing_receipt_assertions = required_assertions - set(receipt.assertion_ids)
    if missing_receipt_assertions:
        return _missing_assertion_reason("skill usage receipt", missing_receipt_assertions)
    conflict = _conflicting_tool_gap(evaluation)
    if conflict is not None:
        return f"conflicting TODO_TOOL evidence for gap: {conflict}"
    required_gaps = tuple(
        marker
        for marker in evaluation_tool_gaps(evaluation)
        if marker.severity == "required" and marker.status == "unresolved"
    )
    if required_gaps:
        return "unresolved required TODO_TOOL gaps: " + ", ".join(
            marker.gap_id for marker in required_gaps
        )
    if evidence.outcome != "passed":
        if evidence.outcome in {"redo", "failed"}:
            return "skill candidate evaluator did not succeed"
        return f"skill evaluation outcome is not passed: {evidence.outcome}"
    if evidence.candidate is None:
        return "skill evaluation is missing a private candidate"
    if evaluation.candidate_ref is None:
        return "skill evaluation candidate was not retained"
    if {check.kind for check in evidence.checks} != {"build", "test"} or any(
        check.status != "passed" for check in evidence.checks
    ):
        return "skill candidate evaluator did not succeed"
    missing_evidence_assertions = required_assertions - set(evidence.assertion_ids)
    if missing_evidence_assertions:
        return _missing_assertion_reason("skill evaluation", missing_evidence_assertions)
    successful_assertions = {
        assertion_id
        for check in evidence.checks
        if check.status == "passed"
        for assertion_id in check.assertion_ids
    }
    missing_check_assertions = required_assertions - successful_assertions
    if missing_check_assertions:
        return _missing_assertion_reason("skill candidate evaluator", missing_check_assertions)
    return None


def evaluation_tool_gaps(evaluation: StoredSkillEvaluation) -> tuple[ToolGapMarker, ...]:
    """Project every typed marker once while retaining optional unresolved gaps."""

    markers: dict[str, ToolGapMarker] = {}
    for marker in (*evaluation.evidence.tool_gaps, *evaluation.tool_gaps):
        markers.setdefault(marker.gap_id, marker)
    return tuple(markers.values())


def evaluation_requires_improvement(
    job: FactoryJob,
    evaluation: StoredSkillEvaluation | None,
) -> bool:
    """Honor only a matching code/skill failure as a behavioral retry."""

    if evaluation is None:
        return False
    evidence = evaluation.evidence
    request = evidence.request
    if (
        evidence.job_id != job.job_id
        or request.job_id != job.job_id
        or evidence.correlation_id != job.correlation_id
        or request.correlation_id != job.correlation_id
        or evidence.subject_version != job.subject_version
        or request.subject_version != job.subject_version
    ):
        return False
    return evidence.outcome in {"redo", "failed"}


def _valid_usage_receipt(evaluation: StoredSkillEvaluation) -> bool:
    evidence = evaluation.evidence
    request = evidence.request
    receipt = evidence.receipt
    released_skill = request.released_skill
    return (
        receipt.request_id == evidence.request_id == request.request_id
        and receipt.lease_id == request.lease.lease_id
        and receipt.released_skill == released_skill
        and receipt.used_skill_id == released_skill.skill_id
        and receipt.used_skill_version == released_skill.version
        and receipt.used_skill_sha256 == released_skill.content_sha256
        and receipt.outcome in {"unresolved", "passed"}
        and request.lease.issued_at <= receipt.occurred_at < request.lease.expires_at
        and receipt.occurred_at >= request.occurred_at
    )


def _conflicting_tool_gap(evaluation: StoredSkillEvaluation) -> str | None:
    markers: dict[str, ToolGapMarker] = {}
    for marker in (*evaluation.evidence.tool_gaps, *evaluation.tool_gaps):
        existing = markers.get(marker.gap_id)
        if existing is not None and existing != marker:
            return marker.gap_id
        markers[marker.gap_id] = marker
    return None


def _missing_assertion_reason(subject: str, missing: set[str]) -> str:
    return f"{subject} is missing required acceptance assertions: {', '.join(sorted(missing))}"


def _blocked(
    job: FactoryJob,
    reason: str,
    evaluation: StoredSkillEvaluation | None = None,
) -> FactoryReleaseDecision:
    return FactoryReleaseDecision(
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        status="blocked",
        reasons=(reason,),
        evaluation_id=None if evaluation is None else evaluation.evidence.evidence_id,
        evaluation_ref=None if evaluation is None else evaluation.evidence_ref,
        tool_gaps=() if evaluation is None else evaluation_tool_gaps(evaluation),
    )


def _workflow_evaluation_block_reason(
    job: AgentFactoryJobV3,
    evidence: tuple[TeamExecutionEvidenceV1, ...],
    evaluation: TeamEvaluationV1,
) -> str | None:
    if not job.execution_policy.live_execution:
        return "workflow release requires Captain-authorized live execution"
    if (
        evaluation.job_id != job.job_id
        or evaluation.correlation_id != job.correlation_id
        or evaluation.subject_version != job.subject_version
        or evaluation.attempt < 1
        or evaluation.acceptance_assertion_ids != job.acceptance_assertion_ids
        or evaluation.invocation.job_id != job.job_id
        or evaluation.invocation.correlation_id != job.correlation_id
        or evaluation.invocation.subject_version != job.subject_version
        or evaluation.invocation.attempt != evaluation.attempt
    ):
        return "workflow evaluation identity does not match the Factory job"
    if not evidence:
        return "missing live workflow execution evidence"
    evaluation_ids = tuple(
        item.assertion_id for item in evaluation.assertion_outcomes
    )
    if evaluation_ids != job.acceptance_assertion_ids:
        return "workflow evaluation assertions do not match the Captain release"
    if (
        evaluation.failure_class is not None
        or evaluation.recommendation
        is not FactoryFeedbackRecommendation.PROMOTE_CANDIDATE
        or any(item.status != "passed" for item in evaluation.assertion_outcomes)
    ):
        return "workflow evaluation did not recommend the candidate"
    return None


def factory_workflow_release_decision_block_reason(
    job: AgentFactoryJobV3,
    evaluation: TeamEvaluationV1 | None,
    decision: FactoryReleaseDecision | None,
) -> str | None:
    """Require a Captain V3 decision bound only to workflow evaluation evidence."""

    if decision is None:
        return "missing accepted Factory workflow release decision"
    if decision.status != "ready":
        return "Factory workflow release decision is blocked: " + ", ".join(
            decision.reasons
        )
    if decision.job_id != job.job_id or decision.correlation_id != job.correlation_id:
        return "Factory workflow release decision does not match the factory job"
    if evaluation is None:
        return "missing accepted workflow evaluation evidence"
    if (
        decision.evaluation_id != evaluation.invocation_id
        or decision.evaluation_ref != evaluation.artifact_ref
    ):
        return "Factory workflow release decision does not match the workflow evaluation"
    if decision.tool_gaps:
        return "Factory workflow release decision contains unvalidated tool gaps"
    return None


def _workflow_blocked(
    job: AgentFactoryJobV3,
    evaluation: TeamEvaluationV1,
    reason: str,
) -> FactoryReleaseDecision:
    return FactoryReleaseDecision(
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        status="blocked",
        reasons=(reason,),
        evaluation_id=evaluation.invocation_id,
        evaluation_ref=evaluation.artifact_ref,
        tool_gaps=(),
    )
