from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agenten.agent_factory.execution_budget import FactoryBudgetProjection
from agenten.agent_factory.factory_feedback import FactoryFeedbackBuilder
from agenten.agent_factory.skill_evaluation import ToolGapMarker
from agenten.agent_factory.skill_workflow_contracts import (
    FactoryFeedbackRecommendation,
    FactorySkillInvocationV1,
    TeamEvaluationV1,
    TeamExecutionEvidenceV1,
)
from agenten.agent_factory.team_evaluation import TeamEvaluationService
from agenten.agent_runtime.contracts import ArtifactRef
from tests.agent_factory.test_skill_workflow_contracts import (
    artifact,
    execution_outcome_payload,
    execution_payload,
    invocation_payload,
    tool_gap_payload,
)
from tests.agent_factory.test_team_evaluation import _benchmark


NOW = datetime(2026, 7, 21, 10, 3, tzinfo=timezone.utc)


def _candidate() -> ArtifactRef:
    return ArtifactRef.model_validate(artifact("candidate", "d" * 64))


def _execution(
    *,
    failed: bool = False,
    termination_reason: str = "task_completed",
) -> TeamExecutionEvidenceV1:
    outcome = execution_outcome_payload(status="failed" if failed else "succeeded")
    if failed:
        outcomes = list(outcome["assertion_outcomes"])
        outcomes[1] = {**outcomes[1], "status": "failed"}
        outcome["assertion_outcomes"] = outcomes
    return TeamExecutionEvidenceV1.model_validate(
        execution_payload(
            execution_outcome=outcome,
            status="failed" if failed else "succeeded",
            termination_reason=termination_reason,
        )
    )


def _budget(*, exhausted: bool = False) -> FactoryBudgetProjection:
    return FactoryBudgetProjection(
        job_id=_evaluation_invocation().job_id,
        limit_usd="5.00",
        consumed_usd="5.00" if exhausted else "0.40",
        reserved_usd="0",
        remaining_usd="0" if exhausted else "4.60",
    )


def _evaluation_invocation() -> FactorySkillInvocationV1:
    return FactorySkillInvocationV1.model_validate(invocation_payload("evaluate_team"))


def _evaluation(
    *,
    failed: bool = False,
    benchmark_failure: str | None = None,
) -> TeamEvaluationV1:
    return TeamEvaluationService(clock=lambda: NOW).evaluate(
        _evaluation_invocation(),
        _candidate(),
        _execution(failed=failed),
        benchmark_summary=_benchmark(failure=benchmark_failure),
        budget_projection=_budget(),
    )


def _report_invocation(evaluation: TeamEvaluationV1) -> FactorySkillInvocationV1:
    return FactorySkillInvocationV1.model_validate(
        invocation_payload(
            "report_captain",
            invocation_id="00000000-0000-0000-0000-000000000308",
            idempotency_key="8" * 64,
            input_ref=evaluation.artifact_ref.model_dump(mode="json"),
            input_sha256=evaluation.artifact_ref.sha256,
        )
    )


def test_required_tool_gap_wins_while_optional_gap_remains_evidence() -> None:
    evaluation = _evaluation()
    required = ToolGapMarker.model_validate(tool_gap_payload())
    optional = ToolGapMarker.model_validate(tool_gap_payload(severity="optional")).model_copy(
        update={
            "gap_id": "optional-observability",
            "evidence_ref": ArtifactRef.model_validate(
                artifact("optional-tool-gap", "3" * 64)
            ),
        }
    )

    feedback = FactoryFeedbackBuilder(clock=lambda: NOW).build(
        invocation=_report_invocation(evaluation),
        candidate_ref=_candidate(),
        evaluation=evaluation,
        tool_gaps=(optional, required),
        budget_projection=_budget(),
    )

    assert feedback.recommendation is FactoryFeedbackRecommendation.BLOCKED_TOOL_REQUIRED
    assert feedback.reason_codes == ("required_tool_unresolved",)
    assert feedback.tool_gaps == (optional, required)


@pytest.mark.parametrize(
    ("failure_class", "expected"),
    [
        ("credential_required", FactoryFeedbackRecommendation.BLOCKED_CREDENTIAL_REQUIRED),
        ("infrastructure_failure", FactoryFeedbackRecommendation.BLOCKED_INFRASTRUCTURE),
        ("unresolved", FactoryFeedbackRecommendation.MANUAL_DECISION_REQUIRED),
    ],
)
def test_feedback_maps_redacted_failure_class_exactly(
    failure_class: str,
    expected: FactoryFeedbackRecommendation,
) -> None:
    evaluation = _evaluation(failed=True).model_copy(
        update={"failure_class": failure_class}
    )

    feedback = FactoryFeedbackBuilder(clock=lambda: NOW).build(
        invocation=_report_invocation(evaluation),
        candidate_ref=_candidate(),
        evaluation=evaluation,
        budget_projection=_budget(),
    )

    assert feedback.recommendation is expected
    serialized = feedback.model_dump_json(by_alias=True)
    assert "secret" not in serialized.lower()
    assert "holdout_body" not in serialized
    assert "raw_prompt" not in serialized


