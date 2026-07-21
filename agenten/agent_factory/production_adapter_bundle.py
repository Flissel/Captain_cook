"""Attested production composition for Package C and the paid Factory graph.

The module intentionally contains the two stable top-level symbols referenced by
the separated adapter manifests.  Local storage, Gateway authority, Minibook
projection and the digest-pinned sandbox are concrete.  Provider bridges which
cannot yet satisfy the repository's claim/evidence contracts fail with a
machine-readable ``TODO_TOOL`` marker before emitting evidence.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from agenten.agent_factory.capability_factory_entrypoint import (
    CapabilityFactoryEntrypoint,
    CapabilityFactoryRunSummary,
    DockerCapabilitySandboxRunner,
    FileCapabilityFactoryCheckpointStore,
)
from agenten.agent_factory.capability_factory_production import AdapterManifestKind
from agenten.agent_factory.capability_factory_production import (
    MinibookSwarmCreationHttpPort,
    RuntimeCaptainEvidenceHttpPort,
)
from agenten.agent_factory.capability_live_adapters import (
    ContentAddressedArtifactStore,
)
from agenten.agent_factory.factory_live_prepared_dispatch import (
    FactoryLivePreparedDispatch,
)
from agenten.agent_factory.factory_live_runner import (
    FactoryLiveEffectOutcomeV1,
    FactoryLiveEffectRequestV1,
)
from agenten.agent_factory.forge_contracts import ArtifactRef as ForgeArtifactRef
from agenten.agent_factory.forge_contracts import CreationJobV1, ReleasedSkillRefV1
from agenten.agent_factory.hermes_cli import (
    HermesCliFactory,
    HermesCliSettings,
    _parse_evidence_payload,
    _parse_paid_usage,
)
from agenten.agent_factory.holdout_store import InMemoryPrivateHoldoutStore
from agenten.agent_factory.input_document import load_factory_input
from agenten.agent_factory.claim_aware_capability_runtime import (
    ClaimAwareCapabilityRuntime,
    ContentAddressedCapabilityEffectStore,
)
from agenten.agent_factory.state_machine import FactoryAction
from agenten.agent_factory.skill_evaluation import (
    BoundedEvaluationCommand,
    HermesSkillUsageReceipt,
    ReleasedHermesSkill,
    ToolGapMarker,
    ToolImplementationOption,
)
from agenten.agent_factory.skill_store import reject_sensitive_data
import httpx
from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr, model_validator

from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeResult,
    ArtifactRef,
    CapabilityGrant,
    HermesPlanResult,
)
from agenten.agent_factory.contracts import AgentFactoryJobV2
from agenten.agent_runtime.runtime_entrypoint import (
    RuntimeEntrypointSettings,
    compose_gateway_backed_runtime_executor,
)
from agenten.delivery.minibook_client import MinibookClient
from agenten.delivery.projector import MinibookProjector
from agenten.delivery.projection_cursor import ProjectionCursorStore
from gateway.capability_catalog import GatewayCapabilityCatalog
from gateway.factory_live_runtime import FactoryLiveExternalRuntimeGraph
from gateway.factory_repository import GatewayFactoryRepository
from gateway.registry_feed import factory_promotion_projection, runtime_result_projection
from gateway.production_store import LazyGatewayStore
from gateway.store import GatewayStore


class ProductionToolRequired(RuntimeError):
    """A required external effect has no truthful production implementation."""


def _todo(tool_id: str) -> ProductionToolRequired:
    return ProductionToolRequired(f"TODO_TOOL:{tool_id}")


class ContentAddressedRuntimeArtifactPort:
    """Read-only runtime artifact port over the Factory content store layout."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    async def require(self, reference: ArtifactRef | Any) -> None:
        self.read_bytes(reference)

    def read_bytes(self, reference: ArtifactRef | Any) -> bytes:
        digest = str(reference.sha256)
        if (
            len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or str(reference.uri).rsplit("/", 1)[-1] != digest
        ):
            raise ValueError("runtime artifact reference is not content-addressed")
        target = self._root / "content" / "sha256" / digest[:2] / digest
        try:
            content = target.read_bytes()
        except OSError as exc:
            raise ValueError("runtime artifact content is unavailable") from exc
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("runtime artifact digest changed")
        return content


def _resolved_executable(environ: Mapping[str, str], name: str, default: str) -> str:
    raw = environ.get(name, default).strip()
    candidate = Path(raw)
    resolved = (
        str(candidate.resolve())
        if candidate.is_absolute() and candidate.is_file()
        else shutil.which(raw)
    )
    if not resolved:
        raise ProductionToolRequired(f"TODO_TOOL:runtime_executable:{name}")
    return resolved


