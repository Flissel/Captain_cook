"""Canonical effect-deferred construction for the capability Factory runtime."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from agenten.agent_factory.business_benchmark import BusinessBenchmarkEvaluator
from agenten.agent_factory.business_benchmark_dispatch import BusinessBenchmarkDispatchService
from agenten.agent_factory.business_benchmark_factory import BusinessBenchmarkFactoryComposition
from agenten.agent_factory.business_benchmark_production_ports import BusinessBenchmarkContentAddressedArtifactStore, BusinessBenchmarkPricingAuthority, ConfiguredBusinessBenchmarkPricingSource, ProxyBoundResponsesModelClientBuilder
from agenten.agent_factory.business_benchmark_store import InMemoryBusinessBenchmarkRepository
from agenten.agent_factory.candidate_evaluation import CandidateEvaluationFactory, GatewayForgeCandidateProvider
from agenten.agent_factory.evidence_store import FilesystemFactoryEvidenceStore
from agenten.agent_factory.factory_feedback import FactoryFeedbackBuilder
from agenten.agent_factory.hermes_cli import FilesystemFactorySkillReplayStore, HermesCliFactory, HermesCliSettings
from agenten.agent_factory.orchestration import FactoryDispatcher
from agenten.agent_factory.production_dispatch_runner import ProductionFactoryDispatchRunner
from agenten.agent_factory.service import FactoryCoordinator
from agenten.agent_factory.team_evaluation import TeamEvaluationService
from gateway.agent_factory_dispatch_runner import GatewayNextActionLeaseIssuer
from gateway.agent_factory_live_composition import AgentFactoryLiveComposition, GatewayTechnicalTeamExecutionPortsProvider, ProductionFactoryTeamExecutionPort, _CallableFactoryClock
from gateway.factory_repository import GatewayFactoryBudgetLedger, GatewayFactoryRepository
from gateway.minibook_creation_artifacts import GatewayMinibookCreationArtifactStore


class _DeferredForge:
    async def create(self, *_args, **_kwargs):
        raise RuntimeError("Factory Forge authority has not been provisioned")


class _DeferredBenchmarkInputs:
    def dispatch_inputs(self, *_args, **_kwargs):
        raise RuntimeError("Factory benchmark inputs have not been provisioned")


class _DeferredImprovementAuthority:
    def active(self, *_args, **_kwargs):
        return None


def build_capability_factory_runtime(configuration, authorities) -> AgentFactoryLiveComposition:
    """Construct exact graph types; external effects stay deferred until dispatch."""

    store, clock = authorities.store, authorities.clock
    repository = GatewayFactoryRepository(store)
    coordinator = FactoryCoordinator(repository)
    lease_issuer = GatewayNextActionLeaseIssuer(store=store, workspace_namespace="capability-assembly-v1", n8n_work_batch_source=authorities.n8n)
    evidence_store = FilesystemFactoryEvidenceStore(configuration.authority_root / "runtime-state/factory-evidence")
    candidate_provider = GatewayForgeCandidateProvider(repository=repository, artifacts=GatewayMinibookCreationArtifactStore(configuration.authority_root / "minibook-creation-cas"))
    maximum = configuration.execution_policy.max_cost_usd
    artifacts = BusinessBenchmarkContentAddressedArtifactStore(configuration.authority_root / "cas")
    model_client = ProxyBoundResponsesModelClientBuilder(
        provider=configuration.provider, model=configuration.model,
        proxy_url=configuration.provider_proxy_url,
        context_sha256=configuration.provider_context_sha256,
        policy_sha256=configuration.provider_policy_sha256,
        price_card_sha256=configuration.provider_price_card_sha256,
        executable_sha256=configuration.provider_proxy_executable_sha256,
        code_sha256=configuration.provider_proxy_code_sha256,
        paid_command_sha256=configuration.provider_paid_command_sha256,
        hard_ceiling_enforced=configuration.provider_hard_ceiling_enforced,
        _client_token=authorities.provider_token.get_secret_value(),
    )
    pricing = BusinessBenchmarkPricingAuthority(ConfiguredBusinessBenchmarkPricingSource(provider=configuration.provider, model=configuration.model, version="capability-production-v1", effective_at=datetime(2026, 1, 1, tzinfo=timezone.utc), max_cost_per_call=maximum, input_cost_per_million=Decimal("1.25"), output_cost_per_million=Decimal("10.00"), minimum_cost_usd=Decimal("0.000001"), artifacts=artifacts))
    ports = GatewayTechnicalTeamExecutionPortsProvider(candidate_bindings=candidate_provider, technical_holdout_root=configuration.authority_root / "technical-holdouts", model_client_for=model_client, budget=GatewayFactoryBudgetLedger(store), pricing_authority=pricing, replay_store=FilesystemFactorySkillReplayStore(configuration.repository_root / ".captain-cook/private/business-benchmarks/runtime-state/factory-team-replays"), released_skill_catalog=repository, skill_root=configuration.repository_root / "agenten/agent_factory/skills", provider=configuration.provider, model=configuration.model, max_cost_per_call=maximum, clock=clock)
    team_execution = ProductionFactoryTeamExecutionPort(evidence_store=evidence_store, ports_for=ports, holdout_selector=None)
    candidate_validator = CandidateEvaluationFactory(provider=candidate_provider, evidence_store=evidence_store, team_execution=team_execution, workflow_artifacts=repository)
    benchmark_composition = BusinessBenchmarkFactoryComposition(private_repository=InMemoryBusinessBenchmarkRepository(), gateway_repository=repository, evaluator=BusinessBenchmarkEvaluator(clock=clock), team_evaluator=TeamEvaluationService(clock=clock), feedback_builder=FactoryFeedbackBuilder(clock=clock))
    business_benchmark = BusinessBenchmarkDispatchService(composition=benchmark_composition, inputs=_DeferredBenchmarkInputs(), clock=clock)
    improvement = _DeferredImprovementAuthority()
    hermes = HermesCliFactory(settings=HermesCliSettings(skill_root=configuration.repository_root / "agenten/agent_factory/skills", evidence_root=configuration.authority_root / "runtime-state/hermes-evidence", working_directory=configuration.repository_root), released_skill_catalog=repository, workflow_artifacts=repository, clock=clock)
    dispatcher = FactoryDispatcher(coordinator=coordinator, hermes=hermes, forge=_DeferredForge(), candidate_validator=candidate_validator, business_benchmark=business_benchmark, leases=lease_issuer, clock=_CallableFactoryClock(clock), improvements=improvement)
    runner = ProductionFactoryDispatchRunner(coordinator=coordinator, dispatcher=dispatcher, lease_issuer=lease_issuer, clock=clock)
    return AgentFactoryLiveComposition(repository=repository, coordinator=coordinator, lease_issuer=lease_issuer, hermes=hermes, team_execution=team_execution, candidate_validator=candidate_validator, business_benchmark=business_benchmark, dispatcher=dispatcher, runner=runner, improvement_authority=improvement, runtime_retry_authority=None)
