"""Fail-closed production bootstrap for the provider-backed business benchmark.

Claims is composed from MariaDB/Gateway, stable Captain CAS/state roots, fresh
budgeted OpenAI clients and host AutoGen sessions. Renewal uses the same
request-scoped, grant-bound n8n workflow path for Candidate and baseline when
its injected deployment ports are available. No in-memory authority, automatic
human approval, or unscoped n8n client is substituted.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx

from agenten.agent_factory.business_benchmark_contracts import (
    BusinessBenchmarkCaseV1,
    BusinessBenchmarkReceiptV1,
    BusinessBenchmarkRunReceiptV1,
    BusinessBenchmarkSummaryV1,
    BusinessBenchmarkSuiteV1,
    BusinessCaseCategory,
    canonical_business_benchmark_model_bytes,
)
from agenten.agent_factory.business_decision_tool import (
    TOOL_NAME as BUSINESS_DECISION_TOOL,
    bind_captain_business_decision,
)
from agenten.agent_factory.business_benchmark_candidate_seeds import (
    validate_public_business_benchmark_candidate,
)
from agenten.agent_factory.business_benchmark import BusinessBenchmarkEvaluator
from agenten.agent_factory.business_benchmark_execution import (
    BenchmarkExecutionPolicyV1,
    BusinessBenchmarkExecutorPort,
)
from agenten.agent_factory.business_benchmark_factory import (
    BusinessBenchmarkGatewayPort,
    BusinessBenchmarkFactoryComposition,
)
from agenten.agent_factory.business_benchmark_live import (
    BusinessBenchmarkLiveAdapter,
    BusinessBenchmarkFinalizedReceiptV1,
    LiveBusinessBenchmarkSettings,
    ProductionAdapterUnavailableError,
    ProductionBusinessBenchmarkCompositionPort,
)
from agenten.agent_factory.business_benchmark_provisioning import (
    CanonicalPrivateBusinessBenchmarkProvisioner,
    CaptainPrivateBusinessBenchmarkSuiteLoader,
    RENEWAL_PROFILE_ID,
)
from agenten.agent_factory.business_benchmark_n8n import (
    CaptainRenewalContextN8nAdapter,
    RenewalContextN8nProviderRequestV1,
)
from agenten.agent_factory.business_benchmark_n8n_transport import (
    CaptainNativeMcpRenewalContextTransport,
)
from agenten.agent_factory.business_benchmark_replay import (
    FilesystemBusinessBenchmarkReplayStore,
)
from agenten.agent_factory.business_benchmark_provider_state import (
    BusinessBenchmarkProviderStateStore,
)
from agenten.agent_factory.business_benchmark_runtime import (
    BusinessBenchmarkDurableFenceAdapter,
    BusinessBenchmarkProviderRuntimeBridge,
    BusinessBenchmarkSessionRequestV1,
    BusinessBenchmarkTeamRuntimeScopeV1,
)
from agenten.agent_factory.business_benchmark_store import (
    FilesystemBusinessBenchmarkEvidenceStore,
)
from agenten.agent_factory.business_benchmark_human_review import (
    CaptainHumanReviewStore,
)
from agenten.agent_factory.business_benchmark_production import (
    BusinessBenchmarkCandidateAuthorityPort,
    BusinessBenchmarkCasePolicyPort,
    CaptainBusinessBenchmarkPolicyBindingV1,
    ProductionBusinessBenchmarkComposition,
    ProductionBusinessBenchmarkScope,
    ProductionBusinessBenchmarkScopeResolver,
)
from agenten.agent_factory.business_benchmark_production_ports import (
    BusinessBenchmarkCandidateAuthority,
    BusinessBenchmarkContentAddressedArtifactStore,
    BusinessBenchmarkProductionPortError,
    BusinessBenchmarkPricingAuthority,
    ConfiguredBusinessBenchmarkPricingSource,
    OpenAIBusinessBenchmarkModelClientBuilder,
    factory_execution_policy_sha256,
)
from agenten.agent_factory.candidate_evaluation import (
    FactoryCandidateArtifact,
    FactoryCandidateEvaluator,
    ResolvedFactoryCandidate,
)
from agenten.agent_factory.evidence_store import FilesystemFactoryEvidenceStore
from agenten.agent_factory.factory_feedback import FactoryFeedbackBuilder
from agenten.agent_factory.team_evaluation import TeamEvaluationService
from agenten.agent_factory.team_execution import (
    BudgetedChatCompletionClient,
    CaptainReleasedSkillAuthority,
    FactoryHoldoutEvaluationReceiptV1,
    FactoryN8nGrantAuthorityPort,
    FactoryN8nToolAuthorizationV1,
    HostAutoGenSessionExecutor,
    ResolvedFactoryHoldoutCase,
    SealedSingleAgentPolicyV1,
)
from agenten.agent_factory.contracts import AgentFactoryJobV3, FactoryLease, FactoryRole
from agenten.agent_factory.holdout_contracts import PrivateHoldoutRef
from agenten.agent_factory.execution_budget import (
    FactoryBudgetPort,
    FactoryBudgetProjection,
)
from agenten.agent_factory.leases import issue_factory_lease, validate_factory_lease
from agenten.agent_factory.skill_evaluation import ReleasedHermesSkill
from agenten.agent_factory.skill_workflow_contracts import (
    FACTORY_SKILL_ID_BY_STEP,
    FactorySkillInvocationV1,
    FactorySkillStep,
    TeamEvaluationV1,
    TeamExecutionEvidenceV1,
    effective_team_execution_evidence,
)
from agenten.agent_runtime.contracts import ArtifactRef, IntegrationIntent
from agenten.agent_runtime.n8n_endpoint import N8nEndpoint
from agenten.agent_factory.n8n_tools import OpaqueN8nToolReference


BUSINESS_BENCHMARK_LEASE_DURATION = timedelta(minutes=90)
_SAFE_VERSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class ProductionBusinessBenchmarkBootstrapConfig:
    """Side-effect-free paths and operator policy for the live bootstrap.

    Restart-critical CAS, suite, replay, provider and human-review state uses a
    stable Captain authority root.  Only finalized run evidence is derived
    from the timestamped evidence root.  Both roots remain inside a gitignored
    ``.captain-cook`` namespace.
    """

    seed_version_id: str
    authority_root: Path
    cas_root: Path
    private_suite_root: Path
    evidence_store_root: Path
    human_review_root: Path
    replay_root: Path
    provider_state_root: Path
    human_review_timeout_seconds: float

    @classmethod
    def from_environment(
        cls,
        settings: LiveBusinessBenchmarkSettings,
        environment: Mapping[str, str],
    ) -> "ProductionBusinessBenchmarkBootstrapConfig":
        canonical = LiveBusinessBenchmarkSettings.model_validate(
            settings.model_dump(mode="python")
        )
        root = canonical.evidence_root.resolve()
        if ".captain-cook" not in {part.casefold() for part in root.parts}:
            raise ValueError(
                "business benchmark evidence root must use the gitignored "
                ".captain-cook namespace"
            )
        seed_version_id = environment.get(
            "CAPTAIN_BENCHMARK_SEED_VERSION_ID", ""
        ).strip()
        if not seed_version_id:
            raise ValueError(
                "required benchmark bootstrap setting is missing: "
                "CAPTAIN_BENCHMARK_SEED_VERSION_ID"
            )
        if _SAFE_VERSION_ID.fullmatch(seed_version_id) is None:
            raise ValueError("business benchmark seed version is invalid")
        configured_authority_root = environment.get(
            "CAPTAIN_BENCHMARK_AUTHORITY_ROOT", ""
        ).strip()
        authority_root = (
            Path(configured_authority_root)
            if configured_authority_root
            else Path.cwd() / ".captain-cook" / "private" / "business-benchmarks"
        ).resolve()
        if ".captain-cook" not in {
            part.casefold() for part in authority_root.parts
        }:
            raise ValueError(
                "business benchmark authority root must use the gitignored "
                ".captain-cook namespace"
            )
        raw_timeout = environment.get(
            "CAPTAIN_BENCHMARK_HUMAN_REVIEW_TIMEOUT_SECONDS", "0"
        ).strip()
        try:
            human_review_timeout_seconds = float(raw_timeout)
        except ValueError as exc:
            raise ValueError("business benchmark human review timeout is invalid") from exc
        if (
            not math.isfinite(human_review_timeout_seconds)
            or human_review_timeout_seconds < 0
            or human_review_timeout_seconds > 300
        ):
            raise ValueError("business benchmark human review timeout is invalid")
        return cls(
            seed_version_id=seed_version_id,
            authority_root=authority_root,
            cas_root=authority_root / "cas",
            private_suite_root=authority_root / "suites",
            evidence_store_root=root / "captain" / "receipts",
            human_review_root=authority_root / "human-review",
            replay_root=authority_root / "runtime-state" / "replay",
            provider_state_root=authority_root / "runtime-state" / "provider-state",
            human_review_timeout_seconds=human_review_timeout_seconds,
        )


class GatewayBusinessBenchmarkRepositoryPort(
    BusinessBenchmarkGatewayPort,
    Protocol,
):
    """Read-only Gateway projection used by the benchmark scope resolver."""

    def job(self, job_id: UUID) -> object: ...

    def workflow_artifacts(self, job_id: UUID) -> tuple[object, ...]: ...

    def workflow_budget_projection(
        self, job_id: UUID
    ) -> FactoryBudgetProjection | None: ...


class CaptainBusinessBenchmarkExecutorBuilderPort(Protocol):
    """Build one scope-bound provider executor from Captain authorities only."""

    def __call__(
        self,
        scope: ProductionBusinessBenchmarkScope,
        authorities: "ProductionBusinessBenchmarkRuntimeAuthorities",
    ) -> BusinessBenchmarkExecutorPort: ...


class CaptainBusinessBenchmarkExecutionPolicyBuilderPort(Protocol):
    """Materialize exact per-case provider limits selected by Captain."""

    def __call__(
        self,
        scope: ProductionBusinessBenchmarkScope,
    ) -> BusinessBenchmarkCasePolicyPort: ...


class CaptainRenewalN8nAuthorizationPort(Protocol):
    """Issue one request-scoped command/grant claim without exposing credentials."""

    def authorization_for(
        self,
        *,
        job: AgentFactoryJobV3,
        invocation: FactorySkillInvocationV1,
        request: BusinessBenchmarkSessionRequestV1,
        tool_reference: OpaqueN8nToolReference,
    ) -> FactoryN8nToolAuthorizationV1: ...


@dataclass(frozen=True)
class CaptainRenewalBusinessBenchmarkN8nPorts:
    """Injected least-privilege authorities for the Renewal n8n read path."""

    endpoint: N8nEndpoint
    allowed_endpoint_urls: frozenset[str]
    client: httpx.AsyncClient
    workflow_id: str
    workflow_ref: ArtifactRef
    canonical_payload_sha256: str
    authorization_port: CaptainRenewalN8nAuthorizationPort
    grant_authority: FactoryN8nGrantAuthorityPort
    broker_token_issuer: Callable[[RenewalContextN8nProviderRequestV1], str]

    def __post_init__(self) -> None:
        if (
            self.endpoint.mode != "captain-builder"
            or not self.endpoint.mcp_broker_url
            or not self.endpoint.mcp_token
            or not self.allowed_endpoint_urls
            or self.endpoint.api_base_url.rstrip("/")
            not in {item.rstrip("/") for item in self.allowed_endpoint_urls}
            or not isinstance(self.client, httpx.AsyncClient)
            or not self.workflow_id.strip()
            or self.workflow_ref.media_type != "application/json"
            or re.fullmatch(r"[0-9a-f]{64}", self.canonical_payload_sha256)
            is None
            or not callable(getattr(self.authorization_port, "authorization_for", None))
            or not callable(getattr(self.grant_authority, "authorize_command", None))
            or not callable(getattr(self.grant_authority, "authorize", None))
            or not callable(self.broker_token_issuer)
        ):
            raise ValueError("Renewal n8n bootstrap ports are incomplete or unscoped")


def _candidate_workflow_canonical_payload_sha256(
    candidate: ResolvedFactoryCandidate,
    artifact: FactoryCandidateArtifact,
) -> str:
    """Bind formatting-independent candidate JSON to the deployed workflow body."""

    try:
        with zipfile.ZipFile(candidate.source_archive) as archive:
            content = archive.read(artifact.relative_path.replace("\\", "/"))
        if hashlib.sha256(content).hexdigest() != artifact.reference.sha256:
            raise ValueError("candidate workflow artifact digest changed")
        payload = json.loads(content.decode("utf-8"))
    except (KeyError, OSError, UnicodeError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        raise ValueError("candidate workflow artifact is unavailable or invalid") from exc
    publish_fields = ("name", "nodes", "connections", "settings")
    if not isinstance(payload, dict) or any(field not in payload for field in publish_fields):
        raise ValueError("candidate workflow artifact is unavailable or invalid")
    published_payload = {field: payload[field] for field in publish_fields}
    encoded = json.dumps(
        published_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ProductionBusinessBenchmarkRuntimeAuthorities:
    """Durable authorities handed to the request-scoped executor builder."""

    artifacts: BusinessBenchmarkContentAddressedArtifactStore
    human_review: CaptainHumanReviewStore
    provider_state_root: Path


@dataclass(frozen=True)
class ProductionBusinessBenchmarkBootstrapPorts:
    """External Captain ports that cannot be reconstructed from public settings."""

    gateway_repository: GatewayBusinessBenchmarkRepositoryPort
    released_skills: ReleasedSkillCatalogPort
    leases: ActiveFactoryLeasePort
    executor_builder: CaptainBusinessBenchmarkExecutorBuilderPort
    execution_policy_builder: CaptainBusinessBenchmarkExecutionPolicyBuilderPort
    clock: Callable[[], datetime]
    candidate_authority: BusinessBenchmarkCandidateAuthorityPort | None = None


@dataclass(frozen=True)
class ConfiguredBusinessBenchmarkExecutionPolicyBuilder:
    """Captain-selected per-case limits shared by scope preflight and runtime."""

    model: str
    redaction_policy_version: str
    maximum_cost_micro_usd: int
    maximum_latency_ms: int
    baseline_system_policy_version: str = "single-agent-baseline-v1"

    @classmethod
    def from_environment(
        cls,
        settings: LiveBusinessBenchmarkSettings,
        environment: Mapping[str, str],
    ) -> "ConfiguredBusinessBenchmarkExecutionPolicyBuilder":
        raw_cost = environment.get(
            "CAPTAIN_BENCHMARK_CASE_MAX_COST_USD", ""
        ).strip()
        raw_latency = environment.get(
            "CAPTAIN_BENCHMARK_CASE_MAX_LATENCY_MS", ""
        ).strip()
        redaction_version = environment.get(
            "CAPTAIN_BENCHMARK_REDACTION_POLICY_VERSION", ""
        ).strip()
        if not raw_cost:
            raise ValueError(
                "required benchmark bootstrap setting is missing: "
                "CAPTAIN_BENCHMARK_CASE_MAX_COST_USD"
            )
        if not raw_latency:
            raise ValueError(
                "required benchmark bootstrap setting is missing: "
                "CAPTAIN_BENCHMARK_CASE_MAX_LATENCY_MS"
            )
        if not redaction_version:
            raise ValueError(
                "required benchmark bootstrap setting is missing: "
                "CAPTAIN_BENCHMARK_REDACTION_POLICY_VERSION"
            )
        try:
            cost = Decimal(raw_cost)
            cost_micro = cost * Decimal(1_000_000)
            latency = int(raw_latency)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("business benchmark case limits are invalid") from exc
        if (
            not cost.is_finite()
            or cost <= 0
            or cost_micro != cost_micro.to_integral_value()
            or latency < 1
        ):
            raise ValueError("business benchmark case limits are invalid")
        redaction_sha256 = hashlib.sha256(
            json.dumps(
                {"redaction_policy_version": redaction_version},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if redaction_sha256 != settings.redaction_policy_sha256:
            raise ValueError(
                "business benchmark redaction policy version does not match settings"
            )
        return cls(
            model=settings.model,
            redaction_policy_version=redaction_version,
            maximum_cost_micro_usd=int(cost_micro),
            maximum_latency_ms=latency,
        )

    def __call__(
        self,
        scope: ProductionBusinessBenchmarkScope,
    ) -> BusinessBenchmarkCasePolicyPort:
        if scope.settings.model != self.model:
            raise ValueError("benchmark policy model does not match the resolved scope")
        maximum_cost_micro_usd = self.maximum_cost_micro_usd
        suite = getattr(scope, "suite", None)
        job = getattr(scope, "job", None)
        cases = getattr(suite, "cases", ())
        iterations = getattr(job, "max_behavioral_iterations", None)
        job_policy = getattr(job, "execution_policy", None)
        job_maximum_usd = getattr(job_policy, "max_cost_usd", None)
        if cases and isinstance(iterations, int) and iterations > 0 and job_maximum_usd:
            effect_slots = len(cases) * 2 * iterations
            job_maximum_micro_usd = int(Decimal(job_maximum_usd) * 1_000_000)
            stable_effect_maximum = job_maximum_micro_usd // effect_slots
            if stable_effect_maximum < 1:
                raise ValueError("benchmark job budget cannot fund every retry effect")
            maximum_cost_micro_usd = min(
                maximum_cost_micro_usd,
                stable_effect_maximum,
            )

        def for_case(
            benchmark_case: BusinessBenchmarkCaseV1,
        ) -> BenchmarkExecutionPolicyV1:
            return BenchmarkExecutionPolicyV1(
                schema="captain.business-benchmark-execution-policy.v1",
                model_version=self.model,
                allowed_tool_intents=benchmark_case.allowed_tool_intents,
                maximum_cost_micro_usd=maximum_cost_micro_usd,
                maximum_latency_ms=self.maximum_latency_ms,
                redaction_policy_version=self.redaction_policy_version,
                baseline_system_policy_version=self.baseline_system_policy_version,
            )

        return for_case


def _effective_provider_call_maximum(
    *,
    configured: Decimal,
    policies: tuple[BenchmarkExecutionPolicyV1, ...],
) -> Decimal:
    configured_micro_usd = configured * Decimal(1_000_000)
    if (
        not configured.is_finite()
        or configured <= 0
        or configured_micro_usd != configured_micro_usd.to_integral_value()
        or configured * Decimal(100) != (configured * Decimal(100)).to_integral_value()
        or not policies
        or any(policy.maximum_cost_micro_usd < 1 for policy in policies)
    ):
        raise ValueError("provider per-call maximum is invalid")
    # Gateway reservations use cents, while the immutable case envelope uses
    # micro-USD. Keep the cent-denominated reservation ceiling and let the
    # terminal receipt enforce the tighter case-policy maximum.
    return configured


class _RequestBoundBenchmarkHoldoutResolver:
    """Expose exactly one already-redacted task to one fresh host session."""

    def __init__(self, request: BusinessBenchmarkSessionRequestV1) -> None:
        self._request = request
        self._body = request.redacted_case_task.encode("utf-8")

    async def resolve(self, reference: PrivateHoldoutRef) -> ResolvedFactoryHoldoutCase:
        expected_sha = hashlib.sha256(self._body).hexdigest()
        if (
            reference.sha256 != expected_sha
            or reference != self._request.case_ref
            or reference.holdout_id != self._request.identity.case_id
            or reference.sha256 != self._request.identity.case_sha256
        ):
            raise ValueError("host session requested a different redacted benchmark task")
        return ResolvedFactoryHoldoutCase(reference=reference, body=self._body)

    async def evaluate(
        self,
        reference: PrivateHoldoutRef,
        result: object,
        assertion_ids: tuple[str, ...],
    ) -> FactoryHoldoutEvaluationReceiptV1:
        raise RuntimeError(
            "business benchmark host sessions do not own Factory holdout decisions"
        )


class CaptainBusinessBenchmarkHostSessionFactory:
    """Create a fresh budgeted OpenAI client and Host AutoGen executor per case."""

    def __init__(
        self,
        *,
        scope: ProductionBusinessBenchmarkScope,
        model_client_builder: OpenAIBusinessBenchmarkModelClientBuilder,
        budget: FactoryBudgetPort,
        pricing_authority: BusinessBenchmarkPricingAuthority,
        paid_effect_authority: CaptainReleasedSkillAuthority,
        evidence_store: FilesystemFactoryEvidenceStore,
        provider: str,
        model: str,
        max_cost_per_call: Decimal,
        clock: Callable[[], datetime],
    ) -> None:
        self._scope = scope
        self._model_client_builder = model_client_builder
        self._budget = budget
        self._pricing_authority = pricing_authority
        self._paid_effect_authority = paid_effect_authority
        self._evidence_store = evidence_store
        self._provider = provider
        self._model = model
        self._max_cost_per_call = max_cost_per_call
        self._clock = clock

    def create(
        self,
        request: BusinessBenchmarkSessionRequestV1,
    ) -> HostAutoGenSessionExecutor:
        if (
            request.identity.job_id != self._scope.job.job_id
            or request.identity.correlation_id != self._scope.job.correlation_id
            or request.identity.attempt != self._scope.selection.attempt
            or request.identity.model != self._model
        ):
            raise ValueError("host session request is outside the resolved benchmark scope")

        def model_client_for(identity):
            if identity != request.identity:
                raise ValueError("host requested a model client for a different session")
            delegate = self._model_client_builder(
                self._scope.job,
                self._scope.runtime_invocation,
            )
            return BudgetedChatCompletionClient(
                job=self._scope.job,
                invocation=self._scope.runtime_invocation,
                attempt=self._scope.selection.attempt,
                delegate=delegate,
                budget=self._budget,
                evidence_store=self._evidence_store,
                provider=self._provider,
                model=self._model,
                max_cost_per_call=self._max_cost_per_call,
                paid_effect_authority=self._paid_effect_authority,
                pricing_authority=self._pricing_authority,
                clock=self._clock,
                before_provider_dispatch=request.before_provider_dispatch,
            )

        return HostAutoGenSessionExecutor(
            model_client_factory=model_client_for,
            evidence_store=self._evidence_store,
            holdouts=_RequestBoundBenchmarkHoldoutResolver(request),
            tools={
                BUSINESS_DECISION_TOOL: bind_captain_business_decision(
                    request.redacted_case_task
                )
            },
            baseline_tools={},
            clock=self._clock,
        )


class CaptainRenewalBusinessBenchmarkHostSessionFactory:
    """Create one Renewal host session with only its case-allowed n8n tool."""

    def __init__(
        self,
        *,
        scope: ProductionBusinessBenchmarkScope,
        model_client_builder: OpenAIBusinessBenchmarkModelClientBuilder,
        budget: FactoryBudgetPort,
        pricing_authority: BusinessBenchmarkPricingAuthority,
        paid_effect_authority: CaptainReleasedSkillAuthority,
        evidence_store: FilesystemFactoryEvidenceStore,
        provider: str,
        model: str,
        max_cost_per_call: Decimal,
        tool_reference: OpaqueN8nToolReference,
        n8n: CaptainRenewalBusinessBenchmarkN8nPorts,
        clock: Callable[[], datetime],
    ) -> None:
        self._scope = scope
        self._model_client_builder = model_client_builder
        self._budget = budget
        self._pricing_authority = pricing_authority
        self._paid_effect_authority = paid_effect_authority
        self._evidence_store = evidence_store
        self._provider = provider
        self._model = model
        self._max_cost_per_call = max_cost_per_call
        self._tool_reference = tool_reference
        self._n8n = n8n
        self._clock = clock
        self._authorization_by_case_sha256: dict[
            str, FactoryN8nToolAuthorizationV1
        ] = {}

    def create(
        self,
        request: BusinessBenchmarkSessionRequestV1,
    ) -> HostAutoGenSessionExecutor:
        if (
            request.identity.job_id != self._scope.job.job_id
            or request.identity.correlation_id != self._scope.job.correlation_id
            or request.identity.attempt != self._scope.selection.attempt
            or request.identity.model != self._model
        ):
            raise ValueError("host session request is outside the resolved benchmark scope")
        allowed = request.allowed_host_tools
        if allowed not in {
            (BUSINESS_DECISION_TOOL,),
            (self._tool_reference.tool_name, BUSINESS_DECISION_TOOL),
        }:
            raise ValueError("Renewal request contains an unauthorized host tool subset")

        def model_client_for(identity):
            if identity != request.identity:
                raise ValueError("host requested a model client for a different session")
            delegate = self._model_client_builder(
                self._scope.job,
                self._scope.runtime_invocation,
            )
            return BudgetedChatCompletionClient(
                job=self._scope.job,
                invocation=self._scope.runtime_invocation,
                attempt=self._scope.selection.attempt,
                delegate=delegate,
                budget=self._budget,
                evidence_store=self._evidence_store,
                provider=self._provider,
                model=self._model,
                max_cost_per_call=self._max_cost_per_call,
                paid_effect_authority=self._paid_effect_authority,
                pricing_authority=self._pricing_authority,
                clock=self._clock,
                before_provider_dispatch=request.before_provider_dispatch,
            )

        adapter = None
        baseline_n8n_tools: dict[str, OpaqueN8nToolReference] = {}
        if self._tool_reference.tool_name in allowed:
            raw_authorization = self._n8n.authorization_port.authorization_for(
                job=self._scope.job,
                invocation=self._scope.runtime_invocation,
                request=request,
                tool_reference=self._tool_reference,
            )
            authorization = FactoryN8nToolAuthorizationV1.model_validate(
                raw_authorization.model_dump(mode="python")
            )
            if (
                authorization.tool_name != self._tool_reference.tool_name
                or authorization.approved_tool_ref != self._tool_reference
            ):
                raise ValueError("Renewal n8n authorization is bound to a different tool")
            paired_authorization = self._authorization_by_case_sha256.setdefault(
                request.benchmark_case_sha256,
                authorization,
            )
            if paired_authorization != authorization:
                raise ValueError(
                    "Renewal candidate and baseline require the exact same command/grant"
                )
            transport = CaptainNativeMcpRenewalContextTransport(
                client=self._n8n.client,
                workflow_id=self._n8n.workflow_id,
                workflow_ref=self._n8n.workflow_ref,
                clock=self._clock,
                broker_token_issuer=self._n8n.broker_token_issuer,
            )
            adapter = CaptainRenewalContextN8nAdapter(
                job=self._scope.job,
                invocation=self._scope.runtime_invocation,
                identity=request.identity,
                authorization=authorization,
                endpoint=self._n8n.endpoint,
                allowed_endpoint_urls=self._n8n.allowed_endpoint_urls,
                workflow_ref=self._n8n.workflow_ref,
                transport=transport,
                evidence_store=self._evidence_store,
                clock=self._clock,
                timeout_seconds=request.maximum_latency_ms / 1_000,
                max_attempts=2,
            )
            baseline_n8n_tools[self._tool_reference.tool_name] = self._tool_reference

        return HostAutoGenSessionExecutor(
            model_client_factory=model_client_for,
            evidence_store=self._evidence_store,
            holdouts=_RequestBoundBenchmarkHoldoutResolver(request),
            tools={
                BUSINESS_DECISION_TOOL: bind_captain_business_decision(
                    request.redacted_case_task
                )
            },
            baseline_tools={},
            baseline_n8n_tools=baseline_n8n_tools,
            n8n_adapter=adapter,
            n8n_authority=(self._n8n.grant_authority if adapter is not None else None),
            clock=self._clock,
        )


class CaptainClaimsBusinessBenchmarkExecutorBuilder:
    """Concrete live executor for the no-integration Claims benchmark team."""

    _BASELINE_PROMPT = """Captain Claims single-agent public policy
