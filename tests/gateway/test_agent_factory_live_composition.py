from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenten.agent_factory.business_benchmark_dispatch import (
    BusinessBenchmarkDispatchUnavailable,
    BusinessBenchmarkDispatchService,
)
from agenten.agent_factory.candidate_evaluation import CandidateEvaluationFactory
from agenten.agent_factory.candidate_evaluation import ResolvedFactoryCandidate
from agenten.agent_factory.contracts import AgentFactoryJobV3
from agenten.agent_factory.business_benchmark_demo_provisioning import (
    BusinessBenchmarkDemoProvisioner,
    BusinessBenchmarkDemoProvisioningSettings,
)
from agenten.agent_factory.hermes_cli import HermesCliFactory, HermesCliSettings
from agenten.agent_factory.orchestration import FactoryDispatcher
from agenten.agent_factory.production_dispatch_runner import (
    ProductionFactoryDispatchRunner,
)
from gateway.agent_factory_dispatch_runner import GatewayNextActionLeaseIssuer
from gateway.agent_factory_live_composition import (
    CaptainImprovementAuthorityRequired,
    GatewayTechnicalTeamExecutionPortsProvider,
    GatewayBoundBusinessBenchmarkInputPort,
    ProductionFactoryTeamExecutionPort,
    compose_agent_factory_live,
    select_technical_business_holdout,
)
from gateway.factory_repository import GatewayFactoryRepository
from agenten.agent_runtime.contracts import ArtifactRef


NOW = datetime(2026, 7, 28, 20, 0, tzinfo=timezone.utc)


class _EffectTrap:
    def __init__(self) -> None:
        self.calls = 0

    def __getattr__(self, _name: str):
        self.calls += 1
        raise AssertionError("composition must not invoke an external port")

    def __call__(self, *_args: object, **_kwargs: object):
        self.calls += 1
        raise AssertionError("composition must not invoke an external port")


def _compose(tmp_path: Path, **overrides: object):
    provider_port = overrides.pop("team_execution_ports_for", _EffectTrap())
    values = {
        "store": _EffectTrap(),
        "forge": _EffectTrap(),
        "candidate_bindings": _EffectTrap(),
        "team_execution_ports_for": provider_port,
        "business_benchmark_repository": _EffectTrap(),
        "business_benchmark_inputs": _EffectTrap(),
        "workspace_namespace": "captain-factory-live",
        "evidence_root": tmp_path / "factory-evidence",
        "hermes_settings": HermesCliSettings(
            skill_root=tmp_path / "released-skill-content",
            evidence_root=tmp_path / "hermes-evidence",
        ),
        "clock": lambda: NOW,
    }
    values.update(overrides)
    return compose_agent_factory_live(**values)


def test_composes_real_factory_runner_without_provider_effects(tmp_path: Path) -> None:
    store = _EffectTrap()
    forge = _EffectTrap()
    candidate_bindings = _EffectTrap()
    team_ports = _EffectTrap()
    benchmark_repository = _EffectTrap()
    benchmark_inputs = _EffectTrap()

    composition = _compose(
        tmp_path,
        store=store,
        forge=forge,
        candidate_bindings=candidate_bindings,
        team_execution_ports_for=team_ports,
        business_benchmark_repository=benchmark_repository,
        business_benchmark_inputs=benchmark_inputs,
    )

    assert isinstance(composition.repository, GatewayFactoryRepository)
    assert isinstance(composition.lease_issuer, GatewayNextActionLeaseIssuer)
    assert isinstance(composition.hermes, HermesCliFactory)
    assert isinstance(composition.team_execution, ProductionFactoryTeamExecutionPort)
    assert isinstance(composition.candidate_validator, CandidateEvaluationFactory)
    assert isinstance(composition.business_benchmark, BusinessBenchmarkDispatchService)
    assert isinstance(composition.dispatcher, FactoryDispatcher)
    assert isinstance(composition.runner, ProductionFactoryDispatchRunner)
    assert composition.dispatcher.lease_authority is composition.lease_issuer
    assert all(
        port.calls == 0
        for port in (
            store,
            forge,
            candidate_bindings,
            team_ports,
            benchmark_repository,
            benchmark_inputs,
        )
    )


