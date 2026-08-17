"""Gateway-native production bootstrap for the sealed capability assembly.

The module deliberately contains no import-time composition.  Configuration is
validated in full before a path is resolved, a schema is created, or a network
client is constructed.  Every authority remains bound to the one GatewayStore
owned by ``gateway.app``.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID
from weakref import WeakKeyDictionary

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from agenten.agent_factory.capability_live_runner import CapabilityLiveStage
from agenten.agent_factory.contracts import AgentFactoryJobV3
from agenten.agent_factory.execution_policy import FactoryExecutionPolicyV1
from agenten.agent_runtime.captain_production_adapters import (
    ContentAddressedArtifactAdapter,
)
from agenten.agent_runtime.contracts import ArtifactRef
from agenten.agent_runtime.production_bootstrap import (
    RuntimeAdapterManifestError,
    load_verified_repository_module,
    read_verified_repository_bytes,
)
from agenten.delivery.minibook_client import MinibookClient, validate_service_base_url
from agenten.delivery.minibook_events import (
    MinibookProjectionAcknowledgementV1,
    MinibookProjectionEvent,
    minibook_projection_acknowledgement_id,
)
from agenten.delivery.projection_cursor import ProjectionCursorStore
from agenten.delivery.projector import MinibookProjector
from agenten.validation.contracts import WorkBatch
from gateway.agent_factory_live_composition import AgentFactoryLiveComposition
from gateway.agent_factory_live_composition import (
    GatewayTechnicalTeamExecutionPortsProvider,
    ProductionFactoryTeamExecutionPort,
)
from gateway.agent_factory_dispatch_runner import GatewayNextActionLeaseIssuer
from gateway.factory_repository import GatewayFactoryBudgetLedger, GatewayFactoryRepository
from agenten.agent_factory.business_benchmark_dispatch import BusinessBenchmarkDispatchService
from agenten.agent_factory.business_benchmark_production_ports import (
    BusinessBenchmarkPricingAuthority,
    ConfiguredBusinessBenchmarkPricingSource,
    OpenAIBusinessBenchmarkModelClientBuilder,
)
from agenten.agent_factory.candidate_evaluation import (
    CandidateEvaluationFactory,
    GatewayForgeCandidateProvider,
)
from agenten.agent_factory.hermes_cli import (
    FilesystemFactorySkillReplayStore,
    HermesCliFactory,
)
from agenten.agent_factory.orchestration import FactoryDispatcher
from agenten.agent_factory.production_dispatch_runner import ProductionFactoryDispatchRunner
from agenten.agent_factory.service import FactoryCoordinator
from agenten.agent_factory.state_machine import FactoryActionKind
from gateway.capability_assembly_contracts import (
    CapabilityAssemblyReconciliationOutcome,
    CapabilityAssemblyReconciliationRequest,
)
from gateway.capability_assembly_production import (
    AsyncMinibookProjectorAdapter,
    CapabilityAssemblyProductionAuthorities,
    CapabilityAssemblyProductionConfigurationError,
    CapabilityAssemblyProductionSettings,
    GatewayMinibookProjectionEffectsAdapter,
    ProductionCapabilityAssemblyComposition,
    RepositoryFactoryInputArtifactStore,
    _current_stage_request,
    build_capability_assembly_controller,
)
from gateway.capability_benchmark_process_executor import (
    GatewayHttpBenchmarkProcessTransport,
    GatewayIsolatedBenchmarkExecutorClient,
)
from gateway.capability_generic_benchmark import (
    BenchmarkToolEvidenceVerificationRequest,
    ContentAddressedBenchmarkEvidenceResolver,
    GenericBenchmarkCategory,
    GenericBenchmarkReceipt,
    GenericBenchmarkRecordingRequest,
    GenericPrivateCaseRef,
    GenericToolCallEvidence,
    generic_benchmark_passes,
)
from gateway.capability_live_current_adapters import CapabilityProjectionReadback
from gateway.capability_live_factory_adapter import CurrentCapabilityFactoryAdapter
from gateway.capability_live_pipeline import (
    CapabilityFactoryAuthority,
    CapabilityLiveAuthorityUnavailable,
)
from gateway.capability_live_provisioning import CurrentAuthorityCapabilityFactoryProvisioner
from gateway.capability_resume_client import GatewayCapabilityResumeClient
from gateway.registry_feed import factory_promotion_projection
from gateway.store import GatewayStore


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TOOL_AUTHORITY_ID = UUID("14ec6774-b648-44df-9b66-f50957c5c6f7")
_BENCHMARK_RECORDER_AUTHORITY_ID = UUID("fefc0e43-6955-4d50-aae2-b2ef5051e9af")
_BOOTSTRAP_RESOURCES: WeakKeyDictionary[
    ProductionCapabilityAssemblyComposition, tuple[httpx.AsyncClient, httpx.Client]
] = WeakKeyDictionary()


class CapabilityAssemblyBootstrapConfigurationError(ValueError):
    """Production bootstrap is incomplete; the message never includes values."""


@dataclass(frozen=True, repr=False)
class CapabilityAssemblyBootstrapSettings:
    assembly: CapabilityAssemblyProductionSettings
    benchmark_url: str
    benchmark_token: SecretStr
    benchmark_authority_id: UUID
    artifact_root: Path
    authority_root: Path
    gateway_url: str
    gateway_token: SecretStr
    minibook_url: str
    minibook_api_key: SecretStr
    minibook_projection_api_key: SecretStr
    runtime_url: str
    runtime_token: SecretStr
    provider_token: SecretStr
    provider_url: str
    provider_policy_sha256: str
    provider_price_card_sha256: str
    provider_context_sha256: str
    provider_proxy_executable_sha256: str
    provider_proxy_code_sha256: str
    provider_paid_command_sha256: str
    provider_hard_ceiling_enforced: bool
    allowed_hosts: tuple[str, ...] = ()


_REQUIRED_BOOTSTRAP_NAMES = (
    "CAPTAIN_CAPABILITY_BENCHMARK_URL",
    "CAPTAIN_CAPABILITY_BENCHMARK_TOKEN",
    "CAPTAIN_CAPABILITY_BENCHMARK_AUTHORITY_ID",
    "CAPTAIN_CAPABILITY_ARTIFACT_ROOT",
    "CAPTAIN_CAPABILITY_AUTHORITY_ROOT",
    "CAPTAIN_GATEWAY_URL",
    "CAPTAIN_GATEWAY_TOKEN",
    "MINIBOOK_BACKEND_URL",
    "MINIBOOK_API_KEY",
    "MINIBOOK_PROJECTION_API_KEY",
    "CAPTAIN_RUNTIME_URL",
    "CAPTAIN_RUNTIME_TOKEN",
    "CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN",
    "CAPTAIN_PROVIDER_PROXY_URL",
    "CAPTAIN_PROVIDER_POLICY_SHA256",
    "CAPTAIN_PROVIDER_PRICE_CARD_SHA256",
    "CAPTAIN_PROVIDER_CONTEXT_SHA256",
    "CAPTAIN_PROVIDER_PROXY_EXECUTABLE_SHA256",
    "CAPTAIN_PROVIDER_PROXY_CODE_SHA256",
    "CAPTAIN_PROVIDER_PAID_COMMAND_SHA256",
    "CAPTAIN_PROVIDER_HARD_CEILING_ENFORCED",
)


def load_capability_assembly_bootstrap_settings(
    *,
    environment: Mapping[str, str],
    repository_root: Path,
    touch_probe: Callable[[], None] | None = None,
) -> CapabilityAssemblyBootstrapSettings:
    """Validate scalar authority settings before touching filesystem/network."""

    raw = {name: environment.get(name, "").strip() for name in _REQUIRED_BOOTSTRAP_NAMES}
    if any(not value for value in raw.values()):
        raise CapabilityAssemblyBootstrapConfigurationError(
            "capability assembly bootstrap configuration is incomplete"
        )
    credential_names = (
        "CAPTAIN_CAPABILITY_BENCHMARK_TOKEN",
        "CAPTAIN_GATEWAY_TOKEN",
        "MINIBOOK_API_KEY",
        "MINIBOOK_PROJECTION_API_KEY",
        "CAPTAIN_RUNTIME_TOKEN",
        "CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN",
    )
    credentials = tuple(raw[name] for name in credential_names)
    if any(
        secrets.compare_digest(left, right)
        for index, left in enumerate(credentials)
        for right in credentials[index + 1 :]
    ):
        raise CapabilityAssemblyBootstrapConfigurationError(
            "capability assembly credentials must be distinct"
        )
    for digest_name in (
        "CAPTAIN_PROVIDER_POLICY_SHA256",
        "CAPTAIN_PROVIDER_PRICE_CARD_SHA256",
        "CAPTAIN_PROVIDER_CONTEXT_SHA256",
        "CAPTAIN_PROVIDER_PROXY_EXECUTABLE_SHA256",
        "CAPTAIN_PROVIDER_PROXY_CODE_SHA256",
        "CAPTAIN_PROVIDER_PAID_COMMAND_SHA256",
    ):
        if _SHA256.fullmatch(raw[digest_name]) is None:
            raise CapabilityAssemblyBootstrapConfigurationError(
                "capability assembly provider authority is invalid"
            )
    if raw["CAPTAIN_PROVIDER_HARD_CEILING_ENFORCED"] != "true":
        raise CapabilityAssemblyBootstrapConfigurationError(
            "capability assembly provider authority is invalid"
        )
    try:
        benchmark_authority_id = UUID(raw["CAPTAIN_CAPABILITY_BENCHMARK_AUTHORITY_ID"])
    except ValueError:
        raise CapabilityAssemblyBootstrapConfigurationError(
            "capability assembly benchmark authority is invalid"
        ) from None
    allowed_hosts = tuple(
        sorted(
            {
                value.strip().casefold()
                for value in environment.get(
                    "CAPTAIN_CAPABILITY_ALLOWED_HOSTS", ""
                ).split(",")
                if value.strip()
            }
        )
    )
    benchmark_url = _allowed_endpoint(
        raw["CAPTAIN_CAPABILITY_BENCHMARK_URL"],
        allowed_hosts=allowed_hosts,
        label="benchmark",
    )
    gateway_url = _allowed_service_url(
        raw["CAPTAIN_GATEWAY_URL"], allowed_hosts=allowed_hosts, label="Gateway"
    )
    minibook_url = _allowed_service_url(
        raw["MINIBOOK_BACKEND_URL"], allowed_hosts=allowed_hosts, label="Minibook"
    )
    provider_url = _allowed_service_url(
        raw["CAPTAIN_PROVIDER_PROXY_URL"],
        allowed_hosts=allowed_hosts,
        label="provider proxy",
    )
    try:
        assembly = CapabilityAssemblyProductionSettings.from_environment(environment)
    except CapabilityAssemblyProductionConfigurationError:
        raise CapabilityAssemblyBootstrapConfigurationError(
            "capability assembly input authority is incomplete"
        ) from None
    root = repository_root.resolve(strict=True)
    artifact_root = _confined_path(root, raw["CAPTAIN_CAPABILITY_ARTIFACT_ROOT"])
    authority_root = _confined_path(root, raw["CAPTAIN_CAPABILITY_AUTHORITY_ROOT"])
    if touch_probe is not None:
        touch_probe()
    return CapabilityAssemblyBootstrapSettings(
        assembly=assembly,
        benchmark_url=benchmark_url,
        benchmark_token=SecretStr(raw["CAPTAIN_CAPABILITY_BENCHMARK_TOKEN"]),
        benchmark_authority_id=benchmark_authority_id,
        artifact_root=artifact_root,
        authority_root=authority_root,
        gateway_url=gateway_url,
        gateway_token=SecretStr(raw["CAPTAIN_GATEWAY_TOKEN"]),
        minibook_url=minibook_url,
        minibook_api_key=SecretStr(raw["MINIBOOK_API_KEY"]),
        minibook_projection_api_key=SecretStr(
            raw["MINIBOOK_PROJECTION_API_KEY"]
        ),
        runtime_token=SecretStr(raw["CAPTAIN_RUNTIME_TOKEN"]),
        runtime_url=_allowed_service_url(
            raw["CAPTAIN_RUNTIME_URL"],
            allowed_hosts=allowed_hosts,
            label="Runtime",
        ),
        provider_token=SecretStr(raw["CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN"]),
        provider_url=provider_url,
        provider_policy_sha256=raw["CAPTAIN_PROVIDER_POLICY_SHA256"],
        provider_price_card_sha256=raw["CAPTAIN_PROVIDER_PRICE_CARD_SHA256"],
        provider_context_sha256=raw["CAPTAIN_PROVIDER_CONTEXT_SHA256"],
        provider_proxy_executable_sha256=raw[
            "CAPTAIN_PROVIDER_PROXY_EXECUTABLE_SHA256"
        ],
        provider_proxy_code_sha256=raw["CAPTAIN_PROVIDER_PROXY_CODE_SHA256"],
        provider_paid_command_sha256=raw["CAPTAIN_PROVIDER_PAID_COMMAND_SHA256"],
        provider_hard_ceiling_enforced=True,
        allowed_hosts=allowed_hosts,
    )


def _allowed_service_url(
    value: str, *, allowed_hosts: tuple[str, ...], label: str
) -> str:
    try:
        normalized = validate_service_base_url(value)
    except ValueError:
        raise CapabilityAssemblyBootstrapConfigurationError(
            f"capability assembly {label} endpoint is not allowed"
        ) from None
    host = (urlsplit(normalized).hostname or "").casefold()
    if not _is_loopback(host) and host not in allowed_hosts:
        raise CapabilityAssemblyBootstrapConfigurationError(
            f"capability assembly {label} endpoint is not allowed"
        )
    return normalized


def _allowed_endpoint(
    value: str, *, allowed_hosts: tuple[str, ...], label: str
) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise CapabilityAssemblyBootstrapConfigurationError(
            f"capability assembly {label} endpoint is not allowed"
        )
    host = parsed.hostname.casefold()
    if not _is_loopback(host) and (
        parsed.scheme != "https" or host not in allowed_hosts
    ):
        raise CapabilityAssemblyBootstrapConfigurationError(
            f"capability assembly {label} endpoint is not allowed"
        )
    return value.rstrip("/")


def _is_loopback(host: str) -> bool:
    return host in {"localhost", "127.0.0.1", "::1"}


class CapabilityProductionProcessBindingV1(BaseModel):
    """Secret-free operator binding for one concrete production process."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    service: Literal[
        "gateway", "benchmark", "runtime", "provider_proxy", "minibook", "n8n"
    ]
    base_url: str
    health_url: str
    execute_url: str | None = None
    authentication: Literal["bearer", "x-n8n-api-key"] = "bearer"
    credential_env: Literal[
        "CAPTAIN_GATEWAY_TOKEN",
        "CAPTAIN_CAPABILITY_BENCHMARK_TOKEN",
        "CAPTAIN_RUNTIME_TOKEN",
        "CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN",
        "MINIBOOK_API_KEY",
        "CAPTAIN_N8N_API_KEY",
    ]
    authenticated_health_url: str
    authenticated_health_credential_env: Literal[
        "CAPTAIN_GATEWAY_TOKEN",
        "CAPTAIN_CAPABILITY_BENCHMARK_TOKEN",
        "CAPTAIN_RUNTIME_TOKEN",
        "CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN",
        "MINIBOOK_PROJECTION_API_KEY",
        "CAPTAIN_N8N_API_KEY",
    ]
    authenticated_health_header: Literal["Authorization", "X-N8N-API-KEY"]
    start_argv: tuple[str, ...] = Field(min_length=1)
    stop_argv: tuple[str, ...] = Field(min_length=1)
    restart_argv: tuple[str, ...] = Field(min_length=1)