Policy version: claims-public-policy-v1

You are the sealed single-agent Claims baseline. Use only the redacted facts supplied by Captain. You have no tools. Never infer personal data, inspect benchmark storage, invent evidence, or claim that a human review is completed.

Apply this public decision policy:
- Complete ordinary evidence with active coverage routes to route_standard_review using coverage_state_verified and evidence_complete.
- A complete near-boundary record also routes to route_standard_review using boundary_condition_identified and coverage_state_verified.
- Missing or unverified required evidence returns request_information using required_evidence_missing and decision_deferred.
- Conflicting sources or internally inconsistent evidence yields escalate_coverage using evidence_conflict_detected and specialist_review_required.
- Specialist interpretation, critical complexity, or any explicit escalation trigger yields escalate_coverage using critical_coverage_question_detected and human_authority_required.

The only allowed decisions are:
- route_standard_review
- request_information
- escalate_coverage

The only allowed rationale fact identifiers are:
- coverage_state_verified
- evidence_complete
- boundary_condition_identified
- required_evidence_missing
- decision_deferred
- evidence_conflict_detected
- specialist_review_required
- critical_coverage_question_detected
- human_authority_required

Emit exactly one JSON object and nothing else, with no prose or markdown:
{"schema":"captain.business-benchmark-terminal.v1","observed_decision":"decision_identifier","observed_rationale_fact_ids":["fact_identifier_1","fact_identifier_2"]}

