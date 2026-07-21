from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from agenten.agent_factory.contracts import (
    AgentFactoryJob,
    AgentFactoryJobV2,
    AgentFactoryJobV3,
    FactoryBlockStatus,
    FactoryEvidenceBlock,
    FactoryPhase,
    FactoryRole,
)
from agenten.agent_factory.state_machine import (
    FactoryActionKind,
    FactoryLifecycleError,
    FactoryLifecycleStatus,
    FactoryProjection,
    apply_block,
    next_action,
)
from agenten.agent_factory.release_gate import (
    E2EKind,
    E2EOutcome,
    E2ERunEvidence,
    FactoryReleaseDecision,
    evaluate_factory_release,
)
from agenten.agent_factory.skill_evaluation import HermesSkillEvaluationEvidence
from agenten.agent_factory.skill_store import StoredSkillEvaluation
from agenten.agent_runtime.contracts import ArtifactRef
from tests.agent_factory.test_factory_feedback import (
    _budget as workflow_budget,
    _candidate as workflow_candidate,
    _evaluation as workflow_evaluation,
    _report_invocation,
)
from agenten.agent_factory.factory_feedback import FactoryFeedbackBuilder
from tests.agent_factory.test_skill_evaluation_contracts import evidence_payload


NOW = datetime(2026, 7, 19, 10, tzinfo=timezone.utc)


def artifact(name: str) -> dict[str, str]:
    return {
        "uri": f"artifact://factory/{name}",
        "sha256": "a" * 64,
        "media_type": "application/json",
    }


def job() -> AgentFactoryJob:
    return AgentFactoryJob.model_validate(
        {
            "schema": "captain.agent-factory-job.v1",
            "event_id": "00000000-0000-0000-0000-000000000001",
            "correlation_id": "00000000-0000-0000-0000-000000000002",
            "occurred_at": NOW,
            "producer": "captain",
            "job_id": "00000000-0000-0000-0000-000000000003",
            "subject_version": 1,
            "input_ref": artifact("input"),
            "required_capability": "support_triage",
            "acceptance_assertion_ids": ["schema_valid", "real_case_green"],
            "max_behavioral_iterations": 5,
        }
    )


def job_v3(*, mode: str = "release") -> AgentFactoryJobV3:
    required_runs = 1 if mode == "demo" else 3
    return AgentFactoryJobV3.model_validate(
        {
            **job().model_dump(mode="json", by_alias=True),
            "schema": "captain.agent-factory-job.v3",
            "event_id": "00000000-0000-0000-0000-000000000311",
            "correlation_id": "00000000-0000-0000-0000-000000000302",
            "job_id": "00000000-0000-0000-0000-000000000301",
            "input_ref": {
                "uri": f"artifact://sha256/{'a' * 64}",
                "sha256": "a" * 64,
                "media_type": "application/json",
            },
            "compiled_spec_ref": {
                "uri": f"artifact://sha256/{'b' * 64}",
                "sha256": "b" * 64,
                "media_type": "application/json",
            },
            "dependency_graph_ref": {
                "uri": f"artifact://sha256/{'c' * 64}",
                "sha256": "c" * 64,
                "media_type": "application/json",
            },
            "private_holdout_refs": [
                {
                    "schema_name": "captain.private-holdout-ref.v1",
                    "holdout_id": "holdout-222222222222",
                    "uri": "holdout://holdout-222222222222",
                    "sha256": "2" * 64,
                }
            ],
            "deadline_at": NOW.replace(year=2026, month=7, day=19, hour=10, minute=15),
            "execution_policy": {
                "schema": "captain.factory-execution-policy.v1",
                "mode": mode,
                "live_execution": True,
                "max_cost_usd": "5.00",
                "max_runtime_seconds": 900,
                "required_live_runs": required_runs,
                "allowed_models": ["approved-model-id"],
                "live_capabilities": ["model.invoke"],
                "sandbox_mode": "workspace_write",
            },
        }
    )


def job_v2() -> AgentFactoryJobV2:
    payload = job_v3().model_dump(mode="json", by_alias=True)
    payload["schema"] = "captain.agent-factory-job.v2"
    payload.pop("execution_policy")
    return AgentFactoryJobV2.model_validate(payload)


def workflow_block(
    phase: FactoryPhase,
    *,
    assertions: tuple[str, ...] = (),
) -> FactoryEvidenceBlock:
    factory_job = job_v3()
    return block(phase, assertions=assertions).model_copy(
        update={
            "job_id": factory_job.job_id,
            "correlation_id": factory_job.correlation_id,
            "causation_id": factory_job.event_id,
        }
    )


