"""Deployable composition for resuming the isolated business-demo Factory jobs."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Protocol
from uuid import UUID

import httpx

from blockchain.mariadb_storage import MariaDBStorage
from agenten.agent_factory.business_benchmark_bootstrap import (
    CaptainCanonicalSuiteAuthority,
    CaptainCanonicalSuiteRepository,
)
from agenten.agent_factory.business_benchmark_demo_provisioning import (
    assert_local_captain_test_dsn,
)
from agenten.agent_factory.business_benchmark_dispatch import (
    BusinessBenchmarkDispatchInputs,
)
from agenten.agent_factory.business_benchmark_live import (
    LiveBusinessBenchmarkSettings,
)
from agenten.agent_factory.business_benchmark_production_ports import (
    BusinessBenchmarkContentAddressedArtifactStore,
)
from agenten.agent_factory.candidate_evaluation import GatewayForgeCandidateProvider
from agenten.agent_factory.codex_build_execution import (
    CaptainCodexBuildSealer,
    CodexCliFactoryBuildExecutor,
    CodexCliFactoryBuildSettings,
    GitDetachedFactoryWorkspacePreparer,
)
from agenten.agent_factory.codex_build_provenance import (
    CaptainCodexBuildReceiptIssuer,
    CodexBuildArtifactCas,
)
from agenten.agent_factory.hermes_cli import HermesCliSettings
from agenten.agent_factory.minibook_forge import (
    CaptainCreationJobMapper,
    MinibookForgeSettings,
    MinibookSwarmForge,
)
from agenten.agent_factory.orchestration import FactoryDispatch
from agenten.agent_factory.production_dispatch_runner import (
    ProductionFactoryDispatchResult,
)
from agenten.agent_runtime.contracts import ArtifactRef
from agenten.execution.codex_policy import CodexExecutionPolicy
from agenten.execution.codex_supervisor import PowerShellCodexRunner
from gateway.agent_factory_live_composition import (
    AgentFactoryLiveComposition,
    GatewayTechnicalTeamExecutionPortsProvider,
    compose_agent_factory_live,
    select_technical_business_holdout,
)
from gateway.business_benchmark_live_composition import (
    GatewayBusinessBenchmarkLiveCompositionLoader,
)
from gateway.factory_repository import GatewayFactoryRepository
from gateway.minibook_creation_artifacts import GatewayMinibookCreationArtifactStore
from gateway.store import GatewayStore


class _DispatchInputComposition(Protocol):
    def dispatch_inputs(
        self,
        settings: LiveBusinessBenchmarkSettings,
        request: FactoryDispatch,
    ) -> BusinessBenchmarkDispatchInputs: ...


@dataclass(frozen=True)
class FactoryLiveOperatorSettings:
    workspace_root: Path
    python_executable: Path
    test_mariadb_dsn: str
    job_ids: tuple[UUID, UUID]
    hermes_provider: str
    hermes_model: str
    hermes_maximum_total_cost_usd: Decimal
    maximum_dispatches: int = 12

    def __post_init__(self) -> None:
        assert_local_captain_test_dsn(self.test_mariadb_dsn)
        workspace = self.workspace_root.resolve()
        python = self.python_executable.resolve()
        if not workspace.is_dir() or not python.is_file():
            raise ValueError("Factory live operator paths are unavailable")
        if len(set(self.job_ids)) != 2:
            raise ValueError("Factory live operator requires two distinct jobs")
        if (
            self.hermes_provider != "openai-api"
            or not self.hermes_model.strip()
            or not self.hermes_maximum_total_cost_usd.is_finite()
            or self.hermes_maximum_total_cost_usd <= 0
            or self.hermes_maximum_total_cost_usd > Decimal("0.10")
        ):
            raise ValueError("Factory Hermes pin or cost allocation is invalid")
        if self.maximum_dispatches < 1 or self.maximum_dispatches > 24:
            raise ValueError("Factory live dispatch bound is invalid")


class _ContentAddressedInputMaterializer:
    def __init__(self, store: BusinessBenchmarkContentAddressedArtifactStore) -> None:
        self._store = store

    def materialize(self, reference: ArtifactRef) -> Path:
        return self._store.local_path(reference)


class _LazyProductionBenchmarkInputs:
    """Create the provider/n8n graph only when Quality Warden is dispatched."""

    def __init__(
        self,
        *,
        settings: Mapping[UUID, LiveBusinessBenchmarkSettings],
        loader: GatewayBusinessBenchmarkLiveCompositionLoader,
    ) -> None:
        self._settings = dict(settings)
        self._loader = loader
        self._compositions: dict[UUID, _DispatchInputComposition] = {}

    def resolve(self, request: FactoryDispatch) -> BusinessBenchmarkDispatchInputs | None:
        selected = self._settings.get(request.job.job_id)
        if selected is None:
            return None
        composition = self._compositions.get(request.job.job_id)
        if composition is None:
            raw = self._loader(selected)
            dispatch_inputs = getattr(raw, "dispatch_inputs", None)
            if not callable(dispatch_inputs):
                raise ValueError(
                    "production benchmark composition lacks dispatch inputs"
                )
            composition = raw  # type: ignore[assignment]
            self._compositions[request.job.job_id] = composition
        return composition.dispatch_inputs(selected, request)


def compose_business_demo_factory_operator(
    settings: FactoryLiveOperatorSettings,
    *,
    environment: Mapping[str, str],
    n8n_client: httpx.AsyncClient,
    clock: Callable[[], datetime] | None = None,
) -> AgentFactoryLiveComposition:
    """Compose every real port without dispatching Hermes, Forge, n8n, or OpenAI."""

    current_time = clock or (lambda: datetime.now(timezone.utc))
    workspace = settings.workspace_root.resolve()
    authority_root = (
        workspace / ".captain-cook" / "private" / "business-benchmarks"
    ).resolve()
    configured_root = Path(
        _required(environment, "CAPTAIN_BENCHMARK_AUTHORITY_ROOT")
    ).resolve()
    if configured_root != authority_root:
        raise ValueError("Factory operator benchmark authority root does not match")
    aggregate = LiveBusinessBenchmarkSettings.from_environment(
        environment,
        repository_root=workspace,
    )
    if aggregate.profile != "all" or {
        item.job_id for item in aggregate.selections
    } != set(settings.job_ids):
        raise ValueError("Factory operator selections do not match the requested jobs")
    if aggregate.model != settings.hermes_model:
        raise ValueError("Hermes and AutoGen must use the same Captain-allowed model")
    if not environment.get("OPENAI_API_KEY", "").strip():
        raise ValueError("Factory provider secret is not present in the process")

    store = GatewayStore(MariaDBStorage(settings.test_mariadb_dsn))
    repository = GatewayFactoryRepository(store)
    creation_artifact_root = (
        workspace / ".captain-cook" / "minibook-creation-cas"
    ).resolve()
    candidate_bindings = GatewayForgeCandidateProvider(
        repository=repository,
        artifacts=GatewayMinibookCreationArtifactStore(creation_artifact_root),
    )
    mapper = CaptainCreationJobMapper(evidence=repository)
    benchmark_cas = BusinessBenchmarkContentAddressedArtifactStore(
        authority_root / "cas"
    )
    codex_workspace_root = (
        workspace / ".captain-cook" / "private" / "codex-workspaces"
    ).resolve()
    codex_state_root = (authority_root / "runtime-state" / "codex").resolve()
    codex_executable = Path(
        _required(environment, "CAPTAIN_CODEX_EXECUTABLE")
    ).resolve(strict=True)
    pwsh_executable = Path(
        _required(environment, "CAPTAIN_PWSH_EXECUTABLE")
    ).resolve(strict=True)
    codex_home = Path(_required(environment, "CAPTAIN_CODEX_HOME")).resolve(
        strict=True
    )
    codex_session_script = (workspace / "scripts" / "codex-session.ps1").resolve(
        strict=True
    )
    codex_authorizer = CodexExecutionPolicy(
        workspace_root=codex_workspace_root,
        environment=environment,
    )

    def codex_runner_factory(
        *,
        session_id: str,
        state_path: Path,
        maximum_runtime_seconds: int,
    ) -> PowerShellCodexRunner:
        return PowerShellCodexRunner(
            pwsh_path=pwsh_executable,
            script_path=codex_session_script,
            codex_path=codex_executable,
            session_id=session_id,
            state_path=state_path,
            artifact_references=(),
            codex_home=codex_home,
            timeout_seconds=maximum_runtime_seconds,
        )

    codex_sealer = CaptainCodexBuildSealer(
        executor=CodexCliFactoryBuildExecutor(
            settings=CodexCliFactoryBuildSettings(
                state_root=codex_state_root,
                maximum_runtime_seconds=900,
            ),
            workspace_preparer=GitDetachedFactoryWorkspacePreparer(
                repository_root=workspace,
                workspaces_root=codex_workspace_root,
            ),
            artifact_reader=benchmark_cas,
            authorizer=codex_authorizer,
            runner_factory=codex_runner_factory,
            clock=current_time,
        ),
        issuer=CaptainCodexBuildReceiptIssuer(
            CodexBuildArtifactCas(
                workspace / ".captain-cook" / "private" / "codex-build-cas"
            )
        ),
    )
    forge = MinibookSwarmForge(
        materializer=_ContentAddressedInputMaterializer(benchmark_cas),
        mapper=mapper,
        skill_receipts=mapper,
        settings=MinibookForgeSettings(
            python_executable=str(settings.python_executable.resolve()),
            swarm_script=workspace / "minibook" / "autogen_swarm.py",
            working_directory=workspace,
            max_runtime_seconds=1800,
            artifact_root=creation_artifact_root,
        ),
    )
    technical_ports = GatewayTechnicalTeamExecutionPortsProvider.from_environment(
        environment=environment,
        store=store,
        candidate_bindings=candidate_bindings,
        authority_root=authority_root,
        skill_root=workspace / "agenten" / "agent_factory" / "skills",
        clock=current_time,
    )
    benchmark_loader = GatewayBusinessBenchmarkLiveCompositionLoader(
        environment=environment,
        n8n_client=n8n_client,
        clock=current_time,
    )
    benchmark_settings = {
        item.job_id: aggregate.for_selection(item)
        for item in aggregate.selections
    }
    benchmark_inputs = _LazyProductionBenchmarkInputs(
        settings=benchmark_settings,
        loader=benchmark_loader,
    )
    suite_repository = CaptainCanonicalSuiteRepository(
        CaptainCanonicalSuiteAuthority(
            root=authority_root / "suites",
            seed_version_id=_required(
                environment,
                "CAPTAIN_BENCHMARK_SEED_VERSION_ID",
            ),
        )
    )
    renewal = next(
        item for item in aggregate.selections if item.profile == "renewal"
    )
    return compose_agent_factory_live(
        store=store,
        forge=forge,
        candidate_bindings=candidate_bindings,
        team_execution_ports_for=technical_ports,
        business_benchmark_repository=suite_repository,
        business_benchmark_inputs=benchmark_inputs,
        workspace_namespace="business-benchmark-factory-v3",
        evidence_root=authority_root / "runtime-state" / "factory-evidence",
        hermes_settings=HermesCliSettings(
            executable=str(settings.python_executable.resolve()),
            skill_root=workspace / "agenten" / "agent_factory" / "skills",
            timeout_seconds=900,
            evidence_root=authority_root / "runtime-state" / "hermes-evidence",
            released_skill_root=(
                workspace / "agenten" / "agent_factory" / "released-skills"
            ),
            module_root=workspace / "hermes-agent",
            provider=settings.hermes_provider,
            model=settings.hermes_model,
            maximum_total_cost_usd=settings.hermes_maximum_total_cost_usd,
        ),
        clock=current_time,
        n8n_work_batches={
            renewal.job_id: _required(
                environment,
                "CAPTAIN_BENCHMARK_RENEWAL_BATCH_ID",
            )
        },
        holdout_selector=select_technical_business_holdout,
        codex_build_sealer=codex_sealer,
    )


async def run_business_demo_factory_jobs(
    settings: FactoryLiveOperatorSettings,
    *,
    environment: Mapping[str, str],
) -> tuple[ProductionFactoryDispatchResult, ProductionFactoryDispatchResult]:
    async with httpx.AsyncClient(timeout=30.0) as n8n_client:
        composition = compose_business_demo_factory_operator(
            settings,
            environment=environment,
            n8n_client=n8n_client,
        )
        results = tuple(
            [
                await composition.run(
                    job_id,
                    maximum_dispatches=settings.maximum_dispatches,
                )
                for job_id in settings.job_ids
            ]
        )
    return results  # type: ignore[return-value]


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise ValueError(f"required Factory operator setting is missing: {name}")
    return value


__all__ = [
    "FactoryLiveOperatorSettings",
    "compose_business_demo_factory_operator",
    "run_business_demo_factory_jobs",
]