Use only rationale identifiers justified by supplied fields. Never reveal hidden labels, benchmark storage, raw identifiers, or credentials.
""".encode("utf-8")

    def __init__(
        self,
        *,
        model_client_builder: OpenAIBusinessBenchmarkModelClientBuilder,
        budget: FactoryBudgetPort,
        pricing_authority: BusinessBenchmarkPricingAuthority,
        paid_effect_authority: CaptainReleasedSkillAuthority,
        evidence_store: FilesystemFactoryEvidenceStore,
        policy_builder: ConfiguredBusinessBenchmarkExecutionPolicyBuilder,
        provider: str,
        model: str,
        max_cost_per_call: Decimal,
        clock: Callable[[], datetime],
    ) -> None:
        self._model_client_builder = model_client_builder
        self._budget = budget
        self._pricing_authority = pricing_authority
        self._paid_effect_authority = paid_effect_authority
        self._evidence_store = evidence_store
        self._policy_builder = policy_builder
        self._provider = provider
        self._model = model
        self._max_cost_per_call = max_cost_per_call
        self._clock = clock

    def __call__(
        self,
        scope: ProductionBusinessBenchmarkScope,
        authorities: ProductionBusinessBenchmarkRuntimeAuthorities,
    ) -> BusinessBenchmarkExecutorPort:
        if scope.selection.profile != "claims":
            raise ProductionAdapterUnavailableError(
                "Captain renewal n8n session binding is required for this profile"
            )
        remaining = (scope.job.deadline_at - self._clock()).total_seconds()
        if remaining <= 0:
            raise ValueError("benchmark candidate deadline has expired")
        preflight = FactoryCandidateEvaluator().validate(
            scope.candidate,
            max_seconds=max(0.001, min(scope.candidate.candidate.timeout_seconds, remaining)),
        )
        if preflight.status != "succeeded" or preflight.team_execution_manifest is None:
            raise ValueError("benchmark candidate failed host AutoGen preflight")
        manifest = preflight.team_execution_manifest
        validate_public_business_benchmark_candidate(
            scope.suite.profile_id,
            scope.candidate,
            manifest,
            attempt=scope.selection.attempt,
        )
        allowed_host_tools = tuple(
            dict.fromkeys(tool for agent in manifest.agents for tool in agent.tools)
        )
        if (
            allowed_host_tools != (BUSINESS_DECISION_TOOL,)
            or scope.candidate.candidate.n8n_tools
            or scope.candidate.candidate.host_tools != (BUSINESS_DECISION_TOOL,)
        ):
            raise ValueError(
                "Claims benchmark candidate must request only the Captain decision tool"
            )

        case_policy = self._policy_builder(scope)
        policies = {
            (
                item.case_id,
                hashlib.sha256(
                    canonical_business_benchmark_model_bytes(item)
                ).hexdigest(),
            ): case_policy(item)
            for item in scope.suite.cases
        }
        effective_max_cost_per_call = _effective_provider_call_maximum(
            configured=self._max_cost_per_call,
            policies=tuple(policies.values()),
        )
        baseline_ref = authorities.artifacts.put(
            self._BASELINE_PROMPT,
            "text/plain",
            namespace="baseline-system-prompt",
        )
        baseline = SealedSingleAgentPolicyV1.seal(
            agent_name="business_benchmark_baseline",
            system_prompt_ref=baseline_ref,
            execution_policy_sha256=factory_execution_policy_sha256(scope.job),
            model=self._model,
            allowed_tools=(),
            max_messages=manifest.max_messages,
            max_tool_calls=0,
        )
        runtime_scope = BusinessBenchmarkTeamRuntimeScopeV1(
            job=scope.job,
            invocation=scope.runtime_invocation,
            candidate_id=scope.candidate.candidate.candidate_id,
            candidate_ref=scope.candidate_ref,
            resolved_candidate=scope.candidate,
            candidate_workspace=authorities.artifacts.root,
            team_manifest=manifest,
            team_manifest_ref=scope.candidate.candidate.team_manifest.reference,
            model=self._model,
            suite_ref=scope.suite_ref,
            suite_id=scope.suite_id,
            benchmark_policies=policies,
            baseline_policy=baseline,
            baseline_system_policy_version="single-agent-baseline-v1",
            allowed_host_tools=allowed_host_tools,
            tool_intents={},
        )
        state = BusinessBenchmarkProviderStateStore(
            authorities.provider_state_root
        )
        session_factory = CaptainBusinessBenchmarkHostSessionFactory(
            scope=scope,
            model_client_builder=self._model_client_builder,
            budget=self._budget,
            pricing_authority=self._pricing_authority,
            paid_effect_authority=self._paid_effect_authority,
            evidence_store=self._evidence_store,
            provider=self._provider,
            model=self._model,
            max_cost_per_call=effective_max_cost_per_call,
            clock=self._clock,
        )
        runtime = BusinessBenchmarkProviderRuntimeBridge(
            scopes={scope.job.job_id: runtime_scope},
            session_factory=session_factory,
            artifacts=authorities.artifacts,
            provider_state=state,
            human_review=authorities.human_review,
            clock=self._clock,
        )
        fence = BusinessBenchmarkDurableFenceAdapter(
            provider_state=state,
            artifacts=authorities.artifacts,
            preparation_for_effect=runtime.preparation_binding_for,
            clock=self._clock,
        )
        return BusinessBenchmarkLiveAdapter(
            runtime_bundle=runtime,
            fence_store=fence,
            trusted_tool_intents={
                BUSINESS_DECISION_TOOL: IntegrationIntent.NONE,
            },
            monotonic_clock=time.monotonic,
            clock=self._clock,
        )


class CaptainRenewalBusinessBenchmarkExecutorBuilder:
    """Concrete fair paired executor for the grant-bound Renewal benchmark."""

    _BASELINE_PROMPT = """Captain Renewal single-agent public policy