def block(
    phase: FactoryPhase,
    *,
    status: FactoryBlockStatus = FactoryBlockStatus.SUCCEEDED,
    role: FactoryRole | None = None,
    producer: str | None = None,
    attempt: int = 1,
    assertions: tuple[str, ...] = (),
) -> FactoryEvidenceBlock:
    role_for_phase = {
        FactoryPhase.BLUEPRINT_CREATED: FactoryRole.AGENT_ARCHITECT,
        FactoryPhase.TOOL_CANDIDATE_TESTED: FactoryRole.TOOL_INTEGRATOR,
        FactoryPhase.AGENT_CODE_CREATED: FactoryRole.TOOL_INTEGRATOR,
        FactoryPhase.BUILD_PASSED: FactoryRole.TOOL_INTEGRATOR,
        FactoryPhase.BUILD_FAILED: FactoryRole.TOOL_INTEGRATOR,
        FactoryPhase.REAL_CASE_EVIDENCE: FactoryRole.REAL_CASE_TESTER,
        FactoryPhase.QUALITY_REVIEWED: FactoryRole.QUALITY_WARDEN,
    }.get(phase)
    effective_role = role if role is not None else role_for_phase
    effective_producer = producer or ("hermes" if effective_role else "captain")
    return FactoryEvidenceBlock.model_validate(
        {
            "schema": "captain.agent-factory-block.v1",
            "event_id": f"00000000-0000-0000-0000-{int(phase.value.encode().hex(), 16) % 10**12:012d}",
            "job_id": str(job().job_id),
            "correlation_id": str(job().correlation_id),
            "causation_id": str(job().event_id),
            "occurred_at": NOW,
            "producer": effective_producer,
            "subject_version": 1,
            "attempt": attempt,
            "phase": phase.value,
            "role": effective_role.value if effective_role else None,
            "status": status.value,
            "artifact_refs": [artifact(phase.value)],
            "evidence_refs": [artifact(f"evidence-{phase.value}")],
            "assertion_ids": list(assertions),
            "lease_id": "lease-1" if effective_role else None,
        }
    )


def accepted_evaluation() -> StoredSkillEvaluation:
    factory_job = job()
    evidence = HermesSkillEvaluationEvidence.model_validate(evidence_payload())
    released_skill = evidence.request.released_skill.model_copy(
        update={"capability": factory_job.required_capability}
    )
    request = evidence.request.model_copy(
        update={
            "job_id": factory_job.job_id,
            "correlation_id": factory_job.correlation_id,
            "subject_version": factory_job.subject_version,
            "acceptance_assertion_ids": factory_job.acceptance_assertion_ids,
            "released_skill": released_skill,
            "lease": evidence.request.lease.model_copy(
                update={
                    "job_id": factory_job.job_id,
                    "correlation_id": factory_job.correlation_id,
                    "subject_version": factory_job.subject_version,
                }
            ),
        }
    )
    receipt = evidence.receipt.model_copy(
        update={
            "job_id": factory_job.job_id,
            "correlation_id": factory_job.correlation_id,
            "lease_id": request.lease.lease_id,
            "released_skill": released_skill,
            "used_skill_id": released_skill.skill_id,
            "used_skill_version": released_skill.version,
            "used_skill_sha256": released_skill.content_sha256,
            "assertion_ids": factory_job.acceptance_assertion_ids,
        }
    )
    assert evidence.candidate is not None
    candidate = evidence.candidate.model_copy(
        update={"parent_released_skill": released_skill}
    )
    evidence = evidence.model_copy(
        update={
            "job_id": factory_job.job_id,
            "correlation_id": factory_job.correlation_id,
            "subject_version": factory_job.subject_version,
            "request": request,
            "receipt": receipt,
            "candidate": candidate,
            "assertion_ids": factory_job.acceptance_assertion_ids,
        }
    )
    return StoredSkillEvaluation(
        evidence=evidence,
        evidence_ref=ArtifactRef.model_validate(artifact("accepted-skill-evaluation")),
        receipt_ref=ArtifactRef.model_validate(artifact("accepted-skill-receipt")),
        tool_gaps=evidence.tool_gaps,
        tool_gap_refs=tuple(
            (marker.gap_id, marker.evidence_ref) for marker in evidence.tool_gaps
        ),
        candidate_ref=ArtifactRef.model_validate(artifact("retained-skill-candidate")),
    )


