"""Lazy, job-scoped builder for all non-candidate Factory V3 external ports."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import httpx

from agenten.agent_factory.capability_live_adapters import (
    ContentAddressedArtifactStore,
)
from agenten.agent_factory.capability_v3_evidence_bridge import (
    CapabilityCandidateAttestationPort,
    CapabilityCandidateProviderPort,
)
from agenten.agent_factory.candidate_evaluation import ResolvedFactoryCandidate
from agenten.agent_factory.contracts import AgentFactoryJobV3
from agenten.agent_factory.input_document import parse_factory_input_bytes
from agenten.agent_factory.outcome_contracts import ForgeCapabilityPackageCandidateV1
from agenten.agent_factory.production_evidence_composition import (
    ProductionV3EvidenceExternalPorts,
    ProductionV3HoldoutPorts,
    ProductionV3N8nPorts,
)
from agenten.agent_factory.production_holdout_policy import (
    CanonicalInputHoldoutPolicy,
)
from agenten.agent_factory.production_model_pricing import (
    ProductionModelPricingConfigurationError,
    build_production_model_pricing,
)
from agenten.agent_factory.production_n8n_adapter import (
    CaptainBrokerN8nToolAdapter,
    CaptainN8nRestExecutionReader,
    CaptainN8nToolBinding,
    GatewayN8nGrantRegistrar,
    HttpxStreamableN8nMcpTransport,
)
from agenten.agent_factory.team_execution import CaptainN8nGrantAuthority
from agenten.agent_runtime.gateway_client import GatewayRuntimeClient
from agenten.agent_runtime.n8n_endpoint import resolve_n8n_endpoint


class ProductionExternalPortsConfigurationError(RuntimeError):
    """The real external graph is incomplete or crosses authority boundaries."""


def _todo(name: str) -> ProductionExternalPortsConfigurationError:
    return ProductionExternalPortsConfigurationError(
        f"TODO_TOOL.v1 required capability=production_v3_external_ports; name={name}"
    )


N8nBindingsForJob = Callable[
    [AgentFactoryJobV3, ResolvedFactoryCandidate],
    Sequence[CaptainN8nToolBinding],
]


class RequestScopedCandidateProvider:
    """Delegate candidate resolution and bind its immutable ref to the exact job."""

    def __init__(self, delegate: CapabilityCandidateProviderPort) -> None:
        self._delegate = delegate
        self._resolved: dict[UUID, tuple[AgentFactoryJobV3, ResolvedFactoryCandidate]] = {}

    def candidate_for(
        self,
        job: AgentFactoryJobV3,
        candidate: ForgeCapabilityPackageCandidateV1,
    ) -> ResolvedFactoryCandidate:
        resolved = self._delegate.candidate_for(job, candidate)
        if resolved.candidate.source_archive_ref != candidate.source_ref:
            raise ValueError("candidate provider changed the request source reference")
        existing = self._resolved.get(job.job_id)
        if existing is not None and existing != (job, resolved):
            raise ValueError("candidate binding changed for an active V3 job")
        self._resolved[job.job_id] = (job, resolved)
        return resolved

    def resolved_for(self, job: AgentFactoryJobV3) -> ResolvedFactoryCandidate:
        try:
            bound_job, resolved = self._resolved[job.job_id]
        except KeyError as exc:
            raise ValueError(
                "candidate must be resolved before request-scoped ports"
            ) from exc
        if bound_job != job:
            raise ValueError("request-scoped candidate belongs to a different V3 job")
        return resolved


class _DisabledN8nAdapter:
    def tool(self, name: str) -> Any:
        raise ValueError(f"n8n is not authorized for canonical input: {name}")

    def authorization(self, name: str) -> Any:
        raise ValueError(f"n8n is not authorized for canonical input: {name}")

    def observed_evidence(self) -> tuple[Any, ...]:
        return ()


class _DisabledN8nAuthority:
    async def authorize_command(self, claim: object, *, now: datetime) -> Any:
        del claim, now
        raise ValueError("n8n is not authorized for canonical input")

    async def authorize(self, evidence: object, *, now: datetime) -> Any:
        del evidence, now
        raise ValueError("n8n is not authorized for canonical input")


class ProductionV3JobPortFactory:
    """Reconstruct and cache holdout/n8n ports under one exact V3 job ID."""

    def __init__(
        self,
        *,
        environ: Mapping[str, str],
        artifacts: ContentAddressedArtifactStore,
        candidates: RequestScopedCandidateProvider,
        n8n_bindings_for: N8nBindingsForJob | None,
        gateway_sync_http: httpx.Client | None,
        gateway_async_http: httpx.AsyncClient | None,
        n8n_async_http: httpx.AsyncClient | None,
        clock: Callable[[], datetime],
    ) -> None:
        self._environ = dict(environ)
        self._artifacts = artifacts
        self._candidates = candidates
        self._n8n_bindings_for = n8n_bindings_for
        self._gateway_sync_http = gateway_sync_http
        self._gateway_async_http = gateway_async_http
        self._n8n_async_http = n8n_async_http
        self._clock = clock
        self._holdout_cache: dict[UUID, tuple[AgentFactoryJobV3, ProductionV3HoldoutPorts]] = {}
        self._n8n_cache: dict[UUID, tuple[AgentFactoryJobV3, ProductionV3N8nPorts]] = {}

    def holdout_ports_for(self, job: AgentFactoryJobV3) -> ProductionV3HoldoutPorts:
        cached = self._holdout_cache.get(job.job_id)
        if cached is not None:
            if cached[0] != job:
                raise ValueError("holdout port cache received a changed V3 job")
            return cached[1]
        resolved = self._candidates.resolved_for(job)
        canonical_input = self._canonical_input(job)
        policy = CanonicalInputHoldoutPolicy(
            canonical_input=canonical_input,
            subject_version=job.subject_version,
            candidate_ref=resolved.candidate.source_archive_ref,
            artifacts=self._artifacts,
            clock=self._clock,
        )
        if (
            policy.assertion_ids != job.acceptance_assertion_ids
            or policy.holdout_refs != job.private_holdout_refs
        ):
            raise ValueError("canonical input does not reconstruct the V3 job authority")
        ports = ProductionV3HoldoutPorts(
            holdout_source=policy,
            holdout_evaluator=policy,
        )
        self._holdout_cache[job.job_id] = (job, ports)
        return ports

    def n8n_ports_for(self, job: AgentFactoryJobV3) -> ProductionV3N8nPorts:
        cached = self._n8n_cache.get(job.job_id)
        if cached is not None:
            if cached[0] != job:
                raise ValueError("n8n port cache received a changed V3 job")
            return cached[1]
        resolved = self._candidates.resolved_for(job)
        document = parse_factory_input_bytes(
            self._canonical_input(job),
            "TO_BE_BUILT.md",
        )
        n8n_required = any(item.required for item in document.integrations) or any(
            agent.n8n_requirement == "required" for agent in document.agents
        )
        if not n8n_required:
            ports = ProductionV3N8nPorts(
                n8n_adapter=_DisabledN8nAdapter(),
                n8n_authority=_DisabledN8nAuthority(),
            )
            self._n8n_cache[job.job_id] = (job, ports)
            return ports
        if self._n8n_bindings_for is None:
            raise _todo("candidate_n8n_tool_bindings")
        bindings = tuple(self._n8n_bindings_for(job, resolved))
        self._validate_n8n_bindings(resolved, bindings)
        if (
            self._gateway_sync_http is None
            or self._gateway_async_http is None
            or self._n8n_async_http is None
        ):
            raise _todo("n8n_http_clients")
        gateway_url = _required(self._environ, "CAPTAIN_GATEWAY_URL")
        gateway_token = _required(self._environ, "CAPTAIN_GATEWAY_TOKEN")
        endpoint = resolve_n8n_endpoint(self._environ)
        broker_url = _required(self._environ, "CAPTAIN_N8N_MCP_BROKER_URL")
        if endpoint.mcp_broker_url != broker_url.rstrip("/"):
            raise _todo("CAPTAIN_N8N_MCP_BROKER_URL:canonical")
        ports = ProductionV3N8nPorts(
            n8n_adapter=CaptainBrokerN8nToolAdapter(
                bindings=bindings,
                correlation_id=job.correlation_id,
                subject_version=job.subject_version,
                project_id=_required(self._environ, "CAPTAIN_N8N_PROJECT_ID"),
                batch_id=_required(self._environ, "CAPTAIN_N8N_BATCH_ID"),
                workspace_ref=_required(self._environ, "CAPTAIN_N8N_WORKSPACE_REF"),
                broker_url=broker_url,
                signing_secret=_required(
                    self._environ,
                    "CAPTAIN_N8N_MCP_BROKER_SIGNING_SECRET",
                ),
                registrar=GatewayN8nGrantRegistrar(
                    gateway_url=gateway_url,
                    token=gateway_token,
                    client=self._gateway_sync_http,
                ),
                mcp=HttpxStreamableN8nMcpTransport(self._n8n_async_http),
                executions=CaptainN8nRestExecutionReader(
                    endpoint,
                    self._n8n_async_http,
                ),
                artifacts=self._artifacts,
                clock=self._clock,
            ),
            n8n_authority=CaptainN8nGrantAuthority(
                GatewayRuntimeClient(
                    gateway_url,
                    gateway_token,
                    self._gateway_async_http,
                )
            ),
        )
        self._n8n_cache[job.job_id] = (job, ports)
        return ports

    def _canonical_input(self, job: AgentFactoryJobV3) -> bytes:
        content = self._artifacts.read_sha256(job.input_ref.sha256)
        document = parse_factory_input_bytes(content, "TO_BE_BUILT.md")
        if document.input_ref != job.input_ref:
            raise ValueError("shared CAS input does not match the V3 job")
        return content

    @staticmethod
    def _validate_n8n_bindings(
        resolved: ResolvedFactoryCandidate,
        bindings: tuple[CaptainN8nToolBinding, ...],
    ) -> None:
        candidate_refs = {
            tool.name: tool.opaque_reference()
            for tool in resolved.candidate.n8n_tools
        }
        supplied_refs = {
            binding.tool.name: binding.tool.opaque_reference()
            for binding in bindings
        }
        if not bindings or supplied_refs != candidate_refs:
            raise ValueError(
                "n8n MCP bindings do not exactly match the sealed candidate tools"
            )


def build_production_v3_external_ports(
    environ: Mapping[str, str],
    *,
    candidate_provider: CapabilityCandidateProviderPort,
    candidate_attestation: CapabilityCandidateAttestationPort,
    artifacts: ContentAddressedArtifactStore,
    n8n_bindings_for: N8nBindingsForJob | None = None,
    tools: Mapping[str, Callable[..., Any]] | None = None,
    gateway_sync_http: httpx.Client | None = None,
    gateway_async_http: httpx.AsyncClient | None = None,
    n8n_async_http: httpx.AsyncClient | None = None,
    clock: Callable[[], datetime] | None = None,
) -> ProductionV3EvidenceExternalPorts:
    """Build singleton factories; job/input/candidate state stays request-scoped."""

    if candidate_provider is None or candidate_attestation is None:
        raise _todo("candidate_provider_and_attestation")
    resolved_clock = clock or (lambda: datetime.now(timezone.utc))
    try:
        model_pricing = build_production_model_pricing(
            environ,
            artifacts=artifacts,
        )
    except ProductionModelPricingConfigurationError as exc:
        raise ProductionExternalPortsConfigurationError(str(exc)) from exc
    candidates = RequestScopedCandidateProvider(candidate_provider)
    factory = ProductionV3JobPortFactory(
        environ=environ,
        artifacts=artifacts,
        candidates=candidates,
        n8n_bindings_for=n8n_bindings_for,
        gateway_sync_http=gateway_sync_http,
        gateway_async_http=gateway_async_http,
        n8n_async_http=n8n_async_http,
        clock=resolved_clock,
    )
    return ProductionV3EvidenceExternalPorts(
        candidate_provider=candidates,
        candidate_attestation=candidate_attestation,
        model_client_for=model_pricing.model_client_for,
        pricing_source=model_pricing.pricing_source,
        holdout_source=None,
        holdout_evaluator=None,
        n8n_adapter=None,
        n8n_authority=None,
        tools=dict(tools or {}),
        holdout_ports_for=factory.holdout_ports_for,
        n8n_ports_for=factory.n8n_ports_for,
    )


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise _todo(name)
    return value