class CapabilityProductionBundleManifestV1(BaseModel):
    """Digest-bound, secret-free contract consumed by the demo orchestrator."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)
    schema_name: Literal["captain.capability-production-bundle-manifest.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    provider_supervisor_mode: Literal["dynamic_gateway_context_v1"]
    provider_context_reference_header: Literal["X-Captain-Provider-Context-Ref"]
    provider_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_price_card_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    gateway: CapabilityProductionProcessBindingV1
    benchmark: CapabilityProductionProcessBindingV1
    runtime: CapabilityProductionProcessBindingV1
    provider_proxy: CapabilityProductionProcessBindingV1
    minibook: CapabilityProductionProcessBindingV1
    n8n: CapabilityProductionProcessBindingV1
    minibook_evidence_url_template: str
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CapabilityProductionBundleHealthV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)
    schema_name: Literal["captain.capability-production-bundle-health.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["ready", "incomplete"]
    ready_services: tuple[str, ...]


_OPERATOR_REQUIRED_NAMES = (
    "CAPTAIN_GATEWAY_URL",
    "CAPTAIN_GATEWAY_TOKEN",
    "CAPTAIN_CAPABILITY_BENCHMARK_URL",
    "CAPTAIN_CAPABILITY_BENCHMARK_TOKEN",
    "CAPTAIN_RUNTIME_URL",
    "CAPTAIN_RUNTIME_TOKEN",
    "CAPTAIN_PROVIDER_PROXY_URL",
    "CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN",
    "CAPTAIN_PROVIDER_POLICY_SHA256",
    "CAPTAIN_PROVIDER_PRICE_CARD_SHA256",
    "MINIBOOK_BACKEND_URL",
    "MINIBOOK_API_KEY",
    "MINIBOOK_PROJECTION_API_KEY",
)


def load_capability_production_bundle_manifest(
    *, environment: Mapping[str, str]
) -> CapabilityProductionBundleManifestV1:
    """Validate and export operator topology without filesystem or network I/O."""

    raw = {name: environment.get(name, "").strip() for name in _OPERATOR_REQUIRED_NAMES}
    if any(not value for value in raw.values()):
        raise CapabilityAssemblyBootstrapConfigurationError(
            "capability assembly bootstrap configuration is incomplete"
        )
    credentials = tuple(
        raw[name]
        for name in _OPERATOR_REQUIRED_NAMES
        if name.endswith("_TOKEN") or name.endswith("_KEY")
    )
    if any(
        secrets.compare_digest(left, right)
        for index, left in enumerate(credentials)
        for right in credentials[index + 1 :]
    ):
        raise CapabilityAssemblyBootstrapConfigurationError(
            "capability assembly credentials must be distinct"
        )
    for name in ("CAPTAIN_PROVIDER_POLICY_SHA256", "CAPTAIN_PROVIDER_PRICE_CARD_SHA256"):
        if _SHA256.fullmatch(raw[name]) is None:
            raise CapabilityAssemblyBootstrapConfigurationError(
                "capability assembly provider authority is invalid"
            )
    allowed_hosts = tuple(
        sorted(
            value.strip().casefold()
            for value in environment.get("CAPTAIN_CAPABILITY_ALLOWED_HOSTS", "").split(",")
            if value.strip()
        )
    )
    gateway = _allowed_service_url(raw["CAPTAIN_GATEWAY_URL"], allowed_hosts=allowed_hosts, label="Gateway")
    runtime = _allowed_service_url(raw["CAPTAIN_RUNTIME_URL"], allowed_hosts=allowed_hosts, label="Runtime")
    minibook = _allowed_service_url(raw["MINIBOOK_BACKEND_URL"], allowed_hosts=allowed_hosts, label="Minibook")
    provider = _allowed_service_url(raw["CAPTAIN_PROVIDER_PROXY_URL"], allowed_hosts=allowed_hosts, label="provider proxy")
    benchmark = _allowed_endpoint(raw["CAPTAIN_CAPABILITY_BENCHMARK_URL"], allowed_hosts=allowed_hosts, label="benchmark")
    n8n = _allowed_service_url(
        environment.get("CAPTAIN_N8N_URL", "http://127.0.0.1:5679").strip(),
        allowed_hosts=allowed_hosts,
        label="n8n",
    )
    if not benchmark.endswith("/v1/execute"):
        raise CapabilityAssemblyBootstrapConfigurationError(
            "capability assembly operator endpoint contract is invalid"
        )
    provider_root = provider.removesuffix("/v1")
    material: dict[str, object] = {
        "schema": "captain.capability-production-bundle-manifest.v1",
        "provider_supervisor_mode": "dynamic_gateway_context_v1",
        "provider_context_reference_header": "X-Captain-Provider-Context-Ref",
        "provider_policy_sha256": raw["CAPTAIN_PROVIDER_POLICY_SHA256"],
        "provider_price_card_sha256": raw["CAPTAIN_PROVIDER_PRICE_CARD_SHA256"],
        "gateway": _process_binding(
            "gateway", gateway, f"{gateway}/v1/capability-assemblies/health",
            credential_env="CAPTAIN_GATEWAY_TOKEN",
        ),
        "benchmark": _process_binding(
            "benchmark", benchmark.removesuffix("/v1/execute"),
            benchmark.removesuffix("/v1/execute") + "/healthz", benchmark,
            credential_env="CAPTAIN_CAPABILITY_BENCHMARK_TOKEN",
        ),
        "runtime": _process_binding(
            "runtime", runtime, f"{runtime}/health",
            credential_env="CAPTAIN_RUNTIME_TOKEN",
        ),
        "provider_proxy": _process_binding(
            "provider_proxy", provider_root, f"{provider_root}/v1/healthz",
            credential_env="CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN",
        ),
        "minibook": _process_binding(
            "minibook", minibook, f"{minibook}/health",
            credential_env="MINIBOOK_API_KEY",
            authenticated_health_url=f"{minibook}/api/v1/projection-health",
            authenticated_health_credential_env="MINIBOOK_PROJECTION_API_KEY",
        ),
        "n8n": _process_binding(
            "n8n", n8n, f"{n8n}/healthz",
            credential_env="CAPTAIN_N8N_API_KEY",
            authenticated_health_url=f"{n8n}/api/v1/workflows?limit=1",
            authenticated_health_header="X-N8N-API-KEY",
            authentication="x-n8n-api-key",
        ),
        "minibook_evidence_url_template": (
            f"{minibook}/api/v1/projection-assemblies/{{assembly_id}}/evidence"
        ),
    }
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return CapabilityProductionBundleManifestV1.model_validate(
        {**material, "manifest_sha256": digest}
    )


def _process_binding(
    service: str, base_url: str, health_url: str, execute_url: str | None = None,
    *, credential_env: str,
    authenticated_health_url: str | None = None,
    authenticated_health_credential_env: str | None = None,
    authenticated_health_header: str = "Authorization",
    authentication: str = "bearer",
) -> dict[str, object]:
    prefix = ("python", "-m", "gateway.capability_process_operator", "--service", service)
    return {
        "service": service,
        "base_url": base_url,
        "health_url": health_url,
        "execute_url": execute_url,
        "authentication": authentication,
        "credential_env": credential_env,
        "authenticated_health_url": authenticated_health_url or health_url,
        "authenticated_health_credential_env": (
            authenticated_health_credential_env or credential_env
        ),
        "authenticated_health_header": authenticated_health_header,
        "start_argv": (*prefix, "start"),
        "stop_argv": (*prefix, "stop"),
        "restart_argv": (*prefix, "restart"),
    }


def _confined_path(root: Path, raw: str) -> Path:
    path = Path(raw)
    if path.is_absolute() or ".." in path.parts:
        raise CapabilityAssemblyBootstrapConfigurationError(
            "capability assembly local path is not repository-confined"
        )
    resolved = (root / path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise CapabilityAssemblyBootstrapConfigurationError(
            "capability assembly local path is not repository-confined"
        ) from None
    return resolved


class CaptainBenchmarkToolEvidenceAuthority:
    """Verify exact Gateway/CAS-issued call, lease, and terminal receipt chain."""

    authority_id = _TOOL_AUTHORITY_ID

    def __init__(self, *, gateway_store: GatewayStore, artifacts: object) -> None:
        self.gateway_store = gateway_store
        self._artifacts = artifacts

    def issue(
        self,
        *,
        case_ref: GenericPrivateCaseRef,
        tool_key: str,
        operation: str,
        call_id: str,
        lease_id: str,
        trace_id: UUID,
        run_id: UUID,
        model_policy_digest: str,
        result_ref_sha256: str,
        batch_id: str,
        execution_result_id: UUID,
    ) -> GenericToolCallEvidence:
        """Issue canonical bytes and durably register their exact references."""

        if (
            not tool_key
            or not operation
            or not call_id
            or not lease_id
            or _SHA256.fullmatch(case_ref.case_digest) is None
            or _SHA256.fullmatch(model_policy_digest) is None
            or _SHA256.fullmatch(result_ref_sha256) is None
            or not batch_id
            or not isinstance(execution_result_id, UUID)
        ):
            raise CapabilityLiveAuthorityUnavailable(
                "benchmark tool evidence issuance is invalid"
            )
        common = {
            "case_digest": case_ref.case_digest,
            "tool_key": tool_key,
            "trace_id": str(trace_id),
            "run_id": str(run_id),
            "model_policy_digest": model_policy_digest,
        }

        def put(payload: dict[str, object]) -> ArtifactRef:
            body = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            ).encode()
            reference = self._artifacts.put(body, "application/json")
            if (
                not isinstance(reference, ArtifactRef)
                or reference.sha256 != hashlib.sha256(body).hexdigest()
                or self._artifacts.read_bytes(reference) != body
            ):
                raise CapabilityLiveAuthorityUnavailable(
                    "benchmark tool evidence artifact readback failed"
                )
            return reference

        call_ref = put({
            "schema": "captain.capability-benchmark-tool-call.v1",
            **common,
            "operation": operation,
            "call_id": call_id,
        })
        lease_ref = put({
            "schema": "captain.capability-benchmark-tool-lease.v1",
            **common,
            "call_id": call_id,
            "call_sha256": call_ref.sha256,
            "lease_id": lease_id,
            "scope": [operation],
        })
        receipt_ref = put({
            "schema": "captain.capability-benchmark-tool-receipt.v1",
            **common,
            "call_id": call_id,
            "call_sha256": call_ref.sha256,
            "lease_sha256": lease_ref.sha256,
            "status": "succeeded",
            "result_ref_sha256": result_ref_sha256,
        })
        try:
            self.gateway_store.record_capability_tool_evidence(
                case_digest=case_ref.case_digest,
                tool_key=tool_key,
                trace_id=trace_id,
                run_id=run_id,
                model_policy_digest=model_policy_digest,
                call_ref=call_ref,
                lease_ref=lease_ref,
                receipt_ref=receipt_ref,
                batch_id=batch_id,
                execution_result_id=execution_result_id,
            )
        except Exception:
            raise CapabilityLiveAuthorityUnavailable(
                "benchmark tool evidence registration failed"
            ) from None
        issued = GenericToolCallEvidence(
            tool_key=tool_key,
            call_ref=call_ref,
            lease_ref=lease_ref,
            receipt_ref=receipt_ref,
        )
        verification = BenchmarkToolEvidenceVerificationRequest(
            case_ref=case_ref,
            tool_key=tool_key,
            call_ref=call_ref,
            lease_ref=lease_ref,
            receipt_ref=receipt_ref,
            trace_id=trace_id,
            run_id=run_id,
            model_policy_digest=model_policy_digest,
        )
        if self.verify(verification) is not True:
            raise CapabilityLiveAuthorityUnavailable(
                "benchmark tool evidence registry readback failed"
            )
        return issued

    def verify(self, request: BenchmarkToolEvidenceVerificationRequest) -> bool:
        try:
            call = self._read(request.call_ref)
            lease = self._read(request.lease_ref)
            receipt = self._read(request.receipt_ref)
            registry = self.gateway_store.capability_tool_evidence(
                request.receipt_ref.sha256
            )
        except Exception:
            return False
        common = {
            "case_digest": request.case_ref.case_digest,
            "tool_key": request.tool_key,
            "trace_id": str(request.trace_id),
            "run_id": str(request.run_id),
            "model_policy_digest": request.model_policy_digest,
        }
        expected_registry = {
            **common,
            "call_ref": request.call_ref.model_dump(mode="json"),
            "lease_ref": request.lease_ref.model_dump(mode="json"),
            "receipt_ref": request.receipt_ref.model_dump(mode="json"),
        }
        return (
            isinstance(registry, dict)
            and all(registry.get(key) == value for key, value in expected_registry.items())
            and isinstance(registry.get("batch_id"), str)
            and isinstance(registry.get("execution_result_id"), str)
            and set(call) == {
                "schema", *common, "operation", "call_id"
            }
            and call.get("schema") == "captain.capability-benchmark-tool-call.v1"
            and all(call.get(key) == value for key, value in common.items())
            and isinstance(call.get("operation"), str)
            and bool(call["operation"])
            and isinstance(call.get("call_id"), str)
            and bool(call["call_id"])
            and set(lease) == {
                "schema", *common, "call_id", "call_sha256", "lease_id", "scope"
            }
            and lease.get("schema") == "captain.capability-benchmark-tool-lease.v1"
            and all(lease.get(key) == value for key, value in common.items())
            and lease.get("call_id") == call.get("call_id")
            and lease.get("call_sha256") == request.call_ref.sha256
            and isinstance(lease.get("lease_id"), str)
            and bool(lease["lease_id"])
            and lease.get("scope") == [call.get("operation")]
            and set(receipt) == {
                "schema", *common, "call_id", "call_sha256", "lease_sha256",
                "status", "result_ref_sha256",
            }
            and receipt.get("schema")
            == "captain.capability-benchmark-tool-receipt.v1"
            and all(receipt.get(key) == value for key, value in common.items())
            and receipt.get("call_id") == call.get("call_id")
            and receipt.get("call_sha256") == request.call_ref.sha256
            and receipt.get("lease_sha256") == request.lease_ref.sha256
            and receipt.get("status") == "succeeded"
            and isinstance(receipt.get("result_ref_sha256"), str)
            and _SHA256.fullmatch(receipt["result_ref_sha256"]) is not None
        )

    def _read(self, reference: ArtifactRef) -> dict[str, object]:
        if (
            reference.media_type != "application/json"
            or not reference.uri.endswith("/" + reference.sha256)
        ):
            raise ValueError("non-canonical evidence reference")
        body = self._artifacts.read_bytes(reference)
        if not isinstance(body, bytes) or hashlib.sha256(body).hexdigest() != reference.sha256:
            raise ValueError("evidence digest mismatch")
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError("evidence is not an object")
        return value


class GatewayCapabilityN8nWorkBatchAuthority:
    """Persist and resolve only the deterministic Factory-owned n8n root batch."""

    def __init__(self, *, gateway_store: GatewayStore) -> None:
        self.gateway_store = gateway_store
        self._pending: dict[UUID, WorkBatch] = {}

    def persist_work_batch(self, job: AgentFactoryJobV3, batch: WorkBatch) -> None:
        expected = self._expected(job.job_id)
        if (
            batch.batch_id != expected
            or f"factory-job-id:{job.job_id}" not in batch.constraints
            or batch.target != "n8n"
            or batch.runtime != "n8n"
            or "n8n-builder" not in batch.capability_tags
        ):
            raise CapabilityLiveAuthorityUnavailable(
                "n8n WorkBatch is not bound to the Factory job"
            )
        prior = self._pending.get(job.job_id)
        if prior is not None and prior != batch:
            raise CapabilityLiveAuthorityUnavailable("n8n WorkBatch staging conflicts")
        self._pending[job.job_id] = batch

    def register_n8n_work_batch(self, job: AgentFactoryJobV3, batch_id: str) -> None:
        if batch_id != self._expected(job.job_id):
            raise CapabilityLiveAuthorityUnavailable("n8n WorkBatch identity changed")
        batch = self._pending.get(job.job_id)
        if batch is None or batch.batch_id != batch_id:
            existing = self.gateway_store.capability_n8n_work_batch(job.job_id)
            if existing == batch_id:
                return
            raise CapabilityLiveAuthorityUnavailable("n8n WorkBatch staging is unavailable")
        recorded = self.gateway_store.record_capability_n8n_work_batch(job=job, batch=batch)
        if recorded != batch_id:
            raise CapabilityLiveAuthorityUnavailable("n8n WorkBatch readback changed identity")
        self._pending.pop(job.job_id, None)

    def active_n8n_work_batch_id(self, job_id: UUID) -> str | None:
        try:
            batch_id = self.gateway_store.capability_n8n_work_batch(job_id)
        except Exception:
            return None
        if batch_id is None or batch_id != self._expected(job_id):
            return None
        return batch_id

    @staticmethod
    def _expected(job_id: UUID) -> str:
        return f"n8n-{job_id.hex[:24]}"


class GatewayPrivateHoldoutStore:
    """Gateway-backed, atomic durable storage for private benchmark bodies."""

    def __init__(self, *, gateway_store: GatewayStore) -> None:
        self.gateway_store = gateway_store

    def put(self, uri: str, body: str) -> None:
        self.put_many(((uri, body),))

    def put_many(self, entries: tuple[tuple[str, str], ...]) -> None:
        self.gateway_store.put_capability_private_holdouts(entries)

    def get(self, uri: str) -> str:
        return self.gateway_store.capability_private_holdout(uri)


class GatewayGenericBenchmarkRecorder:
    """Append one canonical aggregate per job/suite and return its CAS receipt."""

    authority_id = _BENCHMARK_RECORDER_AUTHORITY_ID

    def __init__(
        self, *, gateway_store: GatewayStore, artifacts: ContentAddressedArtifactAdapter
    ) -> None:
        self.gateway_store = gateway_store
        self._artifacts = artifacts
        self._schema_ready = False
        self._schema_lock = Lock()

    def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            with self.gateway_store.storage.transaction() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS capability_generic_benchmarks (
                            job_id CHAR(36) NOT NULL,
                            suite_sha256 CHAR(64) NOT NULL,
                            correlation_id CHAR(36) NOT NULL,
                            summary_sha256 CHAR(64) NOT NULL,
                            payload JSON NOT NULL,
                            created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                            PRIMARY KEY (job_id, suite_sha256)
                        ) ENGINE=InnoDB
                        """
                    )
            self._schema_ready = True

    def record_generic_benchmark(
        self, request: GenericBenchmarkRecordingRequest
    ) -> GenericBenchmarkReceipt:
        self._require_recording_binding(request)
        self._ensure_schema()
        payload = _benchmark_payload(request)
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        summary = self._artifacts.put(body, "application/json")
        if summary.sha256 != hashlib.sha256(body).hexdigest():
            raise CapabilityLiveAuthorityUnavailable("benchmark CAS changed summary bytes")
        with self.gateway_store.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload, summary_sha256 FROM capability_generic_benchmarks "
                    "WHERE job_id = %s AND suite_sha256 = %s FOR UPDATE",
                    (str(request.job_id), request.suite_digest),
                )
                existing = cursor.fetchone()
                if existing is not None:
                    stored = GatewayStore._decode_json(existing["payload"])
                    if stored != payload or existing["summary_sha256"] != summary.sha256:
                        raise CapabilityLiveAuthorityUnavailable(
                            "generic benchmark recording conflicts"
                        )
                else:
                    cursor.execute(
                        "INSERT INTO capability_generic_benchmarks "
                        "(job_id, suite_sha256, correlation_id, summary_sha256, payload) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (
                            str(request.job_id),
                            request.suite_digest,
                            str(request.correlation_id),
                            summary.sha256,
                            body.decode(),
                        ),
                    )
        evidence = tuple(
            dict.fromkeys(
                reference
                for case in request.case_receipts
                for reference in (
                    case.team_evaluation_ref,
                    case.baseline_evaluation_ref,
                )
            )
        )
        return GenericBenchmarkReceipt(
            authority_id=self.authority_id,
            summary_ref=summary,
            evidence_refs=(summary, *evidence),
        )

    def _require_recording_binding(
        self, request: GenericBenchmarkRecordingRequest
    ) -> None:
        cases = request.case_receipts
        categories = {
            category: sum(item.category is category for item in cases)
            for category in GenericBenchmarkCategory
        }
        identities = tuple(
            (trace_id, run_id)
            for item in cases
            for trace_id, run_id in (
                (item.team_trace_id, item.team_run_id),
                (item.baseline_trace_id, item.baseline_run_id),
            )
        )
        refs = tuple(
            ref
            for item in cases
            for ref in (item.team_evaluation_ref, item.baseline_evaluation_ref)
        )
        if (
            request.suite_digest != request.suite_ref.sha256
            or request.suite_ref.media_type != "application/json"
            or not request.suite_ref.uri.endswith("/" + request.suite_digest)
            or len(cases) != 15
            or request.aggregate.case_count != 15
            or any(count != 3 for count in categories.values())
            or len({item.case_id for item in cases}) != 15
            or len({item.case_digest for item in cases}) != 15
            or len(identities) != 30
            or len(set(identities)) != 30
            or len({trace for trace, _ in identities}) != 30
            or len({run for _, run in identities}) != 30
            or any(
                ref.media_type != "application/json"
                or not ref.uri.endswith("/" + ref.sha256)
                for ref in refs
            )
            or request.passed is not generic_benchmark_passes(request.aggregate)
        ):
            raise CapabilityLiveAuthorityUnavailable(
                "generic benchmark recording binding is invalid"
            )
        try:
            projection = self.gateway_store.factory_job(request.job_id)
            job = projection.job
        except Exception:
            raise CapabilityLiveAuthorityUnavailable(
                "generic benchmark Factory job is unavailable"
            ) from None
        if (
            type(job) is not AgentFactoryJobV3
            or job.job_id != request.job_id
            or job.correlation_id != request.correlation_id
            or job.input_ref.media_type != "text/markdown"
            or not job.input_ref.uri.endswith("/" + job.input_ref.sha256)
        ):
            raise CapabilityLiveAuthorityUnavailable(
                "generic benchmark Factory binding is invalid"
            )

    def receipt_for(self, *, job_id: UUID, suite_sha256: str) -> GenericBenchmarkReceipt:
        self._ensure_schema()
        with self.gateway_store.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload, summary_sha256 FROM capability_generic_benchmarks "
                    "WHERE job_id = %s AND suite_sha256 = %s",
                    (str(job_id), suite_sha256),
                )
                row = cursor.fetchone()
        if row is None:
            raise CapabilityLiveAuthorityUnavailable("generic benchmark receipt is unavailable")
        payload = GatewayStore._decode_json(row["payload"])
        if not isinstance(payload, dict) or payload.get("passed") is not True:
            raise CapabilityLiveAuthorityUnavailable("generic benchmark did not pass")
        summary_sha256 = str(row["summary_sha256"])
        summary = ArtifactRef(
            uri=f"artifact://sha256/{summary_sha256}",
            sha256=summary_sha256,
            media_type="application/json",
        )
        cases = payload.get("case_receipts")
        if not isinstance(cases, list) or len(cases) != 15:
            raise CapabilityLiveAuthorityUnavailable("generic benchmark receipt is incomplete")
        refs: list[ArtifactRef] = [summary]
        for case in cases:
            if not isinstance(case, dict):
                raise CapabilityLiveAuthorityUnavailable("generic benchmark receipt is invalid")
            refs.extend(
                ArtifactRef.model_validate(case[name])
                for name in ("team_evaluation_ref", "baseline_evaluation_ref")
            )
        return GenericBenchmarkReceipt(
            authority_id=self.authority_id,
            summary_ref=summary,
            evidence_refs=tuple(dict.fromkeys(refs)),
        )

    def team_trace_ids(self, *, job_id: UUID, suite_sha256: str) -> tuple[UUID, ...]:
        self._ensure_schema()
        with self.gateway_store.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM capability_generic_benchmarks "
                    "WHERE job_id = %s AND suite_sha256 = %s",
                    (str(job_id), suite_sha256),
                )
                row = cursor.fetchone()
        payload = None if row is None else GatewayStore._decode_json(row["payload"])
        cases = payload.get("case_receipts") if isinstance(payload, dict) else None
        if not isinstance(cases, list) or len(cases) != 15:
            raise CapabilityLiveAuthorityUnavailable("generic benchmark traces are unavailable")
        trace_ids = tuple(UUID(str(item["team_trace_id"])) for item in cases)
        if len(set(trace_ids)) != len(trace_ids):
            raise CapabilityLiveAuthorityUnavailable("generic benchmark traces are ambiguous")
        return trace_ids