async def _bounded_process(arguments: tuple[str, ...], *, timeout: int) -> bytes:
    process = await asyncio.create_subprocess_exec(
        *arguments,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError("runtime CLI exceeded its Captain wall-clock limit") from None
    if process.returncode != 0:
        raise RuntimeError("runtime CLI failed")
    if len(stdout) > 1_048_576:
        raise RuntimeError("runtime CLI returned too much evidence")
    return stdout


def _first_json_object(content: bytes) -> object:
    text = content.decode("utf-8")
    start = text.find("{")
    if start < 0:
        raise ValueError("runtime CLI returned no JSON object")
    value, _ = json.JSONDecoder().raw_decode(text[start:])
    return value


class StrictHermesCliPlanner:
    """Lazy Hermes CLI adapter accepting only the typed runtime plan contract."""

    def __init__(self, executable: str, artifacts: ContentAddressedRuntimeArtifactPort) -> None:
        self._executable = executable
        self._artifacts = artifacts

    async def plan(
        self, command: AgentRuntimeCommand, grant: CapabilityGrant
    ) -> HermesPlanResult:
        return await self._invoke(command, grant)

    async def design_agent(
        self, command: AgentRuntimeCommand, grant: CapabilityGrant
    ) -> HermesPlanResult:
        return await self._invoke(command, grant)

    async def _invoke(
        self, command: AgentRuntimeCommand, grant: CapabilityGrant
    ) -> HermesPlanResult:
        prompt = self._artifacts.read_bytes(command.payload.prompt_ref).decode("utf-8")
        envelope = {
            "instruction": "Return exactly one captain.hermes-plan-result.v1 JSON object.",
            "command": command.model_dump(mode="json", by_alias=True),
            "grant": grant.model_dump(mode="json", by_alias=True),
            "prompt": prompt,
        }
        stdout = await _bounded_process(
            (
                self._executable,
                "-z",
                json.dumps(envelope, sort_keys=True, separators=(",", ":")),
            ),
            timeout=command.payload.limits.wall_seconds,
        )
        result = HermesPlanResult.model_validate(_first_json_object(stdout))
        if (
            result.correlation_id != command.correlation_id
            or result.subject_version != command.subject_version
            or result.project_id != command.payload.project_id
        ):
            raise ValueError("Hermes runtime result changed Captain authority")
        return result


class StrictCodexCliExecution:
    """Lazy Codex CLI adapter; session-control operations remain explicit gaps."""

    def __init__(self, executable: str, artifacts: ContentAddressedRuntimeArtifactPort) -> None:
        self._executable = executable
        self._artifacts = artifacts

    async def start(
        self, command: AgentRuntimeCommand, grant: CapabilityGrant
    ) -> AgentRuntimeResult:
        return await self._invoke(command, grant)

    async def resume(
        self, command: AgentRuntimeCommand, grant: CapabilityGrant
    ) -> AgentRuntimeResult:
        return await self._invoke(command, grant)

    async def status(self, command: AgentRuntimeCommand, grant: CapabilityGrant) -> AgentRuntimeResult:
        del command, grant
        raise _todo("codex_session_status_bridge")

    async def cancel(self, command: AgentRuntimeCommand, grant: CapabilityGrant) -> AgentRuntimeResult:
        del command, grant
        raise _todo("codex_session_cancel_bridge")

    async def heartbeat(self, command: AgentRuntimeCommand, grant: CapabilityGrant) -> AgentRuntimeResult:
        del command, grant
        raise _todo("codex_session_heartbeat_bridge")

    async def _invoke(
        self, command: AgentRuntimeCommand, grant: CapabilityGrant
    ) -> AgentRuntimeResult:
        prompt = self._artifacts.read_bytes(command.payload.prompt_ref).decode("utf-8")
        envelope = {
            "instruction": "Complete the authorized work and end with exactly one captain.agent-runtime-result.v1 JSON object.",
            "command": command.model_dump(mode="json", by_alias=True),
            "grant": grant.model_dump(mode="json", by_alias=True),
            "prompt": prompt,
        }
        stdout = await _bounded_process(
            (
                self._executable,
                "exec",
                "--json",
                json.dumps(envelope, sort_keys=True, separators=(",", ":")),
            ),
            timeout=command.payload.limits.wall_seconds,
        )
        candidates: list[object] = []
        for line in stdout.decode("utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("schema") == "captain.agent-runtime-result.v1":
                candidates.append(event)
            item = event.get("item") if isinstance(event, dict) else None
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str) and "captain.agent-runtime-result.v1" in text:
                    candidates.append(_first_json_object(text.encode("utf-8")))
        if len(candidates) != 1:
            raise ValueError("Codex runtime returned no unique typed result")
        result = AgentRuntimeResult.model_validate(candidates[0])
        if (
            result.command_id != command.event_id
            or result.correlation_id != command.correlation_id
            or result.grant_id != grant.grant_id
        ):
            raise ValueError("Codex runtime result changed Captain authority")
        return result


class _PreflightUnavailableCapabilityEvidenceBackend:
    """Fail-closed fallback used only when no production bridge was injected."""

    async def run(self, request: Any) -> None:
        del request
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="TODO_TOOL:capability_release_executor_bridge",
        )

    async def lifecycle_blocks(self, request: Any) -> Any:
        del request
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="TODO_TOOL:capability_release_executor_bridge",
        )


