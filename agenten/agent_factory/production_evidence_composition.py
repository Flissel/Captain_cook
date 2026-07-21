"""Production-only composition for Package-C V3 release evidence.

This module creates persistence, budget, team-execution, and controlled-
recovery components.  External provider/candidate/holdout/n8n adapters remain
explicit typed ports: their absence is a configuration error, never synthetic
evidence or an offline fallback.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from uuid import UUID

from autogen_core.models import ChatCompletionClient

from agenten.agent_factory.capability_controlled_recovery import (
    ContentAddressedControlledRecoveryEffectStore,
    DurableGatewayFactoryLiveEffectLedger,
    FactoryLiveControlledRecoveryPort,
    PreparedControlledRecoveryTeamDispatcher,
    build_production_controlled_recovery_port,
)
from agenten.agent_factory.capability_live_adapters import (
    ContentAddressedArtifactStore,
)
from agenten.agent_factory.capability_v3_evidence_bridge import (
    CapabilityCandidateAttestationPort,
    CapabilityCandidateProviderPort,
    CapabilityReleasedSkillSourcePort,
    CapabilityV3AuthorityPort,
    CapabilityV3EvidenceBuilderContext,
    PackageCV3CapabilityEvidenceBackend,
    build_capability_evidence_backend,
)
from agenten.agent_factory.contracts import AgentFactoryJobV3, FactoryLease
from agenten.agent_factory.evidence_store import FilesystemFactoryEvidenceStore
from agenten.agent_factory.execution_budget import FactoryUsageReceiptV1
from agenten.agent_factory.execution_policy import (
    FactoryExecutionMode,
    FactoryExecutionPolicyV1,
    FactoryLiveCapability,
    FactorySandboxMode,
)
from agenten.agent_factory.hermes_cli import (
    FilesystemFactorySkillReplayStore,
    skill_directory_digest,
)
from agenten.agent_factory.live_holdouts import (
    CaptainPrivateHoldoutAdapter,
    CaptainPrivateHoldoutEvaluationPort,
    CaptainPrivateHoldoutSourcePort,
)
from agenten.agent_factory.live_pricing import (
    CaptainPricingAuthorityAdapter,
    FactoryPricingQuoteSourcePort,
)
from agenten.agent_factory.skill_evaluation import ReleasedHermesSkill
from agenten.agent_factory.skill_workflow_contracts import (
    FACTORY_SKILL_ID_BY_STEP,
    FactorySkillInvocationV1,
    FactorySkillStep,
)
from agenten.agent_factory.team_execution import (
    FactoryLiveTeamExecutionPorts,
    FactoryN8nGrantAuthorityPort,
    FactoryN8nToolAdapterPort,
    TeamExecutionCandidateAdapter,
    compose_live_team_execution,
)
from agenten.agent_runtime.contracts import ArtifactRef
from gateway.factory_repository import (
    GatewayFactoryBudgetLedger,
    GatewayFactoryLiveEffectLedger,
    GatewayFactoryRepository,
)
from gateway.production_store import build_local_captain_test_gateway_store
from gateway.store import GatewayStore


class ProductionV3EvidenceConfigurationError(RuntimeError):
    """A required live dependency is absent or outside Captain authority."""


def _todo(name: str) -> ProductionV3EvidenceConfigurationError:
    return ProductionV3EvidenceConfigurationError(
        f"TODO_TOOL.v1 required capability=configuration; name={name}"
    )


@dataclass(frozen=True)
class ProductionV3EvidenceSettings:
    """Non-secret validated settings for one local live-evidence runtime."""

    gateway_dsn: str = field(repr=False)
    gateway_host: str
    database_name: str
    artifact_root: Path
    skill_root: Path
    workspace_ref: str
    provider: str
    model: str
    max_cost_per_call: Decimal
    execution_policy: FactoryExecutionPolicyV1


@dataclass(frozen=True)
class ProductionV3HoldoutPorts:
    """Private holdout authorities scoped to exactly one V3 job."""

    holdout_source: CaptainPrivateHoldoutSourcePort
    holdout_evaluator: CaptainPrivateHoldoutEvaluationPort


@dataclass(frozen=True)
class ProductionV3N8nPorts:
    """n8n effect authorities scoped to exactly one V3 job."""

    n8n_adapter: FactoryN8nToolAdapterPort
    n8n_authority: FactoryN8nGrantAuthorityPort


@dataclass(frozen=True)
class ProductionV3EvidenceExternalPorts:
    """External authorities that must be concrete before provider-live startup."""

    candidate_provider: CapabilityCandidateProviderPort
    candidate_attestation: CapabilityCandidateAttestationPort
    model_client_for: Callable[
        [AgentFactoryJobV3, FactorySkillInvocationV1], ChatCompletionClient
    ]
    pricing_source: FactoryPricingQuoteSourcePort
    holdout_source: CaptainPrivateHoldoutSourcePort | None
    holdout_evaluator: CaptainPrivateHoldoutEvaluationPort | None
    n8n_adapter: FactoryN8nToolAdapterPort | None
    n8n_authority: FactoryN8nGrantAuthorityPort | None
    tools: Mapping[str, Callable[..., Any]]
    holdout_ports_for: (
        Callable[[AgentFactoryJobV3], ProductionV3HoldoutPorts] | None
    ) = None
    n8n_ports_for: (
        Callable[[AgentFactoryJobV3], ProductionV3N8nPorts] | None
    ) = None

    def __post_init__(self) -> None:
        required = (
            ("candidate_provider", self.candidate_provider),
            ("candidate_attestation", self.candidate_attestation),
            ("model_client_for", self.model_client_for),
            ("pricing_source", self.pricing_source),
            ("tools", self.tools),
        )
        for name, value in required:
            if value is None:
                raise ValueError(f"production V3 external port is missing: {name}")
        if self.holdout_ports_for is None:
            for name, value in (
                ("holdout_source", self.holdout_source),
                ("holdout_evaluator", self.holdout_evaluator),
            ):
                if value is None:
                    raise ValueError(
                        f"production V3 external port is missing: {name}"
                    )
        if self.n8n_ports_for is None:
            for name, value in (
                ("n8n_adapter", self.n8n_adapter),
                ("n8n_authority", self.n8n_authority),
            ):
                if value is None:
                    raise ValueError(
                        f"production V3 external port is missing: {name}"
                    )


class GatewayCapabilityV3Authority(CapabilityV3AuthorityPort):
    """Join the V3 repository and lease writer without a second authority."""

    def __init__(self, store: GatewayStore) -> None:
        self._store = store
        self._repository = GatewayFactoryRepository(store)

    @property
    def repository(self) -> GatewayFactoryRepository:
        return self._repository

    def register(self, job: AgentFactoryJobV3) -> None:
        self._repository.register(job)

    def job(self, job_id: UUID) -> AgentFactoryJobV3:
        job = self._repository.job(job_id)
        if not isinstance(job, AgentFactoryJobV3):
            raise ValueError("Gateway V3 authority returned a non-V3 job")
        return job

    def seed_released_skill_assignments(
        self,
        job: AgentFactoryJobV3,
        source: CapabilityReleasedSkillSourcePort,
    ) -> None:
        self._repository.seed_released_skill_assignments(job, source)

    def released_for(
        self,
        job: AgentFactoryJobV3,
        step: FactorySkillStep,
    ) -> ReleasedHermesSkill:
        return self._repository.released_for(job, step)

    def record_lease(self, lease: FactoryLease) -> None:
        self._store.record_factory_lease(lease)

    def usage_receipts(
        self,
        job_id: UUID,
    ) -> tuple[FactoryUsageReceiptV1, ...]:
        return self._repository.workflow_usage_receipts(job_id)


class DirectoryReleasedSkillSource:
    """Bind the six checked-out Hermes skill directories to a V3 job."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        for skill_id in FACTORY_SKILL_ID_BY_STEP.values():
            if not (self._root / skill_id / "SKILL.md").is_file():
                raise _todo(f"released_skill:{skill_id}")

    def released_for(
        self,
        job: AgentFactoryJobV3,
        step: FactorySkillStep,
    ) -> ReleasedHermesSkill:
        skill_id = FACTORY_SKILL_ID_BY_STEP[step]
        digest = skill_directory_digest(self._root / skill_id)
        return ReleasedHermesSkill(
            schema_name="captain.released-hermes-skill.v1",
            skill_id=skill_id,
            version=1,
            capability=job.required_capability,
            content_ref=ArtifactRef(
                uri=f"artifact://released-factory-skill/{skill_id}/{digest}",
                sha256=digest,
                media_type="application/vnd.captain.hermes-skill-directory",
            ),
            content_sha256=digest,
            status="released",
            released_at=job.occurred_at,
            producer="captain",
        )