def _benchmark_payload(request: GenericBenchmarkRecordingRequest) -> dict[str, object]:
    return {
        "schema": "captain.capability-generic-benchmark-summary.v1",
        "job_id": str(request.job_id),
        "correlation_id": str(request.correlation_id),
        "suite_ref": request.suite_ref.model_dump(mode="json"),
        "suite_digest": request.suite_digest,
        "aggregate": {
            key: str(value) if key.endswith("_usd") else value
            for key, value in request.aggregate.__dict__.items()
        },
        "case_receipts": [
            {
                **case.__dict__,
                "category": case.category.value,
                "team_trace_id": str(case.team_trace_id),
                "team_run_id": str(case.team_run_id),
                "baseline_trace_id": str(case.baseline_trace_id),
                "baseline_run_id": str(case.baseline_run_id),
                "team_evaluation_ref": case.team_evaluation_ref.model_dump(mode="json"),
                "baseline_evaluation_ref": case.baseline_evaluation_ref.model_dump(mode="json"),
            }
            for case in request.case_receipts
        ],
        "passed": request.passed,
    }


class GatewayGenericCapabilityPromotion:
    """Issue a Gateway promotion only after the persisted generic benchmark gate."""

    def __init__(
        self,
        *,
        gateway_store: GatewayStore,
        factory: AgentFactoryLiveComposition,
        provisioning: CurrentAuthorityCapabilityFactoryProvisioner,
        recorder: GatewayGenericBenchmarkRecorder,
        clock: Callable[[], datetime],
    ) -> None:
        self.gateway_store = gateway_store
        self._factory = factory
        self._recorder = recorder
        self._adapter = CurrentCapabilityFactoryAdapter(
            composition=factory, provisioning=provisioning, clock=clock
        )

    async def promote_generic_capability(
        self, request: object, binding: object
    ) -> CapabilityFactoryAuthority:
        converted = _current_stage_request(request)  # type: ignore[arg-type]
        if self._adapter._provisioning.binding_for(converted) != binding:
            raise CapabilityLiveAuthorityUnavailable("Factory promotion binding changed")
        checkpoint = await self._factory.run(binding.job.job_id)
        if (
            getattr(checkpoint, "status", None) != "captain_action_required"
            or getattr(getattr(checkpoint, "next_action", None), "kind", None)
            is not FactoryActionKind.VALIDATE_FOR_PROMOTION
        ):
            raise CapabilityLiveAuthorityUnavailable(
                "Factory is not at the Captain promotion authority"
            )
        if request.suite_ref is None:  # type: ignore[union-attr]
            raise CapabilityLiveAuthorityUnavailable("generic benchmark suite is unavailable")
        receipt = self._recorder.receipt_for(
            job_id=binding.job.job_id,
            suite_sha256=request.suite_ref.sha256,  # type: ignore[union-attr]
        )
        traces = self._recorder.team_trace_ids(
            job_id=binding.job.job_id,
            suite_sha256=request.suite_ref.sha256,  # type: ignore[union-attr]
        )
        if request.hermes_result_ref is None:  # type: ignore[union-attr]
            raise CapabilityLiveAuthorityUnavailable("Hermes release evidence is unavailable")
        try:
            projection_event = self.gateway_store.record_generic_capability_promotion(
                job_id=binding.job.job_id,
                suite_sha256=request.suite_ref.sha256,  # type: ignore[union-attr]
                occurred_at=self._adapter._clock(),
            )
            release_id = UUID(str(projection_event["event_id"]))
            booked = self._factory.workflow_budget_projection(
                binding.job.job_id
            ).consumed_usd
        except Exception:
            raise CapabilityLiveAuthorityUnavailable(
                "Gateway generic promotion authority is unavailable"
            ) from None
        return CapabilityFactoryAuthority(
            stage=CapabilityLiveStage.PROMOTE,
            input_id=request.input.input_id,  # type: ignore[union-attr]
            source_ref=request.input.source_ref,  # type: ignore[union-attr]
            job_id=binding.job.job_id,
            correlation_id=binding.job.correlation_id,
            subject_version=binding.job.subject_version,
            capability_id=binding.job.required_capability,
            capability_version=binding.job.subject_version,
            suite_ref=request.suite_ref,  # type: ignore[arg-type,union-attr]
            hermes_result_ref=request.hermes_result_ref,  # type: ignore[arg-type,union-attr]
            evidence_refs=receipt.evidence_refs,
            booked_cost_usd=booked,
            normal_trace_ids=traces,
            benchmark_authority_id=receipt.authority_id,
            benchmark_summary_ref=receipt.summary_ref,
            release_authority_id=release_id,
        )


