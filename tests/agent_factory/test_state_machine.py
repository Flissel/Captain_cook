from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from agenten.agent_factory.contracts import (
    AgentFactoryJob,
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
