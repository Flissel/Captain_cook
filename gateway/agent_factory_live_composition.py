"""Gateway-owned composition root for the provider-live Agent Factory runner."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID

from autogen_core.models import ChatCompletionClient

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
from agenten.agent_factory.business_benchmark_production_ports import (
    BusinessBenchmarkContentAddressedArtifactStore,
    BusinessBenchmarkPricingAuthority,
    ConfiguredBusinessBenchmarkPricingSource,
    OpenAIBusinessBenchmarkModelClientBuilder,
)
from agenten.agent_factory.business_benchmark_store import (
    BusinessBenchmarkRepository,
)
from agenten.agent_factory.business_benchmark_technical_holdout import (
    CaptainTechnicalBusinessHoldoutEvaluator,
)
from agenten.agent_factory.candidate_evaluation import (
    CandidateEvaluationFactory,
    FactoryCandidateProvider,
    FactoryTeamExecutionPort,
    ResolvedFactoryCandidate,
)
from agenten.agent_factory.codex_brief import CodexPromptArtifactStore
from agenten.agent_factory.contracts import AgentFactoryJobV3, FactoryJob
from agenten.agent_factory.evidence_store import FilesystemFactoryEvidenceStore
from agenten.agent_factory.execution_budget import FactoryBudgetPort
from agenten.agent_factory.factory_feedback import FactoryFeedbackBuilder
from agenten.agent_factory.hermes_cli import (
    CaptainCodexBuildSealerPort,
    FactorySkillReplayStore,
    FilesystemFactorySkillReplayStore,
    HermesCliFactory,
    HermesCliSettings,
    ReleasedFactorySkillCatalog,
)
from agenten.agent_factory.holdout_contracts import PrivateHoldoutRef
from agenten.agent_factory.orchestration import (
    FactoryClock,
    FactoryDispatch,
    FactoryDispatchError,
    FactoryDispatcher,
    FactoryImprovementAuthorizationPort,
    FactoryRuntimeRetryAuthorizationPort,
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
from agenten.agent_factory.state_machine import (
    FactoryAction,
    FactoryActionKind,
    FactoryProjection,
)
from agenten.agent_factory.team_evaluation import TeamEvaluationService
from agenten.agent_factory.team_execution import (
    FactoryN8nExecutionEvidenceV1,
    FactoryLiveTeamExecutionPorts,
    FactoryPricingAuthorityPort,
    TeamExecutionCandidateAdapter,
    compose_live_team_execution,
)
from agenten.agent_runtime.contracts import ArtifactRef
from gateway.agent_factory_dispatch_runner import GatewayNextActionLeaseIssuer
from gateway.factory_repository import GatewayFactoryBudgetLedger, GatewayFactoryRepository
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


def select_technical_business_holdout(job: AgentFactoryJobV3) -> PrivateHoldoutRef:
    """Select the canonical redacted execution case, never the private suite."""

    if len(job.private_holdout_refs) != 2:
        raise ValueError(
            "technical benchmark execution requires technical and suite holdouts"
        )
    return job.private_holdout_refs[0]


class _UnavailableTechnicalN8nAdapter:
    """Fail closed because the selected escalation case has no integration intent."""

    def tool(self, _name: str) -> object:
        raise ValueError("n8n is not available for the technical escalation holdout")

    def authorization(self, _name: str) -> object:
        raise ValueError("n8n is not available for the technical escalation holdout")

    def observed_evidence(self) -> tuple[FactoryN8nExecutionEvidenceV1, ...]:
        return ()


class _UnavailableTechnicalN8nAuthority:
    async def authorize_command(self, _claim: object, *, now: datetime) -> object:
        del now
        raise ValueError("n8n is not available for the technical escalation holdout")

    async def authorize(self, _evidence: object, *, now: datetime) -> object:
        del now
        raise ValueError("n8n is not available for the technical escalation holdout")


class GatewayTechnicalTeamExecutionPortsProvider:
    """Build one candidate-bound live port graph for the technical holdout."""

    def __init__(
        self,
        *,
        candidate_bindings: ForgeCandidateBindingPort,
        technical_holdout_root: Path,
        model_client_for: Callable[
            [AgentFactoryJobV3, FactorySkillInvocationV1], ChatCompletionClient
        ],
        budget: FactoryBudgetPort,
        pricing_authority: FactoryPricingAuthorityPort,
        replay_store: FactorySkillReplayStore,
        released_skill_catalog: ReleasedFactorySkillCatalog,
        skill_root: Path,
        provider: str,
        model: str,
        max_cost_per_call: Decimal,
        clock: Callable[[], datetime],
    ) -> None:
        if provider != "openai" or not model.strip() or max_cost_per_call <= 0:
            raise ValueError("technical team provider configuration is invalid")
        self._candidate_bindings = candidate_bindings
        self._technical_holdout_root = technical_holdout_root.resolve()
        self._model_client_for = model_client_for
        self._budget = budget
        self._pricing_authority = pricing_authority
        self._replay_store = replay_store
        self._released_skill_catalog = released_skill_catalog
        self._skill_root = skill_root.resolve()
        self._provider = provider
        self._model = model
        self._max_cost_per_call = max_cost_per_call
        self._clock = clock

    @classmethod
    def from_environment(
        cls,
        *,
        environment: Mapping[str, str],
        store: GatewayStore,
        candidate_bindings: ForgeCandidateBindingPort,
        authority_root: Path,
        skill_root: Path,
        clock: Callable[[], datetime],
    ) -> "GatewayTechnicalTeamExecutionPortsProvider":
        provider = _required_environment(environment, "CAPTAIN_BENCHMARK_PROVIDER")
        model = _required_environment(environment, "CAPTAIN_BENCHMARK_MODEL")
        maximum = _positive_decimal_environment(
            environment,
            "CAPTAIN_BENCHMARK_MAX_COST_PER_CALL_USD",
        )
        root = authority_root.resolve()
        artifacts = BusinessBenchmarkContentAddressedArtifactStore(root / "cas")
        repository = GatewayFactoryRepository(store)
        return cls(
            candidate_bindings=candidate_bindings,
            technical_holdout_root=root / "technical-holdouts",
            model_client_for=(
                OpenAIBusinessBenchmarkModelClientBuilder.from_environment_deferred(
                    environment
                )
            ),
            budget=GatewayFactoryBudgetLedger(store),
            pricing_authority=BusinessBenchmarkPricingAuthority(
                ConfiguredBusinessBenchmarkPricingSource.from_environment(
                    environment,
                    artifacts=artifacts,
                )
            ),
            replay_store=FilesystemFactorySkillReplayStore(
                root / "runtime-state" / "factory-team-replays"
            ),
            released_skill_catalog=repository,
            skill_root=skill_root,
            provider=provider,
            model=model,
            max_cost_per_call=maximum,
            clock=clock,
        )

    def __call__(self, job: AgentFactoryJobV3) -> FactoryLiveTeamExecutionPorts:
        candidate = self._candidate_bindings.candidate_for(job)
        if not isinstance(candidate, ResolvedFactoryCandidate):
            raise ValueError("technical team candidate is not resolved")
        holdouts = CaptainTechnicalBusinessHoldoutEvaluator(
            self._technical_holdout_root,
            candidate_ref=candidate.candidate.source_archive_ref,
            allowed_tools=(),
            clock=self._clock,
        )
        return FactoryLiveTeamExecutionPorts(
            model_client_for=self._model_client_for,
            budget=self._budget,
            pricing_authority=self._pricing_authority,
            replay_store=self._replay_store,
            holdouts=holdouts,
            n8n_adapter=_UnavailableTechnicalN8nAdapter(),  # type: ignore[arg-type]
            n8n_authority=_UnavailableTechnicalN8nAuthority(),  # type: ignore[arg-type]
            released_skill_catalog=self._released_skill_catalog,
            skill_root=self._skill_root,
            tools={},
            provider=self._provider,
            model=self._model,
            max_cost_per_call=self._max_cost_per_call,
            clock=self._clock,
            allowed_tools_for=holdouts.allowed_tools_for,
        )


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
    runtime_retry_authority: FactoryRuntimeRetryAuthorizationPort | None

    async def run(
        self,
        job_id: UUID,
        *,
        maximum_dispatches: int = 12,
        stop_before_action: FactoryActionKind | None = None,
    ) -> ProductionFactoryDispatchResult:
        return await self.runner.run(
            job_id,
            maximum_dispatches=maximum_dispatches,
            stop_before_action=stop_before_action,
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
    runtime_retries: FactoryRuntimeRetryAuthorizationPort | None = None,
    codex_build_sealer: CaptainCodexBuildSealerPort | None = None,
    codex_prompt_artifact_store: CodexPromptArtifactStore | None = None,
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

    repository = GatewayFactoryRepository(
        store,
        runtime_retries=runtime_retries,
    )
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
        codex_build_sealer=codex_build_sealer,
        codex_prompt_artifact_store=codex_prompt_artifact_store,
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
        runtime_retries=runtime_retries,
    )
    runner = ProductionFactoryDispatchRunner(
        coordinator=coordinator,
        dispatcher=dispatcher,
        lease_issuer=lease_issuer,
        runtime_retries=runtime_retries,
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
        runtime_retry_authority=runtime_retries,
    )


def _required_environment(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise ValueError(f"required Factory live setting is missing: {name}")
    return value


def _positive_decimal_environment(
    environment: Mapping[str, str],
    name: str,
) -> Decimal:
    raw = _required_environment(environment, name)
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"Factory live setting is not a decimal: {name}") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError(f"Factory live setting must be positive: {name}")
    return value


__all__ = [
    "AgentFactoryLiveComposition",
    "CaptainImprovementAuthorityRequired",
    "FactoryLiveTeamExecutionPortsProvider",
    "ForgeCandidateBindingPort",
    "GatewayTechnicalTeamExecutionPortsProvider",
    "GatewayBoundBusinessBenchmarkInputPort",
    "ProductionFactoryTeamExecutionPort",
    "compose_agent_factory_live",
    "select_technical_business_holdout",
]
