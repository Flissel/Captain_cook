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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol, cast
from uuid import UUID

from agenten.agent_factory.capability_live_runner import (
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
from gateway.capability_live_current_adapters import (
    CapabilityProjectionReadback,
    GatewayCapabilityBusinessBenchmarkAdapter,
    GatewayMinibookCapabilityProjectionAdapter,
    RecordedFactoryCapabilityExecutionAdapter,
)
from gateway.capability_live_factory_adapter import (
    CapabilityFactoryProvisioningPort,
    CurrentCapabilityFactoryAdapter,
)
from gateway.capability_live_pipeline import CapabilityLiveAuthorityUnavailable
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
        result = await asyncio.to_thread(self._projector.project, event)
        if not isinstance(result, ProjectionResult):
            raise CapabilityAssemblyProductionConfigurationError(
                "Minibook projector returned an invalid projection result"
            )
        return result

    async def acknowledgement(
        self,
        event: MinibookProjectionEvent,
    ) -> MinibookProjectionAcknowledgementV1:
        result = await asyncio.to_thread(self._projector.acknowledgement, event)
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
                (gateway, "acknowledge_projection", True, 1),
                (gateway, "projection_readback", True, 1),
                (minibook, "project", True, 1),
                (minibook, "acknowledgement", True, 1),
            )
        )
        self._gateway = gateway
        self._minibook = minibook

    async def project_and_ack(
        self,
        event: MinibookProjectionEvent,
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

    def job_id_for(self, request: CapabilityAssemblyStageRequest) -> UUID | None:
        return super().job_id_for(
            cast(CapabilityLiveStageRequest, _current_stage_request(request))
        )

    async def scaffold(self, request: CapabilityAssemblyStageRequest):
        return await super().scaffold(
            cast(CapabilityLiveStageRequest, _current_stage_request(request))
        )

    async def implement(self, request: CapabilityAssemblyStageRequest):
        return await super().implement(
            cast(CapabilityLiveStageRequest, _current_stage_request(request))
        )

    async def seal(self, request: CapabilityAssemblyStageRequest):
        return await super().seal(
            cast(CapabilityLiveStageRequest, _current_stage_request(request))
        )

    async def promote(self, request: CapabilityAssemblyStageRequest):
        if request.input.required_tool_gaps:
            raise CapabilityLiveAuthorityUnavailable(
                "required TODO_TOOL gaps block capability promotion"
            )
        return await super().promote(
            cast(CapabilityLiveStageRequest, _current_stage_request(request))
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
        clock=clock,
    )
    resume = GatewayCapabilityResumeExecutionAdapter(gateway=resume_gateway)
    factory: CapabilityAssemblyFactoryPort = CapabilityAssemblyCurrentFactoryAdapter(
        composition=factory_composition,
        provisioning=provisioning,
        clock=clock,
    )
    benchmark: CapabilityAssemblyBusinessBenchmarkPort = (
        GatewayCapabilityBusinessBenchmarkAdapter(
            composition=factory_composition,
            bindings=cast(object, factory),
        )
    )
    execution: CapabilityAssemblyExecutionPort = RecordedFactoryCapabilityExecutionAdapter(
        composition=factory_composition,
        resume_execution=resume,
    )
    projection: CapabilityAssemblyProjectionPort = GatewayMinibookCapabilityProjectionAdapter(
        composition=factory_composition,
        effects=minibook_effects,
    )
    return CapabilityAssemblyController(
        repository=repository,
        admission=admission,
        factory=factory,
        benchmark=benchmark,
        resume=resume,
        reconciliation=reconciliation,
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
        source_artifacts=authorities.source_artifacts,
        clock=selected_clock,
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
        clock=selected_clock,
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
    source_artifacts: object | None = None,
    clock: object,
) -> None:
    if not isinstance(factory_composition, AgentFactoryLiveComposition):
        raise CapabilityAssemblyProductionConfigurationError(
            "factory composition is not the current Gateway authority"
        )
    if not isinstance(minibook_effects, GatewayMinibookProjectionEffectsAdapter):
        raise CapabilityAssemblyProductionConfigurationError(
            "Minibook projection effects adapter is not concrete"
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
    n8n_required = any(
        agent.n8n_requirement == "required" for agent in request.input.document.agents
    )
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
        suite_ref=request.suite_ref,
        hermes_result_ref=request.hermes_result_ref,
        retry_number=request.retry_number,
    )


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
    "RepositoryFactoryInputArtifactStore",
    "build_capability_assembly_controller",
    "compose_capability_assembly_controller",
]
