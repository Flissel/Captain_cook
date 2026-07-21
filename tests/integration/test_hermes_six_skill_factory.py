from __future__ import annotations

from decimal import Decimal

import pytest

from agenten.agent_factory.execution_budget import BudgetExhausted
from agenten.agent_factory.orchestration import FactoryDispatchError
from agenten.agent_factory.skill_workflow_contracts import (
    FactoryFeedbackRecommendation,
    TeamEvaluationV1,
    TeamExecutionEvidenceV1,
)
from agenten.agent_factory.state_machine import FactoryLifecycleStatus
from tests.support.hermes_six_skill_factory import (
    FIRST_PASS_STEPS,
    RETRY_STEPS,
    SixSkillFactoryHarness,
)


@pytest.mark.asyncio
async def test_first_pass_runs_five_skill_steps_and_promotes() -> None:
    result = await SixSkillFactoryHarness().run()

    assert result.skill_steps == FIRST_PASS_STEPS
    assert result.attempts == 1
    assert result.gateway_projection.status is FactoryLifecycleStatus.READY_TO_USE
    assert result.gateway_phases[-1] == "capability_promoted"
    assert sum(
        isinstance(item, TeamExecutionEvidenceV1)
        for item in result.workflow_artifacts
    ) == 3
    assert sum(
        isinstance(item, TeamEvaluationV1) for item in result.workflow_artifacts
    ) == 1
    assert len(result.usage_receipts) == 3
    assert result.total_cost_usd == Decimal("0.75")
    assert result.used_composed_ports is True
    assert result.production_dispatch_count == 6
    assert result.production_dispatch_actions == (
        "dispatch_agent_architect",
        "dispatch_tool_integrator",
        "submit_forge_job",
        "dispatch_build_validator",
        "dispatch_real_case_tester",
        "dispatch_quality_warden",
    )
    assert result.budget_projection == result.gateway_budget_projection
    assert result.team_execution_execute_calls == 3
    assert result.team_execution_provider_calls == 3
    assert tuple(item.run_number for item in result.team_execution_runs) == (1, 2, 3)
    assert all(
        item.holdout_ref in result.authorized_holdout_refs
        for item in result.team_execution_runs
    )
    assert len({item.invocation_id for item in result.team_execution_runs}) == 3
    output_refs = tuple(
        item.execution_outcome.output_ref for item in result.team_execution_runs
    )
    assert all(reference is not None for reference in output_refs)
    assert len(set(output_refs)) == 3
    assert len(set(result.team_execution_transcript_digests)) == 3


@pytest.mark.asyncio
async def test_behavioral_retry_uses_improve_then_rebuild_and_promotes() -> None:
    result = await SixSkillFactoryHarness(first_run="behavioral_failure").run()

    assert result.skill_steps == RETRY_STEPS
    assert result.attempts == 2
    assert result.gateway_projection.status is FactoryLifecycleStatus.READY_TO_USE
    assert "improvement_requested" in result.gateway_phases
    assert tuple(receipt.attempt for receipt in result.usage_receipts) == (
        1,
        1,
        1,
        2,
        2,
        2,
    )


@pytest.mark.asyncio
async def test_required_tool_gap_blocks_promotion() -> None:
    result = await SixSkillFactoryHarness(tool_gap="required").run()

    assert result.feedback.recommendation is FactoryFeedbackRecommendation.BLOCKED_TOOL_REQUIRED
    assert result.gateway_projection.status is not FactoryLifecycleStatus.READY_TO_USE
    assert result.tool_gap_severities == ("required",)
    assert result.effect_counts["provider"] == 3


@pytest.mark.asyncio
async def test_optional_tool_gap_preserves_passing_assertions() -> None:
    result = await SixSkillFactoryHarness(tool_gap="optional").run()

    assert result.feedback.recommendation is FactoryFeedbackRecommendation.PROMOTE_CANDIDATE
    assert result.gateway_projection.status is FactoryLifecycleStatus.READY_TO_USE
    assert result.tool_gap_severities == ("optional",)
    assert set(result.feedback.assertion_ids) == {"schema_valid", "real_case_green"}


