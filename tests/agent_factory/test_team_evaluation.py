from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from agenten.agent_factory.business_benchmark_contracts import BusinessBenchmarkSummaryV1
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
from tests.agent_factory.test_business_benchmark_contracts import (
    case_metric_payload,
    summary as business_summary,
)


NOW = datetime(2026, 7, 21, 10, 2, tzinfo=timezone.utc)


def _candidate() -> ArtifactRef:
    return ArtifactRef.model_validate(artifact("candidate", "d" * 64))


def _invocation() -> FactorySkillInvocationV1:
    return FactorySkillInvocationV1.model_validate(invocation_payload("evaluate_team"))


def _execution(
    *,
    failed: bool = False,
    run_number: int = 1,
    termination_reason: str = "task_completed",
) -> TeamExecutionEvidenceV1:
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
            run_number=run_number,
            status="failed" if failed else "succeeded",
            termination_reason=termination_reason,
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


def _benchmark(
    *,
    failure: str | None = None,
    invocation: FactorySkillInvocationV1 | None = None,
    candidate: ArtifactRef | None = None,
) -> BusinessBenchmarkSummaryV1:
    bound_invocation = invocation or _invocation()
    overrides: dict[str, object] = {
        "job_id": str(bound_invocation.job_id),
        "correlation_id": str(bound_invocation.correlation_id),
        "subject_version": bound_invocation.subject_version,
        "attempt": bound_invocation.attempt,
        "candidate_ref": (candidate or _candidate()).model_dump(mode="json"),
    }
    if failure == "unsafe_tool_intent":
        overrides.update(
            disposition="failed",
            reason_codes=["unsafe_tool_intent"],
            unsafe_tool_uses=1,
            case_metrics=[
                case_metric_payload(
                    number,
                    **({"candidate_unsafe_tool_use": True} if number == 1 else {}),
                )
                for number in range(1, 16)
            ],
        )
    elif failure == "missing_receipt":
        overrides.update(
            disposition="failed",
            reason_codes=["missing_receipt"],
            case_metrics=[case_metric_payload(number) for number in range(1, 15)],
            missing_receipt_count=1,
        )
    return business_summary(**overrides)


def _invocation_for_attempt(attempt: int) -> FactorySkillInvocationV1:
    payload = invocation_payload("evaluate_team")
    payload["attempt"] = attempt
    payload["invocation_id"] = f"00000000-0000-0000-0000-{700 + attempt:012d}"
    payload["idempotency_key"] = str(attempt) * 64
    lease = payload["lease"]
    assert isinstance(lease, dict)
    lease["attempt"] = attempt
    return FactorySkillInvocationV1.model_validate(payload)


def _execution_for_attempt(
    attempt: int,
    candidate: ArtifactRef,
) -> TeamExecutionEvidenceV1:
    payload = execution_payload(candidate_ref=candidate.model_dump(mode="json"))
    payload["attempt"] = attempt
    payload["invocation_id"] = f"00000000-0000-0000-0000-{710 + attempt:012d}"
    invocation = payload["invocation"]
    assert isinstance(invocation, dict)
    invocation["attempt"] = attempt
    invocation["invocation_id"] = payload["invocation_id"]
    invocation["idempotency_key"] = str(attempt + 1) * 64
    lease = invocation["lease"]
    assert isinstance(lease, dict)
    lease["attempt"] = attempt
    return TeamExecutionEvidenceV1.model_validate(payload)


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
        benchmark_summary=_benchmark(),
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
            benchmark_summary=_benchmark(),
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
            benchmark_summary=_benchmark(),
            budget_projection=_budget(),
        )


def test_optional_judge_runs_only_after_deterministic_evidence_passes() -> None:
    judge = RecordingJudge()

    evaluation = TeamEvaluationService(clock=lambda: NOW, judge=judge).evaluate(
        _invocation(),
        _candidate(),
        _execution(),
        benchmark_summary=_benchmark(),
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
        benchmark_summary=_benchmark(),
        budget_projection=exhausted,
    )

    assert evaluation.failure_class == "budget_exhausted"
    assert evaluation.recommendation.value == "BUDGET_EXHAUSTED"


