"""Production composition for the generic three-input capability assembly.

This module is intentionally a Gateway-side composition root.  It joins the
repository-owned input admission, Gateway sole-writer repository, current
Factory lifecycle, runtime-resume authority, and Minibook projection effects
without opening a second database connection or manufacturing authority IDs.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol, cast
from uuid import UUID

from agenten.agent_factory.capability_live_runner import (
    CapabilityLiveCapabilityEvidence,
    CapabilityLiveReservation,
    CapabilityLiveStage,
    CapabilityLiveStageRequest,
)
from agenten.agent_factory.holdout_store import PrivateHoldoutStore
from agenten.agent_factory.skill_evaluation import ToolGapMarker
from agenten.agent_runtime.contracts import ArtifactRef
from agenten.delivery.minibook_events import (
    MinibookProjectionAcknowledgementV1,
    MinibookProjectionEvent,
)
from agenten.delivery.projector import MinibookProjector, ProjectionResult
from gateway.agent_factory_live_composition import AgentFactoryLiveComposition
from gateway.capability_assembly_admission import (
    RepositoryCapabilityAssemblyInputAdmission,
)
from gateway.capability_assembly_contracts import (
    CapabilityAssemblyBusinessBenchmarkPort,
    CapabilityAssemblyExecutionPort,
    CapabilityAssemblyFactoryPort,
    CapabilityAssemblyInputAdmissionPort,
    CapabilityAssemblyProjectionPort,
    CapabilityAssemblyReconciliationPort,
    CapabilityAssemblyRepositoryPort,
    CapabilityAssemblyStageRequest,
)
from gateway.capability_assembly_controller import CapabilityAssemblyController
from gateway.capability_assembly_repository import GatewayCapabilityAssemblyRepository
from gateway.capability_benchmark_process_executor import (
    GatewayIsolatedBenchmarkExecutorClient,
)
from gateway.capability_generic_benchmark import (
    GenericBenchmarkExecutorPort,
    GenericBenchmarkRecorderPort,
    BenchmarkEvidenceResolverPort,
    BenchmarkToolEvidenceAuthorityPort,
    ContentAddressedBenchmarkEvidenceResolver,
    GenericCapabilityBenchmarkAdapter,
    GenericPrivateHoldoutReader,
    GenericPrivateBenchmarkRepository,
    RuleBasedCapabilityBenchmarkEvaluator,
    build_generic_benchmark_suite,
)
from gateway.capability_live_current_adapters import (
    CapabilityProjectionReadback,
    RecordedFactoryCapabilityExecutionAdapter,
)
from gateway.capability_live_factory_adapter import (
    CapabilityFactoryProvisioningPort,
    CurrentCapabilityFactoryAdapter,
)
from gateway.capability_live_pipeline import (
    CapabilityExecutionAuthority,
    CapabilityFactoryAuthority,
    CapabilityLiveAuthorityUnavailable,
    CapabilityProjectionAuthority,
)
from gateway.capability_live_runtime_resume import (
    CapabilityResumeGatewayPort,
    GatewayCapabilityResumeExecutionAdapter,
)
from gateway.capability_live_provisioning import (
    CurrentAuthorityCapabilityFactoryProvisioner,
)
from gateway.store import GatewayStore


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")


class CapabilityAssemblyProductionConfigurationError(ValueError):
    """A production binding is incomplete before persistence or effects."""


class AsyncMinibookProjectionPort(Protocol):
    async def project(self, event: MinibookProjectionEvent) -> ProjectionResult: ...

    async def acknowledgement(
        self,
        event: MinibookProjectionEvent,
    ) -> MinibookProjectionAcknowledgementV1: ...


class CapabilityProjectionGatewayPort(Protocol):
    """Authenticated Gateway feed, acknowledgement, and read-back client."""

    async def projection_event(
        self,
        event_id: UUID,
    ) -> MinibookProjectionEvent | None: ...

    async def capability_promotion_event(
        self, release: CapabilityFactoryAuthority
    ) -> MinibookProjectionEvent: ...

    async def acknowledge_projection(
        self,
        acknowledgement: MinibookProjectionAcknowledgementV1,
    ) -> None: ...

    async def projection_readback(
        self,
        event_id: UUID,
    ) -> CapabilityProjectionReadback | None: ...


class Sha256ArtifactStorePort(Protocol):
    def put(self, content: bytes, media_type: str) -> ArtifactRef: ...

    def read_bytes(self, reference: ArtifactRef) -> bytes: ...


class CapabilityN8nWorkBatchAuthorityPort(Protocol):
    gateway_store: object

    def active_n8n_work_batch_id(self, job_id: UUID) -> str | None: ...


class GenericCapabilityPromotionPort(Protocol):
    gateway_store: object

    async def promote_generic_capability(
        self, request: CapabilityAssemblyStageRequest, binding: object
    ) -> object: ...


class _SanitizedResumeGateway:
    def __init__(self, gateway: CapabilityResumeGatewayPort) -> None:
        self._gateway = gateway

    async def authorize_resume(self, request: object):
        try:
            return await self._gateway.authorize_resume(request)
        except Exception:
            raise CapabilityLiveAuthorityUnavailable(
                "Gateway resume authorization is unavailable"
            ) from None

    async def dispatch_resume(self, authorization: object):
        try:
            return await self._gateway.dispatch_resume(authorization)
        except Exception:
            raise CapabilityLiveAuthorityUnavailable(
                "Gateway resume dispatch is unavailable"
            ) from None

    async def readback_resume(self, authorization: object):
        try:
            return await self._gateway.readback_resume(authorization)
        except Exception:
            raise CapabilityLiveAuthorityUnavailable(
                "Gateway resume readback is unavailable"
            ) from None


class _SanitizedReconciliation:
    def __init__(self, reconciliation: CapabilityAssemblyReconciliationPort) -> None:
        self._reconciliation = reconciliation

    async def reconcile(self, request: object):
        try:
            return await self._reconciliation.reconcile(request)
        except Exception:
            raise CapabilityLiveAuthorityUnavailable(
                "Gateway reconciliation authority is unavailable"
            ) from None


class RepositoryFactoryInputArtifactStore:
    """Expose canonical Factory refs over the existing SHA-256 artifact CAS."""

    def __init__(self, *, artifacts: Sha256ArtifactStorePort) -> None:
        _preflight_methods(
            (
                (artifacts, "put", False, 2),
                (artifacts, "read_bytes", False, 1),
            )
        )
        self._artifacts = artifacts

    def put_factory_input(self, source: bytes) -> ArtifactRef:
        import hashlib

        digest = hashlib.sha256(source).hexdigest()
        stored = self._artifacts.put(source, "text/markdown")
        if (
            stored.sha256 != digest
            or stored.uri != f"artifact://sha256/{digest}"
            or stored.media_type != "text/markdown"
        ):
            raise CapabilityAssemblyProductionConfigurationError(
                "Factory input artifact store changed source digest"
            )
        expected = ArtifactRef(
            uri=f"artifact://factory-input/{digest}",
            sha256=digest,
            media_type="text/markdown",
        )
        return expected

    def read_bytes(self, reference: ArtifactRef) -> bytes:
        expected_uri = f"artifact://factory-input/{reference.sha256}"
        if reference.uri != expected_uri or reference.media_type != "text/markdown":
            raise CapabilityAssemblyProductionConfigurationError(
                "Factory input artifact reference is not canonical"
            )
        source = self._artifacts.read_bytes(
            ArtifactRef(
                uri=f"artifact://sha256/{reference.sha256}",
                sha256=reference.sha256,
                media_type="text/markdown",
            )
        )
        import hashlib

        if hashlib.sha256(source).hexdigest() != reference.sha256:
            raise CapabilityAssemblyProductionConfigurationError(
                "Factory input artifact bytes changed"
            )
        return source


class AsyncMinibookProjectorAdapter:
    """Run the official synchronous Minibook projector off the event loop."""

    def __init__(self, *, projector: MinibookProjector) -> None:
        _require_method(
            projector,
            "project",
            asynchronous=False,
            positional_count=1,
            label="minibook_projector.project",
        )
        _require_method(
            projector,
            "acknowledgement",
            asynchronous=False,
            positional_count=1,
            label="minibook_projector.acknowledgement",
        )
        self._projector = projector

    async def project(self, event: MinibookProjectionEvent) -> ProjectionResult:
        try:
            result = await asyncio.to_thread(self._projector.project, event)
        except Exception:
            raise CapabilityAssemblyProductionConfigurationError(
                "Minibook projection authority is unavailable"
            ) from None
        if not isinstance(result, ProjectionResult):
            raise CapabilityAssemblyProductionConfigurationError(
                "Minibook projector returned an invalid projection result"
            )
        return result

    async def acknowledgement(
        self,
        event: MinibookProjectionEvent,
    ) -> MinibookProjectionAcknowledgementV1:
        try:
            result = await asyncio.to_thread(self._projector.acknowledgement, event)
        except Exception:
            raise CapabilityAssemblyProductionConfigurationError(
                "Minibook acknowledgement authority is unavailable"
            ) from None
        if not isinstance(result, MinibookProjectionAcknowledgementV1):
            raise CapabilityAssemblyProductionConfigurationError(
                "Minibook projector returned an invalid acknowledgement"
            )
        return result


class GatewayMinibookProjectionEffectsAdapter:
    """Require feed -> project -> acknowledge -> Gateway read-back in order."""

    def __init__(
        self,
        *,
        gateway: CapabilityProjectionGatewayPort,
        minibook: AsyncMinibookProjectionPort,
    ) -> None:
        _preflight_methods(
            (
                (gateway, "projection_event", True, 1),
                (gateway, "capability_promotion_event", True, 1),
                (gateway, "acknowledge_projection", True, 1),
                (gateway, "projection_readback", True, 1),
                (minibook, "project", True, 1),
                (minibook, "acknowledgement", True, 1),
            )
        )
        self._gateway = gateway
        self._minibook = minibook

    async def promotion_event(
        self, release: CapabilityFactoryAuthority
    ) -> MinibookProjectionEvent:
        try:
            event = await self._gateway.capability_promotion_event(release)
        except Exception:
            raise CapabilityAssemblyProductionConfigurationError(
                "Gateway promotion projection authority is unavailable"
            ) from None
        if (
            not isinstance(event, MinibookProjectionEvent)
            or event.event_id != release.release_authority_id
            or event.correlation_id != release.correlation_id
            or event.subject_id != _factory_subject_reference(release.job_id)
            or event.subject_version != release.capability_version
            or event.schema_name != "captain.minibook-projection.v2"
            or event.event_type != "capability.promoted"
            or event.payload.view != "validation"
            or event.payload.template_id != "factory_capability_ready_to_use"
            or event.payload.status_id != "ready_to_use"
            or event.payload.actor_role_id != "captain_gateway"
            or event.payload.artifact_digest != _release_projection_digest(release)
        ):
            raise CapabilityAssemblyProductionConfigurationError(
                "Gateway promotion projection authority is invalid"
            )
        return event
    async def project_and_ack(
        self,
        event: MinibookProjectionEvent,
    ) -> CapabilityProjectionReadback:
        try:
            return await self._project_and_ack(event)
        except CapabilityAssemblyProductionConfigurationError:
            raise
        except Exception:
            raise CapabilityAssemblyProductionConfigurationError(
                "Gateway Minibook projection authority is unavailable"
            ) from None

    async def _project_and_ack(
        self, event: MinibookProjectionEvent
    ) -> CapabilityProjectionReadback:
        authoritative = await self._gateway.projection_event(event.event_id)
        if authoritative != event:
            raise CapabilityAssemblyProductionConfigurationError(
                "projection event does not match the authoritative Gateway feed"
            )
        projection = await self._minibook.project(event)
        if (
            not isinstance(projection, ProjectionResult)
            or projection.event_id != str(event.event_id)
            or projection.outcome not in {"projected", "duplicate"}
        ):
            raise CapabilityAssemblyProductionConfigurationError(
                "Minibook did not persist the authoritative projection"
            )
        acknowledgement = await self._minibook.acknowledgement(event)
        self._require_acknowledgement(event, acknowledgement)
        if projection.post_id is not None and projection.post_id != acknowledgement.post_id:
            raise CapabilityAssemblyProductionConfigurationError(
                "Minibook projection result changed the canonical post identity"
            )
        await self._gateway.acknowledge_projection(acknowledgement)
        readback = await self._gateway.projection_readback(event.event_id)
        return self._require_readback(event, acknowledgement, readback)

    async def readback(
        self,
        event_id: UUID,
    ) -> CapabilityProjectionReadback | None:
        try:
            return await self._readback(event_id)
        except CapabilityAssemblyProductionConfigurationError:
            raise
        except Exception:
            raise CapabilityAssemblyProductionConfigurationError(
                "Gateway Minibook readback authority is unavailable"
            ) from None

    async def _readback(
        self, event_id: UUID
    ) -> CapabilityProjectionReadback | None:
        event = await self._gateway.projection_event(event_id)
        if event is None:
            return None
        readback = await self._gateway.projection_readback(event_id)
        if readback is None:
            return None
        return self._require_readback(event, readback.acknowledgement, readback)

    @staticmethod
    def _require_acknowledgement(
        event: MinibookProjectionEvent,
        acknowledgement: MinibookProjectionAcknowledgementV1,
    ) -> None:
        if (
            not isinstance(acknowledgement, MinibookProjectionAcknowledgementV1)
            or acknowledgement.projection_event_id != event.event_id
            or acknowledgement.correlation_id != event.correlation_id
            or acknowledgement.subject_id != event.subject_id
            or acknowledgement.subject_version != event.subject_version
            or acknowledgement.outcome != "mirrored"
        ):
            raise CapabilityAssemblyProductionConfigurationError(
                "Minibook acknowledgement changed projection authority"
            )

    @classmethod
    def _require_readback(
        cls,
        event: MinibookProjectionEvent,
        acknowledgement: MinibookProjectionAcknowledgementV1,
        readback: CapabilityProjectionReadback | None,
    ) -> CapabilityProjectionReadback:
        cls._require_acknowledgement(event, acknowledgement)
        if (
            not isinstance(readback, CapabilityProjectionReadback)
            or readback.event != event
            or readback.acknowledgement != acknowledgement
            or not isinstance(readback.evidence_ref, ArtifactRef)
        ):
            raise CapabilityAssemblyProductionConfigurationError(
                "Gateway projection acknowledgement read-back is unavailable"
            )
        return readback


def _factory_subject_reference(job_id: UUID) -> str:
    if job_id.version == 4:
        return f"subject:{job_id}"
    subject_digest = bytearray(
        hashlib.sha256(f"captain-factory-subject:{job_id}".encode()).digest()[:16]
    )
    subject_digest[6] = (subject_digest[6] & 0x0F) | 0x40
    subject_digest[8] = (subject_digest[8] & 0x3F) | 0x80
    return f"subject:{UUID(bytes=bytes(subject_digest))}"


def _release_projection_digest(release: CapabilityFactoryAuthority) -> str:
    if release.release_authority_id is None:
        return "sha256:" + "0" * 64
    material = "|".join((
        str(release.job_id), release.capability_id, str(release.capability_version),
        str(release.release_authority_id),
        "" if release.benchmark_summary_ref is None else release.benchmark_summary_ref.sha256,
    ))
    return f"sha256:{hashlib.sha256(material.encode()).hexdigest()}"


class GenericGatewayMinibookCapabilityProjectionAdapter:
    """Project an event issued by Gateway, without profile-specific summaries."""

    def __init__(self, *, effects: GatewayMinibookProjectionEffectsAdapter) -> None:
        self._effects = effects

    async def project_and_readback(
        self,
        request: CapabilityAssemblyStageRequest,
        release: CapabilityFactoryAuthority,
        execution: CapabilityExecutionAuthority,
    ) -> CapabilityProjectionAuthority:
        del request
        try:
            event = await self._effects.promotion_event(release)
            readback = await self._effects.project_and_ack(event)
            return self._authority(release, execution, event, readback)
        except CapabilityAssemblyProductionConfigurationError:
            raise CapabilityLiveAuthorityUnavailable(
                "Minibook projection authority is unavailable"
            ) from None

    async def readback(
        self,
        capability: CapabilityLiveCapabilityEvidence,
        release: CapabilityFactoryAuthority,
        execution: CapabilityExecutionAuthority,
    ) -> CapabilityProjectionAuthority:
        del capability
        try:
            event = await self._effects.promotion_event(release)
            readback = await self._effects.readback(event.event_id)
        except CapabilityAssemblyProductionConfigurationError:
            raise CapabilityLiveAuthorityUnavailable(
                "Minibook projection readback is unavailable"
            ) from None
        if readback is None:
            raise CapabilityLiveAuthorityUnavailable(
                "Minibook projection readback is unavailable"
            )
        return self._authority(release, execution, event, readback)

    @staticmethod
    def _authority(
        release: CapabilityFactoryAuthority,
        execution: CapabilityExecutionAuthority,
        event: MinibookProjectionEvent,
        readback: CapabilityProjectionReadback,
    ) -> CapabilityProjectionAuthority:
        acknowledgement = readback.acknowledgement
        if (
            readback.event != event
            or acknowledgement.projection_event_id != event.event_id
            or acknowledgement.correlation_id != release.correlation_id
            or acknowledgement.subject_version != release.capability_version
        ):
            raise CapabilityLiveAuthorityUnavailable(
                "Minibook acknowledgement does not match Gateway promotion"
            )
        return CapabilityProjectionAuthority(
            job_id=release.job_id,
            correlation_id=release.correlation_id,
            capability_id=release.capability_id,
            capability_version=release.capability_version,
            execution_command_id=execution.execution_command_id,
            execution_result_id=execution.execution_result_id,
            projection_event_id=event.event_id,
            projection_readback_id=acknowledgement.acknowledgement_id,
            evidence_refs=(readback.evidence_ref,),
        )


@dataclass(frozen=True, repr=False)
class CapabilityAssemblyProductionSettings:
    expected_repository_revision: str
    expected_sha256_by_path: Mapping[str, str]
    compiled_tool_gaps_by_source_sha256: Mapping[str, tuple[ToolGapMarker, ...]]
    subject_version: int

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str],
    ) -> "CapabilityAssemblyProductionSettings":
        revision = environment.get(
            "CAPTAIN_CAPABILITY_ASSEMBLY_REPOSITORY_REVISION", ""
        ).strip()
        if _REVISION.fullmatch(revision) is None:
            raise CapabilityAssemblyProductionConfigurationError(
                "capability assembly repository revision is unavailable"
            )
        inputs = _json_object(
            environment.get("CAPTAIN_CAPABILITY_ASSEMBLY_INPUTS_JSON", ""),
            label="input digest manifest",
        )
        if len(inputs) != 3:
            raise CapabilityAssemblyProductionConfigurationError(
                "capability assembly requires exactly three repository inputs"
            )
        digests: dict[str, str] = {}
        for raw_path, raw_digest in inputs.items():
            path = _canonical_input_path(raw_path)
            if not isinstance(raw_digest, str) or _SHA256.fullmatch(raw_digest) is None:
                raise CapabilityAssemblyProductionConfigurationError(
                    "capability assembly input digest is invalid"
                )
            digests[path] = raw_digest
        if len(digests) != 3 or len(set(digests.values())) != 3:
            raise CapabilityAssemblyProductionConfigurationError(
                "capability assembly inputs must have distinct paths and bytes"
            )
        raw_gaps = _json_object(
            environment.get("CAPTAIN_CAPABILITY_ASSEMBLY_TOOL_GAPS_JSON", ""),
            label="tool-gap manifest",
        )
        if set(raw_gaps) != set(digests.values()):
            raise CapabilityAssemblyProductionConfigurationError(
                "tool-gap manifest must bind every input digest"
            )
        gaps: dict[str, tuple[ToolGapMarker, ...]] = {}
        try:
            for digest, markers in raw_gaps.items():
                if not isinstance(markers, list):
                    raise TypeError
                gaps[digest] = tuple(ToolGapMarker.model_validate(item) for item in markers)
        except (TypeError, ValueError):
            raise CapabilityAssemblyProductionConfigurationError(
                "tool-gap manifest is invalid"
            ) from None
        raw_subject_version = environment.get(
            "CAPTAIN_CAPABILITY_ASSEMBLY_SUBJECT_VERSION", ""
        ).strip()
        try:
            subject_version = int(raw_subject_version)
        except ValueError:
            subject_version = 0
        if subject_version < 1 or str(subject_version) != raw_subject_version:
            raise CapabilityAssemblyProductionConfigurationError(
                "capability assembly subject version is invalid"
            )
        return cls(
            expected_repository_revision=revision,
            expected_sha256_by_path=digests,
            compiled_tool_gaps_by_source_sha256=gaps,
            subject_version=subject_version,
        )


@dataclass(frozen=True, repr=False)
class CapabilityAssemblyProductionAuthorities:
    store: GatewayStore
    factory: AgentFactoryLiveComposition
    provisioning: CapabilityFactoryProvisioningPort
    resume_gateway: CapabilityResumeGatewayPort
    reconciliation: CapabilityAssemblyReconciliationPort
    minibook_effects: GatewayMinibookProjectionEffectsAdapter
    source_artifacts: RepositoryFactoryInputArtifactStore
    benchmark_executor: GenericBenchmarkExecutorPort
    benchmark_evidence: BenchmarkEvidenceResolverPort
    benchmark_tool_authority: BenchmarkToolEvidenceAuthorityPort
    benchmark_recorder: GenericBenchmarkRecorderPort
    promotion: GenericCapabilityPromotionPort
    n8n_work_batches: CapabilityN8nWorkBatchAuthorityPort


@dataclass(frozen=True)
class _CurrentInputView:
    input_id: str
    conversation_pattern: Literal["sequential", "selector_group_chat", "swarm"]
    source_ref: ArtifactRef
    integration_intent: Literal["n8n", "none"]


@dataclass(frozen=True)
class _CurrentStageRequestView:
    run_id: UUID
    input: _CurrentInputView
    correlation_id: UUID
    reservation: CapabilityLiveReservation
    stage: CapabilityLiveStage
    controlled_recovery_required: bool
    suite_ref: ArtifactRef | None
    hermes_result_ref: ArtifactRef | None
    retry_number: int


class CapabilityAssemblyCurrentFactoryAdapter(CurrentCapabilityFactoryAdapter):
    """Adapt canonical assembly input semantics to the current Factory seam."""

    def __init__(self, *, private_cases: GenericPrivateBenchmarkRepository,
                 n8n_work_batches: CapabilityN8nWorkBatchAuthorityPort,
                 promotion: GenericCapabilityPromotionPort, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._private_cases = private_cases
        self._n8n_work_batches = n8n_work_batches
        self._promotion = promotion

    def job_id_for(self, request: CapabilityAssemblyStageRequest) -> UUID | None:
        try:
            return super().job_id_for(
                cast(CapabilityLiveStageRequest, _current_stage_request(request))
            )
        except Exception:
            raise CapabilityLiveAuthorityUnavailable(
                "Gateway Factory job authority is unavailable"
            ) from None

    def binding_for(self, request: CapabilityAssemblyStageRequest):
        try:
            return self._provisioning.binding_for(
                cast(CapabilityLiveStageRequest, _current_stage_request(request))
            )
        except Exception:
            raise CapabilityLiveAuthorityUnavailable(
                "Gateway Factory binding authority is unavailable"
            ) from None

    async def scaffold(self, request: CapabilityAssemblyStageRequest):
        try:
            authority = await super().scaffold(
                cast(CapabilityLiveStageRequest, _current_stage_request(request))
            )
        except Exception:
            raise CapabilityLiveAuthorityUnavailable(
                "Gateway Factory scaffold authority is unavailable"
            ) from None
        self._require_n8n_authority(request)
        return self._generic_suite_authority(request, authority)

    async def implement(self, request: CapabilityAssemblyStageRequest):
        try:
            authority = await super().implement(
                cast(CapabilityLiveStageRequest, _current_stage_request(request))
            )
        except Exception:
            raise CapabilityLiveAuthorityUnavailable(
                "Gateway Factory implementation authority is unavailable"
            ) from None
        self._require_n8n_authority(request)
        return self._generic_suite_authority(request, authority)

    async def seal(self, request: CapabilityAssemblyStageRequest):
        try:
            authority = await super().seal(
                cast(CapabilityLiveStageRequest, _current_stage_request(request))
            )
        except Exception:
            raise CapabilityLiveAuthorityUnavailable(
                "Gateway Factory seal authority is unavailable"
            ) from None
        self._require_n8n_authority(request)
        return self._generic_suite_authority(request, authority)

    async def promote(self, request: CapabilityAssemblyStageRequest):
        if request.input.required_tool_gaps:
            raise CapabilityLiveAuthorityUnavailable(
                "required TODO_TOOL gaps block capability promotion"
            )
        binding = self._provisioning.binding_for(
            cast(CapabilityLiveStageRequest, _current_stage_request(request))
        )
        if binding is None:
            raise CapabilityLiveAuthorityUnavailable(
                "Factory provisioning binding is unavailable"
            )
        self._require_n8n_authority(request)
        try:
            authority = await self._promotion.promote_generic_capability(request, binding)
        except Exception:
            raise CapabilityLiveAuthorityUnavailable(
                "generic capability promotion authority is unavailable"
            ) from None
        return self._generic_suite_authority(request, authority)

    def _generic_suite_authority(self, request: CapabilityAssemblyStageRequest, authority: object):
        if authority is None:
            return None
        if not isinstance(authority, CapabilityFactoryAuthority):
            raise CapabilityLiveAuthorityUnavailable(
                "generic Factory authority is invalid"
            )
        try:
            suite = build_generic_benchmark_suite(
                request.input, self._private_cases
            )
            suite_ref = suite.suite_ref
            self._private_cases.retire(suite)
        except Exception:
            raise CapabilityLiveAuthorityUnavailable(
                "private benchmark suite authority is unavailable"
            ) from None
        return authority.model_copy(update={"suite_ref": suite_ref})

    def _require_n8n_authority(self, request: CapabilityAssemblyStageRequest) -> None:
        binding = self._provisioning.binding_for(
            cast(CapabilityLiveStageRequest, _current_stage_request(request))
        )
        if binding is None:
            raise CapabilityLiveAuthorityUnavailable("Factory provisioning binding is unavailable")
        expected = _compiled_n8n_required(request)
        try:
            recorded = self._n8n_work_batches.active_n8n_work_batch_id(binding.job.job_id)
        except Exception:
            raise CapabilityLiveAuthorityUnavailable(
                "Gateway n8n WorkBatch authority is unavailable"
            ) from None
        bound = binding.n8n_work_batch_id
        if expected and (not bound or recorded != bound):
            raise CapabilityLiveAuthorityUnavailable(
                "required n8n WorkBatch lease does not match compiled integration intent"
            )
        if not expected and (bound is not None or recorded is not None):
            raise CapabilityLiveAuthorityUnavailable(
                "unexpected n8n WorkBatch lease conflicts with compiled integration intent"
            )


def compose_capability_assembly_controller(
    *,
    repository: CapabilityAssemblyRepositoryPort,
    admission: CapabilityAssemblyInputAdmissionPort,
    factory_composition: AgentFactoryLiveComposition,
    provisioning: CapabilityFactoryProvisioningPort,
    resume_gateway: CapabilityResumeGatewayPort,
    reconciliation: CapabilityAssemblyReconciliationPort,
    minibook_effects: GatewayMinibookProjectionEffectsAdapter,
    benchmark_executor: GenericBenchmarkExecutorPort,
    benchmark_evidence: BenchmarkEvidenceResolverPort,
    benchmark_tool_authority: BenchmarkToolEvidenceAuthorityPort,
    benchmark_recorder: GenericBenchmarkRecorderPort,
    promotion: GenericCapabilityPromotionPort,
    n8n_work_batches: CapabilityN8nWorkBatchAuthorityPort,
    holdout_store: GenericPrivateHoldoutReader,
    clock: Callable[[], datetime],
) -> CapabilityAssemblyController:
    """Join already configured Gateway-owned ports without performing effects."""

    _preflight_composition(
        repository=repository,
        admission=admission,
        factory_composition=factory_composition,
        provisioning=provisioning,
        resume_gateway=resume_gateway,
        reconciliation=reconciliation,
        minibook_effects=minibook_effects,
        benchmark_executor=benchmark_executor,
        benchmark_evidence=benchmark_evidence,
        benchmark_tool_authority=benchmark_tool_authority,
        benchmark_recorder=benchmark_recorder,
        promotion=promotion,
        n8n_work_batches=n8n_work_batches,
        holdout_store=holdout_store,
        clock=clock,
        require_isolated_executor=False,
    )
    private_cases = GenericPrivateBenchmarkRepository(
        holdouts=holdout_store,
        executor_authority_id=benchmark_executor.authority_id,
        evaluator_authority_id=RuleBasedCapabilityBenchmarkEvaluator.AUTHORITY_ID,
    )
    resume = GatewayCapabilityResumeExecutionAdapter(
        gateway=cast(CapabilityResumeGatewayPort, _SanitizedResumeGateway(resume_gateway))
    )
    factory: CapabilityAssemblyFactoryPort = CapabilityAssemblyCurrentFactoryAdapter(
        composition=factory_composition,
        provisioning=provisioning,
        clock=clock,
        private_cases=private_cases,
        n8n_work_batches=n8n_work_batches,
        promotion=promotion,
    )
    benchmark: CapabilityAssemblyBusinessBenchmarkPort = GenericCapabilityBenchmarkAdapter(
        bindings=factory,
        executor=benchmark_executor,
        evidence=benchmark_evidence,
        tool_authority=benchmark_tool_authority,
        recorder=benchmark_recorder,
        private_cases=private_cases,
    )
    execution: CapabilityAssemblyExecutionPort = RecordedFactoryCapabilityExecutionAdapter(
        composition=factory_composition,
        resume_execution=resume,
    )
    projection: CapabilityAssemblyProjectionPort = (
        GenericGatewayMinibookCapabilityProjectionAdapter(effects=minibook_effects)
    )
    return CapabilityAssemblyController(
        repository=repository,
        admission=admission,
        factory=factory,
        benchmark=benchmark,
        resume=resume,
        reconciliation=cast(
            CapabilityAssemblyReconciliationPort,
            _SanitizedReconciliation(reconciliation),
        ),
        execution=execution,
        minibook_projection=projection,
    )


def build_capability_assembly_controller(
    *,
    repository_root: Path,
    settings: CapabilityAssemblyProductionSettings,
    authorities: CapabilityAssemblyProductionAuthorities,
    holdout_store: PrivateHoldoutStore,
    clock: Callable[[], datetime] | None = None,
) -> CapabilityAssemblyController:
    """Build the future route-ready production controller without live calls."""

    root = repository_root.resolve(strict=True)
    selected_clock = clock or (lambda: datetime.now(timezone.utc))
    if not isinstance(authorities.store, GatewayStore):
        raise CapabilityAssemblyProductionConfigurationError(
            "capability assembly requires the Gateway-owned store"
        )
    _require_same_gateway_authority(authorities)
    _require_method(
        authorities.store.storage,
        "transaction",
        asynchronous=False,
        positional_count=0,
        label="gateway_store.storage.transaction",
    )
    admission = RepositoryCapabilityAssemblyInputAdmission(
        repository_root=root,
        expected_repository_revision=settings.expected_repository_revision,
        expected_sha256_by_path=settings.expected_sha256_by_path,
        compiled_tool_gaps_by_source_sha256=(
            settings.compiled_tool_gaps_by_source_sha256
        ),
        holdout_store=holdout_store,
        subject_version=settings.subject_version,
        source_artifacts=authorities.source_artifacts,
        require_private_benchmark_cases=True,
    )
    # Validate every external binding before Gateway repository construction,
    # whose constructor owns the only schema side effect in this builder.
    _preflight_composition(
        repository=_RepositoryContractProbe(),
        admission=admission,
        factory_composition=authorities.factory,
        provisioning=authorities.provisioning,
        resume_gateway=authorities.resume_gateway,
        reconciliation=authorities.reconciliation,
        minibook_effects=authorities.minibook_effects,
        benchmark_executor=authorities.benchmark_executor,
        benchmark_evidence=authorities.benchmark_evidence,
        benchmark_tool_authority=authorities.benchmark_tool_authority,
        benchmark_recorder=authorities.benchmark_recorder,
        promotion=authorities.promotion,
        n8n_work_batches=authorities.n8n_work_batches,
        holdout_store=holdout_store,
        source_artifacts=authorities.source_artifacts,
        clock=selected_clock,
        require_isolated_executor=True,
    )
    repository = GatewayCapabilityAssemblyRepository(authorities.store)
    return compose_capability_assembly_controller(
        repository=repository,
        admission=admission,
        factory_composition=authorities.factory,
        provisioning=authorities.provisioning,
        resume_gateway=authorities.resume_gateway,
        reconciliation=authorities.reconciliation,
        minibook_effects=authorities.minibook_effects,
        benchmark_executor=authorities.benchmark_executor,
        benchmark_evidence=authorities.benchmark_evidence,
        benchmark_tool_authority=authorities.benchmark_tool_authority,
        benchmark_recorder=authorities.benchmark_recorder,
        promotion=authorities.promotion,
        n8n_work_batches=authorities.n8n_work_batches,
        holdout_store=cast(GenericPrivateHoldoutReader, holdout_store),
        clock=selected_clock,
    )


def _require_same_gateway_authority(
    authorities: CapabilityAssemblyProductionAuthorities,
) -> None:
    """Reject mixed/fake authority graphs before schema or external effects."""
    store = authorities.store
    repository = getattr(authorities.factory, "repository", None)
    candidates = {
        "Factory repository": getattr(repository, "_store", None),
        "Factory provisioner": getattr(
            getattr(authorities.provisioning, "_repository", None), "_store", None
        ),
        "resume Gateway": getattr(authorities.resume_gateway, "gateway_store", None),
        "reconciliation Gateway": getattr(authorities.reconciliation, "gateway_store", None),
        "benchmark executor": getattr(authorities.benchmark_executor, "gateway_store", None),
        "benchmark evidence CAS": getattr(authorities.benchmark_evidence, "gateway_store", None),
        "benchmark tool evidence authority": getattr(authorities.benchmark_tool_authority, "gateway_store", None),
        "benchmark recorder": getattr(authorities.benchmark_recorder, "gateway_store", None),
        "promotion Gateway": getattr(authorities.promotion, "gateway_store", None),
        "n8n WorkBatch Gateway": getattr(authorities.n8n_work_batches, "gateway_store", None),
        "projection Gateway": getattr(
            getattr(authorities.minibook_effects, "_gateway", None),
            "gateway_store",
            None,
        ),
    }
    for label, candidate in candidates.items():
        if candidate is not store:
            raise CapabilityAssemblyProductionConfigurationError(
                f"{label} is not bound to the production Gateway store"
            )


class _RepositoryContractProbe:
    """Signature-only repository used before the real schema-owning adapter."""

    def history(self, assembly_id: object) -> tuple[object, ...]:
        del assembly_id
        return ()

    def find_by_idempotency_key(self, idempotency_key: object) -> None:
        del idempotency_key

    def append(self, checkpoint: object, *, expected_revision: int) -> object:
        del expected_revision
        return checkpoint

    def consume_retry_authorization_and_append(
        self,
        checkpoint: object,
        authorization: object,
        *,
        expected_revision: int,
    ) -> object:
        del authorization, expected_revision
        return checkpoint


def _preflight_composition(
    *,
    repository: object,
    admission: object,
    factory_composition: object,
    provisioning: object,
    resume_gateway: object,
    reconciliation: object,
    minibook_effects: object,
    benchmark_executor: object,
    benchmark_evidence: object,
    benchmark_tool_authority: object,
    benchmark_recorder: object,
    promotion: object,
    n8n_work_batches: object,
    holdout_store: object,
    source_artifacts: object | None = None,
    clock: object,
    require_isolated_executor: bool,
) -> None:
    if not isinstance(factory_composition, AgentFactoryLiveComposition):
        raise CapabilityAssemblyProductionConfigurationError(
            "factory composition is not the current Gateway authority"
        )
    if not isinstance(minibook_effects, GatewayMinibookProjectionEffectsAdapter):
        raise CapabilityAssemblyProductionConfigurationError(
            "Minibook projection effects adapter is not concrete"
        )
    if not isinstance(benchmark_evidence, ContentAddressedBenchmarkEvidenceResolver):
        raise CapabilityAssemblyProductionConfigurationError(
            "benchmark evidence resolver is not the concrete Captain CAS adapter"
        )
    if (not isinstance(getattr(benchmark_executor, "authority_id", None), UUID)
            or benchmark_executor.authority_id == RuleBasedCapabilityBenchmarkEvaluator.AUTHORITY_ID):
        raise CapabilityAssemblyProductionConfigurationError(
            "benchmark executor and evaluator authorities are not independent"
        )
    if (require_isolated_executor
            and (type(benchmark_executor) is not GatewayIsolatedBenchmarkExecutorClient
                 or not benchmark_executor.uses_production_transport())):
        raise CapabilityAssemblyProductionConfigurationError(
            "production benchmark executor must use the immutable Gateway HTTP process client"
        )
    tool_authority_id = getattr(benchmark_tool_authority, "authority_id", None)
    if (not isinstance(tool_authority_id, UUID)
            or tool_authority_id in {
                benchmark_executor.authority_id,
                RuleBasedCapabilityBenchmarkEvaluator.AUTHORITY_ID,
            }):
        raise CapabilityAssemblyProductionConfigurationError(
            "benchmark tool evidence authority is not independent"
        )
    if source_artifacts is not None and not isinstance(
        source_artifacts, RepositoryFactoryInputArtifactStore
    ):
        raise CapabilityAssemblyProductionConfigurationError(
            "Factory input artifact store is not concrete"
        )
    if (
        source_artifacts is not None
        and isinstance(provisioning, CurrentAuthorityCapabilityFactoryProvisioner)
        and getattr(provisioning, "_source_artifacts", None) is not source_artifacts
    ):
        raise CapabilityAssemblyProductionConfigurationError(
            "Factory provisioner is not bound to the admitted input artifact store"
        )
    _require_callable(clock, positional_count=0, label="clock")
    _preflight_methods(
        (
            (repository, "history", False, 1),
            (repository, "find_by_idempotency_key", False, 1),
            (repository, "append", False, 1, {"expected_revision": 0}),
            (
                repository,
                "consume_retry_authorization_and_append",
                False,
                2,
                {"expected_revision": 0},
            ),
            (admission, "admit", False, 1),
            (factory_composition, "run", True, 1),
            (factory_composition, "projection", False, 1),
            (factory_composition, "snapshot", False, 1),
            (factory_composition, "workflow_artifacts", False, 1),
            (factory_composition, "workflow_budget_projection", False, 1),
            (
                factory_composition,
                "business_benchmark_summary_by_artifact",
                False,
                1,
            ),
            (provisioning, "provision", False, 1),
            (provisioning, "binding_for", False, 1),
            (
                resume_gateway,
                "authorize_resume",
                True,
                1,
                "resume_gateway.authorize_resume",
            ),
            (
                resume_gateway,
                "dispatch_resume",
                True,
                1,
                "resume_gateway.dispatch_resume",
            ),
            (
                resume_gateway,
                "readback_resume",
                True,
                1,
                "resume_gateway.readback_resume",
            ),
            (reconciliation, "reconcile", True, 1, "reconciliation.reconcile"),
            (minibook_effects, "project_and_ack", True, 1),
            (minibook_effects, "readback", True, 1),
            (benchmark_executor, "execute", True, 1),
            (benchmark_evidence, "resolve", False, 1),
            (benchmark_evidence, "record_evaluation", False, 1),
            (benchmark_tool_authority, "verify", False, 1),
            (benchmark_recorder, "record_generic_benchmark", False, 1),
            (promotion, "promote_generic_capability", True, 2),
            (n8n_work_batches, "active_n8n_work_batch_id", False, 1),
            (holdout_store, "get", False, 1),
            *(
                (
                    (source_artifacts, "put_factory_input", False, 1),
                    (source_artifacts, "read_bytes", False, 1),
                )
                if source_artifacts is not None
                else ()
            ),
        )
    )


def _preflight_methods(specifications: tuple[tuple[object, ...], ...]) -> None:
    for specification in specifications:
        binding, name, asynchronous, positional_count, *remaining = specification
        keyword_arguments: Mapping[str, object] = {}
        label = name
        for option in remaining:
            if isinstance(option, Mapping):
                keyword_arguments = option
            elif isinstance(option, str):
                label = option
        _require_method(
            binding,
            name,
            asynchronous=asynchronous,
            positional_count=positional_count,
            keyword_arguments=keyword_arguments,
            label=label,
        )


def _require_method(
    binding: object,
    method_name: str,
    *,
    asynchronous: bool,
    positional_count: int,
    label: str,
    keyword_arguments: Mapping[str, object] | None = None,
) -> None:
    method = getattr(binding, method_name, None)
    if not callable(method) or inspect.iscoroutinefunction(method) is not asynchronous:
        raise CapabilityAssemblyProductionConfigurationError(
            f"capability assembly binding {label} has wrong call mode"
        )
    _require_callable(
        method,
        positional_count=positional_count,
        keyword_arguments=keyword_arguments,
        label=label,
    )


def _require_callable(
    value: object,
    *,
    positional_count: int,
    label: str,
    keyword_arguments: Mapping[str, object] | None = None,
) -> None:
    if not callable(value):
        raise CapabilityAssemblyProductionConfigurationError(
            f"capability assembly binding {label} is not callable"
        )
    arguments = (object(),) * positional_count
    try:
        signature = inspect.signature(value)
        signature.bind(
            *arguments,
            **({} if keyword_arguments is None else keyword_arguments),
        )
    except (TypeError, ValueError):
        raise CapabilityAssemblyProductionConfigurationError(
            f"capability assembly binding {label} has incompatible signature"
        ) from None
    if positional_count:
        try:
            signature.bind(
                *arguments[:-1],
                **({} if keyword_arguments is None else keyword_arguments),
            )
        except TypeError:
            pass
        else:
            raise CapabilityAssemblyProductionConfigurationError(
                f"capability assembly binding {label} has incompatible signature"
            )
    try:
        signature.bind(
            *arguments,
            object(),
            **({} if keyword_arguments is None else keyword_arguments),
        )
    except TypeError:
        pass
    else:
        raise CapabilityAssemblyProductionConfigurationError(
            f"capability assembly binding {label} has incompatible signature"
        )


def _current_stage_request(
    request: CapabilityAssemblyStageRequest,
) -> _CurrentStageRequestView:
    n8n_required = _compiled_n8n_required(request)
    return _CurrentStageRequestView(
        run_id=request.run_id,
        input=_CurrentInputView(
            input_id=request.input.input_id,
            conversation_pattern=request.input.conversation_pattern,
            source_ref=request.input.source_ref,
            integration_intent="n8n" if n8n_required else "none",
        ),
        correlation_id=request.correlation_id,
        reservation=request.reservation,
        stage=request.stage,
        controlled_recovery_required=request.controlled_recovery_required,
        suite_ref=None,
        hermes_result_ref=request.hermes_result_ref,
        retry_number=request.retry_number,
    )


def _compiled_n8n_required(request: CapabilityAssemblyStageRequest) -> bool:
    required = {item.integration_key for item in request.input.document.integrations if item.required}
    bound = {key for agent in request.input.document.agents for key in agent.integration_keys}
    if required - bound:
        raise CapabilityLiveAuthorityUnavailable(
            "required integration is not bound to a canonical agent"
        )
    decisions = {key for node in request.input.compiled_specification.work_nodes
                 if node.kind == "integration_decision" for key in node.integration_keys}
    workflows = {node.node_id.removeprefix("n8n-")
                 for node in request.input.compiled_specification.work_nodes
                 if node.kind == "n8n_workflow" and node.node_id.startswith("n8n-")}
    if (required - decisions) or (required - workflows):
        raise CapabilityLiveAuthorityUnavailable(
            "compiled n8n work graph does not cover required integration bindings"
        )
    if (decisions & bound) != (workflows & bound):
        raise CapabilityLiveAuthorityUnavailable(
            "compiled n8n work graph contains an unmatched integration decision"
        )
    return bool(required)


def _canonical_input_path(value: object) -> str:
    if not isinstance(value, str) or "\\" in value or re.match(r"^[A-Za-z]:", value):
        raise CapabilityAssemblyProductionConfigurationError(
            "repository-relative TO_BE_BUILT.md path is required"
        )
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.name != "TO_BE_BUILT.md"
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise CapabilityAssemblyProductionConfigurationError(
            "repository-relative TO_BE_BUILT.md path is required"
        )
    return path.as_posix()


def _json_object(value: str, *, label: str) -> dict[str, object]:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        parsed = None
    if not isinstance(parsed, dict) or any(not isinstance(key, str) for key in parsed):
        raise CapabilityAssemblyProductionConfigurationError(
            f"capability assembly {label} is invalid"
        )
    return parsed


__all__ = [
    "AsyncMinibookProjectorAdapter",
    "CapabilityAssemblyCurrentFactoryAdapter",
    "CapabilityAssemblyProductionAuthorities",
    "CapabilityAssemblyProductionConfigurationError",
    "CapabilityAssemblyProductionSettings",
    "CapabilityProjectionGatewayPort",
    "GatewayMinibookProjectionEffectsAdapter",
    "GenericGatewayMinibookCapabilityProjectionAdapter",
    "RepositoryFactoryInputArtifactStore",
    "build_capability_assembly_controller",
    "compose_capability_assembly_controller",
]
