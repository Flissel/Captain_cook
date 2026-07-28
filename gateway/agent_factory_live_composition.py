"""Gateway-owned composition root for the provider-live Agent Factory runner."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID

from agenten.agent_factory.business_benchmark import BusinessBenchmarkEvaluator
from agenten.agent_factory.business_benchmark_dispatch import (
    BusinessBenchmarkDispatchInputPort,
    BusinessBenchmarkDispatchInputs,
    BusinessBenchmarkDispatchService,
    BusinessBenchmarkDispatchUnavailable,
)
from agenten.agent_factory.business_benchmark_factory import (
    BusinessBenchmarkFactoryComposition,
)
from agenten.agent_factory.business_benchmark_store import (
    BusinessBenchmarkRepository,
)
from agenten.agent_factory.candidate_evaluation import (
    CandidateEvaluationFactory,
    FactoryCandidateProvider,
    FactoryTeamExecutionPort,
    ResolvedFactoryCandidate,
)
from agenten.agent_factory.contracts import AgentFactoryJobV3, FactoryJob
from agenten.agent_factory.evidence_store import FilesystemFactoryEvidenceStore
from agenten.agent_factory.factory_feedback import FactoryFeedbackBuilder
from agenten.agent_factory.hermes_cli import HermesCliFactory, HermesCliSettings
from agenten.agent_factory.holdout_contracts import PrivateHoldoutRef
from agenten.agent_factory.orchestration import (
    FactoryClock,
    FactoryDispatch,
    FactoryDispatchError,
    FactoryDispatcher,
    FactoryImprovementAuthorizationPort,
    MinibookForgePort,
)
from agenten.agent_factory.production_dispatch_runner import (
    ProductionFactoryDispatchResult,
    ProductionFactoryDispatchRunner,
)
from agenten.agent_factory.service import FactoryCoordinator
from agenten.agent_factory.skill_sequence import FactoryImprovementAuthorizationV1
from agenten.agent_factory.skill_workflow_contracts import (
    FactorySkillInvocationV1,
    TeamExecutionEvidenceV1,
)
from agenten.agent_factory.state_machine import FactoryAction, FactoryProjection
from agenten.agent_factory.team_evaluation import TeamEvaluationService
from agenten.agent_factory.team_execution import (
    FactoryLiveTeamExecutionPorts,
    TeamExecutionCandidateAdapter,
    compose_live_team_execution,
)
from agenten.agent_runtime.contracts import ArtifactRef
from gateway.agent_factory_dispatch_runner import GatewayNextActionLeaseIssuer
from gateway.factory_repository import GatewayFactoryRepository
from gateway.store import GatewayStore


class ForgeCandidateBindingPort(Protocol):
    """Resolve only the CAS-bound candidate produced for the exact Forge job."""

    def candidate_for(self, job: FactoryJob) -> ResolvedFactoryCandidate: ...

    def current_candidate_ref(
        self,
        job: AgentFactoryJobV3,
        attempt: int,
    ) -> ArtifactRef | None:
        """Return the current candidate reference recorded by Gateway."""

        ...


class FactoryLiveTeamExecutionPortsProvider(Protocol):
    """Resolve the complete authoritative live port set for one V3 job."""

    def __call__(self, job: AgentFactoryJobV3) -> FactoryLiveTeamExecutionPorts: ...


class CaptainImprovementAuthorityRequired(FactoryDispatchError):
    """A retry reached execution without Captain improvement authorization."""


class _MissingCaptainImprovementAuthority(FactoryImprovementAuthorizationPort):
    def active(
        self,
        job: FactoryJob,
        action: FactoryAction,
        projection: FactoryProjection,
        now: datetime,
    ) -> FactoryImprovementAuthorizationV1:
        del job, projection, now
        raise CaptainImprovementAuthorityRequired(
            "Captain improvement authority is required for "
            f"Factory attempt {action.attempt}"
        )


class GatewayBoundBusinessBenchmarkInputPort(BusinessBenchmarkDispatchInputPort):
    """Reject benchmark inputs not bound to Gateway's current Forge candidate."""

    def __init__(
        self,
        *,
        inputs: BusinessBenchmarkDispatchInputPort,
        candidate_bindings: ForgeCandidateBindingPort,
    ) -> None:
        self._inputs = inputs
        self._candidate_bindings = candidate_bindings

    def resolve(
        self,
        request: FactoryDispatch,
    ) -> BusinessBenchmarkDispatchInputs | None:
        if not isinstance(request.job, AgentFactoryJobV3):
            raise BusinessBenchmarkDispatchUnavailable(
                "business benchmark candidate binding requires a V3 job"
            )
        candidate_ref_resolver = getattr(
            self._candidate_bindings,
            "current_candidate_ref",
            None,
        )
        current_ref = (
            candidate_ref_resolver(request.job, request.action.attempt)
            if callable(candidate_ref_resolver)
            else None
        )
        if not isinstance(current_ref, ArtifactRef):
            raise BusinessBenchmarkDispatchUnavailable(
                "authoritative candidate reference is unavailable"
            )
        candidate = self._candidate_bindings.candidate_for(request.job)
        if (
            not isinstance(candidate, ResolvedFactoryCandidate)
            or candidate.candidate.source_archive_ref != current_ref
        ):
            raise BusinessBenchmarkDispatchUnavailable(
                "Forge candidate binding does not match Gateway authority"
            )
        resolved = self._inputs.resolve(request)
        if resolved is None:
            return None
        if resolved.candidate_ref != current_ref:
            raise BusinessBenchmarkDispatchUnavailable(
                "business benchmark inputs do not match the current Forge candidate"
            )
        return resolved


