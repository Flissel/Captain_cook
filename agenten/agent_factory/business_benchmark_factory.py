"""Product composition for Captain's private business benchmark release gate."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from agenten.agent_factory.business_benchmark import (
    BusinessBenchmarkEvaluationBinding,
    BusinessBenchmarkEvaluator,
)
from agenten.agent_factory.business_benchmark_contracts import (
    BusinessBenchmarkCaseV1,
    BusinessBenchmarkPolicyV1,
    BusinessBenchmarkReceiptV1,
    BusinessBenchmarkRunReceiptV1,
    BusinessBenchmarkSummaryV1,
)
from agenten.agent_factory.business_benchmark_execution import (
    BenchmarkExecutionPolicyV1,
    BusinessBenchmarkExecutorPort,
    PairedBusinessBenchmarkCoordinator,
)
from agenten.agent_factory.business_benchmark_replay import (
    BusinessBenchmarkReplayStore,
)
from agenten.agent_factory.business_benchmark_store import (
    BusinessBenchmarkRepository,
)
from agenten.agent_factory.contracts import AgentFactoryJobV3
from agenten.agent_factory.execution_budget import FactoryBudgetProjection
from agenten.agent_factory.factory_feedback import FactoryFeedbackBuilder
from agenten.agent_factory.holdout_contracts import PrivateHoldoutRef
from agenten.agent_factory.skill_evaluation import ToolGapMarker
from agenten.agent_factory.skill_workflow_contracts import (
    FactoryFeedbackV1,
    FactorySkillInvocationV1,
    TeamEvaluationV1,
    TeamExecutionEvidenceV1,
)
from agenten.agent_factory.team_evaluation import TeamEvaluationService
from agenten.agent_runtime.contracts import ArtifactRef


class BusinessBenchmarkGatewayPort(Protocol):
    """Captain-only writes needed by this composition root."""

    def record_business_benchmark_summary(
        self, summary: BusinessBenchmarkSummaryV1
    ) -> bool: ...

    def record_workflow_artifact(
        self, artifact: TeamEvaluationV1 | FactoryFeedbackV1
    ) -> bool: ...


@dataclass(frozen=True)
class BusinessBenchmarkFactoryResult:
    """Redacted release evidence; private case bodies remain in the repository."""

    summary: BusinessBenchmarkSummaryV1
    evaluation: TeamEvaluationV1
    feedback: FactoryFeedbackV1
    candidate_receipts: tuple[BusinessBenchmarkRunReceiptV1, ...]
    baseline_receipts: tuple[BusinessBenchmarkRunReceiptV1, ...]
    case_receipts: tuple[BusinessBenchmarkReceiptV1, ...]


class BusinessBenchmarkFactoryComposition:
    """Connect existing benchmark/evaluation ports without becoming authority."""

    def __init__(
        self,
        *,
        private_repository: BusinessBenchmarkRepository,
        gateway_repository: BusinessBenchmarkGatewayPort,
        evaluator: BusinessBenchmarkEvaluator,
        team_evaluator: TeamEvaluationService,
        feedback_builder: FactoryFeedbackBuilder,
    ) -> None:
        self._private_repository = private_repository
        self._gateway_repository = gateway_repository
        self._evaluator = evaluator
        self._team_evaluator = team_evaluator
        self._feedback_builder = feedback_builder

    async def run(
        self,
        *,
        job: AgentFactoryJobV3,
        profile_id: str,
        suite_version: int,
        attempt: int,
        candidate_ref: ArtifactRef,
        executor: BusinessBenchmarkExecutorPort,
        replay_store: BusinessBenchmarkReplayStore,
        execution_policy_factory: Callable[
            [BusinessBenchmarkCaseV1], BenchmarkExecutionPolicyV1
        ],
        benchmark_policy: BusinessBenchmarkPolicyV1,
        evaluation_invocation: FactorySkillInvocationV1,
        report_invocation_factory: Callable[[TeamEvaluationV1], FactorySkillInvocationV1],
        technical_executions: tuple[TeamExecutionEvidenceV1, ...],
        budget_projection: FactoryBudgetProjection,
        tool_gaps: tuple[ToolGapMarker, ...] = (),
    ) -> BusinessBenchmarkFactoryResult:
        suite_ref = self._private_repository.suite_ref(profile_id, suite_version)
        suite = self._private_repository.private_suite(suite_ref)
        self._validate_release_bindings(
            job=job,
            profile_id=profile_id,
            suite_version=suite_version,
            suite_ref=suite_ref,
            attempt=attempt,
            candidate_ref=candidate_ref,
            evaluation_invocation=evaluation_invocation,
            technical_executions=technical_executions,
            budget_projection=budget_projection,
        )

        coordinator = PairedBusinessBenchmarkCoordinator(
            job_id=job.job_id,
            correlation_id=job.correlation_id,
            subject_version=job.subject_version,
            attempt=attempt,
            suite_id=suite.suite_id,
            executor=executor,
            replay_store=replay_store,
        )
        candidate_receipts: list[BusinessBenchmarkRunReceiptV1] = []
        baseline_receipts: list[BusinessBenchmarkRunReceiptV1] = []
        case_receipts: list[BusinessBenchmarkReceiptV1] = []
        for benchmark_case in suite.cases:
            execution_policy = execution_policy_factory(benchmark_case)
            candidate, baseline = await coordinator.run_case_pair(
                case=benchmark_case,
                suite_ref=suite_ref,
                candidate_ref=candidate_ref,
                execution_policy=execution_policy,
            )
            self._private_repository.record_run_receipt(candidate)
            self._private_repository.record_run_receipt(baseline)
            case_receipt = self._evaluator.evaluate_case(
                benchmark_case,
                candidate,
                baseline,
            )
            self._private_repository.record_case_receipt(case_receipt)
            candidate_receipts.append(candidate)
            baseline_receipts.append(baseline)
            case_receipts.append(case_receipt)

        summary = self._evaluator.summarize(
            suite,
            tuple(case_receipts),
            benchmark_policy,
            binding=BusinessBenchmarkEvaluationBinding(
                job_id=job.job_id,
                correlation_id=job.correlation_id,
                subject_version=job.subject_version,
                attempt=attempt,
                candidate_ref=candidate_ref,
                suite_ref=suite_ref,
            ),
        )
        # Ordering is authoritative: evaluation can resolve only an already
        # committed immutable summary artifact.
        self._private_repository.record_summary(summary)
        self._gateway_repository.record_business_benchmark_summary(summary)

        evaluation = self._team_evaluator.evaluate(
            evaluation_invocation,
            candidate_ref,
            technical_executions,
            benchmark_summary=summary,
            budget_projection=budget_projection,
        )
        feedback = self._feedback_builder.build(
            invocation=report_invocation_factory(evaluation),
            candidate_ref=candidate_ref,
            evaluation=evaluation,
            tool_gaps=tool_gaps,
            budget_projection=budget_projection,
        )
        self._gateway_repository.record_workflow_artifact(evaluation)
        self._gateway_repository.record_workflow_artifact(feedback)
        return BusinessBenchmarkFactoryResult(
            summary=summary,
            evaluation=evaluation,
            feedback=feedback,
            candidate_receipts=tuple(candidate_receipts),
            baseline_receipts=tuple(baseline_receipts),
            case_receipts=tuple(case_receipts),
        )

    @staticmethod
    def _validate_release_bindings(
        *,
        job: AgentFactoryJobV3,
        profile_id: str,
        suite_version: int,
        suite_ref: PrivateHoldoutRef,
        attempt: int,
        candidate_ref: ArtifactRef,
        evaluation_invocation: FactorySkillInvocationV1,
        technical_executions: tuple[TeamExecutionEvidenceV1, ...],
        budget_projection: FactoryBudgetProjection,
    ) -> None:
        if suite_version < 1 or not profile_id:
            raise ValueError("benchmark profile and suite version are required")
        if suite_ref not in job.private_holdout_refs:
            raise ValueError("benchmark suite is not released by the Captain job")
        if not technical_executions:
            raise ValueError("technical execution evidence is required")
        if (
            evaluation_invocation.job_id != job.job_id
            or evaluation_invocation.correlation_id != job.correlation_id
            or evaluation_invocation.subject_version != job.subject_version
            or evaluation_invocation.attempt != attempt
            or budget_projection.job_id != job.job_id
        ):
            raise ValueError("benchmark evaluation binding does not match Captain job")
        if any(
            execution.job_id != job.job_id
            or execution.correlation_id != job.correlation_id
            or execution.subject_version != job.subject_version
            or execution.attempt != attempt
            or execution.candidate_ref != candidate_ref
            for execution in technical_executions
        ):
            raise ValueError("technical execution binding does not match benchmark")


__all__ = [
    "BusinessBenchmarkFactoryComposition",
    "BusinessBenchmarkFactoryResult",
    "BusinessBenchmarkGatewayPort",
]
