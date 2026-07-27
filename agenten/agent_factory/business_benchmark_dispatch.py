"""Captain quality-dispatch adapter for the product business benchmark gate."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Protocol
from uuid import NAMESPACE_URL, uuid5

from agenten.agent_factory.business_benchmark_contracts import (
    BusinessBenchmarkCaseV1,
    BusinessBenchmarkPolicyV1,
)
from agenten.agent_factory.business_benchmark_execution import (
    BenchmarkExecutionPolicyV1,
    BusinessBenchmarkExecutorPort,
)
from agenten.agent_factory.business_benchmark_factory import (
    BusinessBenchmarkFactoryComposition,
)
from agenten.agent_factory.business_benchmark_replay import (
    BusinessBenchmarkReplayStore,
)
from agenten.agent_factory.contracts import (
    AgentFactoryJobV3,
    FactoryBlockStatus,
    FactoryEvidenceBlock,
    FactoryPhase,
    FactoryRole,
)
from agenten.agent_factory.execution_budget import FactoryBudgetProjection
from agenten.agent_factory.orchestration import (
    FactoryDispatch,
    FactoryDispatchError,
)
from agenten.agent_factory.skill_workflow_contracts import (
    FactorySkillInvocationV1,
    TeamEvaluationV1,
    TeamExecutionEvidenceV1,
)
from agenten.agent_factory.state_machine import FactoryActionKind
from agenten.agent_runtime.contracts import ArtifactRef


@dataclass(frozen=True)
class BusinessBenchmarkDispatchInputs:
    """Exact Captain-selected inputs for one quality-review attempt."""

    profile_id: str
    suite_version: int
    candidate_ref: ArtifactRef
    executor: BusinessBenchmarkExecutorPort
    replay_store: BusinessBenchmarkReplayStore
    execution_policy_factory: Callable[
        [BusinessBenchmarkCaseV1], BenchmarkExecutionPolicyV1
    ]
    benchmark_policy: BusinessBenchmarkPolicyV1
    evaluation_invocation: FactorySkillInvocationV1
    report_invocation_factory: Callable[
        [TeamEvaluationV1], FactorySkillInvocationV1
    ]
    technical_executions: tuple[TeamExecutionEvidenceV1, ...]
    budget_projection: FactoryBudgetProjection


class BusinessBenchmarkDispatchInputPort(Protocol):
    """Resolve already-authorized runtime inputs without inventing adapters."""

    def resolve(
        self, request: FactoryDispatch
    ) -> BusinessBenchmarkDispatchInputs | None: ...


class BusinessBenchmarkDispatchUnavailable(FactoryDispatchError):
    """The V3 business gate lacks a required production input."""


class BusinessBenchmarkDispatchService:
    """Run the gate and return the exact Quality Warden lifecycle block."""

    def __init__(
        self,
        *,
        composition: BusinessBenchmarkFactoryComposition,
        inputs: BusinessBenchmarkDispatchInputPort,
        clock: Callable[[], datetime],
    ) -> None:
        self._composition = composition
        self._inputs = inputs
        self._clock = clock

    async def dispatch(self, request: FactoryDispatch) -> FactoryEvidenceBlock:
        self._validate_request(request)
        resolved = self._inputs.resolve(request)
        if resolved is None:
            raise BusinessBenchmarkDispatchUnavailable(
                "business benchmark production inputs are unavailable"
            )
        assert request.lease is not None
        if resolved.evaluation_invocation.lease != request.lease:
            raise BusinessBenchmarkDispatchUnavailable(
                "business benchmark evaluation lease does not match dispatch"
            )

        result = await self._composition.run(
            job=request.job,
            profile_id=resolved.profile_id,
            suite_version=resolved.suite_version,
            attempt=request.action.attempt,
            candidate_ref=resolved.candidate_ref,
            executor=resolved.executor,
            replay_store=resolved.replay_store,
            execution_policy_factory=resolved.execution_policy_factory,
            benchmark_policy=resolved.benchmark_policy,
            evaluation_invocation=resolved.evaluation_invocation,
            report_invocation_factory=resolved.report_invocation_factory,
            technical_executions=resolved.technical_executions,
            budget_projection=resolved.budget_projection,
        )
        if result.feedback.invocation.lease != request.lease:
            raise BusinessBenchmarkDispatchUnavailable(
                "business benchmark feedback lease does not match dispatch"
            )
        occurred_at = self._utc_now()
        if not request.lease.issued_at <= occurred_at < request.lease.expires_at:
            raise BusinessBenchmarkDispatchUnavailable(
                "business benchmark quality evidence requires an active lease"
            )
        artifact_refs = (
            result.summary.artifact_ref,
            result.evaluation.artifact_ref,
            result.feedback.artifact_ref,
        )
        passed_assertions = tuple(
            outcome.assertion_id
            for outcome in result.evaluation.assertion_outcomes
            if outcome.status == "passed"
        )
        return FactoryEvidenceBlock(
            schema_name="captain.agent-factory-block.v1",
            event_id=uuid5(
                NAMESPACE_URL,
                "business-benchmark-quality|"
                f"{request.job.job_id}|{request.action.attempt}|"
                f"{result.summary.artifact_ref.sha256}",
            ),
            job_id=request.job.job_id,
            correlation_id=request.job.correlation_id,
            causation_id=request.job.event_id,
            occurred_at=occurred_at,
            producer="hermes",
            subject_version=request.job.subject_version,
            attempt=request.action.attempt,
            phase=FactoryPhase.QUALITY_REVIEWED,
            role=FactoryRole.QUALITY_WARDEN,
            status=FactoryBlockStatus.SUCCEEDED,
            artifact_refs=artifact_refs,
            evidence_refs=artifact_refs,
            assertion_ids=passed_assertions,
            lease_id=request.lease.lease_id,
        )

    @staticmethod
    def _validate_request(request: FactoryDispatch) -> None:
        if not isinstance(request.job, AgentFactoryJobV3):
            raise FactoryDispatchError("business benchmark dispatch requires a V3 job")
        if (
            request.action.kind is not FactoryActionKind.DISPATCH_QUALITY_WARDEN
            or request.role is not FactoryRole.QUALITY_WARDEN
            or request.lease is None
        ):
            raise FactoryDispatchError(
                "business benchmark dispatch requires the Quality Warden action"
            )

    def _utc_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise BusinessBenchmarkDispatchUnavailable(
                "business benchmark dispatch clock must be UTC"
            )
        return value


__all__ = [
    "BusinessBenchmarkDispatchInputPort",
    "BusinessBenchmarkDispatchInputs",
    "BusinessBenchmarkDispatchService",
    "BusinessBenchmarkDispatchUnavailable",
]