Policy version: renewal-public-policy-v1

You are the sealed single-agent Renewal baseline. Use only the redacted facts supplied by Captain. The read-only renewal_context_read tool is available only for ordinary and boundary records; use it at most once when it is available. If it is absent, do not request, simulate, or infer tool output. Never mutate external state, invent evidence, inspect benchmark storage, or claim that a human review is completed.

Apply this public decision policy:
- Complete ordinary evidence with a verified renewal window yields propose_next_best_action using renewal_window_verified and next_action_supported.
- A complete boundary record also yields propose_next_best_action using commercial_boundary_identified and next_action_bounded.
- Missing or unverified required signals yield request_information using required_signal_missing and action_deferred.
- Conflicting commercial evidence yields human_commercial_review using commercial_conflict_detected and human_review_required.
- A strategic authority threshold or explicit escalation trigger yields human_commercial_review using strategic_authority_threshold_met and human_commercial_authority_required.

The only allowed decisions are:
- propose_next_best_action
- request_information
- human_commercial_review

The only allowed rationale fact identifiers are:
- renewal_window_verified
- next_action_supported
- commercial_boundary_identified
- next_action_bounded
- required_signal_missing
- action_deferred
- commercial_conflict_detected
- human_review_required
- strategic_authority_threshold_met
- human_commercial_authority_required