def build_runtime_app_from_environment(
    settings: RuntimeEntrypointSettings,
    environ: Mapping[str, str],
    *,
    evidence_backend: Any | None = None,
) -> Any:
    """Build 8091 with lazy CLI/Gateway ports and an injectable evidence bridge.

    ``capability_v3_evidence_bridge.build_capability_evidence_backend`` supplies
    the production backend once its authority-bound dependencies have been
    assembled.  Preflight may omit it; evidence routes then fail with an exact
    ``TODO_TOOL`` marker and can never manufacture provider evidence.
    """

    from agenten.agent_factory.capability_factory_production import (
        create_capability_factory_runtime_app,
    )

    artifact_root_value = environ.get("CAPTAIN_RUNTIME_ARTIFACT_ROOT", "").strip()
    if not artifact_root_value:
        raise _todo("configuration:CAPTAIN_RUNTIME_ARTIFACT_ROOT")
    artifacts = ContentAddressedRuntimeArtifactPort(Path(artifact_root_value))
    hermes = StrictHermesCliPlanner(
        _resolved_executable(environ, "HERMES_EXECUTABLE", "hermes"),
        artifacts,
    )
    codex = StrictCodexCliExecution(
        _resolved_executable(environ, "CODEX_EXECUTABLE", "codex"),
        artifacts,
    )
    client = httpx.AsyncClient(timeout=30.0)
    executor = compose_gateway_backed_runtime_executor(
        settings=settings,
        client=client,
        hermes=hermes,
        codex=codex,
        artifacts=artifacts,
    )
    selected_backend = (
        evidence_backend
        if evidence_backend is not None
        else _PreflightUnavailableCapabilityEvidenceBackend()
    )
    app = create_capability_factory_runtime_app(
        runtime_executor=executor,
        backend=selected_backend,
        token=SecretStr(settings.runtime_token.get_secret_value()),
    )
    app.state.gateway_http_client = client
    app.state.capability_evidence_backend = selected_backend
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def close_gateway_client(application: Any):
        try:
            async with original_lifespan(application):
                yield
        finally:
            await client.aclose()

    app.router.lifespan_context = close_gateway_client
    return app


def build_runtime_app_with_v3_evidence(
    settings: RuntimeEntrypointSettings,
    environ: Mapping[str, str],
    *,
    context: Any,
) -> Any:
    """Compose the authenticated runtime with the explicit V2-to-V3 bridge."""

    from agenten.agent_factory.capability_v3_evidence_bridge import (
        build_capability_evidence_backend,
    )

    backend = build_capability_evidence_backend(context=context)
    return build_runtime_app_from_environment(
        settings,
        environ,
        evidence_backend=backend,
    )


def build_production_runtime_app_from_environment(
    settings: RuntimeEntrypointSettings,
    environ: Mapping[str, str],
) -> Any:
    """Compose Runtime 8091 with every real V3 evidence port, still effect-lazy."""

    from agenten.agent_factory.production_candidate_ports import (
        build_production_candidate_ports,
    )
    from agenten.agent_factory.production_evidence_composition import (
        build_production_v3_evidence_backend_from_environment,
    )
    from agenten.agent_factory.production_external_ports import (
        build_production_v3_external_ports,
    )
    from agenten.agent_factory.production_n8n_adapter import (
        build_captain_factory_n8n_binding,
    )

    artifact_root = _required_from_mapping(environ, "CAPTAIN_RUNTIME_ARTIFACT_ROOT")
    sandbox_image = _required_from_mapping(
        environ, "CAPTAIN_CAPABILITY_SANDBOX_IMAGE"
    )
    gateway_url = _required_from_mapping(environ, "CAPTAIN_GATEWAY_URL").rstrip("/")
    gateway_token = _required_from_mapping(environ, "CAPTAIN_GATEWAY_TOKEN")
    artifacts = ContentAddressedArtifactStore(Path(artifact_root))
    candidate_ports = build_production_candidate_ports(
        artifacts=artifacts,
        sandbox_image=sandbox_image,
    )
    gateway_sync_http = httpx.Client(timeout=30.0)
    gateway_async_http = httpx.AsyncClient(timeout=30.0)
    n8n_async_http = httpx.AsyncClient(timeout=30.0)

    def n8n_bindings_for(job: Any, resolved: Any) -> tuple[Any, ...]:
        tools = tuple(resolved.candidate.n8n_tools)
        batch_id = _ensure_factory_n8n_batch(
            environ=environ,
            gateway_url=gateway_url,
            gateway_token=gateway_token,
            client=gateway_sync_http,
            job_id=job.job_id,
            tool_names=tuple(tool.name for tool in tools),
        )
        return tuple(
            build_captain_factory_n8n_binding(
                environ,
                tool=tool,
                batch_id=batch_id,
            )
            for tool in tools
        )

    external_ports = build_production_v3_external_ports(
        environ,
        candidate_provider=candidate_ports.candidate_provider,
        candidate_attestation=candidate_ports.candidate_attestation,
        artifacts=artifacts,
        n8n_bindings_for=n8n_bindings_for,
        gateway_sync_http=gateway_sync_http,
        gateway_async_http=gateway_async_http,
        n8n_async_http=n8n_async_http,
    )
    evidence_runtime = build_production_v3_evidence_backend_from_environment(
        environ,
        external_ports=external_ports,
    )
    app = build_runtime_app_from_environment(
        settings,
        environ,
        evidence_backend=evidence_runtime.backend,
    )
    app.state.production_v3_evidence_runtime = evidence_runtime
    app.state.production_v3_external_ports = external_ports
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def close_production_clients(application: Any):
        try:
            async with original_lifespan(application):
                yield
        finally:
            gateway_sync_http.close()
            await gateway_async_http.aclose()
            await n8n_async_http.aclose()

    app.router.lifespan_context = close_production_clients
    return app