@dataclass(frozen=True)
class _CallableFactoryClock(FactoryClock):
    value: Callable[[], datetime]

    def now(self) -> datetime:
        return self.value()


class ProductionFactoryTeamExecutionPort(FactoryTeamExecutionPort):
    """Create the governed TeamExecutionService graph only for its dispatch."""

    def __init__(
        self,
        *,
        evidence_store: FilesystemFactoryEvidenceStore,
        ports_for: FactoryLiveTeamExecutionPortsProvider,
        holdout_selector: Callable[[AgentFactoryJobV3], PrivateHoldoutRef] | None,
    ) -> None:
        self._evidence_store = evidence_store
        self._ports_for = ports_for
        self._holdout_selector = holdout_selector
        self._adapters: dict[
            tuple[UUID, int, str], TeamExecutionCandidateAdapter
        ] = {}

    def invocation_for(
        self,
        request: FactoryDispatch,
    ) -> FactorySkillInvocationV1:
        return self._adapter(request).invocation_for(request)

    async def execute(
        self,
        request: FactoryDispatch,
        candidate: ResolvedFactoryCandidate,
    ) -> TeamExecutionEvidenceV1:
        return await self._adapter(request).execute(request, candidate)

    def _adapter(self, request: FactoryDispatch) -> TeamExecutionCandidateAdapter:
        if not isinstance(request.job, AgentFactoryJobV3) or request.lease is None:
            raise FactoryDispatchError("live team execution requires a V3 job")
        key = (
            request.job.job_id,
            request.action.attempt,
            request.lease.lease_id,
        )
        existing = self._adapters.get(key)
        if existing is not None:
            return existing
        ports = self._ports_for(request.job)
        if not isinstance(ports, FactoryLiveTeamExecutionPorts):
            raise FactoryDispatchError(
                "live team execution authoritative ports are unavailable"
            )
        adapter = compose_live_team_execution(
            job=request.job,
            evidence_store=self._evidence_store,
            ports=ports,
            holdout_selector=self._holdout_selector,
        )
        self._adapters[key] = adapter
        return adapter


@dataclass(frozen=True)
class AgentFactoryLiveComposition:
    """Inspectable graph; Captain-only transitions remain outside ``run``."""

    repository: GatewayFactoryRepository
    lease_issuer: GatewayNextActionLeaseIssuer
    hermes: HermesCliFactory
    team_execution: ProductionFactoryTeamExecutionPort
    candidate_validator: CandidateEvaluationFactory
    business_benchmark: BusinessBenchmarkDispatchService
    dispatcher: FactoryDispatcher
    runner: ProductionFactoryDispatchRunner
    improvement_authority: FactoryImprovementAuthorizationPort

    async def run(
        self,
        job_id: UUID,
        *,
        maximum_dispatches: int = 12,
    ) -> ProductionFactoryDispatchResult:
        return await self.runner.run(
            job_id,
            maximum_dispatches=maximum_dispatches,
        )


