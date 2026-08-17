"""Canonical effect-deferred construction for the capability Factory runtime."""

from __future__ import annotations

from agenten.agent_factory.candidate_evaluation import CandidateEvaluationFactory, GatewayForgeCandidateProvider
from agenten.agent_factory.evidence_store import FilesystemFactoryEvidenceStore
from agenten.agent_factory.hermes_cli import HermesCliFactory, HermesCliSettings
from agenten.agent_factory.orchestration import FactoryDispatcher
from agenten.agent_factory.production_dispatch_runner import ProductionFactoryDispatchRunner
from agenten.agent_factory.service import FactoryCoordinator
from gateway.agent_factory_dispatch_runner import GatewayNextActionLeaseIssuer
from gateway.agent_factory_live_composition import AgentFactoryLiveComposition, _CallableFactoryClock
from gateway.factory_repository import GatewayFactoryRepository
from gateway.minibook_creation_artifacts import GatewayMinibookCreationArtifactStore
from agenten.agent_factory.minibook_forge import CaptainCreationJobMapper, MinibookForgeHttpClient, MinibookForgeHttpSettings
from gateway.factory_forge_evidence import CaptainForgeEvidenceBridge
from gateway.capability_factory_benchmark_bridge import AssemblyOwnedGenericBenchmark
from gateway.capability_factory_generic_real_case import GatewayD10GenericCandidateValidator


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
    build_validator = CandidateEvaluationFactory(
        provider=candidate_provider, evidence_store=evidence_store,
        team_execution=None, workflow_artifacts=repository,
    )
    candidate_validator = GatewayD10GenericCandidateValidator(
        gateway_store=store, build_validator=build_validator,
    )
    business_benchmark = AssemblyOwnedGenericBenchmark(
        gateway_store=store,
        clock=clock,
    )
    improvement = _DeferredImprovementAuthority()
    hermes = HermesCliFactory(settings=HermesCliSettings(skill_root=configuration.repository_root / "agenten/agent_factory/skills", evidence_root=configuration.authority_root / "runtime-state/hermes-evidence", working_directory=configuration.repository_root), released_skill_catalog=repository, workflow_artifacts=repository, clock=clock)
    forge = MinibookForgeHttpClient(
        mapper=CaptainCreationJobMapper(
            evidence=CaptainForgeEvidenceBridge(
                repository=repository, evidence_store=evidence_store,
            )
        ),
        settings=MinibookForgeHttpSettings(
            base_url=configuration.minibook_url,
            api_key=authorities.minibook_api_key.get_secret_value(),
        ),
    )
    dispatcher = FactoryDispatcher(coordinator=coordinator, hermes=hermes, forge=forge, candidate_validator=candidate_validator, business_benchmark=business_benchmark, leases=lease_issuer, clock=_CallableFactoryClock(clock), improvements=improvement)
    runner = ProductionFactoryDispatchRunner(coordinator=coordinator, dispatcher=dispatcher, lease_issuer=lease_issuer, clock=clock)
    return AgentFactoryLiveComposition(repository=repository, coordinator=coordinator, lease_issuer=lease_issuer, hermes=hermes, team_execution=candidate_validator, candidate_validator=candidate_validator, business_benchmark=business_benchmark, dispatcher=dispatcher, runner=runner, improvement_authority=improvement, runtime_retry_authority=None)