Emit exactly one JSON object and nothing else, with no prose or markdown:
{"schema":"captain.business-benchmark-terminal.v1","observed_decision":"decision_identifier","observed_rationale_fact_ids":["fact_identifier_1","fact_identifier_2"]}

Use only rationale identifiers justified by supplied fields. Never reveal hidden labels, benchmark storage, raw identifiers, or credentials.
""".encode("utf-8")

    def __init__(
        self,
        *,
        model_client_builder: OpenAIBusinessBenchmarkModelClientBuilder,
        budget: FactoryBudgetPort,
        pricing_authority: BusinessBenchmarkPricingAuthority,
        paid_effect_authority: CaptainReleasedSkillAuthority,
        evidence_store: FilesystemFactoryEvidenceStore,
        policy_builder: ConfiguredBusinessBenchmarkExecutionPolicyBuilder,
        provider: str,
        model: str,
        max_cost_per_call: Decimal,
        n8n: CaptainRenewalBusinessBenchmarkN8nPorts,
        clock: Callable[[], datetime],
    ) -> None:
        self._model_client_builder = model_client_builder
        self._budget = budget
        self._pricing_authority = pricing_authority
        self._paid_effect_authority = paid_effect_authority
        self._evidence_store = evidence_store
        self._policy_builder = policy_builder
        self._provider = provider
        self._model = model
        self._max_cost_per_call = max_cost_per_call
        self._n8n = n8n
        self._clock = clock

    def __call__(
        self,
        scope: ProductionBusinessBenchmarkScope,
        authorities: ProductionBusinessBenchmarkRuntimeAuthorities,
    ) -> BusinessBenchmarkExecutorPort:
        if scope.selection.profile != "renewal":
            raise ProductionAdapterUnavailableError(
                "Captain Renewal executor requires the renewal profile"
            )
        remaining = (scope.job.deadline_at - self._clock()).total_seconds()
        if remaining <= 0:
            raise ValueError("benchmark candidate deadline has expired")
        preflight = FactoryCandidateEvaluator().validate(
            scope.candidate,
            max_seconds=max(
                0.001,
                min(scope.candidate.candidate.timeout_seconds, remaining),
            ),
        )
        if preflight.status != "succeeded" or preflight.team_execution_manifest is None:
            raise ValueError("benchmark candidate failed host AutoGen preflight")
        manifest = preflight.team_execution_manifest
        validate_public_business_benchmark_candidate(
            scope.suite.profile_id,
            scope.candidate,
            manifest,
            attempt=scope.selection.attempt,
        )
        allowed_host_tools = tuple(
            dict.fromkeys(tool for agent in manifest.agents for tool in agent.tools)
        )
        candidate_tools = scope.candidate.candidate.n8n_tools
        candidate_workflows = scope.candidate.candidate.workflow_artifacts
        if (
            allowed_host_tools
            != ("renewal_context_read", BUSINESS_DECISION_TOOL)
            or len(candidate_tools) != 1
            or candidate_tools[0].name != "renewal_context_read"
            or scope.candidate.candidate.host_tools != (BUSINESS_DECISION_TOOL,)
            or len(candidate_workflows) != 1
            or _candidate_workflow_canonical_payload_sha256(
                scope.candidate,
                candidate_workflows[0],
            )
            != self._n8n.canonical_payload_sha256
        ):
            raise ValueError(
                "Renewal candidate must match the one Captain-authorized workflow tool"
            )
        tool_reference = candidate_tools[0].opaque_reference()

        expected_intents = {
            BusinessCaseCategory.ORDINARY: (IntegrationIntent.N8N,),
            BusinessCaseCategory.BOUNDARY: (IntegrationIntent.N8N,),
            BusinessCaseCategory.INCOMPLETE: (IntegrationIntent.NONE,),
            BusinessCaseCategory.CONTRADICTORY: (IntegrationIntent.NONE,),
            BusinessCaseCategory.MANDATORY_ESCALATION: (IntegrationIntent.NONE,),
        }
        if (
            scope.suite.profile_id != RENEWAL_PROFILE_ID
            or any(
                item.allowed_tool_intents != expected_intents[item.category]
                for item in scope.suite.cases
            )
        ):
            raise ValueError("Renewal suite tool intents are not category-bound")

        case_policy = self._policy_builder(scope)
        policies = {
            (
                item.case_id,
                hashlib.sha256(
                    canonical_business_benchmark_model_bytes(item)
                ).hexdigest(),
            ): case_policy(item)
            for item in scope.suite.cases
        }
        effective_max_cost_per_call = _effective_provider_call_maximum(
            configured=self._max_cost_per_call,
            policies=tuple(policies.values()),
        )
        baseline_ref = authorities.artifacts.put(
            self._BASELINE_PROMPT,
            "text/plain",
            namespace="baseline-system-prompt",
        )
        baseline = SealedSingleAgentPolicyV1.seal(
            agent_name="business_benchmark_baseline",
            system_prompt_ref=baseline_ref,
            execution_policy_sha256=factory_execution_policy_sha256(scope.job),
            model=self._model,
            allowed_tools=(candidate_tools[0].name,),
            max_messages=manifest.max_messages,
            max_tool_calls=manifest.max_tool_calls,
        )
        runtime_scope = BusinessBenchmarkTeamRuntimeScopeV1(
            job=scope.job,
            invocation=scope.runtime_invocation,
            candidate_id=scope.candidate.candidate.candidate_id,
            candidate_ref=scope.candidate_ref,
            resolved_candidate=scope.candidate,
            candidate_workspace=authorities.artifacts.root,
            team_manifest=manifest,
            team_manifest_ref=scope.candidate.candidate.team_manifest.reference,
            model=self._model,
            suite_ref=scope.suite_ref,
            suite_id=scope.suite_id,
            benchmark_policies=policies,
            baseline_policy=baseline,
            baseline_system_policy_version="single-agent-baseline-v1",
            allowed_host_tools=allowed_host_tools,
            tool_intents={candidate_tools[0].name: IntegrationIntent.N8N},
        )
        state = BusinessBenchmarkProviderStateStore(authorities.provider_state_root)
        session_factory = CaptainRenewalBusinessBenchmarkHostSessionFactory(
            scope=scope,
            model_client_builder=self._model_client_builder,
            budget=self._budget,
            pricing_authority=self._pricing_authority,
            paid_effect_authority=self._paid_effect_authority,
            evidence_store=self._evidence_store,
            provider=self._provider,
            model=self._model,
            max_cost_per_call=effective_max_cost_per_call,
            tool_reference=tool_reference,
            n8n=self._n8n,
            clock=self._clock,
        )
        runtime = BusinessBenchmarkProviderRuntimeBridge(
            scopes={scope.job.job_id: runtime_scope},
            session_factory=session_factory,
            artifacts=authorities.artifacts,
            provider_state=state,
            human_review=authorities.human_review,
            clock=self._clock,
        )
        fence = BusinessBenchmarkDurableFenceAdapter(
            provider_state=state,
            artifacts=authorities.artifacts,
            preparation_for_effect=runtime.preparation_binding_for,
            clock=self._clock,
        )
        return BusinessBenchmarkLiveAdapter(
            runtime_bundle=runtime,
            fence_store=fence,
            trusted_tool_intents={
                candidate_tools[0].name: IntegrationIntent.N8N,
                BUSINESS_DECISION_TOOL: IntegrationIntent.NONE,
            },
            monotonic_clock=time.monotonic,
            clock=self._clock,
        )


class GatewayBusinessBenchmarkAuthority:
    """Project exact benchmark inputs from Captain's Gateway repository."""

    def __init__(self, repository: GatewayBusinessBenchmarkRepositoryPort) -> None:
        self._repository = repository

    def factory_job(self, job_id: UUID) -> object | None:
        try:
            return self._repository.job(job_id)
        except (KeyError, LookupError):
            return None

    def team_execution_evidence(
        self, job_id: UUID, attempt: int
    ) -> tuple[TeamExecutionEvidenceV1, ...]:
        return effective_team_execution_evidence(
            tuple(
                item
                for item in self._repository.workflow_artifacts(job_id)
                if isinstance(item, TeamExecutionEvidenceV1)
                and item.attempt == attempt
            )
        )

    def budget_projection(self, job_id: UUID) -> FactoryBudgetProjection | None:
        return self._repository.workflow_budget_projection(job_id)

    def candidate_ref(
        self, job_id: UUID, attempt: int, candidate_id: str
    ) -> ArtifactRef | None:
        if not candidate_id.strip():
            raise ValueError("candidate ID is required")
        references = {
            item.candidate_ref
            for item in self.team_execution_evidence(job_id, attempt)
            if isinstance(getattr(item, "candidate_ref", None), ArtifactRef)
        }
        if not references:
            return None
        if len(references) != 1:
            raise ValueError(
                "Gateway workflow evidence contains a mixed candidate reference"
            )
        return next(iter(references))


