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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID
from weakref import WeakKeyDictionary

import httpx
from fastapi import HTTPException
from pydantic import SecretStr

from agenten.agent_factory.capability_live_runner import CapabilityLiveStage
from agenten.agent_factory.contracts import AgentFactoryJobV3
from agenten.agent_factory.holdout_store import PrivateHoldoutStore
from agenten.agent_runtime.captain_production_adapters import (
    ContentAddressedArtifactAdapter,
)
from agenten.agent_runtime.contracts import ArtifactRef
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
from gateway.agent_factory_live_composition import ProductionFactoryTeamExecutionPort
from gateway.agent_factory_dispatch_runner import GatewayNextActionLeaseIssuer
from gateway.factory_repository import GatewayFactoryRepository
from agenten.agent_factory.business_benchmark_dispatch import BusinessBenchmarkDispatchService
from agenten.agent_factory.candidate_evaluation import CandidateEvaluationFactory
from agenten.agent_factory.hermes_cli import HermesCliFactory
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
    GenericBenchmarkReceipt,
    GenericBenchmarkRecordingRequest,
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
    runtime_token: SecretStr
    provider_token: SecretStr
    provider_url: str
    provider_policy_sha256: str
    provider_price_card_sha256: str
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
    "CAPTAIN_RUNTIME_TOKEN",
    "CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN",
    "CAPTAIN_PROVIDER_PROXY_URL",
    "CAPTAIN_PROVIDER_POLICY_SHA256",
    "CAPTAIN_PROVIDER_PRICE_CARD_SHA256",
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
    ):
        if _SHA256.fullmatch(raw[digest_name]) is None:
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
        provider_token=SecretStr(raw["CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN"]),
        provider_url=provider_url,
        provider_policy_sha256=raw["CAPTAIN_PROVIDER_POLICY_SHA256"],
        provider_price_card_sha256=raw["CAPTAIN_PROVIDER_PRICE_CARD_SHA256"],
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

    def verify(self, request: BenchmarkToolEvidenceVerificationRequest) -> bool:
        try:
            call = self._read(request.call_ref)
            lease = self._read(request.lease_ref)
            receipt = self._read(request.receipt_ref)
        except Exception:
            return False
        common = {
            "tool_key": request.tool_key,
            "trace_id": str(request.trace_id),
            "run_id": str(request.run_id),
        }
        return (
            call
            == {
                "schema": "captain.capability-tool-call.v1",
                **common,
                "model_policy_digest": request.model_policy_digest,
            }
            and lease
            == {
                "schema": "captain.capability-tool-lease.v1",
                **common,
                "call_sha256": request.call_ref.sha256,
            }
            and receipt
            == {
                "schema": "captain.capability-tool-receipt.v1",
                **common,
                "call_sha256": request.call_ref.sha256,
                "lease_sha256": request.lease_ref.sha256,
                "status": "succeeded",
            }
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


@dataclass(frozen=True)
class _RootWorkBatchWrite:
    block_type: str
    data: dict[str, Any]
    status: str = "pending"
    parent_index: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class GatewayCapabilityN8nWorkBatchAuthority:
    """Persist and resolve only the deterministic Factory-owned n8n root batch."""

    def __init__(self, *, gateway_store: GatewayStore) -> None:
        self.gateway_store = gateway_store
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
                        CREATE TABLE IF NOT EXISTS capability_n8n_work_batches (
                            job_id CHAR(36) NOT NULL PRIMARY KEY,
                            batch_id VARCHAR(128) NOT NULL UNIQUE,
                            created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                        ) ENGINE=InnoDB
                        """
                    )
            self._schema_ready = True

    def persist_work_batch(self, job: AgentFactoryJobV3, batch: WorkBatch) -> None:
        self._ensure_schema()
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
        self.gateway_store.append(
            _RootWorkBatchWrite(
                block_type="work_batch",
                data=batch.model_dump(mode="json"),
                metadata={
                    "factory_job_id": str(job.job_id),
                    "purpose": "capability_assembly_n8n",
                },
            ),
            None,
        )

    def register_n8n_work_batch(self, job: AgentFactoryJobV3, batch_id: str) -> None:
        self._ensure_schema()
        if batch_id != self._expected(job.job_id):
            raise CapabilityLiveAuthorityUnavailable("n8n WorkBatch identity changed")
        projection = self.gateway_store.batch_projection(batch_id)
        if getattr(projection, "batch_id", None) != batch_id:
            raise CapabilityLiveAuthorityUnavailable("n8n WorkBatch readback is unavailable")
        with self.gateway_store.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT batch_id FROM capability_n8n_work_batches "
                    "WHERE job_id = %s FOR UPDATE",
                    (str(job.job_id),),
                )
                prior = cursor.fetchone()
                if prior is not None and prior["batch_id"] != batch_id:
                    raise CapabilityLiveAuthorityUnavailable(
                        "n8n WorkBatch registration conflicts"
                    )
                if prior is None:
                    cursor.execute(
                        "INSERT INTO capability_n8n_work_batches (job_id, batch_id) "
                        "VALUES (%s, %s)",
                        (str(job.job_id), batch_id),
                    )

    def active_n8n_work_batch_id(self, job_id: UUID) -> str | None:
        self._ensure_schema()
        with self.gateway_store.storage.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT batch_id FROM capability_n8n_work_batches WHERE job_id = %s",
                    (str(job_id),),
                )
                row = cursor.fetchone()
        batch_id = None if row is None else str(row["batch_id"])
        if batch_id is None or batch_id != self._expected(job_id):
            return None
        try:
            projection = self.gateway_store.batch_projection(batch_id)
        except Exception:
            return None
        return batch_id if getattr(projection, "batch_id", None) == batch_id else None

    @staticmethod
    def _expected(job_id: UUID) -> str:
        return f"n8n-{job_id.hex[:24]}"


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
    """Delegate promotion to the existing workflow promotion implementation."""

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
        await self._factory.run(
            binding.job.job_id,
            stop_before_action=FactoryActionKind.VALIDATE_FOR_PROMOTION,
        )
        result = await self._adapter.promote(converted)
        if not isinstance(result, CapabilityFactoryAuthority):
            raise CapabilityLiveAuthorityUnavailable("Factory promotion is unavailable")
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
        return result.model_copy(
            update={
                "benchmark_authority_id": receipt.authority_id,
                "benchmark_summary_ref": receipt.summary_ref,
                "normal_trace_ids": traces,
                "evidence_refs": tuple(
                    dict.fromkeys((*result.evidence_refs, *receipt.evidence_refs))
                ),
            }
        )


class GatewayCapabilityReconciliation:
    """Conservative readback: absence is never upgraded into retry authority."""

    def __init__(self, *, gateway_store: GatewayStore) -> None:
        self.gateway_store = gateway_store

    async def reconcile(
        self, request: CapabilityAssemblyReconciliationRequest
    ) -> CapabilityAssemblyReconciliationOutcome | None:
        # The stage-specific adapters already replay terminal Gateway facts.  A
        # reconciliation miss therefore stays pending until a future bounded
        # advance observes a typed terminal authority; no receipt is minted here.
        del request
        return None


class StoreBoundGatewayCapabilityResumeClient(GatewayCapabilityResumeClient):
    def __init__(self, *, gateway_store: GatewayStore, **kwargs: object) -> None:
        self.gateway_store = gateway_store
        super().__init__(**kwargs)  # type: ignore[arg-type]


class GatewayCapabilityProjectionAuthority:
    """Gateway feed/ack/readback authority backed by the same GatewayStore."""

    def __init__(self, *, gateway_store: GatewayStore) -> None:
        self.gateway_store = gateway_store

    async def projection_event(self, event_id: UUID) -> MinibookProjectionEvent | None:
        source = self.gateway_store.factory_promotion_source(event_id)
        if source is None:
            return None
        block, job = source
        summary = _promotion_benchmark_summary(self.gateway_store, block)
        event = factory_promotion_projection(block, job, benchmark_summary=summary)
        generic_summary_sha256 = _one_generic_summary_sha256(
            self.gateway_store, UUID(str(block["job_id"]))
        )
        material = "|".join(
            (
                str(block["job_id"]),
                str(job["required_capability"]),
                str(block["subject_version"]),
                str(block["event_id"]),
                generic_summary_sha256,
            )
        )
        digest = f"sha256:{hashlib.sha256(material.encode()).hexdigest()}"
        return event.model_copy(
            update={"payload": event.payload.model_copy(update={"artifact_digest": digest})}
        )

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
        source = self.gateway_store.factory_promotion_source(event_id)
        event = await self.projection_event(event_id)
        if source is None or event is None:
            return None
        block, _ = source
        mirrors = tuple(
            item
            for item in self.gateway_store.delivery_events(
                project_id=f"factory:{block['job_id']}",
                run_id=f"attempt:{block['attempt']}",
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


def _one_generic_summary_sha256(store: GatewayStore, job_id: UUID) -> str:
    with store.storage.transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT summary_sha256 FROM capability_generic_benchmarks "
                "WHERE job_id = %s ORDER BY created_at",
                (str(job_id),),
            )
            rows = tuple(cursor.fetchall())
    if len(rows) != 1 or _SHA256.fullmatch(str(rows[0]["summary_sha256"])) is None:
        raise CapabilityLiveAuthorityUnavailable("generic benchmark summary is ambiguous")
    return str(rows[0]["summary_sha256"])


@dataclass(frozen=True)
class CapabilityAssemblyFactoryRuntime:
    """Already-composed current Factory graph; no arbitrary plugin seam."""

    factory: AgentFactoryLiveComposition
    provisioning: CurrentAuthorityCapabilityFactoryProvisioner
    n8n: GatewayCapabilityN8nWorkBatchAuthority


def build_production_capability_assembly_from_environment(
    *,
    repository_root: Path,
    environment: Mapping[str, str],
    store: GatewayStore,
    runtime: CapabilityAssemblyFactoryRuntime,
    private_benchmark_catalog: PrivateHoldoutStore,
    clock: Callable[[], datetime] | None = None,
) -> ProductionCapabilityAssemblyComposition:
    """Compose one sealed D6 graph from one store and native authorities."""

    settings = load_capability_assembly_bootstrap_settings(
        environment=environment, repository_root=repository_root
    )
    selected_clock = clock or (lambda: datetime.now(timezone.utc))
    if not _native_runtime_is_bound(
        runtime=runtime,
        store=store,
        private_benchmark_catalog=private_benchmark_catalog,
    ):
        raise CapabilityAssemblyBootstrapConfigurationError(
            "capability assembly Factory runtime is not Gateway-native"
        )
    expected_provider_binding = (
        settings.provider_url,
        settings.provider_policy_sha256,
        settings.provider_price_card_sha256,
    )
    if expected_provider_binding not in getattr(
        store, "_runtime_provider_proxy_bindings", frozenset()
    ):
        raise CapabilityAssemblyBootstrapConfigurationError(
            "capability assembly provider authority is not Gateway-bound"
        )
    artifacts = ContentAddressedArtifactAdapter(settings.artifact_root)
    provisioner_artifacts = getattr(runtime.provisioning, "_source_artifacts", None)
    if (
        type(provisioner_artifacts) is not ContentAddressedArtifactAdapter
        or getattr(getattr(provisioner_artifacts, "_store", None), "root", None)
        != settings.artifact_root
    ):
        raise CapabilityAssemblyBootstrapConfigurationError(
            "capability assembly Factory CAS is not canonical"
        )
    source_artifacts = RepositoryFactoryInputArtifactStore(artifacts=artifacts)
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
        repository_root=repository_root,
        settings=settings.assembly,
        authorities=authorities,
        holdout_store=private_benchmark_catalog,
        clock=selected_clock,
    )
    _BOOTSTRAP_RESOURCES[composition] = (resume_http, minibook_http)
    return composition


def _native_runtime_is_bound(
    *,
    runtime: CapabilityAssemblyFactoryRuntime,
    store: GatewayStore,
    private_benchmark_catalog: PrivateHoldoutStore,
) -> bool:
    factory = getattr(runtime, "factory", None)
    provisioning = getattr(runtime, "provisioning", None)
    n8n = getattr(runtime, "n8n", None)
    if (
        type(factory) is not AgentFactoryLiveComposition
        or type(provisioning) is not CurrentAuthorityCapabilityFactoryProvisioner
        or type(n8n) is not GatewayCapabilityN8nWorkBatchAuthority
        or n8n.gateway_store is not store
        or type(factory.repository) is not GatewayFactoryRepository
        or getattr(factory.repository, "_store", None) is not store
        or type(factory.coordinator) is not FactoryCoordinator
        or getattr(factory.coordinator, "_repository", None) is not factory.repository
        or type(factory.lease_issuer) is not GatewayNextActionLeaseIssuer
        or getattr(factory.lease_issuer, "_store", None) is not store
        or type(factory.hermes) is not HermesCliFactory
        or getattr(factory.hermes, "_workflow_artifacts", None) is not factory.repository
        or type(factory.team_execution) is not ProductionFactoryTeamExecutionPort
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
    "CapabilityAssemblyFactoryRuntime",
    "CaptainBenchmarkToolEvidenceAuthority",
    "GatewayCapabilityN8nWorkBatchAuthority",
    "GatewayCapabilityProjectionAuthority",
    "GatewayCapabilityReconciliation",
    "GatewayGenericBenchmarkRecorder",
    "GatewayGenericCapabilityPromotion",
    "StoreBoundGatewayCapabilityResumeClient",
    "build_production_capability_assembly_from_environment",
    "close_production_capability_assembly",
    "load_capability_assembly_bootstrap_settings",
]