def test_gateway_technical_ports_bind_candidate_private_holdout_and_no_tools(
    tmp_path: Path,
) -> None:
    configured = BusinessBenchmarkDemoProvisioningSettings(
        workspace_root=tmp_path,
        test_mariadb_dsn=(
            "mariadb://captain_test:redacted@127.0.0.1:3306/captain_test"
        ),
        issued_at=NOW,
        model="gpt-4.1-mini",
        maximum_usd_per_team=Decimal("0.50"),
        suite_version=3,
        seed_version_id="business-benchmark-demo-2026-07-v3",
    )
    team = BusinessBenchmarkDemoProvisioner(configured).plan().teams[0]
    applied = BusinessBenchmarkDemoProvisioner(
        configured,
        gateway=SimpleNamespace(
            resume_state=lambda _job_id: None,
            register=lambda _job: None,
            assign_released_skills=lambda _job, _skills: None,
            append_forge_requested=lambda _block: None,
            record_lease=lambda _lease: None,
            persist_work_batch=lambda _job, _batch: None,
            budget_projection=lambda job_id: SimpleNamespace(
                job_id=job_id,
                limit_usd=Decimal("0.50"),
                consumed_usd=Decimal("0"),
                reserved_usd=Decimal("0"),
                remaining_usd=Decimal("0.50"),
            ),
        ),
        clock=lambda: NOW,
    ).apply().teams[0]
    assert applied.technical_holdout == team.technical_holdout
    candidate = ResolvedFactoryCandidate.model_construct(
        candidate=SimpleNamespace(source_archive_ref=team.candidate_ref),
        source_archive=tmp_path / "candidate.zip",
    )

    class Candidates:
        def candidate_for(self, job: AgentFactoryJobV3) -> ResolvedFactoryCandidate:
            assert job == team.job
            return candidate

    provider = GatewayTechnicalTeamExecutionPortsProvider.from_environment(
        environment={
            "CAPTAIN_BENCHMARK_PROVIDER": "openai",
            "CAPTAIN_BENCHMARK_MODEL": "gpt-4.1-mini",
            "CAPTAIN_BENCHMARK_MAX_COST_PER_CALL_USD": "0.01",
            "CAPTAIN_BENCHMARK_PRICING_MINIMUM_COST_USD": "0",
            "CAPTAIN_BENCHMARK_PRICING_INPUT_COST_PER_MILLION_USD": "0.40",
            "CAPTAIN_BENCHMARK_PRICING_OUTPUT_COST_PER_MILLION_USD": "1.60",
            "CAPTAIN_BENCHMARK_PRICING_VERSION": "test-price-v1",
            "CAPTAIN_BENCHMARK_PRICING_EFFECTIVE_AT": "2026-07-01T00:00:00Z",
        },
        store=_EffectTrap(),
        candidate_bindings=Candidates(),
        authority_root=(
            tmp_path / ".captain-cook" / "private" / "business-benchmarks"
        ),
        skill_root=tmp_path / "skills",
        clock=lambda: NOW,
    )

    ports = provider(team.job)
    selected = select_technical_business_holdout(team.job)
    resolved = asyncio.run(ports.holdouts.resolve(selected))

    assert selected == team.technical_holdout.holdout_ref
    assert resolved.reference == selected
    assert ports.allowed_tools_for is not None
    assert ports.allowed_tools_for(selected, candidate) == ()
    with pytest.raises(ValueError, match="not available"):
        ports.n8n_adapter.authorization("renewal_context_read")