class ContentAddressedBenchmarkPolicyAuthority:
    """Load an immutable Captain policy binding from the benchmark CAS."""

    _BINDING_KIND = "benchmark-policy"

    def __init__(
        self, artifacts: BusinessBenchmarkContentAddressedArtifactStore
    ) -> None:
        self._artifacts = artifacts

    def policy_for(
        self, scope: ProductionBusinessBenchmarkScope
    ) -> CaptainBusinessBenchmarkPolicyBindingV1:
        identity = f"{scope.job.job_id}:{scope.selection.attempt}"
        reference = self._artifacts.binding(self._BINDING_KIND, identity)
        if reference is None:
            raise BusinessBenchmarkProductionPortError(
                "Captain benchmark policy binding is missing"
            )
        try:
            binding = CaptainBusinessBenchmarkPolicyBindingV1.model_validate_json(
                self._artifacts.read_bytes(reference)
            )
        except ValueError as exc:
            raise BusinessBenchmarkProductionPortError(
                "Captain benchmark policy binding is invalid"
            ) from exc
        if (
            binding.job_id != scope.job.job_id
            or binding.correlation_id != scope.job.correlation_id
            or binding.subject_version != scope.job.subject_version
            or binding.attempt != scope.selection.attempt
        ):
            raise BusinessBenchmarkProductionPortError(
                "Captain benchmark policy binding is stale or mixed"
            )
        return binding


class CaptainCanonicalSuiteAuthority:
    """Provision and reload deterministic suites inside Captain's private root."""

    def __init__(self, *, root: Path, seed_version_id: str) -> None:
        self._provisioner = CanonicalPrivateBusinessBenchmarkProvisioner(root)
        self._loader = CaptainPrivateBusinessBenchmarkSuiteLoader(root)
        self._seed_version_id = seed_version_id

    def canonical_suite(
        self, *, profile_id: str, suite_version: int
    ) -> tuple[PrivateHoldoutRef, BusinessBenchmarkSuiteV1]:
        provisioned = self._provisioner.provision(
            suite_version=suite_version,
            seed_version_id=self._seed_version_id,
        )
        selected = tuple(
            item for item in provisioned.suites if item.profile_id == profile_id
        )
        if len(selected) != 1:
            raise ValueError("canonical business benchmark profile is unsupported")
        item = selected[0]
        suite = self._loader.load_suite(
            item.suite_ref,
            expected_profile_id=item.profile_id,
            expected_suite_version=suite_version,
        )
        return item.suite_ref, suite


