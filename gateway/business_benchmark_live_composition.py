"""Gateway-owned provider-live composition for Captain business benchmarks."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import HTTPException
import httpx

from agenten.agent_factory.business_benchmark_bootstrap import (
    CaptainClaimsBusinessBenchmarkExecutorBuilder,
    CaptainRenewalBusinessBenchmarkExecutorBuilder,
    CaptainRenewalBusinessBenchmarkN8nPorts,
    ConfiguredBusinessBenchmarkExecutionPolicyBuilder,
    ProductionBusinessBenchmarkBootstrapConfig,
    ProductionBusinessBenchmarkRuntimeAuthorities,
)
from agenten.agent_factory.business_benchmark_execution import (
    BusinessBenchmarkExecutorPort,
)
from agenten.agent_factory.business_benchmark_live import (
    LiveBusinessBenchmarkSettings,
    ProductionAdapterUnavailableError,
    ProductionBusinessBenchmarkCompositionPort,
)
from agenten.agent_factory.business_benchmark_production import (
    BusinessBenchmarkCandidateAuthorityPort,
    ProductionBusinessBenchmarkScope,
)
from agenten.agent_factory.business_benchmark_production_ports import (
    BusinessBenchmarkContentAddressedArtifactStore,
    BusinessBenchmarkPricingAuthority,
    ConfiguredBusinessBenchmarkPricingSource,
    OpenAIBusinessBenchmarkModelClientBuilder,
)
from agenten.agent_factory.business_benchmark_runtime import (
    BusinessBenchmarkSessionRequestV1,
)
from agenten.agent_factory.contracts import AgentFactoryJobV3
from agenten.agent_factory.candidate_evaluation import ResolvedFactoryCandidate
from agenten.agent_factory.n8n_tools import OpaqueN8nToolReference
from agenten.agent_factory.skill_workflow_contracts import (
    FactorySkillInvocationV1,
    FactorySkillStep,
)
from agenten.agent_factory.team_execution import (
    CaptainN8nGrantAuthority,
    CaptainReleasedSkillAuthority,
    FactoryN8nToolAuthorizationV1,
)
from agenten.agent_factory.evidence_store import FilesystemFactoryEvidenceStore
from agenten.agent_runtime.capabilities import derive_grant
from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeCommandPayload,
    ArtifactRef,
    CapabilityGrant,
    CapabilityGrantRevocation,
    CapabilityProfile,
    IntegrationIntent,
    RuntimeLimits,
    RuntimeOperation,
)
from agenten.agent_runtime.n8n_mcp_broker import McpLeaseIssuer
from agenten.agent_runtime.n8n_endpoint import resolve_n8n_endpoint
from agenten.validation.contracts import WorkBatch
from gateway.business_benchmark_composition import (
    GatewayBusinessBenchmarkCompositionAuthority,
)
from gateway.contracts import RuntimeOperationProjection, RuntimeWriteReceipt


class GatewayForgeCandidateResolverPort(Protocol):
    def candidate_for(self, job: AgentFactoryJobV3) -> ResolvedFactoryCandidate: ...


class GatewayForgeBusinessBenchmarkCandidateAuthority:
    """Use the exact SHA-verified Forge candidate recorded by Gateway."""

    def __init__(self, candidates: GatewayForgeCandidateResolverPort) -> None:
        self._candidates = candidates

    def resolve(
        self,
        *,
        job: AgentFactoryJobV3,
        expected_candidate_id: str,
        expected_candidate_ref: ArtifactRef,
    ) -> ResolvedFactoryCandidate:
        candidate = self._candidates.candidate_for(job)
        if (
            not isinstance(candidate, ResolvedFactoryCandidate)
            or candidate.candidate.candidate_id != expected_candidate_id
            or candidate.candidate.source_archive_ref != expected_candidate_ref
        ):
            raise ValueError(
                "current Forge candidate does not match Gateway benchmark evidence"
            )
        return candidate


_POWERSHELL_CANONICAL_SHA256 = r"""
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
function ConvertTo-SortedValue {
    param([AllowNull()][object]$Value)
    if ($null -eq $Value) { return $null }
    if ($Value -is [System.Collections.IDictionary]) {
        $ordered = [ordered]@{}
        foreach ($key in @($Value.Keys | ForEach-Object { [string]$_ } | Sort-Object)) {
            $ordered[$key] = ConvertTo-SortedValue -Value $Value[$key]
        }
        return $ordered
    }
    if ($Value -is [pscustomobject]) {
        $ordered = [ordered]@{}
        $names = @($Value.PSObject.Properties | ForEach-Object { $_.Name } | Sort-Object)
        foreach ($name in $names) {
            $ordered[$name] = ConvertTo-SortedValue -Value $Value.PSObject.Properties[$name].Value
        }
        return $ordered
    }
    if ($Value -is [System.Collections.IEnumerable] -and $Value -isnot [string]) {
        return @($Value | ForEach-Object { ConvertTo-SortedValue -Value $_ })
    }
    return $Value
}
$value = [Console]::In.ReadToEnd() | ConvertFrom-Json -Depth 32
$json = (ConvertTo-SortedValue -Value $value) | ConvertTo-Json -Compress -Depth 32
$hash = [Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($json))
[Console]::Out.Write([Convert]::ToHexString($hash).ToLowerInvariant())
""".strip()


class GatewayRuntimeStorePort(Protocol):
    def bundle(self, batch_id: str) -> dict[str, object]: ...

    def runtime_operation(self, operation_id: UUID) -> RuntimeOperationProjection: ...

    def accept_runtime_command(
        self, command: AgentRuntimeCommand
    ) -> RuntimeWriteReceipt: ...

    def record_capability_grant(self, grant: CapabilityGrant) -> RuntimeWriteReceipt: ...


@dataclass(frozen=True)
class GatewayRenewalN8nDeploymentBinding:
    """Verified immutable deployment and activation evidence for one workflow."""

    workflow_id: str
    workflow_ref: ArtifactRef
    published_sha256: str
    canonical_payload_sha256: str

    @classmethod
    def from_evidence_root(
        cls,
        *,
        evidence_root: Path,
        canonical_workflow_path: Path,
    ) -> "GatewayRenewalN8nDeploymentBinding":
        canonical_path = canonical_workflow_path.resolve()
        if not canonical_path.is_file():
            raise ValueError("canonical Renewal n8n workflow is missing")
        canonical_bytes = canonical_path.read_bytes()
        canonical_sha256 = hashlib.sha256(canonical_bytes).hexdigest()
        canonical = _read_json_object(canonical_path)
        workflow_name = canonical.get("name")
        if not isinstance(workflow_name, str) or not workflow_name.strip():
            raise ValueError("canonical Renewal n8n workflow name is invalid")
        publish_fields = ("name", "nodes", "connections", "settings")
        if any(field not in canonical for field in publish_fields):
            raise ValueError("canonical Renewal n8n workflow payload is invalid")
        expected_published_payload = {
            field: canonical[field] for field in publish_fields
        }

        root = evidence_root.resolve()
        deployment_root = root / "renewal-context-n8n-deployments"
        activation_root = root / "renewal-context-n8n-activations"
        if not deployment_root.is_dir() or not activation_root.is_dir():
            raise ValueError("Renewal n8n deployment receipt is missing")
        deployment_paths = tuple(sorted(deployment_root.glob("*.json")))
        activation_paths = tuple(sorted(activation_root.glob("*.json")))
        if not deployment_paths or not activation_paths:
            raise ValueError("Renewal n8n deployment receipt is missing")

        activations: dict[str, dict[str, object]] = {}
        for path in activation_paths:
            receipt = _read_json_object(path)
            published_sha256 = receipt.get("published_sha256")
            if not _is_sha256(published_sha256) or path.stem != published_sha256:
                raise ValueError("Renewal n8n deployment receipt is invalid")
            if published_sha256 in activations:
                raise ValueError("Renewal n8n deployment receipt is ambiguous")
            activations[published_sha256] = receipt

        matches: list[GatewayRenewalN8nDeploymentBinding] = []
        for path in deployment_paths:
            deployment = _read_json_object(path)
            published_sha256 = deployment.get("published_sha256")
            if not _is_sha256(published_sha256) or path.stem != published_sha256:
                raise ValueError("Renewal n8n deployment receipt is invalid")
            activation = activations.get(published_sha256)
            if activation is None:
                continue
            workflow_id = deployment.get("workflow_id")
            ownership_sha256 = deployment.get("ownership_binding_sha256")
            published_payload = deployment.get("published_payload")
            if (
                deployment.get("schema")
                != "captain.business-benchmark-renewal-n8n-deployment-receipt.v1"
                or deployment.get("verification") != "provider_read_back_matched"
                or deployment.get("workflow_name") != workflow_name
                or not isinstance(deployment.get("canonical_sha256"), str)
                or not isinstance(workflow_id, str)
                or not workflow_id.strip()
                or not isinstance(ownership_sha256, str)
                or not isinstance(published_payload, dict)
                or published_payload.get("name") != workflow_name
                or _contains_secret_field(published_payload)
                or activation.get("schema")
                != "captain.business-benchmark-renewal-n8n-activation-receipt.v1"
                or activation.get("workflow_id") != workflow_id
                or activation.get("workflow_name") != workflow_name
                or activation.get("published_sha256") != published_sha256
                or activation.get("ownership_binding_sha256") != ownership_sha256
                or activation.get("status") != "active"
                or ownership_sha256
                != hashlib.sha256(
                    (
                        "captain.business-benchmark-renewal-n8n.v1|"
                        f"{workflow_name}|{workflow_id}"
                    ).encode("utf-8")
                ).hexdigest()
            ):
                raise ValueError("Renewal n8n deployment receipt is invalid")
            if _deployment_payload_sha256(published_payload) != published_sha256:
                raise ValueError("Renewal n8n deployment receipt is invalid")
            if deployment["canonical_sha256"] != canonical_sha256:
                continue
            if published_payload != expected_published_payload:
                raise ValueError("Renewal n8n deployment receipt is invalid")
            matches.append(
                cls(
                    workflow_id=workflow_id,
                    workflow_ref=ArtifactRef(
                        uri=(
                            "artifact://business-benchmark-production/"
                            f"candidate-workflow/{canonical_sha256}"
                        ),
                        sha256=canonical_sha256,
                        media_type="application/json",
                    ),
                    published_sha256=published_sha256,
                    canonical_payload_sha256=hashlib.sha256(
                        json.dumps(
                            expected_published_payload,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest(),
                )
            )
        if len(matches) != 1:
            raise ValueError("Renewal n8n deployment receipt is ambiguous or missing")
        return matches[0]


class GatewayRenewalN8nRuntimeAuthority:
    """Own Renewal runtime command, grant, and broker-lease authority."""

    def __init__(
        self,
        *,
        store: GatewayRuntimeStorePort,
        batch: WorkBatch,
        workspace_ref: str,
        endpoint_identity: str,
        broker_issuer: McpLeaseIssuer,
        clock: Callable[[], datetime],
    ) -> None:
        self._store = store
        self._batch = batch
        self._workspace_ref = workspace_ref
        self._endpoint_identity = endpoint_identity
        self._broker_issuer = broker_issuer
        self._clock = clock

    @classmethod
    def from_gateway_store(
        cls,
        *,
        store: GatewayRuntimeStorePort,
        batch_id: str,
        workspace_ref: str,
        endpoint_identity: str,
        broker_signing_secret: str,
        clock: Callable[[], datetime],
    ) -> "GatewayRenewalN8nRuntimeAuthority":
        try:
            batch = WorkBatch.model_validate(store.bundle(batch_id))
        except Exception as exc:
            raise ValueError(
                "Captain Renewal benchmark requires a released WorkBatch"
            ) from exc
        if (
            "renewal_context_read" not in batch.subtask_ids
            or CapabilityProfile.N8N_BUILDER.value not in batch.capability_tags
        ):
            raise ValueError(
                "Captain Renewal benchmark WorkBatch lacks the n8n work node"
            )
        if not workspace_ref.startswith("workspace://"):
            raise ValueError("Renewal benchmark workspace must be opaque")
        return cls(
            store=store,
            batch=batch,
            workspace_ref=workspace_ref,
            endpoint_identity=endpoint_identity,
            broker_issuer=McpLeaseIssuer(broker_signing_secret),
            clock=clock,
        )

    def authorization_for(
        self,
        *,
        job: AgentFactoryJobV3,
        invocation: FactorySkillInvocationV1,
        request: BusinessBenchmarkSessionRequestV1,
        tool_reference: OpaqueN8nToolReference,
    ) -> FactoryN8nToolAuthorizationV1:
        identity = request.identity
        if (
            invocation.step is not FactorySkillStep.EXECUTE_TEAM
            or invocation.job_id != job.job_id
            or invocation.correlation_id != job.correlation_id
            or invocation.subject_version != job.subject_version
            or identity.job_id != job.job_id
            or identity.correlation_id != job.correlation_id
            or identity.subject_version != job.subject_version
            or identity.attempt != invocation.attempt
            or identity.invocation_id != invocation.invocation_id
            or request.benchmark_case_sha256 != identity.case_sha256
            or tool_reference.tool_name not in self._batch.subtask_ids
        ):
            raise ValueError("Renewal n8n request is outside Captain authority")
        now = _utc(self._clock())
        command_id = _runtime_command_id(
            job_id=job.job_id,
            invocation_id=invocation.invocation_id,
            case_sha256=request.benchmark_case_sha256,
            tool_reference=tool_reference,
        )
        try:
            operation = self._store.runtime_operation(command_id)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise ValueError("Captain Renewal runtime state is unavailable") from exc
            operation = None
        if operation is None:
            tool_sha256 = _tool_reference_sha256(tool_reference)
            command = AgentRuntimeCommand(
                schema_name="captain.agent-runtime-command.v1",
                event_id=command_id,
                correlation_id=job.correlation_id,
                causation_id=invocation.invocation_id,
                occurred_at=now,
                producer="captain",
                subject_id=tool_reference.tool_name,
                subject_version=job.subject_version,
                payload=AgentRuntimeCommandPayload(
                    operation=RuntimeOperation.CODEX_RUN,
                    project_id=str(job.job_id),
                    batch_id=self._batch.batch_id,
                    subtask_id=tool_reference.tool_name,
                    workspace_ref=self._workspace_ref,
                    prompt_ref=ArtifactRef(
                        uri=(
                            "artifact://business-benchmark-runtime/n8n-tool/"
                            f"{tool_sha256}"
                        ),
                        sha256=tool_sha256,
                        media_type="application/json",
                    ),
                    integration_intent=IntegrationIntent.N8N,
                    capability_profile=CapabilityProfile.N8N_BUILDER,
                    limits=RuntimeLimits(
                        wall_seconds=max(
                            1,
                            min(3600, math.ceil(request.maximum_latency_ms / 1000)),
                        ),
                        max_iterations=1,
                    ),
                ),
            )
            grant = derive_grant(command, self._batch, now)
            self._store.accept_runtime_command(command)
            self._store.record_capability_grant(grant)
            operation = self._store.runtime_operation(command_id)
        return self._authorization_from_operation(
            operation,
            job=job,
            invocation=invocation,
            request=request,
            tool_reference=tool_reference,
            now=now,
        )

    def broker_token_for(self, request: object) -> str:
        now = _utc(self._clock())
        command_id = getattr(request, "runtime_command_id")
        operation = self._store.runtime_operation(command_id)
        command = operation.command
        grant = operation.grant
        tool = getattr(request, "tool")
        expected_id = _runtime_command_id(
            job_id=getattr(request, "job_id"),
            invocation_id=getattr(request, "invocation_id"),
            case_sha256=getattr(request, "case_sha256"),
            tool_reference=tool,
        )
        if (
            grant is None
            or command.event_id != expected_id
            or command.event_id != command_id
            or command.payload.project_id != str(getattr(request, "job_id"))
            or command.causation_id != getattr(request, "invocation_id")
            or command.correlation_id != getattr(request, "correlation_id")
            or command.subject_version != getattr(request, "subject_version")
            or command.payload.workspace_ref != getattr(request, "workspace_ref")
            or grant.grant_id != getattr(request, "grant_id")
        ):
            raise ValueError("Renewal MCP lease request is not Gateway-authorized")
        return self._broker_issuer.issue(
            grant,
            command,
            self._endpoint_identity,
            now,
        )

    async def get_grant(self, command_id: UUID) -> CapabilityGrant | None:
        operation = self._runtime_operation_or_none(command_id)
        return operation.grant if operation is not None else None

    async def get_grant_revocation(
        self, command_id: UUID
    ) -> CapabilityGrantRevocation | None:
        operation = self._runtime_operation_or_none(command_id)
        return operation.revocation if operation is not None else None

    def _runtime_operation_or_none(
        self, command_id: UUID
    ) -> RuntimeOperationProjection | None:
        try:
            return self._store.runtime_operation(command_id)
        except HTTPException as exc:
            if exc.status_code == 404:
                return None
            raise ValueError("Captain Renewal runtime state is unavailable") from exc

    def _authorization_from_operation(
        self,
        operation: RuntimeOperationProjection,
        *,
        job: AgentFactoryJobV3,
        invocation: FactorySkillInvocationV1,
        request: BusinessBenchmarkSessionRequestV1,
        tool_reference: OpaqueN8nToolReference,
        now: datetime,
    ) -> FactoryN8nToolAuthorizationV1:
        command = operation.command
        grant = operation.grant
        if grant is None:
            raise ValueError("Captain Renewal runtime grant is missing")
        claim = FactoryN8nToolAuthorizationV1(
            tool_name=tool_reference.tool_name,
            approved_tool_ref=tool_reference,
            runtime_command=command,
            capability_grant=grant,
        )
        if (
            command.event_id != operation.operation_id
            or command.correlation_id != job.correlation_id
            or command.causation_id != invocation.invocation_id
            or command.subject_version != job.subject_version
            or command.payload.project_id != str(job.job_id)
            or command.payload.batch_id != self._batch.batch_id
            or command.payload.workspace_ref != self._workspace_ref
            or command.payload.integration_intent is not IntegrationIntent.N8N
            or command.payload.capability_profile is not CapabilityProfile.N8N_BUILDER
            or command.payload.prompt_ref.sha256
            != _tool_reference_sha256(tool_reference)
            or grant.expires_at <= now
            or request.benchmark_case_sha256 != request.identity.case_sha256
        ):
            raise ValueError("Captain Renewal runtime operation is not canonical")
        return claim


class GatewayProfiledBusinessBenchmarkExecutorBuilder:
    def __init__(
        self,
        *,
        claims: CaptainClaimsBusinessBenchmarkExecutorBuilder,
        renewal: Callable[
            [ProductionBusinessBenchmarkScope, ProductionBusinessBenchmarkRuntimeAuthorities],
            BusinessBenchmarkExecutorPort,
        ]
        | None = None,
    ) -> None:
        self._claims = claims
        self._renewal = renewal

    @property
    def profiles(self) -> tuple[str, ...]:
        return ("claims", "renewal") if self._renewal is not None else ("claims",)

    def __call__(
        self,
        scope: ProductionBusinessBenchmarkScope,
        authorities: ProductionBusinessBenchmarkRuntimeAuthorities,
    ) -> BusinessBenchmarkExecutorPort:
        if scope.selection.profile == "claims":
            return self._claims(scope, authorities)
        if scope.selection.profile == "renewal" and self._renewal is not None:
            return self._renewal(scope, authorities)
        raise ProductionAdapterUnavailableError(
            "Captain Renewal Gateway runtime authority is not configured"
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}(profiles={self.profiles!r})"


class GatewayBusinessBenchmarkLiveCompositionLoader:
    def __init__(
        self,
        *,
        environment: Mapping[str, str],
        n8n_client: httpx.AsyncClient,
        clock: Callable[[], datetime],
        candidate_authority: BusinessBenchmarkCandidateAuthorityPort | None = None,
    ) -> None:
        self._environment = dict(environment)
        self._n8n_client = n8n_client
        self._clock = clock
        self._candidate_authority = candidate_authority

    def __call__(
        self, settings: LiveBusinessBenchmarkSettings
    ) -> ProductionBusinessBenchmarkCompositionPort:
        canonical = LiveBusinessBenchmarkSettings.model_validate(
            settings.model_dump(mode="python")
        )
        environment = self._environment
        if (
            canonical.provider != "openai"
            or canonical.provider_secret_name != "OPENAI_API_KEY"
        ):
            raise ValueError("benchmark provider authority is not allowlisted")
        authority = GatewayBusinessBenchmarkCompositionAuthority(
            _required(environment, "TEST_MARIADB_DSN")
        )
        config = ProductionBusinessBenchmarkBootstrapConfig.from_environment(
            canonical,
            environment,
        )
        policy_builder = (
            ConfiguredBusinessBenchmarkExecutionPolicyBuilder.from_environment(
                canonical,
                environment,
            )
        )
        artifacts = BusinessBenchmarkContentAddressedArtifactStore(config.cas_root)
        model_builder = OpenAIBusinessBenchmarkModelClientBuilder.from_environment_deferred(
            environment
        )
        if model_builder.model != canonical.model:
            raise ValueError("OpenAI model configuration does not match settings")
        pricing = BusinessBenchmarkPricingAuthority(
            ConfiguredBusinessBenchmarkPricingSource.from_environment(
                environment,
                artifacts=artifacts,
            )
        )
        max_cost_per_call = _positive_decimal(
            environment,
            "CAPTAIN_BENCHMARK_MAX_COST_PER_CALL_USD",
        )
        skill_root = Path(
            environment.get("CAPTAIN_BENCHMARK_SKILL_ROOT", "").strip()
            or Path(__file__).resolve().parents[1]
            / "agenten"
            / "agent_factory"
            / "skills"
        ).resolve()
        paid_effect_authority = CaptainReleasedSkillAuthority(
            catalog=authority.repository,
            skill_root=skill_root,
        )
        evidence_store = FilesystemFactoryEvidenceStore(
            config.authority_root / "runtime-state" / "factory-evidence"
        )
        claims = CaptainClaimsBusinessBenchmarkExecutorBuilder(
            model_client_builder=model_builder,
            budget=authority.budget,
            pricing_authority=pricing,
            paid_effect_authority=paid_effect_authority,
            evidence_store=evidence_store,
            policy_builder=policy_builder,
            provider=canonical.provider,
            model=canonical.model,
            max_cost_per_call=max_cost_per_call,
            clock=self._clock,
        )
        renewal = None
        if any(selection.profile == "renewal" for selection in canonical.selections):
            endpoint = resolve_n8n_endpoint(environment)
            if endpoint.mode != "captain-builder" or not endpoint.mcp_broker_url:
                raise ValueError(
                    "Renewal benchmark requires the Captain n8n MCP broker"
                )
            runtime_authority = (
                GatewayRenewalN8nRuntimeAuthority.from_gateway_store(
                    store=authority.runtime_store,
                    batch_id=_required(
                        environment,
                        "CAPTAIN_BENCHMARK_RENEWAL_BATCH_ID",
                    ),
                    workspace_ref=_required(
                        environment,
                        "CAPTAIN_BENCHMARK_RENEWAL_WORKSPACE_REF",
                    ),
                    endpoint_identity=endpoint.mcp_broker_url,
                    broker_signing_secret=_required(
                        environment,
                        "CAPTAIN_N8N_MCP_BROKER_SIGNING_SECRET",
                    ),
                    clock=self._clock,
                )
            )
            deployment = GatewayRenewalN8nDeploymentBinding.from_evidence_root(
                evidence_root=Path(
                    environment.get(
                        "CAPTAIN_BENCHMARK_RENEWAL_N8N_EVIDENCE_ROOT",
                        "",
                    ).strip()
                    or Path(__file__).resolve().parents[1]
                    / ".captain-cook"
                    / "business-benchmark"
                ),
                canonical_workflow_path=Path(
                    environment.get(
                        "CAPTAIN_BENCHMARK_RENEWAL_WORKFLOW_PATH",
                        "",
                    ).strip()
                    or Path(__file__).resolve().parents[1]
                    / "examples"
                    / "business_benchmark_candidates"
                    / "customer_renewal_orchestration_team"
                    / "workflows"
                    / "renewal_context_read.json"
                ),
            )
            renewal_ports = CaptainRenewalBusinessBenchmarkN8nPorts(
                endpoint=endpoint,
                allowed_endpoint_urls=frozenset({endpoint.api_base_url}),
                client=self._n8n_client,
                workflow_id=deployment.workflow_id,
                workflow_ref=deployment.workflow_ref,
                canonical_payload_sha256=deployment.canonical_payload_sha256,
                authorization_port=runtime_authority,
                grant_authority=CaptainN8nGrantAuthority(runtime_authority),
                broker_token_issuer=runtime_authority.broker_token_for,
            )
            renewal = CaptainRenewalBusinessBenchmarkExecutorBuilder(
                model_client_builder=model_builder,
                budget=authority.budget,
                pricing_authority=pricing,
                paid_effect_authority=paid_effect_authority,
                evidence_store=evidence_store,
                policy_builder=policy_builder,
                provider=canonical.provider,
                model=canonical.model,
                max_cost_per_call=max_cost_per_call,
                n8n=renewal_ports,
                clock=self._clock,
            )
        executor_builder = GatewayProfiledBusinessBenchmarkExecutorBuilder(
            claims=claims,
            renewal=renewal,
        )
        return authority.compose(
            canonical,
            config=config,
            executor_builder=executor_builder,
            execution_policy_builder=policy_builder,
            clock=self._clock,
            candidate_authority=self._candidate_authority,
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}(configured=True)"


def _tool_reference_sha256(reference: OpaqueN8nToolReference) -> str:
    return hashlib.sha256(
        json.dumps(
            reference.model_dump(mode="json", by_alias=True),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _runtime_command_id(
    *,
    job_id: UUID,
    invocation_id: UUID,
    case_sha256: str,
    tool_reference: OpaqueN8nToolReference,
) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        "|".join(
            (
                "captain.business-benchmark-renewal-n8n-command.v1",
                str(job_id),
                str(invocation_id),
                case_sha256,
                _tool_reference_sha256(tool_reference),
            )
        ),
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError("Gateway benchmark clock must be UTC")
    return value.astimezone(timezone.utc)


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise ValueError(f"required Gateway benchmark setting is missing: {name}")
    return value


def _positive_decimal(environment: Mapping[str, str], name: str) -> Decimal:
    raw = _required(environment, name)
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"Gateway benchmark setting is invalid: {name}") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError(f"Gateway benchmark setting is invalid: {name}")
    return value


def _read_json_object(path: Path) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON property")
            result[key] = value
        return result

    try:
        raw = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicates)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("Renewal n8n deployment receipt is invalid") from exc
    if not isinstance(raw, dict):
        raise ValueError("Renewal n8n deployment receipt is invalid")
    return raw


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _deployment_payload_sha256(value: dict[str, object]) -> str:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        raise ValueError("Renewal n8n deployment receipt verifier is unavailable")
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        completed = subprocess.run(
            (
                pwsh,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _POWERSHELL_CANONICAL_SHA256,
            ),
            input=payload,
            capture_output=True,
            timeout=10,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(
            "Renewal n8n deployment receipt verifier is unavailable"
        ) from exc
    try:
        digest = completed.stdout.decode("ascii", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise ValueError(
            "Renewal n8n deployment receipt verifier failed closed"
        ) from exc
    if completed.returncode != 0 or not _is_sha256(digest):
        raise ValueError("Renewal n8n deployment receipt verifier failed closed")
    return digest


def _contains_secret_field(value: object) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = key.lower().replace("-", "_")
            if any(
                marker in normalized
                for marker in ("credential", "secret", "token", "authorization", "api_key")
            ):
                return True
            if _contains_secret_field(nested):
                return True
    elif isinstance(value, list):
        return any(_contains_secret_field(item) for item in value)
    return False


__all__ = [
    "GatewayBusinessBenchmarkLiveCompositionLoader",
    "GatewayProfiledBusinessBenchmarkExecutorBuilder",
    "GatewayRenewalN8nDeploymentBinding",
    "GatewayRenewalN8nRuntimeAuthority",
]