class GatewayCapabilityReconciliation:
    """Durable Gateway readback; absence is never upgraded into retry authority."""

    def __init__(self, *, gateway_store: GatewayStore) -> None:
        self.gateway_store = gateway_store

    def record_terminal(
        self,
        request: CapabilityAssemblyReconciliationRequest,
        outcome: CapabilityAssemblyReconciliationOutcome,
    ) -> None:
        GatewayCapabilityAssemblyRepository(
            self.gateway_store
        ).record_terminal_readback(request, outcome)

    def record_effect(
        self,
        request: CapabilityAssemblyReconciliationRequest,
        outcome: CapabilityAssemblyReconciliationOutcome,
    ) -> None:
        repository = GatewayCapabilityAssemblyRepository(self.gateway_store)
        repository.record_effect_receipt(request, outcome)
        if repository.effect_receipt(request) != outcome:
            raise CapabilityLiveAuthorityUnavailable(
                "effect receipt readback changed before adapter return"
            )

    async def reconcile(
        self, request: CapabilityAssemblyReconciliationRequest
    ) -> CapabilityAssemblyReconciliationOutcome | None:
        repository = GatewayCapabilityAssemblyRepository(self.gateway_store)
        return repository.effect_receipt(request) or repository.terminal_readback(request)


class StoreBoundGatewayCapabilityResumeClient(GatewayCapabilityResumeClient):
    def __init__(self, *, gateway_store: GatewayStore, **kwargs: object) -> None:
        self.gateway_store = gateway_store
        super().__init__(**kwargs)  # type: ignore[arg-type]