def test_budget_exhaustion_wins_before_retry() -> None:
    evaluation = _evaluation(failed=True)

    feedback = FactoryFeedbackBuilder(clock=lambda: NOW).build(
        invocation=_report_invocation(evaluation),
        candidate_ref=_candidate(),
        evaluation=evaluation,
        budget_projection=_budget(exhausted=True),
    )

    assert feedback.recommendation is FactoryFeedbackRecommendation.BUDGET_EXHAUSTED
    assert feedback.reason_codes == ("budget_exhausted",)


def test_promotion_is_only_a_recommendation_with_all_assertions_bound() -> None:
    evaluation = _evaluation()

    feedback = FactoryFeedbackBuilder(clock=lambda: NOW).build(
        invocation=_report_invocation(evaluation),
        candidate_ref=_candidate(),
        evaluation=evaluation,
        budget_projection=_budget(),
    )

    assert feedback.recommendation is FactoryFeedbackRecommendation.PROMOTE_CANDIDATE
    assert feedback.assertion_ids == evaluation.acceptance_assertion_ids
    assert evaluation.artifact_ref in feedback.evidence_refs
    assert _candidate() in feedback.evidence_refs


def test_feedback_rejects_a_candidate_not_bound_by_the_evaluation() -> None:
    evaluation = _evaluation()
    other = ArtifactRef.model_validate(artifact("other-candidate", "4" * 64))

    with pytest.raises(ValueError, match="candidate.*binding"):
        FactoryFeedbackBuilder(clock=lambda: NOW).build(
            invocation=_report_invocation(evaluation),
            candidate_ref=other,
            evaluation=evaluation,
            budget_projection=_budget(),
        )


def test_feedback_preserves_business_benchmark_reason_codes() -> None:
    evaluation = _evaluation(benchmark_failure="unsafe_tool_intent")

    feedback = FactoryFeedbackBuilder(clock=lambda: NOW).build(
        invocation=_report_invocation(evaluation),
        candidate_ref=_candidate(),
        evaluation=evaluation,
        budget_projection=_budget(),
    )

    assert feedback.recommendation is FactoryFeedbackRecommendation.RETRY_BUILD
    assert "unsafe_tool_intent" in feedback.reason_codes
    assert "candidate_retry_required" in feedback.reason_codes


@pytest.mark.parametrize(
    ("termination_reason", "recommendation"),
    (
        (
            "credential_required",
            FactoryFeedbackRecommendation.BLOCKED_CREDENTIAL_REQUIRED,
        ),
        ("preflight_failed", FactoryFeedbackRecommendation.RETRY_BUILD),
    ),
)
def test_feedback_preserves_execution_gate_over_missing_benchmark_receipt(
    termination_reason: str,
    recommendation: FactoryFeedbackRecommendation,
) -> None:
    evaluation = TeamEvaluationService(clock=lambda: NOW).evaluate(
        _evaluation_invocation(),
        _candidate(),
        _execution(failed=True, termination_reason=termination_reason),
        benchmark_summary=_benchmark(failure="missing_receipt"),
        budget_projection=_budget(),
    )

    feedback = FactoryFeedbackBuilder(clock=lambda: NOW).build(
        invocation=_report_invocation(evaluation),
        candidate_ref=_candidate(),
        evaluation=evaluation,
        budget_projection=_budget(),
    )

    assert feedback.recommendation is recommendation
    assert "missing_receipt" in feedback.reason_codes


def test_feedback_never_promotes_legacy_evaluation_without_benchmark() -> None:
    evaluation = _evaluation().model_copy(
        update={
            "benchmark_summary_ref": None,
            "benchmark_policy_id": None,
            "benchmark_disposition": None,
            "benchmark_reason_codes": (),
            "failed_benchmark_metric_ids": (),
        }
    )

    feedback = FactoryFeedbackBuilder(clock=lambda: NOW).build(
        invocation=_report_invocation(evaluation),
        candidate_ref=_candidate(),
        evaluation=evaluation,
        budget_projection=_budget(),
    )

    assert feedback.recommendation is FactoryFeedbackRecommendation.MANUAL_DECISION_REQUIRED
    assert feedback.reason_codes == ("business_benchmark_missing",)
