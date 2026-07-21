from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agenten.agent_factory.execution_budget import FactoryBudgetProjection
from agenten.agent_factory.skill_workflow_contracts import (
    FactorySkillInvocationV1,
    TeamExecutionEvidenceV1,
)
from agenten.agent_factory.team_evaluation import TeamEvaluationService
from agenten.agent_runtime.contracts import ArtifactRef
from tests.agent_factory.test_skill_workflow_contracts import (
    artifact,
    execution_outcome_payload,
    execution_payload,
    invocation_payload,
)


NOW = datetime(2026, 7, 21, 10, 2, tzinfo=timezone.utc)


def _candidate() -> ArtifactRef:
    return ArtifactRef.model_validate(artifact("candidate", "d" * 64))


def _invocation() -> FactorySkillInvocationV1:
    return FactorySkillInvocationV1.model_validate(invocation_payload("evaluate_team"))


def _execution(*, failed: bool = False) -> TeamExecutionEvidenceV1:
    outcome = execution_outcome_payload(status="failed" if failed else "succeeded")
    if failed:
        assertions = list(outcome["assertion_outcomes"])
        assertions[1] = {
            **assertions[1],
            "status": "failed",
        }
        outcome["assertion_outcomes"] = assertions
    return TeamExecutionEvidenceV1.model_validate(
        execution_payload(
            execution_outcome=outcome,
            status="failed" if failed else "succeeded",
        )
    )


def _budget() -> FactoryBudgetProjection:
    return FactoryBudgetProjection(
        job_id=_invocation().job_id,
        limit_usd="5.00",
        consumed_usd="0.40",
        reserved_usd="0",
        remaining_usd="4.60",
    )


class RecordingJudge:
    def __init__(self) -> None:
        self.calls: list[tuple[ArtifactRef, tuple[int, ...]]] = []

    def evaluate(
        self,
        *,
        invocation: FactorySkillInvocationV1,
        candidate_ref: ArtifactRef,
        executions: tuple[TeamExecutionEvidenceV1, ...],
    ) -> ArtifactRef:
        assert invocation.step.value == "evaluate_team"
        self.calls.append((candidate_ref, tuple(item.run_number for item in executions)))
        return ArtifactRef.model_validate(artifact("qualitative-judge", "9" * 64))


def test_deterministic_assertions_run_before_optional_model_judge() -> None:
    judge = RecordingJudge()

    evaluation = TeamEvaluationService(clock=lambda: NOW, judge=judge).evaluate(
        _invocation(),
        _candidate(),
        _execution(failed=True),
        budget_projection=_budget(),
    )

    assert evaluation.recommendation.value == "RETRY_BUILD"
    assert evaluation.failure_class == "behavioral_failure"
    assert judge.calls == []
    assert evaluation.judge_ref is None
    assert evaluation.prior_green_regression_ids == ("schema_valid",)


def test_evaluator_cannot_accept_unknown_or_missing_captain_assertions() -> None:
    execution = _execution()
    incomplete_outcome = execution.execution_outcome.model_copy(
        update={"assertion_outcomes": execution.execution_outcome.assertion_outcomes[:1]}
    )
    fabricated = execution.model_construct(
        **{
            **execution.__dict__,
            "execution_outcome": incomplete_outcome,
        }
    )

    with pytest.raises(ValueError, match="Captain assertions"):
        TeamEvaluationService(clock=lambda: NOW).evaluate(
            _invocation(),
            _candidate(),
            fabricated,
            budget_projection=_budget(),
        )


@pytest.mark.parametrize("binding", ["job", "correlation", "version", "attempt", "candidate"])
def test_evaluator_requires_exact_execution_and_candidate_bindings(binding: str) -> None:
    execution = _execution()
    updates: dict[str, object] = {}
    if binding == "job":
        updates["job_id"] = "00000000-0000-0000-0000-000000000999"
    elif binding == "correlation":
        updates["correlation_id"] = "00000000-0000-0000-0000-000000000999"
    elif binding == "version":
        updates["subject_version"] = 2
    elif binding == "attempt":
        updates["attempt"] = 2
    else:
        updates["candidate_ref"] = ArtifactRef.model_validate(
            artifact("other-candidate", "8" * 64)
        )
    fabricated = execution.model_construct(**{**execution.__dict__, **updates})

    with pytest.raises(ValueError, match="binding"):
        TeamEvaluationService(clock=lambda: NOW).evaluate(
            _invocation(),
            _candidate(),
            fabricated,
            budget_projection=_budget(),
        )


def test_optional_judge_runs_only_after_deterministic_evidence_passes() -> None:
    judge = RecordingJudge()

    evaluation = TeamEvaluationService(clock=lambda: NOW, judge=judge).evaluate(
        _invocation(),
        _candidate(),
        _execution(),
        budget_projection=_budget(),
    )

    assert evaluation.recommendation.value == "PROMOTE_CANDIDATE"
    assert evaluation.failure_class is None
    assert judge.calls == [(_candidate(), (1,))]
    assert evaluation.judge_ref == ArtifactRef.model_validate(
        artifact("qualitative-judge", "9" * 64)
    )
    assert evaluation.judge_ref in evaluation.evidence_refs


def test_exhausted_budget_wins_before_behavioral_retry() -> None:
    exhausted = _budget().model_copy(
        update={"consumed_usd": _budget().limit_usd, "remaining_usd": 0}
    )

    evaluation = TeamEvaluationService(clock=lambda: NOW).evaluate(
        _invocation(),
        _candidate(),
        _execution(failed=True),
        budget_projection=exhausted,
    )

    assert evaluation.failure_class == "budget_exhausted"
    assert evaluation.recommendation.value == "BUDGET_EXHAUSTED"
