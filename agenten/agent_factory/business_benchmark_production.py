"""Injected production composition for one Captain-authorized benchmark team.

This module resolves immutable Gateway, CAS, private-suite, invocation, and
budget projections before creating any runtime dependency. Environment, DB,
provider, and n8n adapters are deliberately outside this composition root.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agenten.agent_factory.business_benchmark_contracts import (
    BusinessBenchmarkCaseV1,
    BusinessBenchmarkPolicyV1,
    BusinessBenchmarkReceiptV1,
    BusinessBenchmarkRunReceiptV1,
    BusinessBenchmarkSuiteV1,
    BusinessBenchmarkSummaryV1,
    canonical_business_benchmark_model_bytes,
)
from agenten.agent_factory.business_benchmark_execution import (
    BenchmarkExecutionPolicyV1,
    BusinessBenchmarkExecutorPort,
    PairedBusinessBenchmarkCoordinator,
)
from agenten.agent_factory.business_benchmark_dispatch import (
    BusinessBenchmarkDispatchInputs,
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
from agenten.agent_factory.contracts import AgentFactoryJobV3, FactoryRole
from agenten.agent_factory.execution_budget import FactoryBudgetProjection
from agenten.agent_factory.execution_policy import FactoryLiveCapability
from agenten.agent_factory.holdout_contracts import PrivateHoldoutRef
from agenten.agent_factory.orchestration import FactoryDispatch
from agenten.agent_factory.state_machine import FactoryActionKind
from agenten.agent_factory.skill_workflow_contracts import (
    FactorySkillInvocationV1,
    FactorySkillStep,
    FactoryFeedbackV1,
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


class CaptainBusinessBenchmarkPolicyBindingV1(BaseModel):
    """Digest-bound Captain policy for one exact Factory job attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_name: str = Field(
        default="captain.business-benchmark-policy-binding.v1",
        alias="schema",
        serialization_alias="schema",
        pattern=r"^captain\.business-benchmark-policy-binding\.v1$",
    )
    job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    attempt: int = Field(ge=1, le=5, strict=True)
    policy: BusinessBenchmarkPolicyV1
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def require_exact_policy_digest(self) -> "CaptainBusinessBenchmarkPolicyBindingV1":
        if self.policy_sha256 != _model_sha256(self.policy):
            raise ValueError("Captain benchmark policy digest changed")
        return self

    @classmethod
    def create(
        cls,
        *,
        job: AgentFactoryJobV3,
        attempt: int,
        policy: BusinessBenchmarkPolicyV1,
    ) -> "CaptainBusinessBenchmarkPolicyBindingV1":
        return cls(
            schema="captain.business-benchmark-policy-binding.v1",
            job_id=job.job_id,
            correlation_id=job.correlation_id,
            subject_version=job.subject_version,
            attempt=attempt,
            policy=policy,
            policy_sha256=_model_sha256(policy),
        )


class BusinessBenchmarkProductionGatewayPort(Protocol):
    def factory_job(self, job_id: UUID) -> object | None: ...

    def team_execution_evidence(
        self, job_id: UUID, attempt: int
    ) -> tuple[TeamExecutionEvidenceV1, ...]: ...

    def budget_projection(self, job_id: UUID) -> FactoryBudgetProjection | None: ...

    def candidate_ref(
        self, job_id: UUID, attempt: int, candidate_id: str
    ) -> ArtifactRef | None: ...


class BusinessBenchmarkCanonicalSuiteAuthorityPort(Protocol):
    def canonical_suite(
        self, *, profile_id: str, suite_version: int
    ) -> tuple[PrivateHoldoutRef, BusinessBenchmarkSuiteV1]: ...


class BusinessBenchmarkCandidateAuthorityPort(Protocol):
    def resolve(
        self,
        *,
        job: AgentFactoryJobV3,
        expected_candidate_id: str,
        expected_candidate_ref: ArtifactRef,
    ) -> ResolvedFactoryCandidate: ...


class BusinessBenchmarkPolicyAuthorityPort(Protocol):
    def policy_for(
        self, scope: "ProductionBusinessBenchmarkScope"
    ) -> CaptainBusinessBenchmarkPolicyBindingV1: ...