def test_multi_run_evaluation_keeps_an_early_failure_failed_and_repairable() -> None:
    evaluation = TeamEvaluationService(clock=lambda: NOW).evaluate(
        _invocation(),
        _candidate(),
        (
            _execution(failed=True, run_number=1),
            _execution(run_number=2),
        ),
        benchmark_summary=_benchmark(),
        budget_projection=_budget(),
    )

    outcomes = {
        outcome.assertion_id: outcome
        for outcome in evaluation.assertion_outcomes
    }
    assert outcomes["schema_valid"].status == "passed"
    assert outcomes["real_case_green"].status == "failed"
    assert evaluation.failure_class == "behavioral_failure"
    assert evaluation.recommendation.value == "RETRY_BUILD"
    assert evaluation.prior_green_regression_ids == ("schema_valid",)


@pytest.mark.parametrize(
    ("termination_reason", "failure_class", "recommendation"),
    (
        (
            "credential_required",
            "credential_required",
            "BLOCKED_CREDENTIAL_REQUIRED",
        ),
        (
            "infrastructure_failure",
            "infrastructure_failure",
            "BLOCKED_INFRASTRUCTURE",
        ),
    ),
)
def test_typed_execution_reason_codes_reach_exact_block_classification(
    termination_reason: str,
    failure_class: str,
    recommendation: str,
) -> None:
    evaluation = TeamEvaluationService(clock=lambda: NOW).evaluate(
        _invocation(),
        _candidate(),
        _execution(failed=True, termination_reason=termination_reason),
        benchmark_summary=_benchmark(),
        budget_projection=_budget(),
    )

    assert evaluation.failure_class == failure_class
    assert evaluation.recommendation.value == recommendation


def test_multi_run_evaluation_is_canonical_regardless_of_input_order() -> None:
    first = _execution(run_number=1)
    second = _execution(run_number=2)
    evaluator = TeamEvaluationService(clock=lambda: NOW)

    forward = evaluator.evaluate(
        _invocation(),
        _candidate(),
        (first, second),
        benchmark_summary=_benchmark(),
        budget_projection=_budget(),
    )
    reverse = evaluator.evaluate(
        _invocation(),
        _candidate(),
        (second, first),
        benchmark_summary=_benchmark(),
        budget_projection=_budget(),
    )

    assert reverse == forward


def test_team_evaluation_requires_authoritative_business_summary() -> None:
    with pytest.raises(ValueError, match="business benchmark"):
        TeamEvaluationService(clock=lambda: NOW).evaluate(
            _invocation(),
            _candidate(),
            _execution(),
            benchmark_summary=None,  # type: ignore[arg-type]
            budget_projection=_budget(),
        )


@pytest.mark.parametrize(
    "binding",
    ["job_id", "correlation_id", "subject_version", "attempt", "candidate_ref", "artifact_ref"],
)
def test_team_evaluation_requires_exact_business_summary_binding(binding: str) -> None:
    summary = _benchmark()
    updates: dict[str, object] = {
        "job_id": UUID("00000000-0000-0000-0000-000000000999"),
        "correlation_id": UUID("00000000-0000-0000-0000-000000000999"),
        "subject_version": 2,
        "attempt": 2,
        "candidate_ref": ArtifactRef.model_validate(artifact("other", "8" * 64)),
        "artifact_ref": ArtifactRef(
            uri="artifact://business-benchmark-summary/" + "9" * 64,
            sha256="9" * 64,
            media_type="application/json",
        ),
    }
    fabricated = summary.model_construct(**{**summary.__dict__, binding: updates[binding]})

    with pytest.raises(ValueError, match="business benchmark"):
        TeamEvaluationService(clock=lambda: NOW).evaluate(
            _invocation(),
            _candidate(),
            _execution(),
            benchmark_summary=fabricated,
            budget_projection=_budget(),
        )


