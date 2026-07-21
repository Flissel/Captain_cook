"""Captain-only release gate for the required E2E and recovery evidence."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agenten.agent_factory.contracts import FactoryJob, FactoryPhase
from agenten.agent_factory.outcome_contracts import (
    CapabilityPackageManifestV1,
    CapabilityReleaseEvidenceV1,
    FactoryTerminalDecision,
    FactoryTerminalState,
)
from agenten.agent_factory.skill_evaluation import ToolGapMarker
from agenten.agent_factory.skill_store import StoredSkillEvaluation
from agenten.agent_runtime.contracts import ArtifactRef

if TYPE_CHECKING:
    from agenten.agent_factory.state_machine import FactoryProjection


class FactoryTerminalReasonCode(str, Enum):
    """Stable Captain policy codes; worker prose never becomes authority."""

    STRUCTURAL_VIOLATION = "structural_violation"
    SECURITY_VIOLATION = "security_violation"
    FOREIGN_LEASE = "foreign_lease"
    SCHEMA_VIOLATION = "schema_violation"
    DIGEST_VIOLATION = "digest_violation"
    AUTHORITY_VIOLATION = "authority_violation"
    MISSING_CREDENTIAL_ALIAS = "missing_credential_alias"
    MISSING_PROVIDER = "missing_provider"
    MISSING_API = "missing_api"
    USER_DECISION_REQUIRED = "user_decision_required"
    REQUIRED_TOOL_GAP = "required_tool_gap"
    BEHAVIORAL_ITERATIONS_EXHAUSTED = "behavioral_iterations_exhausted"
    DEADLINE_EXHAUSTED = "deadline_exhausted"
    PACKAGE_VALIDATION_BLOCKED = "package_validation_blocked"
    EVALUATION_BLOCKED = "evaluation_blocked"
    ASSERTION_BLOCKED = "assertion_blocked"
    E2E_BLOCKED = "e2e_blocked"
    READY_TO_USE = "ready_to_use"


_REJECTION_CODES = frozenset(
    {
        FactoryTerminalReasonCode.STRUCTURAL_VIOLATION,
        FactoryTerminalReasonCode.SECURITY_VIOLATION,
        FactoryTerminalReasonCode.FOREIGN_LEASE,
        FactoryTerminalReasonCode.SCHEMA_VIOLATION,
        FactoryTerminalReasonCode.DIGEST_VIOLATION,
        FactoryTerminalReasonCode.AUTHORITY_VIOLATION,
    }
)
_PREREQUISITE_CODES = frozenset(
    {
        FactoryTerminalReasonCode.MISSING_CREDENTIAL_ALIAS,
        FactoryTerminalReasonCode.MISSING_PROVIDER,
        FactoryTerminalReasonCode.MISSING_API,
        FactoryTerminalReasonCode.USER_DECISION_REQUIRED,
    }
)


class FactoryPolicyFinding(BaseModel):
    """Captain-authored evidence for an explicit external prerequisite."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason_code: FactoryTerminalReasonCode
    evidence_ref: ArtifactRef
    producer: Literal["captain"] = "captain"

    @model_validator(mode="after")
    def require_policy_finding_code(self) -> "FactoryPolicyFinding":
        if self.reason_code not in _REJECTION_CODES | _PREREQUISITE_CODES:
            raise ValueError("policy finding requires a rejection or prerequisite reason code")
        return self