class BusinessBenchmarkInvocationAuthorityPort(Protocol):
    def runtime_invocation(
        self, *, job: AgentFactoryJobV3, attempt: int
    ) -> FactorySkillInvocationV1: ...

    def benchmark_invocation(
        self,
        *,
        job: AgentFactoryJobV3,
        attempt: int,
        suite_ref: PrivateHoldoutRef,
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

    settings: LiveBusinessBenchmarkSettings
    selection: BusinessBenchmarkTeamSelectionV1
    job: AgentFactoryJobV3
    profile_id: str
    suite_ref: PrivateHoldoutRef
    suite_id: str
    suite: BusinessBenchmarkSuiteV1
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

            expected_candidate_ref = self._gateway.candidate_ref(
                job.job_id,
                selection.attempt,
                selection.candidate_id,
            )
            if not isinstance(expected_candidate_ref, ArtifactRef):
                raise ValueError("authoritative candidate reference is unavailable")
            candidate = self._candidates.resolve(
                job=job,
                expected_candidate_id=selection.candidate_id,
                expected_candidate_ref=expected_candidate_ref,
            )
            if not isinstance(candidate, ResolvedFactoryCandidate):
                raise ValueError("candidate authority returned an invalid candidate")
            if candidate.candidate.candidate_id != selection.candidate_id:
                raise ValueError("candidate authority returned a different candidate")
            candidate_ref = candidate.candidate.source_archive_ref
            if candidate_ref != expected_candidate_ref:
                raise ValueError("candidate authority returned a different reference")

            technical_runtime_invocation = self._invocations.runtime_invocation(
                job=job,
                attempt=selection.attempt,
            )
            runtime_invocation = self._invocations.benchmark_invocation(
                job=job,
                attempt=selection.attempt,
                suite_ref=suite_ref,
            )
            evaluation_invocation = self._invocations.evaluation_invocation(
                job=job,
                attempt=selection.attempt,
            )
            technical_holdout_ref = technical_runtime_invocation.execution_scope_ref
            if (
                not isinstance(technical_holdout_ref, PrivateHoldoutRef)
                or technical_holdout_ref not in job.private_holdout_refs
                or technical_holdout_ref == suite_ref
            ):
                raise ValueError(
                    "technical execution scope is unavailable, stale, or mixed"
                )
            self._require_invocation(
                technical_runtime_invocation,
                job=job,
                attempt=selection.attempt,
                step=FactorySkillStep.EXECUTE_TEAM,
                suite_ref=technical_holdout_ref,
            )
            self._require_invocation(
                runtime_invocation,
                job=job,
                attempt=selection.attempt,
                step=FactorySkillStep.EXECUTE_TEAM,
                suite_ref=suite_ref,
            )
            if (
                runtime_invocation.invocation_id
                == technical_runtime_invocation.invocation_id
                or runtime_invocation.idempotency_key
                == technical_runtime_invocation.idempotency_key
                or runtime_invocation.lease.role
                is not technical_runtime_invocation.lease.role
                or runtime_invocation.lease.integration_intent
                is not technical_runtime_invocation.lease.integration_intent
                or runtime_invocation.lease.capabilities
                != technical_runtime_invocation.lease.capabilities
                or not runtime_invocation.lease.workspace_ref.startswith(
                    "workspace://business-benchmark-suite/"
                )
                or runtime_invocation.released_skill
                != technical_runtime_invocation.released_skill
                or runtime_invocation.input_ref
                != technical_runtime_invocation.input_ref
            ):
                raise ValueError(
                    "benchmark invocation is not derived from technical authority"
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
                holdout_ref=technical_holdout_ref,
                runtime_invocation=technical_runtime_invocation,
            )
            budget = self._gateway.budget_projection(job.job_id)
            if isinstance(budget, FactoryBudgetProjection):
                budget = FactoryBudgetProjection.model_validate(
                    budget.model_dump(mode="python")
                )
            selection = self._refresh_budget_selection(
                budget,
                job=job,
                selection=selection,
            )
            assert budget is not None
            canonical_settings = LiveBusinessBenchmarkSettings.model_validate(
                canonical_settings.model_dump(mode="python")
                | {
                    "selections": (selection,),
                    "maximum_usd": selection.maximum_usd,
                }
            )

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
                settings=canonical_settings,
                selection=selection,
                job=job,
                profile_id=profile_id,
                suite_ref=suite_ref,
                suite_id=private_suite.suite_id,
                suite=private_suite,
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
        holdout_ref: PrivateHoldoutRef,
        runtime_invocation: FactorySkillInvocationV1,
    ) -> None:
        required_runs = job.execution_policy.required_live_runs
        if (
            len(executions) != required_runs
            or tuple(sorted(item.run_number for item in executions))
            != tuple(range(1, required_runs + 1))
        ):
            raise ValueError(
                "required team execution evidence run numbers are incomplete"
            )
        if any(
            not isinstance(item, TeamExecutionEvidenceV1)
            or item.job_id != job.job_id
            or item.correlation_id != job.correlation_id
            or item.subject_version != job.subject_version
            or item.attempt != attempt
            or item.candidate_ref != candidate_ref
            or item.holdout_ref != holdout_ref
            or item.invocation_id != runtime_invocation.invocation_id
            or item.invocation != runtime_invocation
            or item.status != "succeeded"
            for item in executions
        ):
            raise ValueError("team execution evidence is stale or mixed")

    @staticmethod
    def _refresh_budget_selection(
        budget: FactoryBudgetProjection | None,
        *,
        job: AgentFactoryJobV3,
        selection: BusinessBenchmarkTeamSelectionV1,
    ) -> BusinessBenchmarkTeamSelectionV1:
        if (
            not isinstance(budget, FactoryBudgetProjection)
            or budget.job_id != job.job_id
            or budget.limit_usd != job.execution_policy.max_cost_usd
            or budget.remaining_usd <= 0
        ):
            raise ValueError("Gateway budget projection is missing or stale")
        maximum_usd = min(selection.maximum_usd, budget.remaining_usd)
        return BusinessBenchmarkTeamSelectionV1.model_validate(
            selection.model_dump(mode="python")
            | {
                "maximum_usd": maximum_usd,
                "captain_remaining_usd": budget.remaining_usd,
            }
        )


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