@dataclass(frozen=True)
class ProductionV3EvidenceRuntime:
    """Concrete graph plus inspectable authority markers for runtime startup."""

    backend: PackageCV3CapabilityEvidenceBackend
    context: CapabilityV3EvidenceBuilderContext
    controlled_recovery_ledger: DurableGatewayFactoryLiveEffectLedger


GatewayStoreFactory = Callable[[str, Callable[[], datetime]], GatewayStore]


def load_production_v3_evidence_settings(
    environ: Mapping[str, str],
) -> ProductionV3EvidenceSettings:
    """Parse only explicit, non-fallback live configuration."""

    dsn = _required(environ, "TEST_MARIADB_DSN")
    parsed = urlparse(dsn)
    database_name = unquote(parsed.path.lstrip("/"))
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme not in {"mysql", "mariadb"}
        or host not in {"127.0.0.1", "localhost", "::1"}
        or database_name != "captain_test"
        or not parsed.username
    ):
        raise _todo("TEST_MARIADB_DSN:local_captain_test")
    artifact_root = _absolute_directory(
        _required(environ, "CAPTAIN_RUNTIME_ARTIFACT_ROOT"),
        "CAPTAIN_RUNTIME_ARTIFACT_ROOT",
    )
    skill_root = _absolute_directory(
        _required(environ, "CAPTAIN_FACTORY_SKILL_ROOT"),
        "CAPTAIN_FACTORY_SKILL_ROOT",
    )
    workspace_ref = _required(environ, "CAPTAIN_FACTORY_WORKSPACE_REF")
    if not workspace_ref.startswith("workspace://"):
        raise _todo("CAPTAIN_FACTORY_WORKSPACE_REF")
    provider = _required(environ, "CAPTAIN_FACTORY_PROVIDER")
    model = _required(environ, "CAPTAIN_FACTORY_MODEL")
    total_cost = _positive_money(environ, "CAPTAIN_FACTORY_MAX_COST_USD")
    per_call = _positive_money(environ, "CAPTAIN_FACTORY_MAX_COST_PER_CALL_USD")
    if per_call > total_cost:
        raise _todo("CAPTAIN_FACTORY_MAX_COST_PER_CALL_USD")
    runtime_seconds = _positive_integer(environ, "CAPTAIN_FACTORY_RUNTIME_SECONDS")
    try:
        policy = FactoryExecutionPolicyV1(
            schema_name="captain.factory-execution-policy.v1",
            mode=FactoryExecutionMode.RELEASE,
            live_execution=True,
            max_cost_usd=total_cost,
            max_runtime_seconds=runtime_seconds,
            required_live_runs=3,
            allowed_models=(model,),
            live_capabilities=(
                FactoryLiveCapability.MODEL_INVOKE,
                FactoryLiveCapability.CAPTAIN_TEST_DATABASE,
                FactoryLiveCapability.DOCKER_RUN,
            ),
            sandbox_mode=FactorySandboxMode.ISOLATED_DANGER_FULL_ACCESS,
        )
    except (TypeError, ValueError) as exc:
        raise _todo("CAPTAIN_FACTORY_EXECUTION_POLICY") from exc
    return ProductionV3EvidenceSettings(
        gateway_dsn=dsn,
        gateway_host=host,
        database_name=database_name,
        artifact_root=artifact_root,
        skill_root=skill_root,
        workspace_ref=workspace_ref,
        provider=provider,
        model=model,
        max_cost_per_call=per_call,
        execution_policy=policy,
    )