class GatewayCapabilityProjectionAuthority:
    """Gateway feed/ack/readback authority backed by the same GatewayStore."""

    def __init__(self, *, gateway_store: GatewayStore) -> None:
        self.gateway_store = gateway_store

    async def projection_event(self, event_id: UUID) -> MinibookProjectionEvent | None:
        generic = self.gateway_store.generic_capability_promotion(event_id)
        if generic is not None:
            try:
                return MinibookProjectionEvent.model_validate(generic)
            except ValueError:
                raise CapabilityLiveAuthorityUnavailable(
                    "Gateway generic promotion event is invalid"
                ) from None
        source = self.gateway_store.factory_promotion_source(event_id)
        if source is None:
            return None
        block, job = source
        summary = _promotion_benchmark_summary(self.gateway_store, block)
        event = factory_promotion_projection(block, job, benchmark_summary=summary)
        return event

    async def capability_promotion_event(
        self, release: CapabilityFactoryAuthority
    ) -> MinibookProjectionEvent:
        if release.release_authority_id is None:
            raise CapabilityLiveAuthorityUnavailable("Factory promotion event is unavailable")
        event = await self.projection_event(release.release_authority_id)
        if event is None:
            raise CapabilityLiveAuthorityUnavailable("Factory promotion event is unavailable")
        return event

    async def acknowledge_projection(
        self, acknowledgement: MinibookProjectionAcknowledgementV1
    ) -> None:
        result = self.gateway_store.record_minibook_projection_acknowledgement(
            acknowledgement
        )
        if result.event.event_id != acknowledgement.acknowledgement_id:
            raise CapabilityLiveAuthorityUnavailable("projection acknowledgement changed")

    async def projection_readback(
        self, event_id: UUID
    ) -> CapabilityProjectionReadback | None:
        event = await self.projection_event(event_id)
        if event is None:
            return None
        generic = self.gateway_store.generic_capability_promotion(event_id)
        source = None if generic is not None else self.gateway_store.factory_promotion_source(event_id)
        if generic is not None:
            job_id = UUID(str(event.subject_id).removeprefix("subject:"))
            project_id = f"factory:{job_id}"
            run_id = "generic-benchmark-promotion"
        elif source is not None:
            block, _ = source
            project_id = f"factory:{block['job_id']}"
            run_id = f"attempt:{block['attempt']}"
        else:
            return None
        mirrors = tuple(
            item
            for item in self.gateway_store.delivery_events(
                project_id=project_id,
                run_id=run_id,
            )
            if item.event_type == "registry_mirror"
            and item.trace.trace_id == f"minibook-projection:{event_id}"
        )
        if len(mirrors) != 1 or mirrors[0].trace.artifact_id is None:
            return None
        mirror = mirrors[0]
        rendered = MinibookProjector.render(event)
        acknowledgement = MinibookProjectionAcknowledgementV1(
            acknowledgement_id=minibook_projection_acknowledgement_id(
                event_id,
                post_id=mirror.trace.artifact_id,
                content_sha256=rendered.content_hash,
            ),
            projection_event_id=event_id,
            correlation_id=event.correlation_id,
            subject_id=event.subject_id,
            subject_version=event.subject_version,
            project_id=MinibookProjector.PROJECTION_PROJECT_ID,
            post_id=mirror.trace.artifact_id,
            content_sha256=rendered.content_hash,
            acknowledged_at=mirror.occurred_at,
            outcome="mirrored",
        )
        if mirror.event_id != acknowledgement.acknowledgement_id:
            raise CapabilityLiveAuthorityUnavailable(
                "persisted projection acknowledgement changed"
            )
        payload = mirror.model_dump_json().encode()
        digest = hashlib.sha256(payload).hexdigest()
        return CapabilityProjectionReadback(
            event=event,
            acknowledgement=acknowledgement,
            evidence_ref=ArtifactRef(
                uri=f"artifact://gateway-registry-mirror/{digest}",
                sha256=digest,
                media_type="application/json",
            ),
        )