class BusinessBenchmarkClockPort(Protocol):
    def __call__(self) -> datetime: ...


@dataclass(frozen=True)
class _PreparedProductionBusinessBenchmarkScope:
    scope: ProductionBusinessBenchmarkScope
    executor: BusinessBenchmarkExecutorPort
    replay_store: BusinessBenchmarkReplayStore
    case_policies: dict[str, BenchmarkExecutionPolicyV1]
    policy_binding: CaptainBusinessBenchmarkPolicyBindingV1


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
        benchmark_policy_authority: BusinessBenchmarkPolicyAuthorityPort,
        receipt_finalizer: BusinessBenchmarkReceiptFinalizerPort,
        clock: BusinessBenchmarkClockPort,
    ) -> None:
        self._resolver = resolver
        self._factory_composition = factory_composition
        self._invocation_authority = invocation_authority
        self._executor_factory = executor_factory
        self._replay_store_factory = replay_store_factory
        self._execution_policy_factory = execution_policy_factory
        self._benchmark_policy_authority = benchmark_policy_authority
        self._receipt_finalizer = receipt_finalizer
        self._clock = clock
        self._scope_lock = Lock()
        self._scopes: dict[
            tuple[str, UUID, int], BusinessBenchmarkExpectedScopeV1
        ] = {}
        self._prepared_scopes: dict[
            tuple[str, UUID, int], _PreparedProductionBusinessBenchmarkScope
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

    async def preflight(
        self,
        settings: LiveBusinessBenchmarkSettings,
        environment: Mapping[str, str],
        *,
        repository_root: Path | None = None,
    ) -> tuple[BusinessBenchmarkExpectedScopeV1, ...]:
        """Materialize every real team adapter without provider or n8n effects."""

        del repository_root
        canonical = LiveBusinessBenchmarkSettings.model_validate(
            settings.model_dump(mode="python")
        )
        secret_getter = getattr(environment, "get", None)
        if not callable(secret_getter) or not str(
            secret_getter(canonical.provider_secret_name, "")
        ).strip():
            raise BusinessBenchmarkProductionScopeError(
                "benchmark provider secret is not present"
            )
        prepared = tuple(
            self._prepare_scope(canonical.for_selection(selection))
            for selection in canonical.selections
        )
        return tuple(item.scope.expected_scope for item in prepared)

    def dispatch_inputs(
        self,
        settings: LiveBusinessBenchmarkSettings,
        request: FactoryDispatch,
    ) -> BusinessBenchmarkDispatchInputs:
        """Resolve the exact Quality-Warden inputs without running a provider."""

        canonical = LiveBusinessBenchmarkSettings.model_validate(
            settings.model_dump(mode="python")
        )
        if canonical.profile == "all" or len(canonical.selections) != 1:
            raise BusinessBenchmarkProductionScopeError(
                "production dispatch inputs require single team settings"
            )
        selection = canonical.selections[0]
        if (
            not isinstance(request.job, AgentFactoryJobV3)
            or request.job.job_id != selection.job_id
            or request.action.job_id != selection.job_id
            or request.action.attempt != selection.attempt
            or request.action.kind is not FactoryActionKind.DISPATCH_QUALITY_WARDEN
            or request.role is not FactoryRole.QUALITY_WARDEN
            or request.lease is None
        ):
            raise BusinessBenchmarkProductionScopeError(
                "production dispatch request is outside the benchmark selection"
            )
        prepared = self._prepare_scope(canonical)
        scope = prepared.scope
        if request.job != scope.job or request.lease != scope.evaluation_invocation.lease:
            raise BusinessBenchmarkProductionScopeError(
                "production dispatch lease or job does not match resolved authority"
            )

        def exact_case_policy(
            benchmark_case: BusinessBenchmarkCaseV1,
        ) -> BenchmarkExecutionPolicyV1:
            expected_case = next(
                (
                    item
                    for item in scope.suite.cases
                    if item.case_id == benchmark_case.case_id
                ),
                None,
            )
            if expected_case is None or canonical_business_benchmark_model_bytes(
                expected_case
            ) != canonical_business_benchmark_model_bytes(benchmark_case):
                raise BusinessBenchmarkProductionScopeError(
                    "Factory requested a case outside the preflighted suite"
                )
            return prepared.case_policies[benchmark_case.case_id]

        return BusinessBenchmarkDispatchInputs(
            profile_id=scope.profile_id,
            suite_version=scope.selection.suite_version,
            candidate_ref=scope.candidate_ref,
            executor=prepared.executor,
            replay_store=prepared.replay_store,
            execution_policy_factory=exact_case_policy,
            benchmark_policy=prepared.policy_binding.policy,
            evaluation_invocation=scope.evaluation_invocation,
            report_invocation_factory=lambda evaluation: self._report_invocation(
                scope,
                evaluation,
            ),
            technical_executions=scope.technical_executions,
            budget_projection=scope.budget_projection,
        )

    async def run(
        self, settings: LiveBusinessBenchmarkSettings
    ) -> BusinessBenchmarkLiveRunResultV1:
        if settings.profile == "all" or len(settings.selections) != 1:
            raise BusinessBenchmarkProductionScopeError(
                "production composition requires single team settings"
            )
        prepared = self._prepare_scope(settings)
        scope = prepared.scope
        case_policies = prepared.case_policies
        policy_binding = prepared.policy_binding
        executor = prepared.executor
        replay_store = prepared.replay_store

        def exact_case_policy(
            benchmark_case: BusinessBenchmarkCaseV1,
        ) -> BenchmarkExecutionPolicyV1:
            expected_case = next(
                (
                    item
                    for item in scope.suite.cases
                    if item.case_id == benchmark_case.case_id
                ),
                None,
            )
            if expected_case is None or canonical_business_benchmark_model_bytes(
                expected_case
            ) != canonical_business_benchmark_model_bytes(benchmark_case):
                raise BusinessBenchmarkProductionScopeError(
                    "Factory requested a case outside the preflighted suite"
                )
            return case_policies[benchmark_case.case_id]

        report_invocations: list[FactorySkillInvocationV1] = []

        def report_invocation_factory(
            evaluation: TeamEvaluationV1,
        ) -> FactorySkillInvocationV1:
            invocation = self._report_invocation(scope, evaluation)
            report_invocations.append(invocation)
            return invocation

        result = await self._factory_composition.run(
            job=scope.job,
            profile_id=scope.profile_id,
            suite_version=scope.selection.suite_version,
            expected_suite_ref=scope.suite_ref,
            expected_suite=scope.suite,
            attempt=scope.selection.attempt,
            candidate_ref=scope.candidate_ref,
            executor=executor,
            replay_store=replay_store,
            execution_policy_factory=exact_case_policy,
            benchmark_policy=policy_binding.policy,
            evaluation_invocation=scope.evaluation_invocation,
            report_invocation_factory=report_invocation_factory,
            technical_executions=scope.technical_executions,
            budget_projection=scope.budget_projection,
        )
        self._require_factory_result(
            scope,
            result,
            policy_binding=policy_binding,
            case_policies=case_policies,
            report_invocations=tuple(report_invocations),
        )

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

    def _prepare_scope(
        self, settings: LiveBusinessBenchmarkSettings
    ) -> _PreparedProductionBusinessBenchmarkScope:
        scope = self._resolver.resolve(settings)
        self._remember_scope(scope)
        key = self._scope_key(scope)
        with self._scope_lock:
            existing = self._prepared_scopes.get(key)
        if existing is not None:
            if existing.scope != scope:
                raise BusinessBenchmarkProductionScopeError(
                    "production benchmark scope changed after preflight"
                )
            return existing

        case_policy_factory = self._execution_policy_factory(scope)
        case_policies = self._materialize_case_policies(
            scope,
            case_policy_factory,
        )
        policy_binding = self._captain_policy(scope)
        executor = self._executor_factory(scope)
        replay_store = self._replay_store_factory(scope)
        self._validate_outstanding_policy_budget(
            scope,
            case_policies,
            executor=executor,
            replay_store=replay_store,
        )
        prepared = _PreparedProductionBusinessBenchmarkScope(
            scope=scope,
            executor=executor,
            replay_store=replay_store,
            case_policies=case_policies,
            policy_binding=policy_binding,
        )
        with self._scope_lock:
            existing = self._prepared_scopes.get(key)
            if existing is not None and existing != prepared:
                raise BusinessBenchmarkProductionScopeError(
                    "production benchmark adapter changed after preflight"
                )
            self._prepared_scopes[key] = prepared
        return prepared

    @staticmethod
    def _scope_key(scope: ProductionBusinessBenchmarkScope) -> tuple[str, UUID, int]:
        return (
            scope.selection.profile,
            scope.job.job_id,
            scope.selection.attempt,
        )

    @staticmethod
    def _materialize_case_policies(
        scope: ProductionBusinessBenchmarkScope,
        factory: BusinessBenchmarkCasePolicyPort,
    ) -> dict[str, BenchmarkExecutionPolicyV1]:
        policies: dict[str, BenchmarkExecutionPolicyV1] = {}
        for benchmark_case in scope.suite.cases:
            try:
                supplied = factory(benchmark_case)
                if not isinstance(supplied, BenchmarkExecutionPolicyV1):
                    raise ValueError("case policy has the wrong contract")
                policy = BenchmarkExecutionPolicyV1.model_validate(
                    supplied.model_dump(mode="json", by_alias=True)
                )
            except (TypeError, ValueError) as exc:
                raise BusinessBenchmarkProductionScopeError(
                    "benchmark execution policy is invalid"
                ) from exc
            if (
                policy.model_version != scope.settings.model
                or policy.model_version not in scope.job.execution_policy.allowed_models
                or policy.allowed_tool_intents != benchmark_case.allowed_tool_intents
                or _redaction_policy_sha256(policy.redaction_policy_version)
                != scope.settings.redaction_policy_sha256
            ):
                raise BusinessBenchmarkProductionScopeError(
                    "benchmark execution policy binding is stale or mixed"
                )
            policies[benchmark_case.case_id] = policy

        return policies

    @staticmethod
    def _validate_outstanding_policy_budget(
        scope: ProductionBusinessBenchmarkScope,
        policies: Mapping[str, BenchmarkExecutionPolicyV1],
        *,
        executor: BusinessBenchmarkExecutorPort,
        replay_store: BusinessBenchmarkReplayStore,
    ) -> None:

        maximum_cost_micro_usd = int(scope.selection.maximum_usd * 1_000_000)
        remaining_cost_micro_usd = int(
            scope.budget_projection.remaining_usd * 1_000_000
        )
        coordinator = PairedBusinessBenchmarkCoordinator(
            job_id=scope.job.job_id,
            correlation_id=scope.job.correlation_id,
            subject_version=scope.job.subject_version,
            attempt=scope.selection.attempt,
            suite_id=scope.suite.suite_id,
            executor=executor,
            replay_store=replay_store,
        )
        snapshot_for = getattr(replay_store, "snapshot", None)
        total_cost_micro_usd = 0
        total_latency_ms = 0
        for benchmark_case in scope.suite.cases:
            policy = policies[benchmark_case.case_id]
            envelopes = coordinator.envelopes_for_case(
                case=benchmark_case,
                suite_ref=scope.suite_ref,
                candidate_ref=scope.candidate_ref,
                execution_policy=policy,
            )
            for envelope in envelopes:
                receipt = None
                if callable(snapshot_for):
                    snapshot = snapshot_for(coordinator.effect_identity(envelope))
                    receipt = getattr(snapshot, "receipt", None)
                if receipt is None:
                    total_cost_micro_usd += policy.maximum_cost_micro_usd
                    total_latency_ms += policy.maximum_latency_ms
        if (
            total_cost_micro_usd > maximum_cost_micro_usd
            or total_cost_micro_usd > remaining_cost_micro_usd
        ):
            raise BusinessBenchmarkProductionScopeError(
                "benchmark execution policy exceeds the Gateway budget"
            )
        if total_latency_ms > scope.job.execution_policy.max_runtime_seconds * 1000:
            raise BusinessBenchmarkProductionScopeError(
                "benchmark execution policy exceeds the job latency budget"
            )

    def _captain_policy(
        self, scope: ProductionBusinessBenchmarkScope
    ) -> CaptainBusinessBenchmarkPolicyBindingV1:
        try:
            supplied = self._benchmark_policy_authority.policy_for(scope)
            binding = CaptainBusinessBenchmarkPolicyBindingV1.model_validate(
                supplied.model_dump(mode="json", by_alias=True)
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise BusinessBenchmarkProductionScopeError(
                "Captain benchmark policy binding is invalid"
            ) from exc
        if (
            binding.job_id != scope.job.job_id
            or binding.correlation_id != scope.job.correlation_id
            or binding.subject_version != scope.job.subject_version
            or binding.attempt != scope.selection.attempt
        ):
            raise BusinessBenchmarkProductionScopeError(
                "Captain benchmark policy binding is stale or mixed"
            )
        return binding

    def _remember_scope(self, scope: ProductionBusinessBenchmarkScope) -> None:
        key = self._scope_key(scope)
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
        *,
        policy_binding: CaptainBusinessBenchmarkPolicyBindingV1,
        case_policies: dict[str, BenchmarkExecutionPolicyV1],
        report_invocations: tuple[FactorySkillInvocationV1, ...],
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
                or item.allowed_tool_intents
                != case_policies[item.case_id].allowed_tool_intents
                or item.maximum_cost_micro_usd
                != case_policies[item.case_id].maximum_cost_micro_usd
                or item.maximum_latency_ms
                != case_policies[item.case_id].maximum_latency_ms
                or item.execution_policy_sha256
                != _model_sha256(case_policies[item.case_id])
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
        try:
            summary = BusinessBenchmarkSummaryV1.model_validate(
                result.summary.model_dump(mode="json", by_alias=True)
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise BusinessBenchmarkProductionScopeError(
                "production benchmark summary is invalid"
            ) from exc
        if (
            summary.job_id != scope.job.job_id
            or summary.correlation_id != scope.job.correlation_id
            or summary.subject_version != scope.job.subject_version
            or summary.attempt != scope.selection.attempt
            or summary.candidate_ref != scope.candidate_ref
            or summary.suite_ref != scope.suite_ref
            or summary.suite_id != scope.suite_id
            or summary.policy != policy_binding.policy
        ):
            raise BusinessBenchmarkProductionScopeError(
                "production benchmark summary is stale or mixed"
            )

        try:
            case_receipts = tuple(
                BusinessBenchmarkReceiptV1.model_validate(
                    item.model_dump(mode="json", by_alias=True)
                )
                for item in result.case_receipts
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise BusinessBenchmarkProductionScopeError(
                "production benchmark case receipt is invalid"
            ) from exc
        candidate_by_case = {item.case_id: item for item in candidate_receipts}
        baseline_by_case = {item.case_id: item for item in baseline_receipts}
        if (
            len(case_receipts) != 15
            or {item.candidate.case_id for item in case_receipts}
            != set(expected_cases)
            or any(
                item.candidate != candidate_by_case[item.candidate.case_id]
                or item.baseline != baseline_by_case[item.candidate.case_id]
                or item.case_ref.sha256 != expected_cases[item.candidate.case_id]
                for item in case_receipts
            )
        ):
            raise BusinessBenchmarkProductionScopeError(
                "production benchmark case receipt binding is stale or mixed"
            )

        try:
            evaluation = TeamEvaluationV1.model_validate(
                result.evaluation.model_dump(mode="json", by_alias=True)
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise BusinessBenchmarkProductionScopeError(
                "production benchmark evaluation is invalid"
            ) from exc
        if (
            evaluation.job_id != scope.job.job_id
            or evaluation.correlation_id != scope.job.correlation_id
            or evaluation.subject_version != scope.job.subject_version
            or evaluation.attempt != scope.selection.attempt
            or evaluation.invocation != scope.evaluation_invocation
            or evaluation.benchmark_summary_ref != summary.artifact_ref
            or evaluation.benchmark_policy_id != summary.policy.policy_id
            or evaluation.benchmark_disposition != summary.disposition.value
            or scope.candidate_ref not in evaluation.evidence_refs
            or summary.artifact_ref not in evaluation.evidence_refs
        ):
            raise BusinessBenchmarkProductionScopeError(
                "production benchmark evaluation binding is stale or mixed"
            )

        try:
            feedback = FactoryFeedbackV1.model_validate(
                result.feedback.model_dump(mode="json", by_alias=True)
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise BusinessBenchmarkProductionScopeError(
                "production benchmark feedback is invalid"
            ) from exc
        if (
            len(report_invocations) != 1
            or feedback.job_id != scope.job.job_id
            or feedback.correlation_id != scope.job.correlation_id
            or feedback.subject_version != scope.job.subject_version
            or feedback.attempt != scope.selection.attempt
            or feedback.invocation != report_invocations[0]
            or feedback.invocation.input_ref != evaluation.artifact_ref
            or evaluation.artifact_ref not in feedback.evidence_refs
            or scope.candidate_ref not in feedback.evidence_refs
        ):
            raise BusinessBenchmarkProductionScopeError(
                "production benchmark feedback binding is stale or mixed"
            )


def _unique_refs(references: tuple[ArtifactRef, ...]) -> tuple[ArtifactRef, ...]:
    unique: dict[tuple[str, str, str], ArtifactRef] = {}
    for reference in references:
        unique.setdefault(
            (reference.uri, reference.sha256, reference.media_type),
            reference,
        )
    return tuple(unique.values())


def _model_sha256(model: BaseModel) -> str:
    return hashlib.sha256(canonical_business_benchmark_model_bytes(model)).hexdigest()


def _redaction_policy_sha256(version: str) -> str:
    return hashlib.sha256(
        json.dumps(
            {"redaction_policy_version": version},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "BusinessBenchmarkCandidateAuthorityPort",
    "BusinessBenchmarkCasePolicyPort",
    "BusinessBenchmarkCanonicalSuiteAuthorityPort",
    "BusinessBenchmarkClockPort",
    "BusinessBenchmarkExecutionPolicyFactoryPort",
    "BusinessBenchmarkExecutorFactoryPort",
    "BusinessBenchmarkInvocationAuthorityPort",
    "BusinessBenchmarkPolicyAuthorityPort",
    "BusinessBenchmarkProductionGatewayPort",
    "BusinessBenchmarkProductionScopeError",
    "BusinessBenchmarkReceiptFinalizerPort",
    "BusinessBenchmarkReplayStoreFactoryPort",
    "CaptainBusinessBenchmarkPolicyBindingV1",
    "ProductionBusinessBenchmarkComposition",
    "ProductionBusinessBenchmarkScope",
    "ProductionBusinessBenchmarkScopeResolver",
]