def compose_agent_factory_live(
    *,
    store: GatewayStore,
    forge: MinibookForgePort,
    candidate_bindings: ForgeCandidateBindingPort | None,
    team_execution_ports_for: FactoryLiveTeamExecutionPortsProvider,
    business_benchmark_repository: BusinessBenchmarkRepository,
    business_benchmark_inputs: BusinessBenchmarkDispatchInputPort,
    workspace_namespace: str,
    evidence_root: Path,
    hermes_settings: HermesCliSettings,
    clock: Callable[[], datetime],
    n8n_work_batches: Mapping[UUID, str] | None = None,
    holdout_selector: Callable[[AgentFactoryJobV3], PrivateHoldoutRef] | None = None,
    improvements: FactoryImprovementAuthorizationPort | None = None,
) -> AgentFactoryLiveComposition:
    """Wire production ports without executing Hermes, providers, n8n, or Forge."""

    if candidate_bindings is None:
        raise ValueError("Forge candidate binding port is required")
    if not callable(team_execution_ports_for):
        raise ValueError("live TeamExecutionService ports provider is required")
    if forge is None or business_benchmark_repository is None:
        raise ValueError("Factory production artifact ports are required")
    if business_benchmark_inputs is None:
        raise ValueError("business benchmark production inputs are required")
    if not callable(clock):
        raise ValueError("Factory live clock is required")

    repository = GatewayFactoryRepository(store)
    coordinator = FactoryCoordinator(repository)
    lease_issuer = GatewayNextActionLeaseIssuer(
        store=store,
        workspace_namespace=workspace_namespace,
        n8n_work_batches=n8n_work_batches,
    )
    evidence_store = FilesystemFactoryEvidenceStore(Path(evidence_root))
    hermes = HermesCliFactory(
        settings=hermes_settings,
        evidence_store=evidence_store,
        released_skill_catalog=repository,
        clock=clock,
    )
    team_execution = ProductionFactoryTeamExecutionPort(
        evidence_store=evidence_store,
        ports_for=team_execution_ports_for,
        holdout_selector=holdout_selector,
    )
    candidate_validator = CandidateEvaluationFactory(
        provider=cast(FactoryCandidateProvider, candidate_bindings),
        evidence_store=evidence_store,
        team_execution=team_execution,
    )
    benchmark_composition = BusinessBenchmarkFactoryComposition(
        private_repository=business_benchmark_repository,
        gateway_repository=repository,
        evaluator=BusinessBenchmarkEvaluator(clock=clock),
        team_evaluator=TeamEvaluationService(clock=clock),
        feedback_builder=FactoryFeedbackBuilder(clock=clock),
    )
    guarded_benchmark_inputs = GatewayBoundBusinessBenchmarkInputPort(
        inputs=business_benchmark_inputs,
        candidate_bindings=candidate_bindings,
    )
    business_benchmark = BusinessBenchmarkDispatchService(
        composition=benchmark_composition,
        inputs=guarded_benchmark_inputs,
        clock=clock,
    )
    improvement_authority = (
        improvements
        if improvements is not None
        else _MissingCaptainImprovementAuthority()
    )
    dispatcher = FactoryDispatcher(
        coordinator=coordinator,
        hermes=hermes,
        forge=forge,
        candidate_validator=candidate_validator,
        business_benchmark=business_benchmark,
        leases=lease_issuer,
        clock=_CallableFactoryClock(clock),
        improvements=improvement_authority,
    )
    runner = ProductionFactoryDispatchRunner(
        coordinator=coordinator,
        dispatcher=dispatcher,
        lease_issuer=lease_issuer,
        clock=clock,
    )
    return AgentFactoryLiveComposition(
        repository=repository,
        lease_issuer=lease_issuer,
        hermes=hermes,
        team_execution=team_execution,
        candidate_validator=candidate_validator,
        business_benchmark=business_benchmark,
        dispatcher=dispatcher,
        runner=runner,
        improvement_authority=improvement_authority,
    )


__all__ = [
    "AgentFactoryLiveComposition",
    "CaptainImprovementAuthorityRequired",
    "FactoryLiveTeamExecutionPortsProvider",
    "ForgeCandidateBindingPort",
    "GatewayBoundBusinessBenchmarkInputPort",
    "ProductionFactoryTeamExecutionPort",
    "compose_agent_factory_live",
]