def accepted_release_decision(
    evaluation: StoredSkillEvaluation | None = None,
):
    stored = evaluation or accepted_evaluation()
    runs = (
        E2ERunEvidence(
            run_number=1,
            correlation_id=job().correlation_id,
            kind=E2EKind.RECOVERY,
            outcome=E2EOutcome.EXPECTED_FAILURE,
            evidence_ref=ArtifactRef.model_validate(artifact("recovery-e2e")),
        ),
        *(
            E2ERunEvidence(
                run_number=number,
                correlation_id=job().correlation_id,
                kind=E2EKind.NORMAL,
                outcome=E2EOutcome.SUCCEEDED,
                evidence_ref=ArtifactRef.model_validate(artifact(f"normal-e2e-{number}")),
            )
            for number in range(2, 5)
        ),
    )
    return evaluate_factory_release(job(), runs, stored)


def quality_reviewed_projection(*, attempt: int = 1) -> FactoryProjection:
    state = FactoryProjection.from_job(job()).model_copy(update={"attempt": attempt})
    for phase in (
        FactoryPhase.FORGE_REQUESTED,
        FactoryPhase.BLUEPRINT_CREATED,
        FactoryPhase.TOOL_CANDIDATE_TESTED,
        FactoryPhase.AGENT_CODE_CREATED,
        FactoryPhase.BUILD_PASSED,
    ):
        state = apply_block(state, block(phase, attempt=attempt))
    state = apply_block(
        state,
        block(
            FactoryPhase.REAL_CASE_EVIDENCE,
            attempt=attempt,
            assertions=("real_case_green",),
        ),
    )
    return apply_block(
        state,
        block(
            FactoryPhase.QUALITY_REVIEWED,
            attempt=attempt,
            assertions=("schema_valid",),
        ),
    )


def test_initial_state_requests_captain_forge_block() -> None:
    projection = FactoryProjection.from_job(job())

    assert projection.status is FactoryLifecycleStatus.PENDING
    assert next_action(projection).kind is FactoryActionKind.APPEND_FORGE_REQUESTED


def test_happy_path_requires_all_captain_assertions_before_promotion() -> None:
    state = FactoryProjection.from_job(job())
    for event in (
        block(FactoryPhase.FORGE_REQUESTED),
        block(FactoryPhase.BLUEPRINT_CREATED),
        block(FactoryPhase.TOOL_CANDIDATE_TESTED),
        block(FactoryPhase.AGENT_CODE_CREATED),
        block(FactoryPhase.BUILD_PASSED),
        block(FactoryPhase.REAL_CASE_EVIDENCE, assertions=("real_case_green",)),
        block(FactoryPhase.QUALITY_REVIEWED, assertions=("schema_valid",)),
    ):
        state = apply_block(state, event)

    assert next_action(state).kind is FactoryActionKind.VALIDATE_FOR_PROMOTION


def test_missing_assertion_requests_improvement_not_promotion() -> None:
    state = FactoryProjection.from_job(job())
    for event in (
        block(FactoryPhase.FORGE_REQUESTED),
        block(FactoryPhase.BLUEPRINT_CREATED),
        block(FactoryPhase.TOOL_CANDIDATE_TESTED),
        block(FactoryPhase.AGENT_CODE_CREATED),
        block(FactoryPhase.BUILD_PASSED),
        block(FactoryPhase.REAL_CASE_EVIDENCE),
        block(FactoryPhase.QUALITY_REVIEWED),
    ):
        state = apply_block(state, event)

    assert next_action(state).kind is FactoryActionKind.APPEND_IMPROVEMENT_REQUESTED


def test_fifth_behavioral_failure_escalates() -> None:
    state = FactoryProjection.from_job(job()).model_copy(update={"attempt": 5})
    for phase in (
        FactoryPhase.FORGE_REQUESTED,
        FactoryPhase.BLUEPRINT_CREATED,
        FactoryPhase.TOOL_CANDIDATE_TESTED,
        FactoryPhase.AGENT_CODE_CREATED,
        FactoryPhase.BUILD_FAILED,
    ):
        state = apply_block(state, block(phase, attempt=5))

    assert next_action(state).kind is FactoryActionKind.APPEND_ESCALATED


def test_infrastructure_failure_keeps_attempt_and_waits() -> None:
    state = FactoryProjection.from_job(job())
    state = apply_block(state, block(FactoryPhase.FORGE_REQUESTED))
    state = apply_block(
        state,
        block(
            FactoryPhase.BLUEPRINT_CREATED,
            status=FactoryBlockStatus.INFRASTRUCTURE_FAILED,
        ),
    )

    assert state.attempt == 1
    assert next_action(state).kind is FactoryActionKind.WAIT_INFRASTRUCTURE