class CapabilityValidationFailure(BaseModel):
    """Thin adapter for a fail-closed validator failure and its Captain evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason_code: FactoryTerminalReasonCode
    evidence_ref: ArtifactRef
    producer: Literal["captain"] = "captain"

    @model_validator(mode="after")
    def require_rejection_code(self) -> "CapabilityValidationFailure":
        if self.reason_code not in _REJECTION_CODES:
            raise ValueError("capability validation failure requires a rejection reason code")
        return self


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
    status: Literal["blocked", "ready"]
    reasons: tuple[str, ...]
    evaluation_id: UUID | None = None
    evaluation_ref: ArtifactRef | None = None
    tool_gaps: tuple[ToolGapMarker, ...] = ()


def derive_terminal_decision(
    job: FactoryJob,
    projection: "FactoryProjection",
    validation: CapabilityPackageManifestV1 | CapabilityValidationFailure | None,
    evaluation: StoredSkillEvaluation | None,
    e2e: tuple[CapabilityReleaseEvidenceV1, ...],
    now: datetime,
) -> FactoryTerminalDecision | None:
    """Derive Captain's terminal state in one explicit fail-closed priority order."""

    if projection.terminal_decision is not None:
        return projection.terminal_decision
    if (
        projection.job != job
        or projection.job.job_id != job.job_id
        or projection.job.correlation_id != job.correlation_id
    ):
        return _terminal_decision(
            job,
            FactoryTerminalState.REJECTED,
            FactoryTerminalReasonCode.AUTHORITY_VIOLATION,
            (),
            now,
        )

    if isinstance(validation, CapabilityValidationFailure):
        return _terminal_decision(
            job,
            FactoryTerminalState.REJECTED,
            validation.reason_code,
            (validation.evidence_ref,),
            now,
        )

    validation_rejection = _validation_rejection(job, validation)
    if validation_rejection is not None:
        code, references = validation_rejection
        return _terminal_decision(
            job,
            FactoryTerminalState.REJECTED,
            code,
            references,
            now,
        )

    evaluation_reason = factory_evaluation_block_reason(job, evaluation)
    if evaluation_reason is not None and _evaluation_is_rejection(evaluation_reason):
        return _terminal_decision(
            job,
            FactoryTerminalState.REJECTED,
            _evaluation_rejection_code(evaluation_reason),
            _evaluation_references(evaluation),
            now,
        )

    rejection = next(
        (finding for finding in projection.policy_findings if finding.reason_code in _REJECTION_CODES),
        None,
    )
    if rejection is not None:
        return _terminal_decision(
            job,
            FactoryTerminalState.REJECTED,
            rejection.reason_code,
            (rejection.evidence_ref,),
            now,
        )

    prerequisite = next(
        (finding for finding in projection.policy_findings if finding.reason_code in _PREREQUISITE_CODES),
        None,
    )
    if prerequisite is not None:
        return _terminal_decision(
            job,
            FactoryTerminalState.BLOCKED,
            prerequisite.reason_code,
            (prerequisite.evidence_ref,),
            now,
        )

    all_tool_gaps = (
        *((() if validation is None else validation.tool_gaps)),
        *((() if evaluation is None else evaluation_tool_gaps(evaluation))),
    )
    required_gaps = tuple(
        marker
        for marker in all_tool_gaps
        if marker.severity == "required" and marker.status == "unresolved"
    )
    if required_gaps:
        return _terminal_decision(
            job,
            FactoryTerminalState.BLOCKED,
            FactoryTerminalReasonCode.REQUIRED_TOOL_GAP,
            tuple(marker.evidence_ref for marker in required_gaps),
            now,
        )

    if _behavioral_budget_exhausted(job, projection, evaluation):
        return _terminal_decision(
            job,
            FactoryTerminalState.ESCALATED,
            FactoryTerminalReasonCode.BEHAVIORAL_ITERATIONS_EXHAUSTED,
            _projection_evidence(projection),
            now,
        )
    deadline_at = getattr(job, "deadline_at", None)
    if deadline_at is not None and now >= deadline_at:
        return _terminal_decision(
            job,
            FactoryTerminalState.ESCALATED,
            FactoryTerminalReasonCode.DEADLINE_EXHAUSTED,
            _projection_evidence(projection),
            now,
        )

    if validation is None and evaluation is None and not e2e and not _validation_due(projection):
        return None
    if validation is None:
        return _terminal_decision(
            job,
            FactoryTerminalState.BLOCKED,
            FactoryTerminalReasonCode.PACKAGE_VALIDATION_BLOCKED,
            (),
            now,
        )
    if evaluation_reason is not None:
        return _terminal_decision(
            job,
            FactoryTerminalState.BLOCKED,
            FactoryTerminalReasonCode.EVALUATION_BLOCKED,
            _evaluation_references(evaluation),
            now,
        )

    assertion_references = _failed_assertion_references(job, validation, e2e)
    if assertion_references is not None:
        return _terminal_decision(
            job,
            FactoryTerminalState.BLOCKED,
            FactoryTerminalReasonCode.ASSERTION_BLOCKED,
            assertion_references,
            now,
        )

    if not _accepted_capability_e2e(job, projection, validation, e2e):
        return _terminal_decision(
            job,
            FactoryTerminalState.BLOCKED,
            FactoryTerminalReasonCode.E2E_BLOCKED,
            validation.release_evidence_refs,
            now,
        )

    assert evaluation is not None
    return _terminal_decision(
        job,
        FactoryTerminalState.READY_TO_USE,
        FactoryTerminalReasonCode.READY_TO_USE,
        (*validation.release_evidence_refs, evaluation.evidence_ref),
        now,
    )


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


def _validation_rejection(
    job: FactoryJob,
    validation: CapabilityPackageManifestV1 | None,
) -> tuple[FactoryTerminalReasonCode, tuple[ArtifactRef, ...]] | None:
    if validation is None:
        return None
    if (
        validation.factory_job_id != job.job_id
        or validation.correlation_id != job.correlation_id
        or validation.subject_version != job.subject_version
        or validation.capability_id != job.required_capability
    ):
        return FactoryTerminalReasonCode.AUTHORITY_VIOLATION, (validation.source_ref,)
    return None


def _evaluation_is_rejection(reason: str) -> bool:
    return any(
        marker in reason
        for marker in (
            "does not match",
            "conflicting TODO_TOOL",
            "receipt is not valid",
        )
    )


def _evaluation_rejection_code(reason: str) -> FactoryTerminalReasonCode:
    if "conflicting" in reason:
        return FactoryTerminalReasonCode.STRUCTURAL_VIOLATION
    if "receipt is not valid" in reason:
        return FactoryTerminalReasonCode.DIGEST_VIOLATION
    return FactoryTerminalReasonCode.AUTHORITY_VIOLATION