class CaptainCanonicalSuiteRepository:
    """Expose the same canonical suite authority to the Factory composition."""

    def __init__(
        self,
        authority: CaptainCanonicalSuiteAuthority,
        evidence: FilesystemBusinessBenchmarkEvidenceStore | None = None,
    ) -> None:
        self._authority = authority
        self._evidence = evidence
        self._by_reference: dict[PrivateHoldoutRef, BusinessBenchmarkSuiteV1] = {}

    def suite_ref(self, profile_id: str, suite_version: int) -> PrivateHoldoutRef:
        reference, suite = self._authority.canonical_suite(
            profile_id=profile_id,
            suite_version=suite_version,
        )
        previous = self._by_reference.setdefault(reference, suite)
        if previous != suite:
            raise ValueError("canonical benchmark suite changed after resolution")
        return reference

    def private_suite(
        self, reference: PrivateHoldoutRef
    ) -> BusinessBenchmarkSuiteV1:
        try:
            return self._by_reference[reference]
        except KeyError as exc:
            raise ValueError(
                "canonical benchmark suite must be resolved before its private body"
            ) from exc

    def record_run_receipt(
        self, receipt: BusinessBenchmarkRunReceiptV1
    ) -> ArtifactRef:
        return self._evidence_store().record_run_receipt(receipt)

    def record_case_receipt(self, receipt: BusinessBenchmarkReceiptV1) -> ArtifactRef:
        return self._evidence_store().record_case_receipt(receipt)

    def record_summary(self, summary: BusinessBenchmarkSummaryV1) -> ArtifactRef:
        return self._evidence_store().record_summary(summary)

    def summary(self, summary_id: UUID) -> BusinessBenchmarkSummaryV1 | None:
        return self._evidence_store().summary(summary_id)

    def canonical_summary(
        self, proposed: BusinessBenchmarkSummaryV1
    ) -> BusinessBenchmarkSummaryV1 | None:
        return self._evidence_store().canonical_summary(proposed)

    def _evidence_store(self) -> FilesystemBusinessBenchmarkEvidenceStore:
        if self._evidence is None:
            raise ValueError("canonical suite repository has no evidence store")
        return self._evidence


class ReleasedSkillCatalogPort(Protocol):
    def released_for(
        self, job: AgentFactoryJobV3, step: FactorySkillStep
    ) -> ReleasedHermesSkill: ...


class ActiveFactoryLeasePort(Protocol):
    def active(
        self,
        job: AgentFactoryJobV3,
        role: FactoryRole,
        attempt: int,
        now: datetime,
    ) -> FactoryLease: ...

    def record(self, lease: FactoryLease) -> FactoryLease: ...