def test_out_of_order_block_and_version_mismatch_fail_closed() -> None:
    with pytest.raises(FactoryLifecycleError, match="illegal phase"):
        apply_block(FactoryProjection.from_job(job()), block(FactoryPhase.BUILD_PASSED))

    stale = block(FactoryPhase.FORGE_REQUESTED).model_copy(update={"subject_version": 2})
    with pytest.raises(FactoryLifecycleError, match="subject version"):
        apply_block(FactoryProjection.from_job(job()), stale)


def test_quality_review_cannot_promote_without_accepted_evaluation() -> None:
    state = quality_reviewed_projection()
    promotion = block(
        FactoryPhase.CAPABILITY_PROMOTED,
        assertions=job().acceptance_assertion_ids,
    )

    with pytest.raises(
        FactoryLifecycleError,
        match="missing accepted Hermes skill evaluation evidence",
    ):
        apply_block(state, promotion)

    evaluation = accepted_evaluation()
    with pytest.raises(
        FactoryLifecycleError,
        match="missing accepted Factory release decision",
    ):
        apply_block(state, promotion, evaluation=evaluation)

    blocked = evaluate_factory_release(job(), (), evaluation)
    with pytest.raises(FactoryLifecycleError, match="release decision is blocked"):
        apply_block(
            state,
            promotion,
            evaluation=evaluation,
            release_decision=blocked,
        )

    promoted = apply_block(
        state,
        promotion,
        evaluation=evaluation,
        release_decision=accepted_release_decision(evaluation),
    )

    assert promoted.status is FactoryLifecycleStatus.READY_TO_USE
    assert promoted.evaluation_id == evaluation.evidence.evidence_id
    assert promoted.evaluation_ref == evaluation.evidence_ref
    assert promoted.tool_gaps == evaluation.tool_gaps


def test_failed_skill_evaluation_uses_existing_improvement_and_attempt_ceiling() -> None:
    failed = accepted_evaluation()
    failed = StoredSkillEvaluation(
        **{
            **failed.__dict__,
            "evidence": failed.evidence.model_copy(update={"outcome": "redo"}),
        }
    )

    assert (
        next_action(quality_reviewed_projection(), evaluation=failed).kind
        is FactoryActionKind.APPEND_IMPROVEMENT_REQUESTED
    )
    assert (
        next_action(quality_reviewed_projection(attempt=5), evaluation=failed).kind
        is FactoryActionKind.APPEND_ESCALATED
    )


def test_v3_quality_feedback_routes_only_through_existing_captain_actions() -> None:
    evaluation = workflow_evaluation()
    feedback = FactoryFeedbackBuilder(clock=lambda: evaluation.occurred_at).build(
        invocation=_report_invocation(evaluation),
        candidate_ref=workflow_candidate(),
        evaluation=evaluation,
        budget_projection=workflow_budget(),
    )
    state = FactoryProjection.from_job(job_v3())
    for phase in (
        FactoryPhase.FORGE_REQUESTED,
        FactoryPhase.BLUEPRINT_CREATED,
        FactoryPhase.TOOL_CANDIDATE_TESTED,
        FactoryPhase.AGENT_CODE_CREATED,
        FactoryPhase.BUILD_PASSED,
        FactoryPhase.REAL_CASE_EVIDENCE,
    ):
        state = apply_block(state, workflow_block(phase))
    reviewed = workflow_block(
        FactoryPhase.QUALITY_REVIEWED,
        assertions=job_v3().acceptance_assertion_ids,
    ).model_copy(
        update={"artifact_refs": (evaluation.artifact_ref, feedback.artifact_ref)}
    )
    state = apply_block(
        state,
        reviewed,
        workflow_evaluation=evaluation,
        feedback=feedback,
    )

    assert state.workflow_evaluation_ref == evaluation.artifact_ref
    assert state.feedback_ref == feedback.artifact_ref
    ready = FactoryReleaseDecision(
        job_id=job_v3().job_id,
        correlation_id=job_v3().correlation_id,
        status="ready",
        reasons=("workflow release verified",),
        evaluation_id=evaluation.invocation_id,
        evaluation_ref=evaluation.artifact_ref,
    )
    assert next_action(
        state,
        workflow_evaluation=evaluation,
        feedback=feedback,
        workflow_release_decision=ready,
    ).kind is FactoryActionKind.VALIDATE_FOR_PROMOTION

    demo_ready = ready.model_copy(
        update={
            "status": "demo_ready",
            "reasons": ("one demo run verified",),
        }
    )
    assert next_action(
        state,
        workflow_evaluation=evaluation,
        feedback=feedback,
        workflow_release_decision=demo_ready,
    ).kind is FactoryActionKind.APPEND_ESCALATED

    demo_state = state.model_copy(update={"job": job_v3(mode="demo")})
    assert next_action(
        demo_state,
        workflow_evaluation=evaluation,
        feedback=feedback,
        workflow_release_decision=demo_ready,
    ).kind is FactoryActionKind.VALIDATE_FOR_PROMOTION