def test_business_summary_fields_are_copied_without_fabricating_assertions() -> None:
    summary = _benchmark(failure="unsafe_tool_intent")

    evaluation = TeamEvaluationService(clock=lambda: NOW).evaluate(
        _invocation(),
        _candidate(),
        _execution(),
        benchmark_summary=summary,
        budget_projection=_budget(),
    )

    assert evaluation.benchmark_summary_ref == summary.artifact_ref
    assert summary.artifact_ref in evaluation.evidence_refs
    assert evaluation.benchmark_policy_id == summary.policy.policy_id
    assert evaluation.benchmark_disposition == "failed"
    assert evaluation.benchmark_reason_codes == ("unsafe_tool_intent",)
    assert evaluation.failed_benchmark_metric_ids == ("tool_safety",)
    assert evaluation.prior_green_benchmark_metric_ids == summary.passed_metric_ids
    assert tuple(item.assertion_id for item in evaluation.assertion_outcomes) == (
        "schema_valid",
        "real_case_green",
    )
    assert evaluation.failure_class == "behavioral_failure"
    assert evaluation.recommendation.value == "RETRY_BUILD"


def test_missing_benchmark_receipt_is_infrastructure_failure() -> None:
    evaluation = TeamEvaluationService(clock=lambda: NOW).evaluate(
        _invocation(),
        _candidate(),
        _execution(),
        benchmark_summary=_benchmark(failure="missing_receipt"),
        budget_projection=_budget(),
    )

    assert evaluation.failure_class == "infrastructure_failure"
    assert evaluation.recommendation.value == "BLOCKED_INFRASTRUCTURE"


@pytest.mark.parametrize(
    ("termination_reason", "failure_class", "recommendation"),
    (
        ("credential_required", "credential_required", "BLOCKED_CREDENTIAL_REQUIRED"),
        ("preflight_failed", "test_regression", "RETRY_BUILD"),
    ),
)
def test_execution_failure_precedence_survives_missing_benchmark_receipt(
    termination_reason: str,
    failure_class: str,
    recommendation: str,
) -> None:
    evaluation = TeamEvaluationService(clock=lambda: NOW).evaluate(
        _invocation(),
        _candidate(),
        _execution(failed=True, termination_reason=termination_reason),
        benchmark_summary=_benchmark(failure="missing_receipt"),
        budget_projection=_budget(),
    )

    assert evaluation.failure_class == failure_class
    assert evaluation.recommendation.value == recommendation


def test_prior_green_guards_require_the_immediately_preceding_attempt() -> None:
    first_invocation = _invocation_for_attempt(1)
    first_candidate = ArtifactRef.model_validate(artifact("candidate-one", "1" * 64))
    first_summary = _benchmark(
        invocation=first_invocation,
        candidate=first_candidate,
    )
    first = TeamEvaluationService(clock=lambda: NOW).evaluate(
        first_invocation,
        first_candidate,
        _execution_for_attempt(1, first_candidate),
        benchmark_summary=first_summary,
        budget_projection=_budget(),
    )
    third_invocation = _invocation_for_attempt(3)
    third_candidate = ArtifactRef.model_validate(artifact("candidate-three", "3" * 64))

    with pytest.raises(ValueError, match="immediately preceding"):
        TeamEvaluationService(clock=lambda: NOW).evaluate(
            third_invocation,
            third_candidate,
            _execution_for_attempt(3, third_candidate),
            benchmark_summary=_benchmark(
                invocation=third_invocation,
                candidate=third_candidate,
            ),
            budget_projection=_budget(),
            prior_evaluation=first,
            prior_benchmark_summary=first_summary,
        )


