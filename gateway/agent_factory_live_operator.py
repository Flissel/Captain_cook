"""Deployable composition for resuming the isolated business-demo Factory jobs."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
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
from agenten.agent_factory.business_benchmark_store import (
    FilesystemBusinessBenchmarkEvidenceStore,
)
from agenten.agent_factory.candidate_evaluation import (
    GatewayForgeCandidateProvider,
    ResolvedFactoryCandidate,
)
from agenten.agent_factory.contracts import AgentFactoryJobV3
from agenten.agent_factory.contracts import FactoryRole
from agenten.agent_factory.codex_build_execution import (
    CaptainCodexAssignmentBuilder,
    CaptainCodexBuildSealer,
    CaptainFactoryCodexResumeAuthorizer,
    CaptainV3CodexRequestMapper,
    CodexCliFactoryBuildExecutor,
    CodexCliFactoryBuildSettings,
    GitDetachedFactoryWorkspacePreparer,
    PowerShellFactoryCodexProcessInspector,
)
from agenten.agent_factory.codex_build_recovery import (
    FilesystemFactoryCodexBuildCheckpointStore,
    FilesystemFactoryCodexScaffoldManifestStore,
    FilesystemFactoryCodexSealedEvidenceStore,
)
from agenten.agent_factory.codex_build_provenance import (
    CaptainCodexBuildReceiptIssuer,
    CodexBuildArtifactCas,
)
from agenten.agent_factory.evidence_store import FilesystemFactoryEvidenceStore
from agenten.agent_factory.hermes_cli import HermesCliSettings
from agenten.agent_factory.hermes_factory_worker import (
    AuthenticatedMinibookV3WorkerClient,
    FilesystemHermesFactoryWorkerCheckpointStore,
    HermesFactoryWorker,
)
from agenten.agent_factory.minibook_forge import (
    CaptainCreationJobMapper,
    CaptainCreationJobV3Authority,
    CaptainCreationJobV3Mapper,
    FilesystemCaptainCreationJobV3Store,
    MinibookForgeHttpClient,
    MinibookForgeHttpSettings,
    MinibookForgeSettings,
    MinibookSwarmForge,
)
from agenten.agent_factory.orchestration import FactoryDispatch, FactoryDispatchError
from agenten.agent_factory.technical_revalidation import (
    FilesystemFactoryTechnicalRevalidationAuthority,
    TECHNICAL_REVALIDATION_EVALUATOR_PATHS,
    TECHNICAL_REVALIDATION_RUNTIME_PATHS,
)
from agenten.agent_factory.state_machine import FactoryActionKind
from agenten.agent_factory.capability_resolution import CapabilityResolution
from agenten.agent_factory.production_dispatch_runner import (
    ProductionFactoryDispatchResult,
)
from agenten.agent_runtime.contracts import ArtifactRef
from agenten.execution.codex_policy import CodexExecutionPolicy
from agenten.execution.codex_supervisor import PowerShellCodexRunner
from gateway.agent_factory_live_composition import (
    AgentFactoryLiveComposition,
    GatewayTechnicalTeamExecutionPortsProvider,
    HermesFactoryWorkerRuntime,
    compose_agent_factory_live,
    select_technical_business_holdout,
)
from gateway.business_benchmark_live_composition import (
    GatewayForgeBusinessBenchmarkCandidateAuthority,
    GatewayBusinessBenchmarkLiveCompositionLoader,
)
from gateway.factory_repository import (
    GatewayFactoryBudgetLedger,
    GatewayFactoryLeases,
    GatewayFactoryRepository,
)
from gateway.factory_forge_evidence import CaptainForgeEvidenceBridge
from gateway.factory_runtime_retry_authority import (
    FilesystemFactoryRuntimeRetryAuthority,
)
from gateway.factory_improvement_authority import (
    FilesystemFactoryImprovementAuthority,
)
from gateway.factory_hermes_retry_authority import (
    FilesystemFactoryHermesRetryAuthority,
)
from gateway.minibook_creation_artifacts import GatewayMinibookCreationArtifactStore
from gateway.store import GatewayStore


class _DispatchInputComposition(Protocol):
    def dispatch_inputs(
        self,
        settings: LiveBusinessBenchmarkSettings,
        request: FactoryDispatch,
    ) -> BusinessBenchmarkDispatchInputs: ...


class _BenchmarkCodexPromptArtifactStore:
    def __init__(self, artifacts: BusinessBenchmarkContentAddressedArtifactStore) -> None:
        self._artifacts = artifacts

    def persist(self, job_id: UUID, content: bytes) -> ArtifactRef:
        del job_id
        return self._artifacts.put(
            content,
            "application/json",
            namespace="codex-brief-prompt",
        )


class _FactoryCodexArtifactReader:
    """Route immutable inputs to their owning CAS and reject foreign refs."""

    def __init__(
        self,
        *,
        benchmark_artifacts: BusinessBenchmarkContentAddressedArtifactStore,
        minibook_creation_artifacts: GatewayMinibookCreationArtifactStore,
    ) -> None:
        self._benchmark_artifacts = benchmark_artifacts
        self._minibook_creation_artifacts = minibook_creation_artifacts

    def read_bytes(self, reference: ArtifactRef) -> bytes:
        if reference.uri.startswith("artifact://business-benchmark-production/"):
            return self._benchmark_artifacts.read_bytes(reference)
        if reference.uri.startswith("artifact://minibook-creation/"):
            return self._minibook_creation_artifacts.read_bytes(reference)
        raise ValueError("artifact reference has no recognized Factory owner")


def validate_factory_total_cost_envelope(
    *,
    environment: Mapping[str, str],
    benchmark_maximum_usd_per_team: tuple[Decimal, ...],
    benchmark_profiles: tuple[str, ...] = ("claims", "renewal"),
) -> None:
    """Reject any demo composition that could exceed its total team reserve."""

    if environment.get("CAPTAIN_CODEX_AUTH_MODE", "").strip() != (
        "chatgpt_subscription"
    ):
        raise ValueError("Factory Codex must use ChatGPT subscription authentication")
    if (
        environment.get("CAPTAIN_FACTORY_HERMES_PROVIDER", "").strip()
        != "openai-api"
        or environment.get("CAPTAIN_FACTORY_HERMES_MODEL", "").strip()
        != "gpt-5.6-terra"
        or environment.get(
            "CAPTAIN_FACTORY_HERMES_REASONING_EFFORT", ""
        ).strip()
        != "high"
    ):
        raise ValueError("Factory cloud Hermes route is invalid")
    try:
        user_maximum_eur = Decimal(
            _required(environment, "CAPTAIN_FACTORY_USER_MAX_EUR_PER_TEAM")
        )
        budget_eur_per_usd = Decimal(
            _required(environment, "CAPTAIN_FACTORY_BUDGET_EUR_PER_USD")
        )
        total_maximum_usd = Decimal(
            _required(
                environment,
                "CAPTAIN_FACTORY_MAX_TOTAL_COST_USD_PER_TEAM",
            )
        )
        codex_metered_usd = Decimal(
            _required(
                environment,
                "CAPTAIN_FACTORY_CODEX_METERED_USD_PER_TEAM",
            )
        )
        hermes_incremental_usd = Decimal(
            _required(
                environment,
                "CAPTAIN_FACTORY_HERMES_INCREMENTAL_MAX_USD",
            )
        )
        unresolved_hermes_effect_reserve_usd = Decimal(
            _required(
                environment,
                "CAPTAIN_FACTORY_HERMES_UNRESOLVED_EFFECT_RESERVE_USD",
            )
        )
        prior_actual_usd_by_profile = {
            "claims": Decimal(
                _required(
                    environment,
                    "CAPTAIN_FACTORY_PRIOR_ACTUAL_USD_CLAIMS",
                )
            ),
            "renewal": Decimal(
                _required(
                    environment,
                    "CAPTAIN_FACTORY_PRIOR_ACTUAL_USD_RENEWAL",
                )
            ),
        }
    except ArithmeticError as exc:
        raise ValueError("Factory total cost envelope is invalid") from exc
    values = (
        user_maximum_eur,
        budget_eur_per_usd,
        total_maximum_usd,
        codex_metered_usd,
        hermes_incremental_usd,
        unresolved_hermes_effect_reserve_usd,
        *prior_actual_usd_by_profile.values(),
        *benchmark_maximum_usd_per_team,
    )
    selected_prior_actual_usd = tuple(
        prior_actual_usd_by_profile.get(profile, Decimal("NaN"))
        for profile in benchmark_profiles
    )
    if (
        any(not value.is_finite() or value < 0 for value in values)
        or user_maximum_eur != Decimal("6.00")
        or budget_eur_per_usd < Decimal("1.00")
        or total_maximum_usd <= 0
        or total_maximum_usd > Decimal("4.80")
        or total_maximum_usd * budget_eur_per_usd > user_maximum_eur
        or codex_metered_usd != 0
        or hermes_incremental_usd != Decimal("1.50")
        or unresolved_hermes_effect_reserve_usd != Decimal("0.25")
        or len(benchmark_maximum_usd_per_team) not in {1, 2}
        or len(benchmark_profiles) != len(benchmark_maximum_usd_per_team)
        or len(set(benchmark_profiles)) != len(benchmark_profiles)
        or any(profile not in prior_actual_usd_by_profile for profile in benchmark_profiles)
        or any(
            benchmark_usd
            + hermes_incremental_usd
            + prior_usd
            + codex_metered_usd
            > total_maximum_usd
            for benchmark_usd, prior_usd in zip(
                benchmark_maximum_usd_per_team,
                selected_prior_actual_usd,
                strict=True,
            )
        )
    ):
        raise ValueError("Factory total cost envelope is invalid")


class _CaptainV3CreationAuthorityBuilder:
    """Reconstruct exact V3 mapping inputs from Captain ledger and private CAS."""

    def __init__(
        self,
        *,
        repository: GatewayFactoryRepository,
        artifacts: BusinessBenchmarkContentAddressedArtifactStore,
    ) -> None:
        self._repository = repository
        self._artifacts = artifacts

    def __call__(self, request: FactoryDispatch) -> CaptainCreationJobV3Authority:
        if not isinstance(request.job, AgentFactoryJobV3) or request.lease is None:
            raise FactoryDispatchError("V3 authority requires an active Captain dispatch")
        job = request.job
        attempt = request.action.attempt
        blocks = tuple(
            block
            for block in self._repository.blocks(job.job_id)
            if block.job_id == job.job_id
            and block.correlation_id == job.correlation_id
            and block.subject_version == job.subject_version
            and block.attempt == attempt
            and block.status.value == "succeeded"
        )
        if not any(block.phase.value == "forge_requested" for block in blocks):
            raise FactoryDispatchError("V3 authority is missing the durable capability miss")

        def unique_ref(phase: str) -> ArtifactRef:
            candidates = tuple(
                block.artifact_refs[0]
                for block in blocks
                if block.phase.value == phase and len(block.artifact_refs) == 1
            )
            if len(candidates) != 1 or candidates[0].media_type != "application/json":
                raise FactoryDispatchError(f"V3 authority requires one durable {phase} artifact")
            return candidates[0]

        blueprint_ref = unique_ref("blueprint_created")
        tool_plan_ref = unique_ref("tool_candidate_tested")
        try:
            compiled_bytes = self._artifacts.read_bytes(job.compiled_spec_ref)
            compiled = json.loads(compiled_bytes)
            if not isinstance(compiled, dict):
                raise ValueError
            required_agent_ids = tuple(compiled["required_agent_ids"])
            ordered_handoffs = tuple(compiled["ordered_handoffs"])
            tool_requirements = tuple(compiled["tool_requirements"])
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise FactoryDispatchError("V3 compiled topology authority is unavailable") from exc
        lease_bytes = json.dumps(
            request.lease.model_dump(mode="json", by_alias=True),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        lease_ref = self._artifacts.put(
            lease_bytes, "application/json", namespace="factory-v3-lease"
        )
        workspace_policy_ref = self._artifacts.put(
            json.dumps(
                {
                    "schema": "captain.factory-v3-workspace-policy.v1",
                    "workspace_ref": request.lease.workspace_ref,
                    "execution_policy": job.execution_policy.model_dump(mode="json", by_alias=True),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
            "application/json",
            namespace="factory-v3-workspace-policy",
        )
        creation_material = "\x1f".join(
            (
                job.required_capability,
                str(job.subject_version),
                job.input_ref.sha256,
                job.compiled_spec_ref.sha256,
                job.dependency_graph_ref.sha256,
                *job.acceptance_assertion_ids,
            )
        )
        return CaptainCreationJobV3Authority(
            command_id=job.event_id,
            capability_resolution=CapabilityResolution(
                kind="create",
                creation_key="factory-create-"
                + hashlib.sha256(creation_material.encode("utf-8")).hexdigest(),
            ),
            compiled_spec_ref=job.compiled_spec_ref,
            lease_ref=lease_ref,
            blueprint_ref=blueprint_ref,
            tool_plan_ref=tool_plan_ref,
            required_agent_ids=required_agent_ids,
            ordered_handoffs=ordered_handoffs,
            tool_requirements=tool_requirements,
            workspace_policy_ref=workspace_policy_ref,
            clarification_policy={
                "allowed_request_types": (
                    "clarification",
                    "credential_required",
                    "external_api_approval",
                    "n8n_approval",
                ),
                "max_requests": 2,
            },
        )


@dataclass(frozen=True)
class FactoryLiveOperatorSettings:
    workspace_root: Path
    python_executable: Path
    hermes_python_executable: Path
    test_mariadb_dsn: str
    job_ids: tuple[UUID, ...]
    hermes_provider: str
    hermes_model: str
    hermes_maximum_total_cost_usd: Decimal
    hermes_reasoning_effort: str = "high"
    maximum_dispatches: int = 12
    stop_before_quality_warden: bool = False
    factory_v3_enabled: bool = False

    def __post_init__(self) -> None:
        assert_local_captain_test_dsn(self.test_mariadb_dsn)
        workspace = self.workspace_root.resolve()
        python = self.python_executable.resolve()
        hermes_python = self.hermes_python_executable.resolve()
        if (
            not workspace.is_dir()
            or not python.is_file()
            or not hermes_python.is_file()
        ):
            raise ValueError("Factory live operator paths are unavailable")
        _assert_hermes_python_runtime(
            hermes_python,
            module_root=workspace / "hermes-agent",
        )
        if len(self.job_ids) not in {1, 2}:
            raise ValueError("Factory live operator requires one or two distinct jobs")
        if len(set(self.job_ids)) != len(self.job_ids):
            raise ValueError("Factory live operator requires distinct jobs")
        if (
            self.hermes_provider not in {"openai-api", "custom"}
            or not self.hermes_model.strip()
            or self.hermes_reasoning_effort != "high"
            or (
                self.hermes_provider == "custom"
                and self.hermes_model != "captain-hermes:8b"
            )
            or not self.hermes_maximum_total_cost_usd.is_finite()
            or self.hermes_maximum_total_cost_usd <= 0
            or self.hermes_maximum_total_cost_usd > Decimal("1.50")
        ):
            raise ValueError("Factory Hermes pin or cost allocation is invalid")
        if self.maximum_dispatches < 1 or self.maximum_dispatches > 24:
            raise ValueError("Factory live dispatch bound is invalid")


class _FactoryInputMaterializer:
    """Route only Captain-owned artifacts to their exact verified CAS."""

    def __init__(
        self,
        *,
        benchmark_artifacts: BusinessBenchmarkContentAddressedArtifactStore,
        codex_build_artifacts: CodexBuildArtifactCas,
    ) -> None:
        self._benchmark_artifacts = benchmark_artifacts
        self._codex_build_artifacts = codex_build_artifacts

    def materialize(self, reference: ArtifactRef) -> Path:
        if reference.uri.startswith("artifact://business-benchmark-production/"):
            self._benchmark_artifacts.read_bytes(reference)
            return self._benchmark_artifacts.local_path(reference)
        if reference.uri.startswith("artifact://captain-codex-build/"):
            self._codex_build_artifacts.read_bytes(reference)
            return self._codex_build_artifacts.local_path(reference)
        raise FactoryDispatchError("factory input artifact owner is unsupported")


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


def _canonical_business_benchmark_repository(
    *,
    authority_root: Path,
    seed_version_id: str,
) -> CaptainCanonicalSuiteRepository:
    return CaptainCanonicalSuiteRepository(
        CaptainCanonicalSuiteAuthority(
            root=authority_root / "suites",
            seed_version_id=seed_version_id,
        ),
        FilesystemBusinessBenchmarkEvidenceStore(
            authority_root / "runtime-state" / "benchmark-receipts"
        ),
    )


def _suite_scoped_hermes_evidence_root(
    *,
    authority_root: Path,
    seed_version_id: str,
) -> Path:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", seed_version_id) is None:
        raise ValueError("Hermes evidence scope requires a safe suite identifier")
    return (
        authority_root.resolve()
        / "runtime-state"
        / f"hermes-evidence-{seed_version_id}"
    )


def compose_business_demo_factory_operator(
    settings: FactoryLiveOperatorSettings,
    *,
    environment: Mapping[str, str],
    n8n_client: httpx.AsyncClient,
    clock: Callable[[], datetime] | None = None,
    hermes_factory_worker: HermesFactoryWorker | None = None,
    minibook_client: httpx.AsyncClient | None = None,
) -> AgentFactoryLiveComposition:
    """Compose every real port without dispatching Hermes, Forge, n8n, or OpenAI."""

    current_time = clock or (lambda: datetime.now(timezone.utc))
    if (settings.factory_v3_enabled and minibook_client is None) or (
        not settings.factory_v3_enabled
        and (hermes_factory_worker is not None or minibook_client is not None)
    ):
        raise ValueError("Factory V3 worker configuration is incomplete")
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
    if {item.job_id for item in aggregate.selections} != set(settings.job_ids):
        raise ValueError("Factory operator selections do not match the requested jobs")
    if (
        environment.get("CAPTAIN_FACTORY_HERMES_PROVIDER")
        != settings.hermes_provider
        or environment.get("CAPTAIN_FACTORY_HERMES_MODEL")
        != settings.hermes_model
    ):
        raise ValueError("Factory Hermes route does not match operator settings")
    validate_factory_total_cost_envelope(
        environment=environment,
        benchmark_maximum_usd_per_team=tuple(
            item.maximum_usd for item in aggregate.selections
        ),
        benchmark_profiles=tuple(item.profile for item in aggregate.selections),
    )
    if not environment.get("OPENAI_API_KEY", "").strip():
        raise ValueError("Factory provider secret is not present in the process")

    store = GatewayStore(MariaDBStorage(settings.test_mariadb_dsn))
    budget_ledger = GatewayFactoryBudgetLedger(store)
    codex_state_root = (authority_root / "runtime-state" / "codex").resolve()
    runtime_retry_authority = FilesystemFactoryRuntimeRetryAuthority(
        authority_root=(
            authority_root / "runtime-state" / "runtime-retry-authorizations"
        ),
        checkpoint_root=codex_state_root / "checkpoints",
    )
    improvement_authority = FilesystemFactoryImprovementAuthority(
        authority_root / "runtime-state" / "improvement-authorizations"
    )
    hermes_retry_authority = FilesystemFactoryHermesRetryAuthority(
        authority_root / "runtime-state" / "hermes-retry-authorizations"
    )
    repository = GatewayFactoryRepository(
        store,
        runtime_retries=runtime_retry_authority,
    )
    creation_artifact_root = (
        workspace / ".captain-cook" / "minibook-creation-cas"
    ).resolve()
    creation_artifacts = GatewayMinibookCreationArtifactStore(
        creation_artifact_root
    )
    v3_authority_store = (
        FilesystemCaptainCreationJobV3Store(
            authority_root / "runtime-state" / "factory-v3-authority"
        )
        if settings.factory_v3_enabled
        else None
    )
    candidate_bindings = GatewayForgeCandidateProvider(
        repository=repository,
        artifacts=creation_artifacts,
        v3_jobs=v3_authority_store,
    )
    technical_revalidation_authority = (
        FilesystemFactoryTechnicalRevalidationAuthority(
            authority_root
            / "runtime-state"
            / "technical-revalidation-authorizations",
            repository_root=workspace,
            runtime_paths=TECHNICAL_REVALIDATION_RUNTIME_PATHS,
            evaluator_paths=TECHNICAL_REVALIDATION_EVALUATOR_PATHS,
        )
    )
    def validate_technical_revalidation(request: FactoryDispatch) -> None:
        if not isinstance(request.job, AgentFactoryJobV3):
            raise FactoryDispatchError("technical revalidation requires a V3 job")
        resolved = candidate_bindings.candidate_for(request.job)
        if not isinstance(resolved, ResolvedFactoryCandidate):
            raise FactoryDispatchError("technical revalidation candidate is unavailable")
        technical_revalidation_authority.active(
            job=request.job,
            action=request.action,
            budget=budget_ledger.projection(request.job.job_id),
            now=current_time(),
            code_revision=_git_revision(workspace),
            candidate_ref=resolved.candidate.source_archive_ref,
            holdout_ref=select_technical_business_holdout(request.job),
        )
    factory_evidence_root = (
        authority_root / "runtime-state" / "factory-evidence"
    ).resolve()
    benchmark_cas = BusinessBenchmarkContentAddressedArtifactStore(
        authority_root / "cas"
    )
    legacy_mapper = CaptainCreationJobMapper(
        evidence=CaptainForgeEvidenceBridge(
            repository=repository,
            evidence_store=FilesystemFactoryEvidenceStore(factory_evidence_root),
        )
    )
    mapper = (
        CaptainCreationJobV3Mapper(
            authority=_CaptainV3CreationAuthorityBuilder(
                repository=repository,
                artifacts=benchmark_cas,
            ),
            released_skill=lambda job, step: repository.released_for(job, step),
            store=v3_authority_store,
        )
        if v3_authority_store is not None
        else legacy_mapper
    )
    codex_workspace_root = (
        workspace / ".captain-cook" / "private" / "codex-workspaces"
    ).resolve()
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
        journal_path: Path,
        maximum_runtime_seconds: int,
        deadline_at: datetime,
    ) -> PowerShellCodexRunner:
        return PowerShellCodexRunner(
            pwsh_path=pwsh_executable,
            script_path=codex_session_script,
            codex_path=codex_executable,
            session_id=session_id,
            state_path=state_path,
            journal_path=journal_path,
            artifact_references=(),
            codex_home=codex_home,
            deadline_at=deadline_at,
            timeout_seconds=maximum_runtime_seconds,
        )

    codex_build_cas = CodexBuildArtifactCas(
        workspace / ".captain-cook" / "private" / "codex-build-cas"
    )
    codex_cli_settings = CodexCliFactoryBuildSettings(
        state_root=codex_state_root,
        maximum_runtime_seconds=900,
        maximum_files=4_096,
        maximum_provider_cost_usd=float(
            settings.hermes_maximum_total_cost_usd
        ),
        supported_output_roots=("output",),
        supported_commands=(
            ("python", "-m", "pytest"),
            ("pytest",),
            ("python", "-m", "compileall", "output"),
        ),
    )
    codex_receipt_issuer = CaptainCodexBuildReceiptIssuer(codex_build_cas)
    def codex_sealer_for(
        execution_settings: CodexCliFactoryBuildSettings,
    ) -> CaptainCodexBuildSealer:
        return CaptainCodexBuildSealer(
            executor=CodexCliFactoryBuildExecutor(
                settings=execution_settings,
                workspace_preparer=GitDetachedFactoryWorkspacePreparer(
                    repository_root=workspace,
                    workspaces_root=codex_workspace_root,
                ),
                artifact_reader=_FactoryCodexArtifactReader(
                    benchmark_artifacts=benchmark_cas,
                    minibook_creation_artifacts=creation_artifacts,
                ),
                authorizer=codex_authorizer,
                runner_factory=codex_runner_factory,
                checkpoint_store=FilesystemFactoryCodexBuildCheckpointStore(
                    codex_state_root / "checkpoints"
                ),
                scaffold_manifest_store=FilesystemFactoryCodexScaffoldManifestStore(
                    codex_state_root / "scaffold-manifests"
                ),
                sealed_evidence_store=FilesystemFactoryCodexSealedEvidenceStore(
                    codex_state_root / "sealed-evidence"
                ),
                resume_authorizer=CaptainFactoryCodexResumeAuthorizer(
                    clock=current_time
                ),
                process_inspector=PowerShellFactoryCodexProcessInspector(
                    pwsh_path=pwsh_executable,
                    script_path=codex_session_script,
                ),
                clock=current_time,
            ),
            issuer=codex_receipt_issuer,
        )

    codex_sealer = codex_sealer_for(codex_cli_settings)
    if settings.factory_v3_enabled:
        assert minibook_client is not None
        forge = MinibookForgeHttpClient(
            mapper=mapper,
            settings=MinibookForgeHttpSettings(
                base_url=_required(environment, "CAPTAIN_FACTORY_V3_MINIBOOK_URL").rstrip("/"),
                api_key=_required(environment, "CAPTAIN_FACTORY_V3_CREATION_API_KEY"),
            ),
            client=minibook_client,
        )
    else:
        forge = MinibookSwarmForge(
            materializer=_FactoryInputMaterializer(
                benchmark_artifacts=benchmark_cas,
                codex_build_artifacts=codex_build_cas,
            ),
            mapper=legacy_mapper,
            skill_receipts=legacy_mapper,
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
        candidate_authority=GatewayForgeBusinessBenchmarkCandidateAuthority(
            candidate_bindings
        ),
    )
    benchmark_settings = {
        item.job_id: aggregate.for_selection(item)
        for item in aggregate.selections
    }
    benchmark_inputs = _LazyProductionBenchmarkInputs(
        settings=benchmark_settings,
        loader=benchmark_loader,
    )
    seed_version_id = _required(
        environment,
        "CAPTAIN_BENCHMARK_SEED_VERSION_ID",
    )
    suite_repository = _canonical_business_benchmark_repository(
        authority_root=authority_root,
        seed_version_id=seed_version_id,
    )
    n8n_work_batches = {
        item.job_id: _required(
            environment,
            "CAPTAIN_BENCHMARK_RENEWAL_BATCH_ID",
        )
        for item in aggregate.selections
        if item.profile == "renewal"
    }
    codex_prompt_store = _BenchmarkCodexPromptArtifactStore(benchmark_cas)
    worker_runtime: HermesFactoryWorkerRuntime | None = None
    if settings.factory_v3_enabled and hermes_factory_worker is None:
        assert minibook_client is not None
        configured_minibook_url = _required(
            environment,
            "CAPTAIN_FACTORY_V3_MINIBOOK_URL",
        ).rstrip("/")
        if str(minibook_client.base_url).rstrip("/") != configured_minibook_url:
            raise ValueError("Factory V3 Minibook client URL does not match")
        worker_id = _required(environment, "CAPTAIN_FACTORY_V3_WORKER_ID")
        runtime_skill_name = _required(
            environment,
            "CAPTAIN_FACTORY_V3_RELEASED_SKILL_NAME",
        )
        runtime_skill_sha256 = _required(
            environment,
            "CAPTAIN_FACTORY_V3_RELEASED_SKILL_SHA256",
        )
        checkpoint_store = FilesystemHermesFactoryWorkerCheckpointStore(
            codex_state_root / "hermes-worker-checkpoints"
        )
        minibook = AuthenticatedMinibookV3WorkerClient(
            client=minibook_client,
            worker_token=_required(
                environment,
                "CAPTAIN_FACTORY_V3_WORKER_TOKEN",
            ),
            worker_id=worker_id,
            checkpoint_store=checkpoint_store,
        )
        leases = GatewayFactoryLeases(store)
        assignment_mapper = CaptainV3CodexRequestMapper(
            job_for=lambda job_id: repository.job(job_id),
            lease_for=lambda job, attempt, now: leases.active(
                job,
                FactoryRole.TOOL_INTEGRATOR,
                attempt,
                now,
            ),
            released_skill_for=lambda job, step: repository.released_for(
                job,
                step,
            ),
            prompt_store=codex_prompt_store,
            artifact_cas=codex_build_cas,
            worker_id=worker_id,
            runtime_skill_sha256=runtime_skill_sha256,
            clock=current_time,
            runtime_retry_for=lambda job, action, now: (
                runtime_retry_authority.active(
                    job,
                    action,
                    store.factory_job(job.job_id),  # type: ignore[arg-type]
                    now,
                )
            ),
        )
        codex_builder = CaptainCodexAssignmentBuilder(
            sealer_for=codex_sealer_for,
            mapper=assignment_mapper,
            settings=codex_cli_settings,
        )
        worker_runtime = HermesFactoryWorkerRuntime(
            minibook=minibook,
            codex=codex_builder,
            checkpoint_store=checkpoint_store,
            codex_receipts=codex_receipt_issuer,
            worker_id=worker_id,
            released_skill_name=runtime_skill_name,
            released_skill_sha256=runtime_skill_sha256,
            retry_authority=codex_builder,
        )
    return compose_agent_factory_live(
        store=store,
        forge=forge,
        candidate_bindings=candidate_bindings,
        team_execution_ports_for=technical_ports,
        business_benchmark_repository=suite_repository,
        business_benchmark_inputs=benchmark_inputs,
        workspace_namespace="business-benchmark-factory-v3",
        evidence_root=factory_evidence_root,
        hermes_settings=HermesCliSettings(
            executable=str(settings.hermes_python_executable.resolve()),
            skill_root=workspace / "agenten" / "agent_factory" / "skills",
            timeout_seconds=900,
            evidence_root=_suite_scoped_hermes_evidence_root(
                authority_root=authority_root,
                seed_version_id=seed_version_id,
            ),
            released_skill_root=(
                workspace / "agenten" / "agent_factory" / "released-skills"
            ),
            module_root=workspace / "hermes-agent",
            working_directory=workspace,
            provider=settings.hermes_provider,
            model=settings.hermes_model,
            reasoning_effort=settings.hermes_reasoning_effort,
            maximum_total_cost_usd=settings.hermes_maximum_total_cost_usd,
            unresolved_effect_reserve_usd=Decimal(
                _required(
                    environment,
                    "CAPTAIN_FACTORY_HERMES_UNRESOLVED_EFFECT_RESERVE_USD",
                )
            ),
        ),
        clock=current_time,
        n8n_work_batches=n8n_work_batches,
        holdout_selector=select_technical_business_holdout,
        codex_build_sealer=codex_sealer,
        codex_prompt_artifact_store=codex_prompt_store,
        improvements=improvement_authority,
        runtime_retries=runtime_retry_authority,
        hermes_retry_authority=hermes_retry_authority,
        technical_revalidation_validator=validate_technical_revalidation,
        factory_v3_enabled=settings.factory_v3_enabled,
        hermes_factory_worker=hermes_factory_worker,
        hermes_factory_worker_runtime=worker_runtime,
    )


async def run_business_demo_factory_jobs(
    settings: FactoryLiveOperatorSettings,
    *,
    environment: Mapping[str, str],
) -> tuple[ProductionFactoryDispatchResult, ...]:
    stop_before_action = (
        FactoryActionKind.DISPATCH_QUALITY_WARDEN
        if settings.stop_before_quality_warden
        else None
    )
    async with AsyncExitStack() as stack:
        n8n_client = await stack.enter_async_context(
            httpx.AsyncClient(timeout=30.0)
        )
        minibook_client = None
        if getattr(settings, "factory_v3_enabled", False):
            minibook_client = await stack.enter_async_context(
                httpx.AsyncClient(
                    timeout=30.0,
                    base_url=_required(
                        environment,
                        "CAPTAIN_FACTORY_V3_MINIBOOK_URL",
                    ),
                )
            )
        composition = compose_business_demo_factory_operator(
            settings,
            environment=environment,
            n8n_client=n8n_client,
            minibook_client=minibook_client,
        )
        results = tuple(
            [
                await composition.run(
                    job_id,
                    maximum_dispatches=settings.maximum_dispatches,
                    stop_before_action=stop_before_action,
                )
                for job_id in settings.job_ids
            ]
        )
    return results


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise ValueError(f"required Factory operator setting is missing: {name}")
    return value


def _git_revision(workspace: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    revision = completed.stdout.strip().lower()
    if completed.returncode != 0 or len(revision) != 40 or any(
        character not in "0123456789abcdef" for character in revision
    ):
        raise ValueError("Factory operator Git revision is unavailable")
    return revision


def _assert_hermes_python_runtime(
    python_executable: Path,
    *,
    module_root: Path,
) -> None:
    environment = os.environ.copy()
    environment.pop("OPENAI_API_KEY", None)
    environment["PYTHONPATH"] = str(module_root.resolve())
    environment["CAPTAIN_EXPECTED_HERMES_MODULE_ROOT"] = str(
        module_root.resolve()
    )
    probe = subprocess.run(
        [
            str(python_executable),
            "-c",
            (
                "import os; from pathlib import Path; "
                "import concurrent_log_handler, hermes_cli; "
                "Path(hermes_cli.__file__).resolve().relative_to("
                "Path(os.environ['CAPTAIN_EXPECTED_HERMES_MODULE_ROOT']).resolve())"
            ),
        ],
        cwd=module_root.parent,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if probe.returncode != 0:
        raise ValueError("dedicated Hermes Python runtime is incomplete")


__all__ = [
    "FactoryLiveOperatorSettings",
    "compose_business_demo_factory_operator",
    "run_business_demo_factory_jobs",
    "validate_factory_total_cost_envelope",
]