def _ensure_factory_n8n_batch(
    *,
    environ: Mapping[str, str],
    gateway_url: str,
    gateway_token: str,
    client: httpx.Client,
    job_id: UUID,
    tool_names: tuple[str, ...],
) -> str:
    """Release the exact candidate tool set before its first scoped MCP lease."""

    batch_namespace = _required_from_mapping(environ, "CAPTAIN_N8N_BATCH_ID")
    if not tool_names or len(tool_names) != len(set(tool_names)):
        raise ProductionToolRequired("TODO_TOOL:factory_n8n_candidate_tools")
    batch_digest = hashlib.sha256(
        json.dumps(
            {
                "namespace": batch_namespace,
                "job_id": str(job_id),
                "tool_names": list(tool_names),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    batch_id = f"factory-n8n-{batch_digest[:20]}"
    batch = {
        "batch_id": batch_id,
        "title": "Captain Factory n8n evidence tools",
        "goal": "Execute only the sealed candidate n8n tools under a short-lived lease",
        "subtask_ids": list(tool_names),
        "target": "n8n",
        "runtime": "n8n-mcp",
        "runtime_version": "v1",
        "interface_schema": "captain.n8n-mcp-tool-reference.v1",
        "capability_tags": ["n8n-builder"],
        "constraints": [
            "integration_intent=n8n",
            "workflow identity is host pinned",
        ],
        "acceptance_criteria": [
            {
                "assertion_id": "n8n-evidence",
                "kind": "side_effect_observed",
                "description": "Provider execution and workflow digests match Captain authority",
            }
        ],
    }
    headers = {"Authorization": f"Bearer {gateway_token}"}
    response = client.post(
        f"{gateway_url}/blocks",
        headers=headers,
        json={
            "block_type": "work_batch",
            "status": "pending",
            "data": batch,
        },
    )
    if response.status_code == status.HTTP_201_CREATED:
        return batch_id
    if response.status_code == status.HTTP_409_CONFLICT:
        replay = client.get(
            f"{gateway_url}/batches/{batch_id}/bundle",
            headers=headers,
        )
        if replay.status_code == status.HTTP_200_OK and replay.json() == batch:
            return batch_id
    if response.status_code != status.HTTP_201_CREATED:
        raise ProductionToolRequired("TODO_TOOL:factory_n8n_work_batch_release")
    return batch_id


def _required_from_mapping(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise ProductionToolRequired(f"TODO_TOOL:configuration:{name}")
    return value


class GatewayCapabilityFactoryPort:
    """Package-C adapter preserving GatewayStore claim/fence authority."""

    def __init__(self, store: GatewayStore) -> None:
        self._store = store

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def record_runtime_result(self, result: Any, **claim: Any) -> object:
        return self._store.record_runtime_result(result, **claim)

    def projection_events(self, correlation_id: UUID) -> tuple[Any, ...]:
        cursor = -1
        projected: list[Any] = []
        while True:
            records, has_more = self._store.minibook_projection_feed(
                after_index=cursor,
                limit=100,
            )
            for index, block_type, data, parent in records:
                cursor = index
                event = (
                    factory_promotion_projection(data, parent)
                    if block_type == "agent_factory_block" and parent is not None
                    else runtime_result_projection(data)
                )
                if event is not None and event.correlation_id == correlation_id:
                    projected.append(event)
            if not has_more:
                return tuple(projected)


class _UtcClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class _HermesCreationReceiptDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    receipt_id: UUID
    request_id: UUID
    lease_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    occurred_at: datetime
    commands: tuple[BoundedEvaluationCommand, ...] = Field(min_length=1, max_length=5)
    assertion_ids: tuple[str, ...] = Field(min_length=1)
    outcome: Literal["passed", "blocked_tool_gap"]


class _HermesCreationGapDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_name: Literal["TODO_TOOL.v1"] = Field(alias="schema", serialization_alias="schema")
    gap_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    severity: Literal["required", "optional"]
    input_contract: dict[str, JsonValue] = Field(min_length=1)
    output_contract: dict[str, JsonValue] = Field(min_length=1)
    least_privilege_capability: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
    )
    implementation_options: tuple[ToolImplementationOption, ...] = Field(max_length=3)
    acceptance_assertion_ids: tuple[str, ...] = Field(min_length=1)
    evidence: dict[str, JsonValue] = Field(min_length=1)
    status: Literal["unresolved", "resolved"]


class _HermesCreationAnalysisPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_name: Literal["captain.hermes-creation-analysis.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    creation_job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    receipt: _HermesCreationReceiptDeclaration
    tool_gaps: tuple[_HermesCreationGapDeclaration, ...]

    @model_validator(mode="after")
    def require_consistent_outcome(self) -> "_HermesCreationAnalysisPayload":
        gap_ids = tuple(item.gap_id for item in self.tool_gaps)
        if len(gap_ids) != len(set(gap_ids)):
            raise ValueError("Hermes creation tool gap IDs must be unique")
        blocked = any(
            item.severity == "required" and item.status == "unresolved"
            for item in self.tool_gaps
        )
        expected = "blocked_tool_gap" if blocked else "passed"
        if self.receipt.outcome != expected:
            raise ValueError("Hermes creation outcome contradicts declared tool gaps")
        return self


@dataclass(frozen=True)
class HermesCreationEvidenceMaterialization:
    creation_job_id: UUID
    skill_usage_receipt_ref: ArtifactRef
    tool_gaps_ref: ArtifactRef
    envelope_ref: ArtifactRef


def _creation_json_bytes(value: BaseModel | Mapping[str, object]) -> bytes:
    payload = (
        value.model_dump(mode="json", by_alias=True)
        if isinstance(value, BaseModel)
        else value
    )
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class ProductionHermesCreationAnalysis:
    """One paid Hermes analysis, then immutable CAS-only replay."""

    def __init__(
        self,
        *,
        artifacts: ContentAddressedArtifactStore,
        hermes: HermesCliFactory,
        released_skill_path: Path,
        max_cost_per_call_usd: Decimal | str,
        timeout_seconds: int,
    ) -> None:
        self._artifacts = artifacts
        self._hermes = hermes
        self._released_skill_path = released_skill_path.resolve()
        try:
            maximum = Decimal(str(max_cost_per_call_usd))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("Hermes creation cost cap is invalid") from exc
        if not maximum.is_finite() or maximum <= 0:
            raise ValueError("Hermes creation cost cap is invalid")
        if timeout_seconds < 1:
            raise ValueError("Hermes creation timeout is invalid")
        self._maximum = maximum
        self._timeout_seconds = timeout_seconds

    async def analyze(
        self,
        job: AgentFactoryJobV2,
        creation_job: CreationJobV1,
    ) -> HermesCreationEvidenceMaterialization:
        replayed = self._replay(creation_job)
        if replayed is not None:
            return replayed
        if not self._released_skill_path.is_dir():
            raise ProductionToolRequired("TODO_TOOL:released_capability_factory_skill")
        prompt = self._prompt(job, creation_job)
        with tempfile.TemporaryDirectory(prefix="captain-hermes-creation-") as temporary:
            usage_path = Path(temporary) / "usage.json"
            stdout = await self._hermes._run_skill_prompt(
                prompt,
                max_seconds=float(self._timeout_seconds),
                usage_file=usage_path,
            )
            try:
                usage_bytes = usage_path.read_bytes()
            except OSError as exc:
                raise ValueError("Hermes creation paid usage receipt is unavailable") from exc
        usage = _parse_paid_usage(usage_bytes)
        if usage.estimated_cost_usd > self._maximum:
            raise ValueError("Hermes creation provider cost exceeds per-call cap")
        payload = _HermesCreationAnalysisPayload.model_validate(
            _parse_evidence_payload(stdout)
        )
        self._require_binding(job, creation_job, payload)
        reject_sensitive_data(
            payload.model_dump(mode="json", by_alias=True),
            "Hermes creation analysis",
        )
        return self._materialize(job, creation_job, payload, usage_bytes)

    def _prompt(self, job: AgentFactoryJobV2, creation_job: CreationJobV1) -> str:
        request = {
            "instruction": (
                "Use the released skill at released_skill_path. Analyze the exact creation "
                "job before Minibook starts. Return exactly one JSON object matching "
                "response_schema. Declare every missing tool as TODO_TOOL.v1; use [] only "
                "when your actual analysis finds no gaps. Do not include credentials."
            ),
            "released_skill_path": str(self._released_skill_path),
            "released_skill": creation_job.released_skill.model_dump(mode="json", by_alias=True),
            "creation_job": creation_job.model_dump(mode="json", by_alias=True),
            "factory_job_id": str(job.job_id),
            "response_schema": _HermesCreationAnalysisPayload.model_json_schema(by_alias=True),
        }
        return json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _require_binding(
        job: AgentFactoryJobV2,
        creation_job: CreationJobV1,
        payload: _HermesCreationAnalysisPayload,
    ) -> None:
        if (
            payload.creation_job_id != creation_job.creation_job_id
            or payload.correlation_id != job.correlation_id
            or payload.subject_version != job.subject_version
            or payload.receipt.assertion_ids != job.acceptance_assertion_ids
            or payload.receipt.occurred_at < job.occurred_at
            or payload.receipt.occurred_at >= job.deadline_at
            or payload.receipt.commands
            != (
                BoundedEvaluationCommand(
                    command_id="hermes.creation-analysis",
                    max_seconds=payload.receipt.commands[0].max_seconds,
                ),
            )
        ):
            raise ValueError("Hermes creation analysis changed Captain authority")
        allowed = set(job.acceptance_assertion_ids)
        if any(
            not set(gap.acceptance_assertion_ids).issubset(allowed)
            for gap in payload.tool_gaps
        ):
            raise ValueError("Hermes creation tool gap exceeds Captain assertions")

    def _materialize(
        self,
        job: AgentFactoryJobV2,
        creation_job: CreationJobV1,
        payload: _HermesCreationAnalysisPayload,
        usage_bytes: bytes,
    ) -> HermesCreationEvidenceMaterialization:
        paid_ref = self._artifacts.put(
            usage_bytes, "application/json", namespace="hermes-paid-usage"
        )
        gaps: list[ToolGapMarker] = []
        for gap in payload.tool_gaps:
            input_ref = self._artifacts.put(
                _creation_json_bytes(gap.input_contract),
                "application/json",
                namespace="hermes-gap-input",
            )
            output_ref = self._artifacts.put(
                _creation_json_bytes(gap.output_contract),
                "application/json",
                namespace="hermes-gap-output",
            )
            evidence_ref = self._artifacts.put(
                _creation_json_bytes(gap.evidence),
                "application/json",
                namespace="hermes-gap-evidence",
            )
            gaps.append(
                ToolGapMarker(
                    schema="TODO_TOOL.v1",
                    gap_id=gap.gap_id,
                    severity=gap.severity,
                    input_contract_ref=input_ref,
                    output_contract_ref=output_ref,
                    least_privilege_capability=gap.least_privilege_capability,
                    implementation_options=gap.implementation_options,
                    acceptance_assertion_ids=gap.acceptance_assertion_ids,
                    evidence_ref=evidence_ref,
                    status=gap.status,
                )
            )
        gap_envelope = {
            "schema": "minibook.creation-tool-gaps.v1",
            "tool_gaps": [item.model_dump(mode="json", by_alias=True) for item in gaps],
        }
        gaps_ref = self._artifacts.put(
            _creation_json_bytes(gap_envelope),
            "application/json",
            namespace="hermes-tool-gaps",
        )
        released = ReleasedHermesSkill(
            schema="captain.released-hermes-skill.v1",
            skill_id=creation_job.released_skill.skill_id,
            version=creation_job.released_skill.version,
            capability="autogen.agent-factory",
            content_ref=ArtifactRef.model_validate(
                creation_job.released_skill.content_ref.model_dump(mode="json")
            ),
            content_sha256=creation_job.released_skill.content_sha256,
            status="released",
            released_at=job.occurred_at,
            producer="captain",
        )
        evidence_refs = tuple(dict.fromkeys((paid_ref, *(gap.evidence_ref for gap in gaps))))
        receipt = HermesSkillUsageReceipt(
            schema="hermes.skill-usage-receipt.v1",
            receipt_id=payload.receipt.receipt_id,
            request_id=payload.receipt.request_id,
            job_id=job.job_id,
            correlation_id=job.correlation_id,
            lease_id=payload.receipt.lease_id,
            occurred_at=payload.receipt.occurred_at,
            producer="hermes",
            released_skill=released,
            used_skill_id=released.skill_id,
            used_skill_version=released.version,
            used_skill_sha256=released.content_sha256,
            commands=payload.receipt.commands,
            evidence_refs=evidence_refs,
            assertion_ids=payload.receipt.assertion_ids,
            outcome=payload.receipt.outcome,
        )
        receipt_ref = self._artifacts.put(
            _creation_json_bytes(receipt),
            "application/json",
            namespace="hermes-receipt",
        )
        envelope = {
            "schema": "captain.hermes-creation-evidence-binding.v1",
            "creation_job_id": str(creation_job.creation_job_id),
            "skill_usage_receipt_ref": receipt_ref.model_dump(mode="json"),
            "tool_gaps_ref": gaps_ref.model_dump(mode="json"),
        }
        envelope_ref = self._artifacts.put(
            _creation_json_bytes(envelope),
            "application/json",
            namespace="hermes-creation-evidence",
        )
        self._artifacts.bind(
            "hermes-creation-evidence",
            str(creation_job.creation_job_id),
            envelope_ref,
        )
        return HermesCreationEvidenceMaterialization(
            creation_job_id=creation_job.creation_job_id,
            skill_usage_receipt_ref=receipt_ref,
            tool_gaps_ref=gaps_ref,
            envelope_ref=envelope_ref,
        )

    def _replay(
        self, creation_job: CreationJobV1
    ) -> HermesCreationEvidenceMaterialization | None:
        envelope_ref = self._artifacts.binding(
            "hermes-creation-evidence", str(creation_job.creation_job_id)
        )
        if envelope_ref is None:
            return None
        try:
            envelope = json.loads(self._artifacts.read_bytes(envelope_ref))
            if (
                not isinstance(envelope, dict)
                or set(envelope)
                != {
                    "schema",
                    "creation_job_id",
                    "skill_usage_receipt_ref",
                    "tool_gaps_ref",
                }
                or envelope["schema"]
                != "captain.hermes-creation-evidence-binding.v1"
                or envelope["creation_job_id"] != str(creation_job.creation_job_id)
            ):
                raise ValueError
            receipt_ref = ArtifactRef.model_validate(envelope["skill_usage_receipt_ref"])
            gaps_ref = ArtifactRef.model_validate(envelope["tool_gaps_ref"])
            HermesSkillUsageReceipt.model_validate_json(
                self._artifacts.read_bytes(receipt_ref)
            )
            gap_payload = json.loads(self._artifacts.read_bytes(gaps_ref))
            if (
                not isinstance(gap_payload, dict)
                or set(gap_payload) != {"schema", "tool_gaps"}
                or gap_payload["schema"] != "minibook.creation-tool-gaps.v1"
                or not isinstance(gap_payload["tool_gaps"], list)
            ):
                raise ValueError
            for item in gap_payload["tool_gaps"]:
                marker = ToolGapMarker.model_validate(item)
                for reference in (
                    marker.input_contract_ref,
                    marker.output_contract_ref,
                    marker.evidence_ref,
                ):
                    self._artifacts.read_bytes(reference)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("Hermes creation evidence replay is invalid") from exc
        return HermesCreationEvidenceMaterialization(
            creation_job_id=creation_job.creation_job_id,
            skill_usage_receipt_ref=receipt_ref,
            tool_gaps_ref=gaps_ref,
            envelope_ref=envelope_ref,
        )


class ProductionCapabilityFactoryEntrypoint(CapabilityFactoryEntrypoint):
    """Package-C entrypoint that materializes its canonical input in shared CAS."""

    def __init__(
        self,
        *,
        production_artifacts: ContentAddressedArtifactStore,
        **dependencies: Any,
    ) -> None:
        super().__init__(**dependencies)
        self._production_artifacts = production_artifacts

    def seed_input(self, input_path: Path) -> ArtifactRef:
        """Persist exact canonical bytes before any creation boundary is called."""

        document = load_factory_input(input_path)
        source = input_path.read_bytes()
        stored = self._production_artifacts.put(
            source,
            "text/markdown",
            namespace="factory-input",
        )
        if (
            stored.sha256 != document.input_ref.sha256
            or stored.uri.rsplit("/", 1)[-1] != document.input_ref.sha256
        ):
            raise ValueError("canonical Factory input CAS binding changed")
        self._production_artifacts.read_bytes(stored)
        return stored

    def _build_creation_job(
        self,
        job: Any,
        *,
        compiled: Any,
        creation_key: str,
        released_skill: ReleasedSkillRefV1,
    ) -> Any:
        input_ref = self._production_artifacts.put(
            self._production_artifacts.read_sha256(job.input_ref.sha256),
            job.input_ref.media_type,
            namespace="factory-input",
        )
        compiled_content = json.dumps(
            compiled.model_dump(mode="json", by_alias=True),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        compiled_ref = self._production_artifacts.put(
            compiled_content,
            "application/json",
            namespace="compiled-factory-spec",
        )
        graph_payload = {
            "schema": "captain.factory-work-graph.v1",
            "source_sha256": compiled.source_ref.sha256,
            "nodes": [node.model_dump(mode="json") for node in compiled.work_nodes],
            "dependency_order": compiled.dependency_order,
        }
        graph_content = json.dumps(
            graph_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        dependency_graph_ref = self._production_artifacts.put(
            graph_content,
            "application/json",
            namespace="factory-work-graph",
        )
        if dependency_graph_ref.sha256 != job.dependency_graph_ref.sha256:
            raise ValueError("factory work graph CAS binding changed")
        canonical_job = job.model_copy(
            update={
                "input_ref": input_ref,
                "compiled_spec_ref": compiled_ref,
                "dependency_graph_ref": dependency_graph_ref,
            }
        )
        return super()._build_creation_job(
            canonical_job,
            compiled=compiled,
            creation_key=creation_key,
            released_skill=released_skill,
        )

    async def run(
        self,
        *,
        input_path: Path,
        correlation_id: UUID,
        subject_version: int,
        wall_clock_budget_seconds: int,
    ) -> CapabilityFactoryRunSummary:
        self.seed_input(input_path)
        return await super().run(
            input_path=input_path,
            correlation_id=correlation_id,
            subject_version=subject_version,
            wall_clock_budget_seconds=wall_clock_budget_seconds,
        )


class _TodoPreparedDispatch:
    def prepare(self, **_: Any) -> FactoryLivePreparedDispatch:
        raise _todo("factory_live_prepared_dispatch_bridge")

    async def execute(
        self, request: FactoryLiveEffectRequestV1
    ) -> FactoryLiveEffectOutcomeV1:
        del request
        raise _todo("factory_live_prepared_dispatch_bridge")

    async def recover(
        self, request: FactoryLiveEffectRequestV1
    ) -> FactoryLiveEffectOutcomeV1 | None:
        del request
        raise _todo("factory_live_prepared_dispatch_bridge")


class _TodoMaterializer:
    def validate_next(
        self,
        job: Any,
        action: FactoryAction,
        expected_skill_digests: Mapping[str, str],
    ) -> FactoryAction:
        del job, expected_skill_digests
        return action

    async def dispatch_next(self, job_id: UUID) -> FactoryAction:
        del job_id
        raise _todo("factory_live_materializer_bridge")


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ProductionToolRequired(f"TODO_TOOL:configuration:{name}")
    return value


def _released_skill(
    root: Path,
    artifacts: ContentAddressedArtifactStore,
) -> ReleasedSkillRefV1:
    skill_root = root / "agenten" / "agent_factory" / "skills" / "captain-agent-factory-loop"
    if not skill_root.is_dir():
        raise _todo("released_capability_factory_skill")
    entries: list[tuple[str, str]] = []
    for path in sorted(item for item in skill_root.rglob("*") if item.is_file()):
        entries.append(
            (
                path.relative_to(skill_root).as_posix(),
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        )
    content = repr(entries).encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    stored = artifacts.put(
        content,
        "application/octet-stream",
        namespace="released-skill",
    )
    if stored.sha256 != digest:
        raise ValueError("released skill CAS binding changed")
    return ReleasedSkillRefV1(
        skill_id="captain-agent-factory-loop",
        version=1,
        content_ref=ForgeArtifactRef.model_validate(stored.model_dump(mode="json")),
        content_sha256=digest,
    )


def build_capability_factory_entrypoint(config: Any) -> CapabilityFactoryEntrypoint:
    """Build the local production graph without starting an external effect."""

    root = Path(config.workspace_root).resolve()
    dsn = _required_environment("TEST_MARIADB_DSN")
    if not dsn.rstrip("/").endswith("/captain_test"):
        raise ProductionToolRequired("TODO_TOOL:configuration:TEST_MARIADB_DSN")
    artifact_root = Path(config.artifact_dir).resolve()
    shared_root_value = _required_environment("CAPTAIN_RUNTIME_ARTIFACT_ROOT")
    shared_root_candidate = Path(shared_root_value)
    shared_root = (
        shared_root_candidate
        if shared_root_candidate.is_absolute()
        else root / shared_root_candidate
    ).resolve()
    if shared_root != artifact_root:
        raise _todo("configuration:shared_capability_artifact_root")
    content = ContentAddressedArtifactStore(artifact_root)
    released_skill_path = (
        root
        / "agenten"
        / "agent_factory"
        / "skills"
        / "captain-agent-factory-loop"
    ).resolve()
    hermes_timeout_raw = os.environ.get("CAPTAIN_FACTORY_RUNTIME_SECONDS", "600").strip()
    try:
        hermes_timeout = int(hermes_timeout_raw)
    except ValueError as exc:
        raise _todo("configuration:CAPTAIN_FACTORY_RUNTIME_SECONDS") from exc
    if hermes_timeout < 1:
        raise _todo("configuration:CAPTAIN_FACTORY_RUNTIME_SECONDS")
    hermes_creation_analysis = ProductionHermesCreationAnalysis(
        artifacts=content,
        hermes=HermesCliFactory(
            HermesCliSettings(
                executable=_resolved_executable(
                    os.environ, "HERMES_EXECUTABLE", "hermes"
                ),
                model=os.environ.get("CAPTAIN_FACTORY_MODEL", "").strip() or None,
                provider=os.environ.get("CAPTAIN_FACTORY_PROVIDER", "").strip() or None,
                timeout_seconds=hermes_timeout,
            )
        ),
        released_skill_path=released_skill_path,
        max_cost_per_call_usd=_required_environment(
            "CAPTAIN_FACTORY_MAX_COST_PER_CALL_USD"
        ),
        timeout_seconds=hermes_timeout,
    )
    store = LazyGatewayStore(dsn)
    gateway = GatewayCapabilityFactoryPort(store)
    minibook_api_key = _required_environment("MINIBOOK_API_KEY")
    capability_http = httpx.AsyncClient(timeout=30.0)
    runtime_artifacts = ContentAddressedRuntimeArtifactPort(artifact_root)
    codex = StrictCodexCliExecution(
        _resolved_executable(os.environ, "CODEX_EXECUTABLE", "codex"),
        runtime_artifacts,
    )
    minibook = MinibookClient(
        config.minibook_url,
        minibook_api_key,
        projection_api_key=config.minibook_projection_api_key.get_secret_value(),
    )
    return ProductionCapabilityFactoryEntrypoint(
        production_artifacts=content,
        creation_analysis=hermes_creation_analysis,
        checkpoint_store=FileCapabilityFactoryCheckpointStore(config.checkpoint_dir),
        holdout_store=InMemoryPrivateHoldoutStore(),
        repository=GatewayFactoryRepository(store),
        catalog=GatewayCapabilityCatalog(store),
        released_skill=_released_skill(root, content),
        creation=MinibookSwarmCreationHttpPort(
            config.minibook_url,
            SecretStr(minibook_api_key),
            capability_http,
        ),
        content_store=content,
        sandbox_runner=DockerCapabilitySandboxRunner(image=config.sandbox_image),
        evidence_issuer=RuntimeCaptainEvidenceHttpPort(
            config.runtime_url,
            SecretStr(config.runtime_token.get_secret_value()),
            capability_http,
        ),
        gateway=gateway,
        runtime=ClaimAwareCapabilityRuntime(
            executor=codex,
            artifacts=content,
            effects=ContentAddressedCapabilityEffectStore(artifact_root),
            clock=lambda: datetime.now(timezone.utc),
        ),
        projector=MinibookProjector(
            minibook,
            ProjectionCursorStore(artifact_root / "projection-cursor.sqlite3"),
            owner_id="capability-factory-production",
        ),
        clock=_UtcClock(),
    )


def build_factory_live_runtime(context: Any) -> FactoryLiveExternalRuntimeGraph:
    """Delegate to the real attestable paid Factory effect graph."""

    from agenten.agent_factory.factory_live_paid_ports import (
        build_factory_live_runtime as build_paid_runtime,
    )

    return build_paid_runtime(context)


def production_manifest_commands(workspace_root: Path) -> dict[AdapterManifestKind, str]:
    root = workspace_root.resolve()
    script = root / "scripts" / "generate-capability-adapter-manifest.py"
    module = root / "agenten" / "agent_factory" / "production_adapter_bundle.py"
    paid_module = root / "agenten" / "agent_factory" / "factory_live_paid_ports.py"
    output = root / ".captain-cook" / "adapters"
    prefix = f'python "{script}" --workspace-root "{root}" --module "{module}"'
    return {
        AdapterManifestKind.ENTRYPOINT: (
            f'{prefix} --factory-symbol build_capability_factory_entrypoint '
            f'--target "{output / "capability-entrypoint.manifest.json"}" --kind entrypoint'
        ),
        AdapterManifestKind.FACTORY_LIVE_RUNTIME: (
            f'python "{script}" --workspace-root "{root}" --module "{paid_module}" '
            f'--factory-symbol build_factory_live_runtime '
            f'--target "{output / "factory-live-runtime.manifest.json"}" '
            "--kind factory_live_runtime"
        ),
    }