def _evaluation_references(
    evaluation: StoredSkillEvaluation | None,
) -> tuple[ArtifactRef, ...]:
    if evaluation is None:
        return ()
    references = [evaluation.evidence_ref, evaluation.receipt_ref]
    if evaluation.candidate_ref is not None:
        references.append(evaluation.candidate_ref)
    references.extend(reference for _, reference in evaluation.tool_gap_refs)
    return _unique_refs(tuple(references))


def _projection_evidence(projection: "FactoryProjection") -> tuple[ArtifactRef, ...]:
    return _unique_refs(
        (*projection.evidence_refs, *(finding.evidence_ref for finding in projection.policy_findings))
    )


def _behavioral_budget_exhausted(
    job: FactoryJob,
    projection: "FactoryProjection",
    evaluation: StoredSkillEvaluation | None,
) -> bool:
    if projection.attempt < job.max_behavioral_iterations:
        return False
    return (
        projection.phase in {FactoryPhase.BUILD_FAILED, FactoryPhase.QUALITY_REVIEWED}
        or evaluation_requires_improvement(job, evaluation)
    )


def _validation_due(projection: "FactoryProjection") -> bool:
    return projection.phase in {FactoryPhase.BUILD_FAILED, FactoryPhase.QUALITY_REVIEWED}


def _failed_assertion_references(
    job: FactoryJob,
    validation: CapabilityPackageManifestV1,
    e2e: tuple[CapabilityReleaseEvidenceV1, ...],
) -> tuple[ArtifactRef, ...] | None:
    required = set(job.acceptance_assertion_ids)
    outcomes = {outcome.assertion_id: outcome for outcome in validation.assertion_outcomes}
    if set(outcomes) != required:
        return _unique_refs(
            tuple(
                reference
                for outcome in validation.assertion_outcomes
                for reference in outcome.evidence_refs
            )
        )
    failed = tuple(
        reference
        for outcome in validation.assertion_outcomes
        if outcome.status != "passed"
        for reference in outcome.evidence_refs
    )
    if failed:
        return _unique_refs(failed)
    for record in e2e:
        results = {result.assertion_id: result for result in record.assertion_results}
        if set(results) != required or any(result.status != "passed" for result in results.values()):
            return _unique_refs(
                tuple(
                    reference
                    for result in record.assertion_results
                    for reference in result.evidence_refs
                )
            )
    return None


def _accepted_capability_e2e(
    job: FactoryJob,
    projection: "FactoryProjection",
    validation: CapabilityPackageManifestV1,
    evidence: tuple[CapabilityReleaseEvidenceV1, ...],
) -> bool:
    ordered = tuple(sorted(evidence, key=lambda item: item.run_number))
    if len(ordered) < 4:
        return False
    if len({item.run_id for item in ordered}) != len(ordered):
        return False
    if len({item.run_number for item in ordered}) != len(ordered):
        return False
    if any(
        item.factory_job_id != job.job_id
        or item.creation_job_id != validation.creation_job_id
        or item.correlation_id != job.correlation_id
        or item.subject_version != job.subject_version
        or item.capability_id != validation.capability_id
        or item.capability_version != validation.capability_version
        or item.attempt != projection.attempt
        or item.package_archive_sha256 != validation.source_ref.sha256
        for item in ordered
    ):
        return False
    if len({item.candidate_manifest_sha256 for item in ordered}) != 1:
        return False
    if len({item.extracted_tree_sha256 for item in ordered}) != 1:
        return False
    recovery = tuple(
        item
        for item in ordered
        if item.kind == "recovery" and item.outcome == "expected_failure_recovered"
    )
    if not recovery:
        return False
    tail = ordered[-3:]
    if any(item.kind != "normal" or item.outcome != "succeeded" for item in tail):
        return False
    if tuple(item.run_number for item in tail) != tuple(
        range(tail[0].run_number, tail[0].run_number + 3)
    ):
        return False
    return max(item.run_number for item in recovery) < tail[0].run_number


def _terminal_decision(
    job: FactoryJob,
    state: FactoryTerminalState,
    reason: FactoryTerminalReasonCode,
    evidence_refs: tuple[ArtifactRef, ...],
    now: datetime,
) -> FactoryTerminalDecision:
    references = _unique_refs(evidence_refs)
    identity = "|".join(
        (
            str(job.job_id),
            str(job.correlation_id),
            str(job.subject_version),
            state.value,
            reason.value,
            *(reference.sha256 for reference in references),
        )
    )
    return FactoryTerminalDecision(
        schema_name="captain.factory-terminal-decision.v1",
        decision_id=uuid5(NAMESPACE_URL, identity),
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        subject_version=job.subject_version,
        state=state,
        reasons=(reason.value,),
        evidence_refs=references,
        decided_at=now,
    )


def _unique_refs(references: tuple[ArtifactRef, ...]) -> tuple[ArtifactRef, ...]:
    unique: list[ArtifactRef] = []
    seen: set[tuple[str, str, str]] = set()
    for reference in references:
        identity = (reference.uri, reference.sha256, reference.media_type)
        if identity not in seen:
            seen.add(identity)
            unique.append(reference)
    return tuple(unique)


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
