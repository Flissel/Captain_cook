"""Injected production composition for one Captain-authorized benchmark team.

This module resolves immutable Gateway, CAS, private-suite, invocation, and
budget projections before creating any runtime dependency. Environment, DB,
provider, and n8n adapters are deliberately outside this composition root.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Protocol
from uuid import UUID

from agenten.agent_factory.business_benchmark_contracts import (
    BusinessBenchmarkCaseV1,
    BusinessBenchmarkPolicyV1,
    BusinessBenchmarkRunReceiptV1,
    BusinessBenchmarkSuiteV1,
    BusinessBenchmarkSummaryV1,
    canonical_business_benchmark_model_bytes,
)
from agenten.agent_factory.business_benchmark_execution import (
    BenchmarkExecutionPolicyV1,
    BusinessBenchmarkExecutorPort,
)
from agenten.agent_factory.business_benchmark_factory import BusinessBenchmarkFactoryResult
from agenten.agent_factory.business_benchmark_live import (
    BusinessBenchmarkExpectedCaseV1,
    BusinessBenchmarkExpectedScopeV1,
    BusinessBenchmarkExpectedSuiteV1,
    BusinessBenchmarkFinalizedReceiptV1,
    BusinessBenchmarkLiveRunResultV1,
    BusinessBenchmarkTeamSelectionV1,
    LiveBusinessBenchmarkSettings,
)
from agenten.agent_factory.business_benchmark_replay import (
    BusinessBenchmarkReplayStore,
)
from agenten.agent_factory.candidate_evaluation import ResolvedFactoryCandidate
from agenten.agent_factory.contracts import AgentFactoryJobV3
from agenten.agent_factory.execution_budget import FactoryBudgetProjection
from agenten.agent_factory.execution_policy import FactoryLiveCapability
from agenten.agent_factory.holdout_contracts import PrivateHoldoutRef
from agenten.agent_factory.skill_workflow_contracts import (
    FactorySkillInvocationV1,
    FactorySkillStep,
    TeamEvaluationV1,
    TeamExecutionEvidenceV1,
)
from agenten.agent_runtime.contracts import ArtifactRef


_PROFILE_IDS = {
    "claims": "insurance_claims_resolution_swarm",
    "renewal": "customer_renewal_orchestration_team",
}


class BusinessBenchmarkProductionScopeError(ValueError):
    """Captain production evidence is missing, stale, or inconsistently bound."""


class BusinessBenchmarkProductionGatewayPort(Protocol):
    def factory_job(self, job_id: UUID) -> object | None: ...

    def team_execution_evidence(
        self, job_id: UUID, attempt: int
    ) -> tuple[TeamExecutionEvidenceV1, ...]: ...

    def budget_projection(self, job_id: UUID) -> FactoryBudgetProjection | None: ...


class BusinessBenchmarkCanonicalSuiteAuthorityPort(Protocol):
    def canonical_suite(
        self, *, profile_id: str, suite_version: int
    ) -> tuple[PrivateHoldoutRef, BusinessBenchmarkSuiteV1]: ...


class BusinessBenchmarkCandidateAuthorityPort(Protocol):
    def resolve_for_job(
        self, *, job: AgentFactoryJobV3, candidate_id: str
    ) -> ResolvedFactoryCandidate: ...


class BusinessBenchmarkInvocationAuthorityPort(Protocol):
    def runtime_invocation(
        self, *, job: AgentFactoryJobV3, attempt: int
    ) -> FactorySkillInvocationV1: ...

    def evaluation_invocation(
        self, *, job: AgentFactoryJobV3, attempt: int
    ) -> FactorySkillInvocationV1: ...

    def require_active_report(
        self, *, job: AgentFactoryJobV3, attempt: int
    ) -> None: ...

    def report_invocation(
        self,
        *,
        job: AgentFactoryJobV3,
        attempt: int,
        evaluation: TeamEvaluationV1,
    ) -> FactorySkillInvocationV1: ...


class BusinessBenchmarkReceiptFinalizerPort(Protocol):
    def finalize(
        self,
        *,
        profile: str,
        receipt: BusinessBenchmarkRunReceiptV1,
    ) -> BusinessBenchmarkFinalizedReceiptV1: ...


class BusinessBenchmarkFactoryCompositionPort(Protocol):
    async def run(self, **kwargs: object) -> BusinessBenchmarkFactoryResult: ...


@dataclass(frozen=True)
class ProductionBusinessBenchmarkScope:
    """Resolved private execution material plus its public-safe expected scope."""

    selection: BusinessBenchmarkTeamSelectionV1
    job: AgentFactoryJobV3
    profile_id: str
    suite_ref: PrivateHoldoutRef
    suite_id: str
    candidate: ResolvedFactoryCandidate
    candidate_ref: ArtifactRef
    runtime_invocation: FactorySkillInvocationV1
    evaluation_invocation: FactorySkillInvocationV1
    technical_executions: tuple[TeamExecutionEvidenceV1, ...]
    budget_projection: FactoryBudgetProjection
    expected_scope: BusinessBenchmarkExpectedScopeV1


class ProductionBusinessBenchmarkScopeResolver:
    """Resolve one benchmark scope exclusively from injected Captain authorities."""

    def __init__(
        self,
        *,
        gateway: BusinessBenchmarkProductionGatewayPort,
        suites: BusinessBenchmarkCanonicalSuiteAuthorityPort,
        candidates: BusinessBenchmarkCandidateAuthorityPort,
        invocations: BusinessBenchmarkInvocationAuthorityPort,
    ) -> None:
        self._gateway = gateway
        self._suites = suites
        self._candidates = candidates
        self._invocations = invocations

    def resolve(
        self, settings: LiveBusinessBenchmarkSettings
    ) -> ProductionBusinessBenchmarkScope:
        try:
            canonical_settings = LiveBusinessBenchmarkSettings.model_validate(
                settings.model_dump(mode="python")
            )
            if canonical_settings.profile == "all" or len(canonical_settings.selections) != 1:
                raise ValueError("production benchmark scope requires one single team")
            selection = canonical_settings.selections[0]
            job = self._gateway.factory_job(selection.job_id)
            if not isinstance(job, AgentFactoryJobV3):
                raise ValueError("exact V3 Gateway job is unavailable")
            job = AgentFactoryJobV3.model_validate(
                job.model_dump(mode="json", by_alias=True)
            )
            self._require_job_policy(job, canonical_settings, selection)

            profile_id = _PROFILE_IDS[selection.profile]
            suite_ref, private_suite = self._suites.canonical_suite(
                profile_id=profile_id,
                suite_version=selection.suite_version,
            )
            self._require_suite(
                job=job,
                profile_id=profile_id,
                selection=selection,
                suite_ref=suite_ref,
                private_suite=private_suite,
            )

            candidate = self._candidates.resolve_for_job(
                job=job,
                candidate_id=selection.candidate_id,
            )
            if not isinstance(candidate, ResolvedFactoryCandidate):
                raise ValueError("candidate authority returned an invalid candidate")
            if candidate.candidate.candidate_id != selection.candidate_id:
                raise ValueError("candidate authority returned a different candidate")
            candidate_ref = candidate.candidate.source_archive_ref

            runtime_invocation = self._invocations.runtime_invocation(
                job=job,
                attempt=selection.attempt,
            )
            evaluation_invocation = self._invocations.evaluation_invocation(
                job=job,
                attempt=selection.attempt,
            )
            self._require_invocation(
                runtime_invocation,
                job=job,
                attempt=selection.attempt,
                step=FactorySkillStep.EXECUTE_TEAM,
                suite_ref=suite_ref,
            )
            self._require_invocation(
                evaluation_invocation,
                job=job,
                attempt=selection.attempt,
                step=FactorySkillStep.EVALUATE_TEAM,
            )
            self._invocations.require_active_report(
                job=job,
                attempt=selection.attempt,
            )

            technical_executions = self._gateway.team_execution_evidence(
                job.job_id,
                selection.attempt,
            )
            self._require_technical_executions(
                technical_executions,
                job=job,
                attempt=selection.attempt,
                candidate_ref=candidate_ref,
                suite_ref=suite_ref,
                runtime_invocation=runtime_invocation,
            )
            budget = self._gateway.budget_projection(job.job_id)
            self._require_budget(
                budget,
                job=job,
                selection=selection,
            )
            assert budget is not None

            expected_suite = BusinessBenchmarkExpectedSuiteV1(
                profile=selection.profile,
                suite_id=private_suite.suite_id,
                suite_version=private_suite.suite_version,
                suite_ref=suite_ref,
                cases=tuple(
                    BusinessBenchmarkExpectedCaseV1(
                        case_id=benchmark_case.case_id,
                        case_sha256=hashlib.sha256(
                            canonical_business_benchmark_model_bytes(benchmark_case)
                        ).hexdigest(),
                    )
                    for benchmark_case in private_suite.cases
                ),
            )
            expected_scope = BusinessBenchmarkExpectedScopeV1(
                job_id=job.job_id,
                correlation_id=job.correlation_id,
                subject_version=job.subject_version,
                attempt=selection.attempt,
                model_version=canonical_settings.model,
                candidate_id=selection.candidate_id,
                candidate_ref=candidate_ref,
                suites=(expected_suite,),
            )
            return ProductionBusinessBenchmarkScope(
                selection=selection,
                job=job,
                profile_id=profile_id,
                suite_ref=suite_ref,
                suite_id=private_suite.suite_id,
                candidate=candidate,
                candidate_ref=candidate_ref,
                runtime_invocation=runtime_invocation,
                evaluation_invocation=evaluation_invocation,
                technical_executions=technical_executions,
                budget_projection=budget,
                expected_scope=expected_scope,
            )
        except BusinessBenchmarkProductionScopeError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise BusinessBenchmarkProductionScopeError(
                f"business benchmark production scope is invalid: {exc}"
            ) from exc

    @staticmethod
    def _require_job_policy(
        job: AgentFactoryJobV3,
        settings: LiveBusinessBenchmarkSettings,
        selection: BusinessBenchmarkTeamSelectionV1,
    ) -> None:
        policy = job.execution_policy
        if (
            job.job_id != selection.job_id
            or not policy.live_execution
            or FactoryLiveCapability.MODEL_INVOKE not in policy.live_capabilities
            or settings.model not in policy.allowed_models
            or settings.model not in settings.allowed_models
            or tuple(settings.allowed_models) != tuple(policy.allowed_models)
            or selection.maximum_usd > policy.max_cost_usd
        ):
            raise ValueError("job, model, live policy, or benchmark budget is stale")

    @staticmethod
    def _require_suite(
        *,
        job: AgentFactoryJobV3,
        profile_id: str,
        selection: BusinessBenchmarkTeamSelectionV1,
        suite_ref: PrivateHoldoutRef,
        private_suite: BusinessBenchmarkSuiteV1,
    ) -> None:
        if not isinstance(suite_ref, PrivateHoldoutRef) or not isinstance(
            private_suite, BusinessBenchmarkSuiteV1
        ):
            raise ValueError("canonical private suite authority returned invalid data")
        canonical_suite = BusinessBenchmarkSuiteV1.model_validate(
            private_suite.model_dump(mode="json", by_alias=True)
        )
        if (
            suite_ref not in job.private_holdout_refs
            or canonical_suite.profile_id != profile_id
            or canonical_suite.suite_version != selection.suite_version
        ):
            raise ValueError("canonical private suite is not authorized by the job")

    @staticmethod
    def _require_invocation(
        invocation: FactorySkillInvocationV1,
        *,
        job: AgentFactoryJobV3,
        attempt: int,
        step: FactorySkillStep,
        suite_ref: PrivateHoldoutRef | None = None,
    ) -> None:
        if not isinstance(invocation, FactorySkillInvocationV1):
            raise ValueError("active Factory invocation is unavailable")
        if (
            invocation.job_id != job.job_id
            or invocation.correlation_id != job.correlation_id
            or invocation.subject_version != job.subject_version
            or invocation.attempt != attempt
            or invocation.step is not step
            or invocation.acceptance_assertion_ids != job.acceptance_assertion_ids
            or (suite_ref is not None and invocation.execution_scope_ref != suite_ref)
        ):
            raise ValueError("active Factory invocation is stale or mixed")

    @staticmethod
    def _require_technical_executions(
        executions: tuple[TeamExecutionEvidenceV1, ...],
        *,
        job: AgentFactoryJobV3,
        attempt: int,
        candidate_ref: ArtifactRef,
        suite_ref: PrivateHoldoutRef,
        runtime_invocation: FactorySkillInvocationV1,
    ) -> None:
        if not executions or len({item.run_number for item in executions}) != len(
            executions
        ):
            raise ValueError("required team execution evidence is missing or duplicated")
        if any(
            not isinstance(item, TeamExecutionEvidenceV1)
            or item.job_id != job.job_id
            or item.correlation_id != job.correlation_id
            or item.subject_version != job.subject_version
            or item.attempt != attempt
            or item.candidate_ref != candidate_ref
            or item.holdout_ref != suite_ref
            or item.invocation_id != runtime_invocation.invocation_id
            or item.status != "succeeded"
            for item in executions
        ):
            raise ValueError("team execution evidence is stale or mixed")

    @staticmethod
    def _require_budget(
        budget: FactoryBudgetProjection | None,
        *,
        job: AgentFactoryJobV3,
        selection: BusinessBenchmarkTeamSelectionV1,
    ) -> None:
        if (
            not isinstance(budget, FactoryBudgetProjection)
            or budget.job_id != job.job_id
            or budget.limit_usd != job.execution_policy.max_cost_usd
            or budget.remaining_usd != selection.captain_remaining_usd
            or selection.maximum_usd > budget.remaining_usd
        ):
            raise ValueError("Gateway budget projection is missing or stale")


class BusinessBenchmarkExecutorFactoryPort(Protocol):
    def __call__(
        self, scope: ProductionBusinessBenchmarkScope
    ) -> BusinessBenchmarkExecutorPort: ...


class BusinessBenchmarkReplayStoreFactoryPort(Protocol):
    def __call__(
        self, scope: ProductionBusinessBenchmarkScope
    ) -> BusinessBenchmarkReplayStore: ...


class BusinessBenchmarkCasePolicyPort(Protocol):
    def __call__(
        self, benchmark_case: BusinessBenchmarkCaseV1
    ) -> BenchmarkExecutionPolicyV1: ...


class BusinessBenchmarkExecutionPolicyFactoryPort(Protocol):
    def __call__(
        self, scope: ProductionBusinessBenchmarkScope
    ) -> BusinessBenchmarkCasePolicyPort: ...


class BusinessBenchmarkPolicyFactoryPort(Protocol):
    def __call__(
        self, scope: ProductionBusinessBenchmarkScope
    ) -> BusinessBenchmarkPolicyV1: ...


class BusinessBenchmarkClockPort(Protocol):
    def __call__(self) -> datetime: ...


class ProductionBusinessBenchmarkComposition:
    """Execute exactly one already-resolved production benchmark team."""

    def __init__(
        self,
        *,
        resolver: ProductionBusinessBenchmarkScopeResolver,
        factory_composition: BusinessBenchmarkFactoryCompositionPort,
        invocation_authority: BusinessBenchmarkInvocationAuthorityPort,
        executor_factory: BusinessBenchmarkExecutorFactoryPort,
        replay_store_factory: BusinessBenchmarkReplayStoreFactoryPort,
        execution_policy_factory: BusinessBenchmarkExecutionPolicyFactoryPort,
        benchmark_policy_factory: BusinessBenchmarkPolicyFactoryPort,
        receipt_finalizer: BusinessBenchmarkReceiptFinalizerPort,
        clock: BusinessBenchmarkClockPort,
    ) -> None:
        self._resolver = resolver
        self._factory_composition = factory_composition
        self._invocation_authority = invocation_authority
        self._executor_factory = executor_factory
        self._replay_store_factory = replay_store_factory
        self._execution_policy_factory = execution_policy_factory
        self._benchmark_policy_factory = benchmark_policy_factory
        self._receipt_finalizer = receipt_finalizer
        self._clock = clock
        self._scope_lock = Lock()
        self._scopes: dict[
            tuple[str, UUID, int], BusinessBenchmarkExpectedScopeV1
        ] = {}

    @property
    def expected_scopes(self) -> tuple[BusinessBenchmarkExpectedScopeV1, ...]:
        with self._scope_lock:
            return tuple(self._scopes.values())

    def prepare_scopes(
        self, settings: LiveBusinessBenchmarkSettings
    ) -> tuple[BusinessBenchmarkExpectedScopeV1, ...]:
        """Resolve every selected authority scope before health or provider effects."""

        canonical = LiveBusinessBenchmarkSettings.model_validate(
            settings.model_dump(mode="python")
        )
        resolved = tuple(
            self._resolver.resolve(canonical.for_selection(selection))
            for selection in canonical.selections
        )
        for scope in resolved:
            self._remember_scope(scope)
        return tuple(scope.expected_scope for scope in resolved)

    async def run(
        self, settings: LiveBusinessBenchmarkSettings
    ) -> BusinessBenchmarkLiveRunResultV1:
        if settings.profile == "all" or len(settings.selections) != 1:
            raise BusinessBenchmarkProductionScopeError(
                "production composition requires single team settings"
            )
        scope = self._resolver.resolve(settings)
        self._remember_scope(scope)

        executor = self._executor_factory(scope)
        replay_store = self._replay_store_factory(scope)
        case_policy_factory = self._execution_policy_factory(scope)
        benchmark_policy = self._benchmark_policy_factory(scope)
        if not isinstance(benchmark_policy, BusinessBenchmarkPolicyV1):
            raise BusinessBenchmarkProductionScopeError(
                "benchmark policy factory returned an invalid policy"
            )

        result = await self._factory_composition.run(
            job=scope.job,
            profile_id=scope.profile_id,
            suite_version=scope.selection.suite_version,
            attempt=scope.selection.attempt,
            candidate_ref=scope.candidate_ref,
            executor=executor,
            replay_store=replay_store,
            execution_policy_factory=case_policy_factory,
            benchmark_policy=benchmark_policy,
            evaluation_invocation=scope.evaluation_invocation,
            report_invocation_factory=lambda evaluation: self._report_invocation(
                scope, evaluation
            ),
            technical_executions=scope.technical_executions,
            budget_projection=scope.budget_projection,
        )
        self._require_factory_result(scope, result)

        finalized = tuple(
            self._finalize(scope, receipt)
            for receipt in (*result.candidate_receipts, *result.baseline_receipts)
        )
        evidence_refs = _unique_refs(
            (
                *(item.receipt_ref for item in finalized),
                *(
                    reference
                    for item in finalized
                    for reference in item.receipt.evidence_refs
                ),
                result.summary.artifact_ref,
                result.evaluation.artifact_ref,
                result.feedback.artifact_ref,
                *(item.artifact_ref for item in scope.technical_executions),
                *(
                    reference
                    for item in scope.technical_executions
                    for reference in item.evidence_refs
                ),
            )
        )
        completed_at = self._clock()
        if (
            completed_at.tzinfo is None
            or completed_at.utcoffset() != timezone.utc.utcoffset(completed_at)
        ):
            raise BusinessBenchmarkProductionScopeError(
                "production benchmark clock must be UTC"
            )
        return BusinessBenchmarkLiveRunResultV1(
            profile=scope.selection.profile,
            selections=(scope.selection,),
            receipts=finalized,
            summary_refs=(result.summary.artifact_ref,),
            evidence_refs=evidence_refs,
            completed_at=completed_at,
        )

    def _remember_scope(self, scope: ProductionBusinessBenchmarkScope) -> None:
        key = (
            scope.selection.profile,
            scope.job.job_id,
            scope.selection.attempt,
        )
        with self._scope_lock:
            existing = self._scopes.get(key)
            if existing is not None and existing != scope.expected_scope:
                raise BusinessBenchmarkProductionScopeError(
                    "production benchmark scope changed for the same team attempt"
                )
            self._scopes[key] = scope.expected_scope

    def _report_invocation(
        self,
        scope: ProductionBusinessBenchmarkScope,
        evaluation: TeamEvaluationV1,
    ) -> FactorySkillInvocationV1:
        invocation = self._invocation_authority.report_invocation(
            job=scope.job,
            attempt=scope.selection.attempt,
            evaluation=evaluation,
        )
        ProductionBusinessBenchmarkScopeResolver._require_invocation(
            invocation,
            job=scope.job,
            attempt=scope.selection.attempt,
            step=FactorySkillStep.REPORT_CAPTAIN,
        )
        if invocation.input_ref != evaluation.artifact_ref:
            raise BusinessBenchmarkProductionScopeError(
                "report invocation is not bound to the evaluation artifact"
            )
        return invocation

    def _finalize(
        self,
        scope: ProductionBusinessBenchmarkScope,
        receipt: BusinessBenchmarkRunReceiptV1,
    ) -> BusinessBenchmarkFinalizedReceiptV1:
        finalized = self._receipt_finalizer.finalize(
            profile=scope.selection.profile,
            receipt=receipt,
        )
        if not isinstance(finalized, BusinessBenchmarkFinalizedReceiptV1) or (
            finalized.profile != scope.selection.profile
            or finalized.receipt != receipt
        ):
            raise BusinessBenchmarkProductionScopeError(
                "receipt finalizer returned mixed benchmark evidence"
            )
        return finalized

    @staticmethod
    def _require_factory_result(
        scope: ProductionBusinessBenchmarkScope,
        result: BusinessBenchmarkFactoryResult,
    ) -> None:
        candidate_receipts = tuple(result.candidate_receipts)
        baseline_receipts = tuple(result.baseline_receipts)
        if len(candidate_receipts) != 15 or len(baseline_receipts) != 15:
            raise BusinessBenchmarkProductionScopeError(
                "production benchmark requires exactly 30 run receipts"
            )
        expected_cases = {
            item.case_id: item.case_sha256
            for item in scope.expected_scope.suites[0].cases
        }
        all_receipts = (*candidate_receipts, *baseline_receipts)
        if len({item.run_id for item in all_receipts}) != 30:
            raise BusinessBenchmarkProductionScopeError(
                "production benchmark receipt IDs are duplicated"
            )
        for expected_variant, receipts in (
            ("candidate", candidate_receipts),
            ("single_agent_baseline", baseline_receipts),
        ):
            if {item.case_id for item in receipts} != set(expected_cases):
                raise BusinessBenchmarkProductionScopeError(
                    "production benchmark receipt case coverage is mixed"
                )
            if any(
                item.variant != expected_variant
                or item.job_id != scope.job.job_id
                or item.correlation_id != scope.job.correlation_id
                or item.subject_version != scope.job.subject_version
                or item.attempt != scope.selection.attempt
                or item.suite_ref != scope.suite_ref
                or item.suite_id != scope.suite_id
                or item.case_sha256 != expected_cases[item.case_id]
                or item.model_version != scope.expected_scope.model_version
                or item.status != "succeeded"
                or (
                    item.candidate_ref != scope.candidate_ref
                    if expected_variant == "candidate"
                    else item.candidate_ref is not None
                )
                for item in receipts
            ):
                raise BusinessBenchmarkProductionScopeError(
                    "production benchmark receipt binding is stale or mixed"
                )
        summary = result.summary
        if not isinstance(summary, BusinessBenchmarkSummaryV1) or (
            summary.job_id != scope.job.job_id
            or summary.correlation_id != scope.job.correlation_id
            or summary.subject_version != scope.job.subject_version
            or summary.attempt != scope.selection.attempt
            or summary.candidate_ref != scope.candidate_ref
            or summary.suite_ref != scope.suite_ref
            or summary.suite_id != scope.suite_id
        ):
            raise BusinessBenchmarkProductionScopeError(
                "production benchmark summary is stale or mixed"
            )


def _unique_refs(references: tuple[ArtifactRef, ...]) -> tuple[ArtifactRef, ...]:
    unique: dict[tuple[str, str, str], ArtifactRef] = {}
    for reference in references:
        unique.setdefault(
            (reference.uri, reference.sha256, reference.media_type),
            reference,
        )
    return tuple(unique.values())


__all__ = [
    "BusinessBenchmarkCandidateAuthorityPort",
    "BusinessBenchmarkCasePolicyPort",
    "BusinessBenchmarkCanonicalSuiteAuthorityPort",
    "BusinessBenchmarkClockPort",
    "BusinessBenchmarkExecutionPolicyFactoryPort",
    "BusinessBenchmarkExecutorFactoryPort",
    "BusinessBenchmarkInvocationAuthorityPort",
    "BusinessBenchmarkPolicyFactoryPort",
    "BusinessBenchmarkProductionGatewayPort",
    "BusinessBenchmarkProductionScopeError",
    "BusinessBenchmarkReceiptFinalizerPort",
    "BusinessBenchmarkReplayStoreFactoryPort",
    "ProductionBusinessBenchmarkComposition",
    "ProductionBusinessBenchmarkScope",
    "ProductionBusinessBenchmarkScopeResolver",
]