def _promotion_benchmark_summary(store: GatewayStore, block: dict[str, Any]):
    from agenten.agent_factory.skill_workflow_contracts import TeamEvaluationV1

    job_id = UUID(str(block["job_id"]))
    attempt = int(block["attempt"])
    evaluations = tuple(
        item
        for item in store.factory_workflow_artifacts(job_id)
        if isinstance(item, TeamEvaluationV1) and item.attempt == attempt
    )
    if len(evaluations) != 1 or evaluations[0].benchmark_summary_ref is None:
        raise CapabilityLiveAuthorityUnavailable("Factory benchmark summary is unavailable")
    summary = store.business_benchmark_summary_by_artifact(
        evaluations[0].benchmark_summary_ref
    )
    if summary is None:
        raise CapabilityLiveAuthorityUnavailable("Factory benchmark summary is unavailable")
    return summary


@dataclass(frozen=True)
class CapabilityAssemblyFactoryRuntime:
    """Already-composed current Factory graph; no arbitrary plugin seam."""

    factory: AgentFactoryLiveComposition
    provisioning: CurrentAuthorityCapabilityFactoryProvisioner
    n8n: GatewayCapabilityN8nWorkBatchAuthority


@dataclass(frozen=True, repr=False)
class CapabilityAssemblyFactoryConfiguration:
    """Strict non-secret configuration exposed to the Factory constructor."""

    repository_root: Path
    artifact_root: Path
    authority_root: Path
    provider: str
    model: str
    execution_policy: FactoryExecutionPolicyV1


@dataclass(frozen=True, repr=False)
class CapabilityAssemblyFactoryAuthorities:
    """Materialized authorities supplied separately from non-secret configuration."""

    store: GatewayStore
    private_benchmark_catalog: GatewayPrivateHoldoutStore
    source_artifacts: RepositoryFactoryInputArtifactStore
    clock: Callable[[], datetime]