@pytest.mark.asyncio
async def test_credential_and_infrastructure_blocks_remain_distinct() -> None:
    credential = await SixSkillFactoryHarness(failure="credential_required").run()
    infrastructure = await SixSkillFactoryHarness(failure="infrastructure_failure").run()

    assert credential.feedback.recommendation is FactoryFeedbackRecommendation.BLOCKED_CREDENTIAL_REQUIRED
    assert infrastructure.feedback.recommendation is FactoryFeedbackRecommendation.BLOCKED_INFRASTRUCTURE
    credential_evaluation = next(
        item
        for item in credential.workflow_artifacts
        if isinstance(item, TeamEvaluationV1)
    )
    infrastructure_evaluation = next(
        item
        for item in infrastructure.workflow_artifacts
        if isinstance(item, TeamEvaluationV1)
    )
    assert credential_evaluation.failure_class == "credential_required"
    assert infrastructure_evaluation.failure_class == "infrastructure_failure"
    assert credential.attempts == infrastructure.attempts == 1


@pytest.mark.asyncio
async def test_budget_exhaustion_stops_before_second_paid_run() -> None:
    harness = SixSkillFactoryHarness(budget_usd=Decimal("0.25"))

    with pytest.raises(BudgetExhausted, match="budget"):
        await harness.run()

    assert harness.effect_counts["provider"] == 1
    assert harness.paid_cost_usd == Decimal("0.25")
    assert harness.coordinator.projection(harness.job.job_id).attempt == 1


@pytest.mark.asyncio
async def test_infrastructure_recovery_stays_on_same_attempt() -> None:
    result = await SixSkillFactoryHarness().run()

    assert result.attempts == 1
    assert result.infrastructure_recoveries == 1
    assert result.effect_counts["provider"] == 3
    assert result.gateway_projection.status is FactoryLifecycleStatus.READY_TO_USE


@pytest.mark.asyncio
async def test_changed_released_skill_digest_is_rejected_before_process() -> None:
    harness = SixSkillFactoryHarness(change_skill_digest=True)

    with pytest.raises(FactoryDispatchError, match="digest"):
        await harness.run()

    assert harness.process_calls == 0
    assert harness.effect_counts == {"codex": 0, "n8n": 0, "provider": 0}


@pytest.mark.asyncio
async def test_restart_after_reservation_and_execution_evidence_replays() -> None:
    result = await SixSkillFactoryHarness(
        restart_after_reservation=True,
        restart_after_evidence=True,
    ).run()

    assert result.recovered_reservations == 1
    assert result.replayed_evidence >= 1
    assert result.effect_counts["provider"] == 3
    assert len(result.runner_instance_ids) >= 2
    assert len(set(result.runner_instance_ids)) >= 2
    assert all(
        len(set(instance_ids)) >= 2
        for instance_ids in result.state_component_instance_ids.values()
    )
    assert result.reservation_recovered_after_restart is True
    assert result.effect_order.index("claim:provider") < result.effect_order.index(
        "start:provider"
    )
    assert result.gateway_projection.status is FactoryLifecycleStatus.READY_TO_USE


@pytest.mark.asyncio
async def test_idempotent_replay_does_not_duplicate_external_effects() -> None:
    harness = SixSkillFactoryHarness(with_n8n=True)
    first = await harness.run()
    replay = await harness.run()

    assert replay.gateway_projection == first.gateway_projection
    assert replay.effect_counts == {"codex": 1, "n8n": 3, "provider": 3}
    assert replay.effect_counts == first.effect_counts
    assert replay.replayed_evidence > first.replayed_evidence
    assert replay.effect_order.index("claim:codex") < replay.effect_order.index(
        "start:codex"
    )
    assert replay.effect_order.index("claim:provider") < replay.effect_order.index(
        "start:provider"
    )
    assert len(replay.n8n_execution_ids) == 3
    assert len(set(replay.n8n_execution_ids)) == 3
    assert replay.effect_order.index("n8n:claim") < replay.effect_order.index(
        "n8n:start"
    ) < replay.effect_order.index("n8n:evidence")


@pytest.mark.asyncio
async def test_demo_ready_never_claims_ready_to_use() -> None:
    result = await SixSkillFactoryHarness(mode="demo").run()

    assert result.live_status == "demo_ready"
    assert result.gateway_projection.status is not FactoryLifecycleStatus.READY_TO_USE
    assert result.captain_promoted is False


@pytest.mark.asyncio
async def test_only_captain_promotion_reaches_terminal_ready_state() -> None:
    result = await SixSkillFactoryHarness().run()

    assert result.feedback.recommendation is FactoryFeedbackRecommendation.PROMOTE_CANDIDATE
    assert result.pre_promotion_status == "running"
    assert result.worker_ready_claim_changed_projection is False
    assert "producer" in result.worker_promotion_error
    assert result.worker_projection_before == result.worker_projection_after
    assert result.gateway_projection.status is FactoryLifecycleStatus.READY_TO_USE
    assert result.coordinator_result.promotion_block is not None
    assert result.coordinator_result.promotion_block.producer == "captain"