def build_production_v3_evidence_backend_from_environment(
    environ: Mapping[str, str],
    *,
    external_ports: ProductionV3EvidenceExternalPorts | None,
    gateway_store_factory: GatewayStoreFactory | None = None,
    clock: Callable[[], datetime] | None = None,
) -> ProductionV3EvidenceRuntime:
    """Build the real 8091 evidence backend without executing an external effect."""

    if external_ports is None:
        raise ProductionV3EvidenceConfigurationError(
            "TODO_TOOL.v1 required capability=production_v3_external_ports; "
            "reason=provider candidate holdout pricing and n8n authorities are absent"
        )
    settings = load_production_v3_evidence_settings(environ)
    resolved_clock = clock or (lambda: datetime.now(timezone.utc))
    released_skills = DirectoryReleasedSkillSource(settings.skill_root)
    store_factory = gateway_store_factory or _gateway_store
    store = store_factory(settings.gateway_dsn, resolved_clock)
    authority = GatewayCapabilityV3Authority(store)
    artifacts = ContentAddressedArtifactStore(settings.artifact_root)
    evidence_store = FilesystemFactoryEvidenceStore(
        settings.artifact_root / "v3-team-evidence"
    )
    replay_store = FilesystemFactorySkillReplayStore(
        settings.artifact_root / "v3-team-replays"
    )
    budget = GatewayFactoryBudgetLedger(store)
    pricing = CaptainPricingAuthorityAdapter(external_ports.pricing_source)
    adapters: dict[UUID, TeamExecutionCandidateAdapter] = {}

    def adapter_for(job: AgentFactoryJobV3) -> TeamExecutionCandidateAdapter:
        existing = adapters.get(job.job_id)
        if existing is not None:
            return existing
        holdout_ports = (
            external_ports.holdout_ports_for(job)
            if external_ports.holdout_ports_for is not None
            else ProductionV3HoldoutPorts(
                holdout_source=external_ports.holdout_source,  # type: ignore[arg-type]
                holdout_evaluator=external_ports.holdout_evaluator,  # type: ignore[arg-type]
            )
        )
        n8n_ports = (
            external_ports.n8n_ports_for(job)
            if external_ports.n8n_ports_for is not None
            else ProductionV3N8nPorts(
                n8n_adapter=external_ports.n8n_adapter,  # type: ignore[arg-type]
                n8n_authority=external_ports.n8n_authority,  # type: ignore[arg-type]
            )
        )
        adapter = compose_live_team_execution(
            job=job,
            evidence_store=evidence_store,
            ports=FactoryLiveTeamExecutionPorts(
                model_client_for=external_ports.model_client_for,
                budget=budget,
                pricing_authority=pricing,
                replay_store=replay_store,
                holdouts=CaptainPrivateHoldoutAdapter(
                    job=job,
                    source=holdout_ports.holdout_source,
                    evaluator=holdout_ports.holdout_evaluator,
                ),
                n8n_adapter=n8n_ports.n8n_adapter,
                n8n_authority=n8n_ports.n8n_authority,
                released_skill_catalog=authority.repository,
                skill_root=settings.skill_root,
                tools=external_ports.tools,
                provider=settings.provider,
                model=settings.model,
                max_cost_per_call=settings.max_cost_per_call,
                clock=resolved_clock,
            ),
        )
        adapters[job.job_id] = adapter
        return adapter

    team_execution = TeamExecutionCandidateAdapter(
        service_for=lambda job, invocation: adapter_for(job).service_for(
            job, invocation
        ),
        invocation_for=lambda dispatch: adapter_for(dispatch.job).invocation_for(
            dispatch
        ),
    )
    prepared = PreparedControlledRecoveryTeamDispatcher(
        team_execution=team_execution,
        service_for=team_execution.service_for,
        production_ready=True,
    )
    durable_ledger = DurableGatewayFactoryLiveEffectLedger(
        GatewayFactoryLiveEffectLedger(store)
    )
    recovery: FactoryLiveControlledRecoveryPort = (
        build_production_controlled_recovery_port(
            repository=authority.repository,
            effect_ledger=durable_ledger,
            prepared_dispatch=prepared,
            effect_store=ContentAddressedControlledRecoveryEffectStore(artifacts),
            clock=resolved_clock,
        )
    )
    context = CapabilityV3EvidenceBuilderContext(
        authority=authority,
        released_skills=released_skills,
        candidate_provider=external_ports.candidate_provider,
        team_execution=team_execution,
        controlled_recovery=recovery,
        candidate_attestation=external_ports.candidate_attestation,
        artifact_store=artifacts,
        execution_policy=settings.execution_policy,
        workspace_ref=settings.workspace_ref,
        clock=resolved_clock,
    )
    return ProductionV3EvidenceRuntime(
        backend=build_capability_evidence_backend(context=context),
        context=context,
        controlled_recovery_ledger=durable_ledger,
    )


def _gateway_store(dsn: str, clock: Callable[[], datetime]) -> GatewayStore:
    return build_local_captain_test_gateway_store(dsn, clock=clock)


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise _todo(name)
    return value


def _absolute_directory(raw: str, name: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        raise _todo(name)
    return path.resolve()


def _positive_money(environ: Mapping[str, str], name: str) -> Decimal:
    raw = _required(environ, name)
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise _todo(name) from exc
    if (
        not value.is_finite()
        or value <= 0
        or value.as_tuple().exponent < -2
    ):
        raise _todo(name)
    return value


def _positive_integer(environ: Mapping[str, str], name: str) -> int:
    raw = _required(environ, name)
    try:
        value = int(raw)
    except ValueError as exc:
        raise _todo(name) from exc
    if str(value) != raw or not 1 <= value <= 86400:
        raise _todo(name)
    return value