class CapabilityAssemblyFactoryRuntimeDescriptorV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_name: Literal["captain.capability-factory-runtime-plan.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    adapter_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    constructor_signature: str
    clock_signature: str
    model_client_signature: str
    factory_type: str
    repository_type: str
    team_execution_type: str
    ports_provider_type: str
    model_client_type: str
    budget_type: str
    pricing_authority_type: str
    pricing_source_type: str
    replay_store_type: str
    candidate_provider_type: str
    skill_root: str
    replay_root: str
    store_authority: str
    clock_authority: str
    provider: str
    model: str


_FACTORY_RUNTIME_DESCRIPTOR_BINDINGS = {
    "constructor_signature": "(configuration, authorities)",
    "clock_signature": "() -> datetime",
    "model_client_signature": "(job, invocation)",
    "factory_type": "gateway.agent_factory_live_composition.AgentFactoryLiveComposition",
    "repository_type": "gateway.factory_repository.GatewayFactoryRepository",
    "team_execution_type": "gateway.agent_factory_live_composition.ProductionFactoryTeamExecutionPort",
    "ports_provider_type": "gateway.agent_factory_live_composition.GatewayTechnicalTeamExecutionPortsProvider",
    "model_client_type": "agenten.agent_factory.business_benchmark_production_ports.OpenAIBusinessBenchmarkModelClientBuilder",
    "budget_type": "gateway.factory_repository.GatewayFactoryBudgetLedger",
    "pricing_authority_type": "agenten.agent_factory.business_benchmark_production_ports.BusinessBenchmarkPricingAuthority",
    "pricing_source_type": "agenten.agent_factory.business_benchmark_production_ports.ConfiguredBusinessBenchmarkPricingSource",
    "replay_store_type": "agenten.agent_factory.hermes_cli.FilesystemFactorySkillReplayStore",
    "candidate_provider_type": "agenten.agent_factory.candidate_evaluation.GatewayForgeCandidateProvider",
    "skill_root": "agenten/agent_factory/skills",
    "replay_root": ".captain-cook/private/business-benchmarks/runtime-state/factory-team-replays",
    "store_authority": "gateway-sole-writer",
    "clock_authority": "shared-production-clock",
    "provider": "openai",
}


@dataclass(frozen=True, repr=False)
class CapabilityAssemblyFactoryRuntimePlan:
    repository_root: Path
    adapter_path: Path
    adapter_sha256: str
    descriptor_sha256: str
    descriptor: CapabilityAssemblyFactoryRuntimeDescriptorV1
    configuration: CapabilityAssemblyFactoryConfiguration


def plan_capability_assembly_factory_runtime_from_environment(
    *,
    repository_root: Path,
    environment: Mapping[str, str],
    artifact_root: Path,
    authority_root: Path,
    repository_revision: str,
) -> CapabilityAssemblyFactoryRuntimePlan:
    """Attest all Factory constructor metadata without importing or constructing it."""

    relative_path = environment.get("CAPTAIN_CAPABILITY_FACTORY_ADAPTER", "").strip()
    adapter_sha256 = environment.get(
        "CAPTAIN_CAPABILITY_FACTORY_ADAPTER_SHA256", ""
    ).strip()
    descriptor_raw = environment.get("CAPTAIN_CAPABILITY_FACTORY_PLAN_JSON", "")
    descriptor_sha256 = environment.get(
        "CAPTAIN_CAPABILITY_FACTORY_PLAN_SHA256", ""
    ).strip()
    policy_raw = environment.get("CAPTAIN_CAPABILITY_EXECUTION_POLICY_JSON", "")
    try:
        if (
            not relative_path
            or _SHA256.fullmatch(adapter_sha256) is None
            or _SHA256.fullmatch(descriptor_sha256) is None
            or not descriptor_raw
            or not policy_raw
        ):
            raise ValueError("incomplete")
        descriptor_payload = json.loads(descriptor_raw)
        canonical_descriptor = json.dumps(
            descriptor_payload, sort_keys=True, separators=(",", ":")
        ).encode()
        if not secrets.compare_digest(
            hashlib.sha256(canonical_descriptor).hexdigest(), descriptor_sha256
        ):
            raise ValueError("descriptor digest")
        descriptor = CapabilityAssemblyFactoryRuntimeDescriptorV1.model_validate(
            descriptor_payload
        )
        if not secrets.compare_digest(descriptor.adapter_sha256, adapter_sha256):
            raise ValueError("adapter binding")
        expected = dict(_FACTORY_RUNTIME_DESCRIPTOR_BINDINGS)
        expected["model"] = environment.get("CAPTAIN_BENCHMARK_MODEL", "").strip()
        if any(getattr(descriptor, name) != value for name, value in expected.items()):
            raise ValueError("descriptor binding")
        if descriptor.provider != environment.get(
            "CAPTAIN_BENCHMARK_PROVIDER", ""
        ).strip():
            raise ValueError("provider binding")
        policy = FactoryExecutionPolicyV1.model_validate(json.loads(policy_raw))
        if descriptor.model not in policy.allowed_models:
            raise ValueError("policy model")
        adapter_path, adapter_bytes = read_verified_repository_bytes(
            relative_path,
            expected_sha256=adapter_sha256,
            expected_repository_revision=repository_revision,
            repository_root=repository_root,
        )
        if not secrets.compare_digest(
            hashlib.sha256(adapter_bytes).hexdigest(), adapter_sha256
        ):
            raise ValueError("adapter digest")
        root = repository_root.resolve(strict=True)
        for relative_root in (descriptor.skill_root, descriptor.replay_root):
            candidate = Path(relative_root)
            if candidate.is_absolute() or ".." in candidate.parts:
                raise ValueError("root")
            (root / candidate).resolve().relative_to(root)
    except Exception:
        raise CapabilityAssemblyBootstrapConfigurationError(
            "capability assembly Factory runtime plan is invalid"
        ) from None
    return CapabilityAssemblyFactoryRuntimePlan(
        repository_root=root,
        adapter_path=adapter_path,
        adapter_sha256=adapter_sha256,
        descriptor_sha256=descriptor_sha256,
        descriptor=descriptor,
        configuration=CapabilityAssemblyFactoryConfiguration(
            repository_root=root,
            artifact_root=artifact_root,
            authority_root=authority_root,
            provider=descriptor.provider,
            model=descriptor.model,
            execution_policy=policy,
        ),
    )


def build_capability_assembly_factory_runtime_from_environment(
    *,
    repository_root: Path,
    environment: Mapping[str, str],
    store: GatewayStore,
    private_benchmark_catalog: GatewayPrivateHoldoutStore,
    settings: CapabilityAssemblyBootstrapSettings,
    clock: Callable[[], datetime],
) -> CapabilityAssemblyFactoryRuntime:
    """Construct the native runtime around one verified local Factory composition."""

    plan = plan_capability_assembly_factory_runtime_from_environment(
        repository_root=repository_root,
        environment=environment,
        artifact_root=settings.artifact_root,
        authority_root=settings.authority_root,
        repository_revision=settings.assembly.expected_repository_revision,
    )
    return materialize_capability_assembly_factory_runtime(
        plan,
        store=store,
        private_benchmark_catalog=private_benchmark_catalog,
        clock=clock,
    )


def materialize_capability_assembly_factory_runtime(
    plan: CapabilityAssemblyFactoryRuntimePlan,
    *,
    store: GatewayStore,
    private_benchmark_catalog: GatewayPrivateHoldoutStore,
    clock: Callable[[], datetime],
    source_artifacts: RepositoryFactoryInputArtifactStore | None = None,
) -> CapabilityAssemblyFactoryRuntime:
    """Import and construct only after the complete descriptor is attested."""

    selected_source_artifacts = source_artifacts or RepositoryFactoryInputArtifactStore(
        artifacts=ContentAddressedArtifactAdapter(plan.configuration.artifact_root)
    )
    authorities = CapabilityAssemblyFactoryAuthorities(
        store=store,
        private_benchmark_catalog=private_benchmark_catalog,
        source_artifacts=selected_source_artifacts,
        clock=clock,
    )
    try:
        module = load_verified_repository_module(
            plan.adapter_path,
            expected_sha256=plan.adapter_sha256,
            repository_root=plan.repository_root,
        )
        constructor = getattr(module, "build_capability_factory", None)
        if not callable(constructor):
            raise ValueError("constructor")
        factory = constructor(plan.configuration, authorities)
    except Exception:
        raise CapabilityAssemblyBootstrapConfigurationError(
            "capability assembly Factory constructor is invalid"
        ) from None
    if type(factory) is not AgentFactoryLiveComposition:
        raise CapabilityAssemblyBootstrapConfigurationError(
            "capability assembly Factory constructor is invalid"
        )
    n8n = GatewayCapabilityN8nWorkBatchAuthority(gateway_store=store)
    provisioning = CurrentAuthorityCapabilityFactoryProvisioner(
        repository=factory.repository,
        source_artifacts=selected_source_artifacts,  # type: ignore[arg-type]
        holdout_store=private_benchmark_catalog,
        released_skills=factory.repository,
        execution_policy=plan.configuration.execution_policy,
        authority_root=plan.configuration.authority_root / "factory-inputs",
        clock=clock,
        work_batches=n8n,
        n8n_registry=n8n,
    )
    return CapabilityAssemblyFactoryRuntime(
        factory=factory,
        provisioning=provisioning,
        n8n=n8n,
    )


@dataclass(frozen=True, repr=False)
class CapabilityAssemblyProductionPlan:
    """Pure, validated inputs for the explicit production materialization step."""

    repository_root: Path
    settings: CapabilityAssemblyBootstrapSettings
    store: GatewayStore
    runtime: CapabilityAssemblyFactoryRuntime
    private_benchmark_catalog: GatewayPrivateHoldoutStore
    source_artifacts: RepositoryFactoryInputArtifactStore
    clock: Callable[[], datetime]


def plan_production_capability_assembly_from_environment(
    *,
    repository_root: Path,
    environment: Mapping[str, str],
    store: GatewayStore,
    runtime: CapabilityAssemblyFactoryRuntime,
    private_benchmark_catalog: GatewayPrivateHoldoutStore,
    clock: Callable[[], datetime] | None = None,
) -> CapabilityAssemblyProductionPlan:
    """Validate the complete native graph without materializing any resource."""

    settings = load_capability_assembly_bootstrap_settings(
        environment=environment, repository_root=repository_root
    )
    selected_clock = clock or (lambda: datetime.now(timezone.utc))
    if not _native_runtime_is_bound(
        runtime=runtime,
        store=store,
        private_benchmark_catalog=private_benchmark_catalog,
        clock=selected_clock,
        repository_root=repository_root,
    ):
        raise CapabilityAssemblyBootstrapConfigurationError(
            "capability assembly Factory runtime is not Gateway-native"
        )
    expected_provider_binding = (
        settings.provider_url,
        settings.provider_policy_sha256,
        settings.provider_price_card_sha256,
        settings.provider_context_sha256,
        settings.provider_proxy_executable_sha256,
        settings.provider_proxy_code_sha256,
        settings.provider_paid_command_sha256,
        settings.provider_hard_ceiling_enforced,
    )
    if expected_provider_binding not in getattr(
        store, "_runtime_provider_bundle_bindings", frozenset()
    ):
        raise CapabilityAssemblyBootstrapConfigurationError(
            "capability assembly provider authority is not Gateway-bound"
        )
    source_artifacts = getattr(runtime.provisioning, "_source_artifacts", None)
    artifacts = getattr(source_artifacts, "_artifacts", None)
    if (
        type(source_artifacts) is not RepositoryFactoryInputArtifactStore
        or type(artifacts) is not ContentAddressedArtifactAdapter
        or getattr(getattr(artifacts, "_store", None), "root", None)
        != settings.artifact_root
    ):
        raise CapabilityAssemblyBootstrapConfigurationError(
            "capability assembly Factory CAS is not canonical"
        )
    return CapabilityAssemblyProductionPlan(
        repository_root=repository_root,
        settings=settings,
        store=store,
        runtime=runtime,
        private_benchmark_catalog=private_benchmark_catalog,
        source_artifacts=source_artifacts,
        clock=selected_clock,
    )


def materialize_production_capability_assembly(
    plan: CapabilityAssemblyProductionPlan,
) -> ProductionCapabilityAssemblyComposition:
    """Materialize clients, local stores, and the sealed controller explicitly."""

    settings = plan.settings
    store = plan.store
    runtime = plan.runtime
    private_benchmark_catalog = plan.private_benchmark_catalog
    source_artifacts = plan.source_artifacts
    selected_clock = plan.clock
    artifacts = source_artifacts._artifacts
    benchmark_executor = GatewayIsolatedBenchmarkExecutorClient(
        gateway_store=store,
        authority_id=settings.benchmark_authority_id,
        endpoint=settings.benchmark_url,
        allowed_hosts=settings.allowed_hosts,
        transport=GatewayHttpBenchmarkProcessTransport(
            bearer_token=settings.benchmark_token
        ),
    )
    benchmark_evidence = ContentAddressedBenchmarkEvidenceResolver(
        gateway_store=store, artifacts=artifacts
    )
    benchmark_tool = CaptainBenchmarkToolEvidenceAuthority(
        gateway_store=store, artifacts=artifacts
    )
    benchmark_recorder = GatewayGenericBenchmarkRecorder(
        gateway_store=store, artifacts=artifacts
    )
    promotion = GatewayGenericCapabilityPromotion(
        gateway_store=store,
        factory=runtime.factory,
        provisioning=runtime.provisioning,
        recorder=benchmark_recorder,
        clock=selected_clock,
    )
    resume_http = httpx.AsyncClient(follow_redirects=False, timeout=30.0)
    resume = StoreBoundGatewayCapabilityResumeClient(
        gateway_store=store,
        base_url=settings.gateway_url,
        token=settings.gateway_token.get_secret_value(),
        client=resume_http,
    )
    minibook_http = httpx.Client(follow_redirects=False, timeout=30.0)
    minibook_client = MinibookClient(
        settings.minibook_url,
        settings.minibook_api_key.get_secret_value(),
        projection_api_key=settings.minibook_projection_api_key.get_secret_value(),
        client=minibook_http,
    )
    projector = MinibookProjector(
        minibook_client,
        ProjectionCursorStore(settings.authority_root / "minibook-projection.sqlite3"),
    )
    projection_gateway = GatewayCapabilityProjectionAuthority(gateway_store=store)
    minibook_effects = GatewayMinibookProjectionEffectsAdapter(
        gateway=projection_gateway,
        minibook=AsyncMinibookProjectorAdapter(projector=projector),
    )
    authorities = CapabilityAssemblyProductionAuthorities(
        store=store,
        factory=runtime.factory,
        provisioning=runtime.provisioning,
        resume_gateway=resume,
        reconciliation=GatewayCapabilityReconciliation(gateway_store=store),
        minibook_effects=minibook_effects,
        source_artifacts=source_artifacts,
        benchmark_executor=benchmark_executor,
        benchmark_evidence=benchmark_evidence,
        benchmark_tool_authority=benchmark_tool,
        benchmark_recorder=benchmark_recorder,
        promotion=promotion,
        n8n_work_batches=runtime.n8n,
    )
    composition = build_capability_assembly_controller(
        repository_root=plan.repository_root,
        settings=settings.assembly,
        authorities=authorities,
        holdout_store=private_benchmark_catalog,
        clock=selected_clock,
    )
    _BOOTSTRAP_RESOURCES[composition] = (resume_http, minibook_http)
    return composition


def build_production_capability_assembly_from_environment(
    *,
    repository_root: Path,
    environment: Mapping[str, str],
    store: GatewayStore,
    runtime: CapabilityAssemblyFactoryRuntime,
    private_benchmark_catalog: GatewayPrivateHoldoutStore,
    clock: Callable[[], datetime] | None = None,
) -> ProductionCapabilityAssemblyComposition:
    """Plan first, then explicitly materialize one sealed production graph."""

    return materialize_production_capability_assembly(
        plan_production_capability_assembly_from_environment(
            repository_root=repository_root,
            environment=environment,
            store=store,
            runtime=runtime,
            private_benchmark_catalog=private_benchmark_catalog,
            clock=clock,
        )
    )


def _native_runtime_is_bound(
    *,
    runtime: CapabilityAssemblyFactoryRuntime,
    store: GatewayStore,
    private_benchmark_catalog: GatewayPrivateHoldoutStore,
    clock: Callable[[], datetime],
    repository_root: Path,
) -> bool:
    factory = getattr(runtime, "factory", None)
    provisioning = getattr(runtime, "provisioning", None)
    n8n = getattr(runtime, "n8n", None)
    if (
        type(factory) is not AgentFactoryLiveComposition
        or type(provisioning) is not CurrentAuthorityCapabilityFactoryProvisioner
        or type(n8n) is not GatewayCapabilityN8nWorkBatchAuthority
        or type(private_benchmark_catalog) is not GatewayPrivateHoldoutStore
        or private_benchmark_catalog.gateway_store is not store
        or n8n.gateway_store is not store
        or type(factory.repository) is not GatewayFactoryRepository
        or getattr(factory.repository, "_store", None) is not store
        or type(factory.coordinator) is not FactoryCoordinator
        or getattr(factory.coordinator, "_repository", None) is not factory.repository
        or type(factory.lease_issuer) is not GatewayNextActionLeaseIssuer
        or getattr(factory.lease_issuer, "_store", None) is not store
        or getattr(factory.lease_issuer, "_n8n_work_batch_source", None) is not n8n
        or type(factory.hermes) is not HermesCliFactory
        or getattr(factory.hermes, "_workflow_artifacts", None) is not factory.repository
        or type(factory.team_execution) is not ProductionFactoryTeamExecutionPort
        or not _native_team_execution_ports_are_bound(
            factory.team_execution,
            repository=factory.repository,
            clock=clock,
            repository_root=repository_root,
        )
        or type(factory.candidate_validator) is not CandidateEvaluationFactory
        or getattr(factory.candidate_validator, "_workflow_artifacts", None)
        is not factory.repository
        or type(factory.business_benchmark) is not BusinessBenchmarkDispatchService
        or type(factory.dispatcher) is not FactoryDispatcher
        or getattr(factory.dispatcher, "_coordinator", None) is not factory.coordinator
        or getattr(factory.dispatcher, "_business_benchmark", None)
        is not factory.business_benchmark
        or type(factory.runner) is not ProductionFactoryDispatchRunner
        or getattr(factory.runner, "_coordinator", None) is not factory.coordinator
        or getattr(factory.runner, "_dispatcher", None) is not factory.dispatcher
        or getattr(provisioning, "_repository", None) is not factory.repository
        or getattr(provisioning, "_work_batches", None) is not n8n
        or getattr(provisioning, "_n8n_registry", None) is not n8n
        or getattr(getattr(provisioning, "_compiler", None), "_holdout_store", None)
        is not private_benchmark_catalog
    ):
        return False
    return True


def _native_team_execution_ports_are_bound(
    team_execution: ProductionFactoryTeamExecutionPort,
    *,
    repository: GatewayFactoryRepository,
    clock: Callable[[], datetime],
    repository_root: Path,
) -> bool:
    """Validate the lazy TeamExecution authority without constructing its ports."""

    provider = getattr(team_execution, "_ports_for", None)
    if type(provider) is not GatewayTechnicalTeamExecutionPortsProvider:
        return False
    skill_root = getattr(provider, "_skill_root", None)
    if not isinstance(skill_root, Path):
        return False
    try:
        skill_root.resolve().relative_to(repository_root.resolve())
    except ValueError:
        return False
    model_client = getattr(provider, "_model_client_for", None)
    budget = getattr(provider, "_budget", None)
    pricing = getattr(provider, "_pricing_authority", None)
    pricing_source = getattr(pricing, "_source", None)
    replay = getattr(provider, "_replay_store", None)
    candidate_bindings = getattr(provider, "_candidate_bindings", None)
    maximum = getattr(provider, "_max_cost_per_call", None)
    replay_root = getattr(replay, "_root", None)
    if not isinstance(replay_root, Path):
        return False
    try:
        replay_root.resolve().relative_to(repository_root.resolve())
    except ValueError:
        return False
    return (
        getattr(provider, "_clock", None) is clock
        and getattr(provider, "_released_skill_catalog", None) is repository
        and getattr(provider, "_provider", None) == "openai"
        and isinstance(getattr(provider, "_model", None), str)
        and bool(getattr(provider, "_model", "").strip())
        and maximum is not None
        and maximum > 0
        and type(model_client) is OpenAIBusinessBenchmarkModelClientBuilder
        and model_client.provider == "openai"
        and model_client.model == getattr(provider, "_model", None)
        and type(budget) is GatewayFactoryBudgetLedger
        and getattr(budget, "_store", None) is getattr(repository, "_store", None)
        and type(pricing) is BusinessBenchmarkPricingAuthority
        and type(pricing_source) is ConfiguredBusinessBenchmarkPricingSource
        and getattr(pricing_source, "_provider", None) == "openai"
        and getattr(pricing_source, "_model", None) == getattr(provider, "_model", None)
        and getattr(pricing_source, "_max_cost_per_call", None) == maximum
        and type(replay) is FilesystemFactorySkillReplayStore
        and type(candidate_bindings) is GatewayForgeCandidateProvider
        and getattr(candidate_bindings, "_repository", None) is repository
        and callable(model_client)
        and callable(getattr(provider, "_clock", None))
        and callable(provider)
    )


async def close_production_capability_assembly(
    composition: ProductionCapabilityAssemblyComposition,
) -> None:
    """Close only HTTP clients owned by this bootstrap; exact replay is safe."""

    resources = _BOOTSTRAP_RESOURCES.pop(composition, None)
    if resources is None:
        return
    resume_http, minibook_http = resources
    await resume_http.aclose()
    minibook_http.close()


__all__ = [
    "CapabilityAssemblyBootstrapConfigurationError",
    "CapabilityAssemblyBootstrapSettings",
    "CapabilityAssemblyFactoryAuthorities",
    "CapabilityAssemblyFactoryConfiguration",
    "CapabilityAssemblyFactoryRuntime",
    "CapabilityAssemblyFactoryRuntimeDescriptorV1",
    "CapabilityAssemblyFactoryRuntimePlan",
    "CapabilityAssemblyProductionPlan",
    "CapabilityProductionBundleHealthV1",
    "CapabilityProductionBundleManifestV1",
    "CapabilityProductionProcessBindingV1",
    "CaptainBenchmarkToolEvidenceAuthority",
    "GatewayCapabilityN8nWorkBatchAuthority",
    "GatewayCapabilityProjectionAuthority",
    "GatewayCapabilityReconciliation",
    "GatewayGenericBenchmarkRecorder",
    "GatewayGenericCapabilityPromotion",
    "GatewayPrivateHoldoutStore",
    "StoreBoundGatewayCapabilityResumeClient",
    "build_production_capability_assembly_from_environment",
    "build_capability_assembly_factory_runtime_from_environment",
    "close_production_capability_assembly",
    "load_capability_assembly_bootstrap_settings",
    "load_capability_production_bundle_manifest",
    "materialize_capability_assembly_factory_runtime",
    "materialize_production_capability_assembly",
    "plan_capability_assembly_factory_runtime_from_environment",
    "plan_production_capability_assembly_from_environment",
]
