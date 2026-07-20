"""Captain-only release gate for the required E2E and recovery evidence."""

from __future__ import annotations

from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agenten.agent_factory.contracts import AgentFactoryJob
from agenten.agent_factory.skill_evaluation import ToolGapMarker
from agenten.agent_factory.skill_store import StoredSkillEvaluation
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
    evaluation_id: UUID | None = None
    evaluation_ref: ArtifactRef | None = None
    tool_gaps: tuple[ToolGapMarker, ...] = ()


def evaluate_factory_release(
    job: AgentFactoryJob,
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


def factory_evaluation_block_reason(
    job: AgentFactoryJob,
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
    job: AgentFactoryJob,
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
    job: AgentFactoryJob,
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