def test_prior_green_guards_require_authoritative_prior_summary_binding() -> None:
    first_invocation = _invocation_for_attempt(1)
    first_candidate = ArtifactRef.model_validate(artifact("candidate-one", "1" * 64))
    first_summary = _benchmark(
        invocation=first_invocation,
        candidate=first_candidate,
    )
    first = TeamEvaluationService(clock=lambda: NOW).evaluate(
        first_invocation,
        first_candidate,
        _execution_for_attempt(1, first_candidate),
        benchmark_summary=first_summary,
        budget_projection=_budget(),
    )
    second_invocation = _invocation_for_attempt(2)
    second_candidate = ArtifactRef.model_validate(artifact("candidate-two", "2" * 64))
    forged_prior = first.model_copy(update={"benchmark_policy_id": "forged-policy"})

    with pytest.raises(ValueError, match="prior business benchmark"):
        TeamEvaluationService(clock=lambda: NOW).evaluate(
            second_invocation,
            second_candidate,
            _execution_for_attempt(2, second_candidate),
            benchmark_summary=_benchmark(
                invocation=second_invocation,
                candidate=second_candidate,
            ),
            budget_projection=_budget(),
            prior_evaluation=forged_prior,
            prior_benchmark_summary=first_summary,
        )


def test_prior_green_guards_accept_authoritative_previous_different_candidate() -> None:
    first_invocation = _invocation_for_attempt(1)
    first_candidate = ArtifactRef.model_validate(artifact("candidate-one", "1" * 64))
    first_summary = _benchmark(
        invocation=first_invocation,
        candidate=first_candidate,
    )
    first = TeamEvaluationService(clock=lambda: NOW).evaluate(
        first_invocation,
        first_candidate,
        _execution_for_attempt(1, first_candidate),
        benchmark_summary=first_summary,
        budget_projection=_budget(),
    )
    second_invocation = _invocation_for_attempt(2)
    second_candidate = ArtifactRef.model_validate(artifact("candidate-two", "2" * 64))
    second_summary = _benchmark(
        invocation=second_invocation,
        candidate=second_candidate,
    )

    second = TeamEvaluationService(clock=lambda: NOW).evaluate(
        second_invocation,
        second_candidate,
        _execution_for_attempt(2, second_candidate),
        benchmark_summary=second_summary,
        budget_projection=_budget(),
        prior_evaluation=first,
        prior_benchmark_summary=first_summary,
    )

    assert second.prior_green_regression_ids == first.prior_green_regression_ids
    assert (
        second.prior_green_benchmark_metric_ids
        == first.prior_green_benchmark_metric_ids
    )


@pytest.mark.parametrize("missing_evidence", ["usage_receipt", "tool_evidence"])
def test_missing_execution_evidence_is_infrastructure_failure(
    missing_evidence: str,
) -> None:
    execution = _execution()
    if missing_evidence == "usage_receipt":
        execution = execution.model_copy(update={"usage_receipt_refs": ()})
    else:
        outcome = execution.execution_outcome.model_copy(
            update={"tool_versions": ("tool-v1",)}
        )
        execution = execution.model_copy(
            update={"execution_outcome": outcome, "tool_evidence_refs": ()}
        )

    evaluation = TeamEvaluationService(clock=lambda: NOW).evaluate(
        _invocation(),
        _candidate(),
        execution,
        benchmark_summary=_benchmark(),
        budget_projection=_budget(),
    )

    assert evaluation.failure_class == "infrastructure_failure"
    assert evaluation.recommendation.value == "BLOCKED_INFRASTRUCTURE"


def test_exhausted_budget_wins_before_business_benchmark_retry() -> None:
    exhausted = _budget().model_copy(
        update={"consumed_usd": _budget().limit_usd, "remaining_usd": 0}
    )

    evaluation = TeamEvaluationService(clock=lambda: NOW).evaluate(
        _invocation(),
        _candidate(),
        _execution(),
        benchmark_summary=_benchmark(failure="unsafe_tool_intent"),
        budget_projection=exhausted,
    )

    assert evaluation.failure_class == "budget_exhausted"
    assert evaluation.recommendation.value == "BUDGET_EXHAUSTED"