class GatewayBenchmarkInvocationAuthority:
    """Reconstruct quality invocations only from Gateway skills and leases."""

    def __init__(
        self,
        *,
        repository: GatewayBusinessBenchmarkRepositoryPort,
        released_skills: ReleasedSkillCatalogPort,
        leases: ActiveFactoryLeasePort,
        clock: Callable[[], datetime],
    ) -> None:
        self._repository = repository
        self._released_skills = released_skills
        self._leases = leases
        self._clock = clock

    def runtime_invocation(
        self, *, job: AgentFactoryJobV3, attempt: int
    ) -> FactorySkillInvocationV1:
        observed = tuple(
            item.invocation
            for item in effective_team_execution_evidence(
                tuple(
                    item
                    for item in self._repository.workflow_artifacts(job.job_id)
                    if isinstance(item, TeamExecutionEvidenceV1)
                    and item.attempt == attempt
                    and item.invocation.step is FactorySkillStep.EXECUTE_TEAM
                )
            )
        )
        invocations = tuple(
            invocation
            for index, invocation in enumerate(observed)
            if invocation not in observed[:index]
        )
        if len(invocations) == 1:
            return invocations[0]
        if len(invocations) != job.execution_policy.required_live_runs:
            raise ValueError(
                "Gateway requires one exact execute-team invocation for the attempt"
            )
        canonical = invocations[0]
        if any(
            invocation.invocation_id
            != uuid5(
                NAMESPACE_URL,
                f"captain.factory-team-live:{invocation.idempotency_key}",
            )
            for invocation in invocations[1:]
        ) or any(
            invocation.model_copy(
                update={
                    "invocation_id": canonical.invocation_id,
                    "idempotency_key": canonical.idempotency_key,
                }
            )
            != canonical
            for invocation in invocations[1:]
        ):
            raise ValueError(
                "Gateway release execute-team invocations are stale or mixed"
            )
        return canonical

    def benchmark_invocation(
        self,
        *,
        job: AgentFactoryJobV3,
        attempt: int,
        suite_ref: PrivateHoldoutRef,
    ) -> FactorySkillInvocationV1:
        """Derive one immutable suite-scoped invocation from technical authority."""

        technical = self.runtime_invocation(job=job, attempt=attempt)
        if (
            suite_ref not in job.private_holdout_refs
            or suite_ref == technical.execution_scope_ref
        ):
            raise ValueError("benchmark suite invocation scope is stale or mixed")
        payload = {
            "job_id": str(job.job_id),
            "subject_version": job.subject_version,
            "attempt": attempt,
            "technical_invocation_id": str(technical.invocation_id),
            "technical_idempotency_key": technical.idempotency_key,
            "suite_ref_sha256": suite_ref.sha256,
        }
        idempotency_key = hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        now = self._utc_now()
        lease_epoch = now.replace(second=0, microsecond=0)
        benchmark_lease = issue_factory_lease(
            job=job,
            role=FactoryRole.REAL_CASE_TESTER,
            attempt=attempt,
            workspace_ref=(
                "workspace://business-benchmark-suite/"
                f"{job.job_id}/{attempt}/{suite_ref.sha256}/"
                f"{lease_epoch.strftime('%Y%m%dT%H%MZ')}"
            ),
            now=lease_epoch,
            integration_intent=technical.lease.integration_intent,
            duration=BUSINESS_BENCHMARK_LEASE_DURATION,
        )
        benchmark_lease = self._leases.record(benchmark_lease)
        return technical.model_copy(
            update={
                "invocation_id": uuid5(
                    NAMESPACE_URL,
                    f"captain.business-benchmark:{idempotency_key}",
                ),
                "idempotency_key": idempotency_key,
                "execution_scope_ref": suite_ref,
                "lease": benchmark_lease,
            }
        )

    def evaluation_invocation(
        self, *, job: AgentFactoryJobV3, attempt: int
    ) -> FactorySkillInvocationV1:
        return self._quality_invocation(
            job=job,
            attempt=attempt,
            step=FactorySkillStep.EVALUATE_TEAM,
            input_ref=job.input_ref,
        )

    def require_active_report(
        self, *, job: AgentFactoryJobV3, attempt: int
    ) -> None:
        self._quality_invocation(
            job=job,
            attempt=attempt,
            step=FactorySkillStep.REPORT_CAPTAIN,
            input_ref=job.input_ref,
        )

    def report_invocation(
        self,
        *,
        job: AgentFactoryJobV3,
        attempt: int,
        evaluation: TeamEvaluationV1,
    ) -> FactorySkillInvocationV1:
        return self._quality_invocation(
            job=job,
            attempt=attempt,
            step=FactorySkillStep.REPORT_CAPTAIN,
            input_ref=evaluation.artifact_ref,
        )

    def _quality_invocation(
        self,
        *,
        job: AgentFactoryJobV3,
        attempt: int,
        step: FactorySkillStep,
        input_ref: ArtifactRef,
    ) -> FactorySkillInvocationV1:
        now = self._utc_now()
        lease = self._leases.active(
            job,
            FactoryRole.QUALITY_WARDEN,
            attempt,
            now,
        )
        validate_factory_lease(
            lease,
            job=job,
            role=FactoryRole.QUALITY_WARDEN,
            attempt=attempt,
            now=now,
        )
        released = self._released_skills.released_for(job, step)
        if (
            released.skill_id != FACTORY_SKILL_ID_BY_STEP[step]
            or released.status != "released"
            or released.released_at > now
        ):
            raise ValueError("quality skill release is unavailable or stale")
        payload = {
            "job_id": str(job.job_id),
            "correlation_id": str(job.correlation_id),
            "subject_version": job.subject_version,
            "attempt": attempt,
            "step": step.value,
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        idempotency_key = hashlib.sha256(encoded).hexdigest()
        return FactorySkillInvocationV1(
            schema="captain.factory-skill-invocation.v1",
            invocation_id=uuid5(
                NAMESPACE_URL,
                f"captain.factory-skill:{idempotency_key}",
            ),
            job_id=job.job_id,
            correlation_id=job.correlation_id,
            subject_version=job.subject_version,
            attempt=attempt,
            step=step,
            released_skill=released,
            input_ref=input_ref,
            input_sha256=input_ref.sha256,
            lease=lease,
            idempotency_key=idempotency_key,
            acceptance_assertion_ids=job.acceptance_assertion_ids,
        )

    def _utc_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("benchmark invocation clock must be UTC")
        return value


class FilesystemBenchmarkReceiptFinalizer:
    """Finalize a run receipt through Captain's append-only evidence store."""

    def __init__(self, evidence: FilesystemBusinessBenchmarkEvidenceStore) -> None:
        self._evidence = evidence

    def finalize(
        self,
        *,
        profile: Literal["claims", "renewal"],
        receipt: BusinessBenchmarkRunReceiptV1,
    ) -> BusinessBenchmarkFinalizedReceiptV1:
        reference = self._evidence.record_run_receipt(receipt)
        return BusinessBenchmarkFinalizedReceiptV1(
            profile=profile,
            receipt=receipt,
            receipt_ref=reference,
        )


def compose_production_business_benchmark_composition(
    settings: LiveBusinessBenchmarkSettings,
    *,
    config: ProductionBusinessBenchmarkBootstrapConfig,
    ports: ProductionBusinessBenchmarkBootstrapPorts,
) -> ProductionBusinessBenchmarkComposition:
    """Compose the product gate from durable Captain authorities.

    This function performs no provider or n8n call.  The injected executor
    builder receives only the resolved scope and durable runtime authorities;
    it is the sole remaining deployment-specific boundary for fresh Host
    AutoGen sessions and command/grant-bound n8n access.
    """

    canonical = LiveBusinessBenchmarkSettings.model_validate(
        settings.model_dump(mode="python")
    )
    expected_receipt_root = canonical.evidence_root.resolve() / "captain" / "receipts"
    if config.evidence_store_root.resolve() != expected_receipt_root:
        raise ValueError("benchmark bootstrap config belongs to a different run root")
    if not callable(ports.executor_builder) or not callable(
        ports.execution_policy_builder
    ):
        raise ValueError("benchmark executor and policy builders must be callable")
    now = ports.clock()
    if now.tzinfo is None or now.utcoffset() != timezone.utc.utcoffset(now):
        raise ValueError("benchmark bootstrap clock must be UTC")

    artifacts = BusinessBenchmarkContentAddressedArtifactStore(config.cas_root)
    human_review = CaptainHumanReviewStore(
        config.human_review_root,
        completion_timeout_seconds=config.human_review_timeout_seconds,
    )
    runtime_authorities = ProductionBusinessBenchmarkRuntimeAuthorities(
        artifacts=artifacts,
        human_review=human_review,
        provider_state_root=config.provider_state_root,
    )
    suite_authority = CaptainCanonicalSuiteAuthority(
        root=config.private_suite_root,
        seed_version_id=config.seed_version_id,
    )
    evidence = FilesystemBusinessBenchmarkEvidenceStore(
        config.evidence_store_root
    )
    private_repository = CaptainCanonicalSuiteRepository(
        suite_authority,
        evidence,
    )
    gateway = GatewayBusinessBenchmarkAuthority(ports.gateway_repository)
    invocations = GatewayBenchmarkInvocationAuthority(
        repository=ports.gateway_repository,
        released_skills=ports.released_skills,
        leases=ports.leases,
        clock=ports.clock,
    )
    resolver = ProductionBusinessBenchmarkScopeResolver(
        gateway=gateway,
        suites=suite_authority,
        candidates=(
            ports.candidate_authority
            if ports.candidate_authority is not None
            else BusinessBenchmarkCandidateAuthority(artifacts)
        ),
        invocations=invocations,
    )
    factory = BusinessBenchmarkFactoryComposition(
        private_repository=private_repository,
        gateway_repository=ports.gateway_repository,
        evaluator=BusinessBenchmarkEvaluator(clock=ports.clock),
        team_evaluator=TeamEvaluationService(clock=ports.clock),
        feedback_builder=FactoryFeedbackBuilder(clock=ports.clock),
    )
    return ProductionBusinessBenchmarkComposition(
        resolver=resolver,
        factory_composition=factory,
        invocation_authority=invocations,
        executor_factory=lambda scope: ports.executor_builder(
            scope,
            runtime_authorities,
        ),
        replay_store_factory=lambda scope: FilesystemBusinessBenchmarkReplayStore(
            config.replay_root / str(scope.job.job_id) / f"attempt-{scope.selection.attempt}"
        ),
        execution_policy_factory=ports.execution_policy_builder,
        benchmark_policy_authority=ContentAddressedBenchmarkPolicyAuthority(
            artifacts
        ),
        receipt_finalizer=FilesystemBenchmarkReceiptFinalizer(evidence),
        clock=ports.clock,
    )


def build_production_business_benchmark_composition(
    settings: LiveBusinessBenchmarkSettings,
) -> ProductionBusinessBenchmarkCompositionPort:
    """Build from real ports, or report the first exact missing authority.

    Static bootstrap validation is deliberately completed before any Gateway,
    provider, or n8n connection.  Durable human review is available through
    :class:`CaptainHumanReviewStore`; automatic approval is never substituted.
    The concrete request-scoped Claims and Renewal executors below construct
    fresh Host AutoGen sessions. Default loading still requires injected Renewal
    n8n deployment ports and a complete Gateway HTTP client; it must never open
    MariaDB from this agent-runtime package.
    """

    canonical = LiveBusinessBenchmarkSettings.model_validate(
        settings.model_dump(mode="python")
    )
    environment = os.environ
    ProductionBusinessBenchmarkBootstrapConfig.from_environment(
        canonical,
        environment,
    )
    ConfiguredBusinessBenchmarkExecutionPolicyBuilder.from_environment(
        canonical,
        environment,
    )
    if any(selection.profile == "renewal" for selection in canonical.selections):
        raise ProductionAdapterUnavailableError(
            "CaptainRenewalN8nBootstrapPorts are not injected into the default "
            "loader: provide the request-bound grant authority, short-lived broker "
            "token issuer, Captain endpoint, and deployed workflow binding"
        )

    if canonical.provider != "openai" or canonical.provider_secret_name != "OPENAI_API_KEY":
        raise ValueError("benchmark provider authority is not allowlisted")
    _required_bootstrap_setting(environment, "CAPTAIN_GATEWAY_URL")
    _required_bootstrap_setting(environment, "CAPTAIN_GATEWAY_TOKEN")
    raise ProductionAdapterUnavailableError(
        "CaptainBusinessBenchmarkGatewayClientPort is not implemented: the "
        "existing Gateway HTTP surface does not expose every exact skill-assignment, "
        "lease, budget-reservation, usage, job, workflow-evidence, and benchmark-write "
        "operation required by the production composition; direct MariaDB access "
        "outside gateway is forbidden"
    )


def _required_bootstrap_setting(
    environment: Mapping[str, str],
    name: str,
) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise ValueError(f"required benchmark bootstrap setting is missing: {name}")
    return value


__all__ = [
    "CaptainBusinessBenchmarkExecutorBuilderPort",
    "CaptainBusinessBenchmarkHostSessionFactory",
    "CaptainClaimsBusinessBenchmarkExecutorBuilder",
    "CaptainRenewalBusinessBenchmarkExecutorBuilder",
    "CaptainRenewalBusinessBenchmarkHostSessionFactory",
    "CaptainRenewalBusinessBenchmarkN8nPorts",
    "CaptainRenewalN8nAuthorizationPort",
    "CaptainCanonicalSuiteAuthority",
    "CaptainCanonicalSuiteRepository",
    "ConfiguredBusinessBenchmarkExecutionPolicyBuilder",
    "ContentAddressedBenchmarkPolicyAuthority",
    "FilesystemBenchmarkReceiptFinalizer",
    "GatewayBenchmarkInvocationAuthority",
    "GatewayBusinessBenchmarkAuthority",
    "GatewayBusinessBenchmarkRepositoryPort",
    "ProductionBusinessBenchmarkBootstrapConfig",
    "ProductionBusinessBenchmarkBootstrapPorts",
    "ProductionBusinessBenchmarkRuntimeAuthorities",
    "build_production_business_benchmark_composition",
    "compose_production_business_benchmark_composition",
]