def test_missing_forge_candidate_binding_fails_before_any_external_port(
    tmp_path: Path,
) -> None:
    store = _EffectTrap()
    team_ports = _EffectTrap()

    with pytest.raises(ValueError, match="Forge candidate binding"):
        _compose(
            tmp_path,
            store=store,
            candidate_bindings=None,
            team_execution_ports_for=team_ports,
        )

    assert store.calls == 0
    assert team_ports.calls == 0


def test_attempt_above_one_requires_injected_captain_improvement_authority(
    tmp_path: Path,
) -> None:
    composition = _compose(tmp_path)

    with pytest.raises(
        CaptainImprovementAuthorityRequired,
        match="Captain improvement authority",
    ):
        composition.improvement_authority.active(
            SimpleNamespace(),
            SimpleNamespace(attempt=2),
            SimpleNamespace(),
            NOW,
        )


def test_benchmark_inputs_require_current_gateway_forge_candidate_reference() -> None:
    candidate_ref = ArtifactRef(
        uri="artifact://forge-candidates/current",
        sha256="a" * 64,
        media_type="application/zip",
    )
    job = AgentFactoryJobV3.model_construct()
    request = SimpleNamespace(job=job, action=SimpleNamespace(attempt=1))

    class Bindings:
        current_candidate_ref = None

        def candidate_for(self, _job: object):
            return SimpleNamespace(
                candidate=SimpleNamespace(source_archive_ref=candidate_ref)
            )

    inputs = SimpleNamespace(
        resolve=lambda _request: SimpleNamespace(candidate_ref=candidate_ref)
    )
    guarded = GatewayBoundBusinessBenchmarkInputPort(
        inputs=inputs,
        candidate_bindings=Bindings(),
    )

    with pytest.raises(
        BusinessBenchmarkDispatchUnavailable,
        match="authoritative candidate reference is unavailable",
    ):
        guarded.resolve(request)


def test_benchmark_inputs_reject_non_v3_before_candidate_resolver() -> None:
    class Bindings:
        calls = 0

        def current_candidate_ref(self, _job: object, _attempt: int):
            self.calls += 1
            raise AssertionError("candidate resolver received a non-V3 job")

    bindings = Bindings()
    guarded = GatewayBoundBusinessBenchmarkInputPort(
        inputs=SimpleNamespace(
            resolve=lambda _request: (_ for _ in ()).throw(
                AssertionError("inputs received a non-V3 job")
            )
        ),
        candidate_bindings=bindings,
    )

    with pytest.raises(
        BusinessBenchmarkDispatchUnavailable,
        match="requires a V3 job",
    ):
        guarded.resolve(
            SimpleNamespace(job=SimpleNamespace(), action=SimpleNamespace(attempt=1))
        )

    assert bindings.calls == 0


def test_benchmark_inputs_preserve_exact_gateway_forge_candidate_reference() -> None:
    candidate_ref = ArtifactRef(
        uri="artifact://forge-candidates/current",
        sha256="b" * 64,
        media_type="application/zip",
    )
    job = AgentFactoryJobV3.model_construct()
    request = SimpleNamespace(job=job, action=SimpleNamespace(attempt=1))
    resolved_candidate = ResolvedFactoryCandidate.model_construct(
        candidate=SimpleNamespace(source_archive_ref=candidate_ref),
        source_archive=Path("sealed-candidate.zip"),
    )
    resolved_inputs = SimpleNamespace(candidate_ref=candidate_ref)

    class Bindings:
        def current_candidate_ref(self, _job: object, _attempt: int):
            return candidate_ref

        def candidate_for(self, _job: object):
            return resolved_candidate

    guarded = GatewayBoundBusinessBenchmarkInputPort(
        inputs=SimpleNamespace(resolve=lambda _request: resolved_inputs),
        candidate_bindings=Bindings(),
    )

    assert guarded.resolve(request) is resolved_inputs

    resolved_inputs.candidate_ref = candidate_ref.model_copy(
        update={"sha256": "c" * 64}
    )
    with pytest.raises(
        BusinessBenchmarkDispatchUnavailable,
        match="do not match the current Forge candidate",
    ):
        guarded.resolve(request)