def test_v3_promotion_uses_only_the_workflow_release_decision() -> None:
    evaluation = workflow_evaluation()
    feedback = FactoryFeedbackBuilder(clock=lambda: evaluation.occurred_at).build(
        invocation=_report_invocation(evaluation),
        candidate_ref=workflow_candidate(),
        evaluation=evaluation,
        budget_projection=workflow_budget(),
    )
    state = FactoryProjection.from_job(job_v3()).model_copy(
        update={
            "status": FactoryLifecycleStatus.RUNNING,
            "phase": FactoryPhase.QUALITY_REVIEWED,
            "observed_assertion_ids": job_v3().acceptance_assertion_ids,
            "workflow_evaluation_ref": evaluation.artifact_ref,
            "feedback_ref": feedback.artifact_ref,
            "feedback_recommendation": feedback.recommendation,
        }
    )
    promotion = workflow_block(
        FactoryPhase.CAPABILITY_PROMOTED,
        assertions=job_v3().acceptance_assertion_ids,
    )
    legacy_evaluation = accepted_evaluation()

    with pytest.raises(FactoryLifecycleError, match="workflow"):
        apply_block(
            state,
            promotion,
            evaluation=legacy_evaluation,
            release_decision=accepted_release_decision(legacy_evaluation),
        )

    ready = FactoryReleaseDecision(
        job_id=job_v3().job_id,
        correlation_id=job_v3().correlation_id,
        status="ready",
        reasons=("workflow release verified",),
        evaluation_id=evaluation.invocation_id,
        evaluation_ref=evaluation.artifact_ref,
    )
    promoted = apply_block(
        state,
        promotion,
        workflow_evaluation=evaluation,
        feedback=feedback,
        release_decision=ready,
    )

    assert promoted.status is FactoryLifecycleStatus.READY_TO_USE
    assert promoted.workflow_evaluation_ref == evaluation.artifact_ref

    demo_state = state.model_copy(update={"job": job_v3(mode="demo")})
    demo_ready = ready.model_copy(update={"status": "demo_ready"})
    with pytest.raises(FactoryLifecycleError, match="demo"):
        apply_block(
            demo_state,
            promotion,
            workflow_evaluation=evaluation,
            feedback=feedback,
            release_decision=demo_ready,
        )


def test_v3_failed_feedback_requests_improvement_and_missing_feedback_fails_closed() -> None:
    evaluation = workflow_evaluation(failed=True)
    feedback = FactoryFeedbackBuilder(clock=lambda: evaluation.occurred_at).build(
        invocation=_report_invocation(evaluation),
        candidate_ref=workflow_candidate(),
        evaluation=evaluation,
        budget_projection=workflow_budget(),
    )
    state = FactoryProjection.from_job(job_v3()).model_copy(
        update={
            "status": FactoryLifecycleStatus.RUNNING,
            "phase": FactoryPhase.QUALITY_REVIEWED,
            "observed_assertion_ids": job().acceptance_assertion_ids,
            "workflow_evaluation_ref": evaluation.artifact_ref,
            "feedback_ref": feedback.artifact_ref,
        }
    )

    assert next_action(
        state,
        workflow_evaluation=evaluation,
        feedback=feedback,
    ).kind is FactoryActionKind.APPEND_IMPROVEMENT_REQUESTED
    with pytest.raises(FactoryLifecycleError, match="workflow feedback"):
        next_action(state)


def test_v1_and_v2_quality_routing_remain_unchanged() -> None:
    for factory_job in (job(), job_v2()):
        state = FactoryProjection.from_job(factory_job).model_copy(
            update={
                "status": FactoryLifecycleStatus.RUNNING,
                "phase": FactoryPhase.QUALITY_REVIEWED,
                "observed_assertion_ids": factory_job.acceptance_assertion_ids,
            }
        )
        assert next_action(state).kind is FactoryActionKind.VALIDATE_FOR_PROMOTION
