"""Restart-safe composition of Captain planning, swarm execution, and evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agenten.agent_runtime.contracts import (
    AgentRuntimeResult,
    ArtifactRef,
    CapabilityProfile,
    HermesPlanResult,
    IntegrationIntent,
    RuntimeStatus,
)
from agenten.agent_runtime.swarm import RuntimeTaskProjection, SwarmOrchestrator
from agenten.agent_runtime.tools import AuthoritativeRuntimeState, RuntimeToolContext
from agenten.planning.captain_pipeline import CaptainCompiledPlan, PlanCompilationResult
from agenten.validation.contracts import WorkBatch


_IDENTIFIER = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
_SHA256 = r"^[0-9a-f]{64}$"
_PRIVATE_VALUE = re.compile(
    r"(?i)(?:api[_ -]?key|authorization|bearer|credential|password|secret|token)\s*[:=]"
)
_TOKEN_LIKE_VALUE = re.compile(
    r"(?i)(?:\bsk-(?:proj-)?[a-z0-9_-]{16,}|\bgh[pousr]_[a-z0-9]{16,}|"
    r"\bbearer\s+[a-z0-9._~+/-]{12,})"
)
_ABSOLUTE_PATH = re.compile(r"(?i)(?:^[a-z]:\\|^/(?:home|Users)/)")


class ValidationDisposition(str, Enum):
    PASSED = "passed"
    REDO = "redo"
    REPLAN = "replan"


class ValidationRecord(BaseModel):
    """Captain-owned public validation outcome; holdout bodies never cross here."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(pattern=_IDENTIFIER)
    disposition: ValidationDisposition
    artifact_ref: ArtifactRef
    assertion_ids: tuple[str, ...] = Field(min_length=1)
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("assertion_ids")
    @classmethod
    def require_unique_assertions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("validation assertion IDs must be unique")
        return value


class EvidenceObservation(BaseModel):
    """One redacted observation in the correlation-scoped evidence manifest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    observation_id: UUID
    boundary: Literal["hermes", "captain", "swarm", "codex", "n8n", "validation"]
    subject_id: str = Field(pattern=_IDENTIFIER)
    subject_version: int = Field(ge=1, strict=True)
    status: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    batch_id: str | None = Field(default=None, pattern=_IDENTIFIER)
    operation: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    grant_id: str | None = Field(default=None, pattern=_IDENTIFIER)
    capability_profile: CapabilityProfile | None = None
    mcp_servers: tuple[str, ...] = ()
    session_id: str | None = Field(default=None, pattern=_IDENTIFIER)
    artifact_refs: tuple[ArtifactRef, ...] = ()
    evidence_refs: tuple[ArtifactRef, ...] = ()
    validation_disposition: ValidationDisposition | None = None

    @model_validator(mode="after")
    def validate_capability_projection(self) -> "EvidenceObservation":
        if len(self.mcp_servers) != len(set(self.mcp_servers)):
            raise ValueError("manifest MCP servers must be unique")
        if self.capability_profile is CapabilityProfile.N8N_BUILDER:
            if self.mcp_servers != ("n8n-mcp",):
                raise ValueError("n8n evidence requires the scoped n8n-mcp server")
        elif self.mcp_servers:
            raise ValueError("non-n8n evidence cannot report MCP servers")
        return self


class ControlPlaneEvidenceManifest(BaseModel):
    """Single immutable, redacted evidence view indexed by correlation ID."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_name: Literal["captain.control-plane-evidence.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    correlation_id: UUID
    project_id: str = Field(pattern=_IDENTIFIER)
    plan_version: int = Field(ge=1, strict=True)
    plan_digest: str = Field(pattern=_SHA256)
    generated_at: datetime
    status: Literal["succeeded", "replanning", "failed"]
    minibook_project_id: str = Field(pattern=_IDENTIFIER)
    minibook_post_id: str = Field(pattern=_IDENTIFIER)
    batch_order: tuple[str, ...] = Field(min_length=1)
    completed_tasks: tuple[str, ...] = ()
    behavioral_redos: int = Field(ge=0, strict=True)
    infrastructure_failures: int = Field(ge=0, strict=True)
    observations: tuple[EvidenceObservation, ...]

    @model_validator(mode="before")
    @classmethod
    def reject_private_values(cls, value: Any) -> Any:
        _reject_private(value)
        return value

    @field_validator("generated_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_manifest(self) -> "ControlPlaneEvidenceManifest":
        if len(self.batch_order) != len(set(self.batch_order)):
            raise ValueError("manifest batch order must be unique")
        if len(self.completed_tasks) != len(set(self.completed_tasks)):
            raise ValueError("completed task IDs must be unique")
        observation_ids = [item.observation_id for item in self.observations]
        if len(observation_ids) != len(set(observation_ids)):
            raise ValueError("manifest observation IDs must be unique")
        return self


class ControlPlaneRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    hermes_result: HermesPlanResult
    workspace_refs: dict[str, str]
    prompt_refs: dict[str, ArtifactRef]
    wall_seconds: int = Field(ge=1, le=3600, strict=True)
    max_iterations: int = Field(ge=1, le=10, strict=True)
    max_infrastructure_failures: int = Field(default=3, ge=1, le=10, strict=True)

    @field_validator("workspace_refs")
    @classmethod
    def require_opaque_workspaces(cls, value: dict[str, str]) -> dict[str, str]:
        if not value or any(not item.startswith("workspace://") for item in value.values()):
            raise ValueError("control-plane workspaces must use workspace:// references")
        return value


class ControlPlaneCheckpoint(BaseModel):
    """Durable orchestration state; contains no prompts, tokens, or holdout bodies."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    correlation_id: UUID
    project_id: str = Field(pattern=_IDENTIFIER)
    plan_version: int = Field(ge=1, strict=True)
    plan_digest: str = Field(pattern=_SHA256)
    compiled_batch_digest: str = Field(pattern=_SHA256)
    created_at: datetime
    status: Literal["running", "succeeded", "replanning", "failed"]
    released: bool
    batch_order: tuple[str, ...]
    task_order: tuple[str, ...]
    task_states: dict[str, AuthoritativeRuntimeState]
    prompt_refs: dict[str, ArtifactRef]
    behavioral_iterations: dict[str, int]
    result_history: tuple[AgentRuntimeResult, ...] = ()
    validations: tuple[ValidationRecord, ...] = ()

    @field_validator("created_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_task_maps(self) -> "ControlPlaneCheckpoint":
        expected = set(self.task_order)
        for name, values in (
            ("task states", self.task_states),
            ("prompt refs", self.prompt_refs),
            ("behavioral iterations", self.behavioral_iterations),
        ):
            if set(values) != expected:
                raise ValueError(f"checkpoint {name} must match task order")
        if any(value < 0 for value in self.behavioral_iterations.values()):
            raise ValueError("behavioral iterations cannot be negative")
        return self


class ControlPlaneRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["succeeded", "replanning", "failed"]
    manifest: ControlPlaneEvidenceManifest


class CaptainCompilerPort(Protocol):
    async def compile_from_hermes_result(
        self, result: HermesPlanResult
    ) -> PlanCompilationResult: ...

    async def release(self, compiled: CaptainCompiledPlan) -> None: ...


class ResultValidatorPort(Protocol):
    async def validate(
        self, batch: WorkBatch, result: AgentRuntimeResult
    ) -> ValidationRecord: ...


class ControlPlaneRunStore(Protocol):
    async def load(self, correlation_id: UUID) -> ControlPlaneCheckpoint | None: ...

    async def save(self, checkpoint: ControlPlaneCheckpoint) -> None: ...


class ControlPlaneClock(Protocol):
    def now(self) -> datetime: ...


class InMemoryControlPlaneRunStore:
    def __init__(self) -> None:
        self._values: dict[UUID, ControlPlaneCheckpoint] = {}

    async def load(self, correlation_id: UUID) -> ControlPlaneCheckpoint | None:
        return self._values.get(correlation_id)

    async def save(self, checkpoint: ControlPlaneCheckpoint) -> None:
        self._values[checkpoint.correlation_id] = checkpoint


class JsonControlPlaneRunStore:
    """Atomic local checkpoint store for the offline demo and embedded runtime."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root).resolve()

    async def load(self, correlation_id: UUID) -> ControlPlaneCheckpoint | None:
        path = self._path(correlation_id)
        if not path.exists():
            return None
        try:
            return ControlPlaneCheckpoint.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            raise RuntimeError("control-plane checkpoint is invalid") from None

    async def save(self, checkpoint: ControlPlaneCheckpoint) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._path(checkpoint.correlation_id)
        temporary = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(checkpoint.model_dump_json(indent=2))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _path(self, correlation_id: UUID) -> Path:
        return self._root / f"{correlation_id}.json"


class AgentRuntimeControlPlane:
    """Compose reviewed ports while Captain remains the lifecycle authority."""

    def __init__(
        self,
        *,
        captain: CaptainCompilerPort,
        swarm: SwarmOrchestrator,
        validator: ResultValidatorPort,
        store: ControlPlaneRunStore,
        clock: ControlPlaneClock,
    ) -> None:
        self._captain = captain
        self._swarm = swarm
        self._validator = validator
        self._store = store
        self._clock = clock

    async def execute(self, request: ControlPlaneRunRequest) -> ControlPlaneRunResult:
        compilation = await self._captain.compile_from_hermes_result(
            request.hermes_result
        )
        batches = compilation.compiled.batches
        task_to_batch = {
            task_id: batch for batch in batches for task_id in batch.subtask_ids
        }
        task_order = tuple(task_to_batch)
        self._validate_request_bindings(request, task_order)
        checkpoint = await self._store.load(request.hermes_result.correlation_id)
        if checkpoint is None:
            checkpoint = ControlPlaneCheckpoint(
                correlation_id=request.hermes_result.correlation_id,
                project_id=request.hermes_result.project_id,
                plan_version=compilation.source_plan_version,
                plan_digest=compilation.source_plan_digest,
                compiled_batch_digest=_compiled_batch_digest(compilation.compiled),
                created_at=self._clock.now(),
                status="running",
                released=False,
                batch_order=tuple(batch.batch_id for batch in batches),
                task_order=task_order,
                task_states={
                    task_id: AuthoritativeRuntimeState.SUBTASK_READY
                    for task_id in task_order
                },
                prompt_refs=dict(request.prompt_refs),
                behavioral_iterations={task_id: 0 for task_id in task_order},
            )
            await self._store.save(checkpoint)
        else:
            self._validate_checkpoint(checkpoint, compilation, task_order)

        if not checkpoint.released:
            await self._captain.release(compilation.compiled)
            checkpoint = checkpoint.model_copy(update={"released": True})
            await self._store.save(checkpoint)
        if checkpoint.status != "running":
            return self._result(checkpoint, request.hermes_result, batches)

        dependencies = self._task_dependencies(batches)
        while checkpoint.status == "running":
            if all(
                checkpoint.task_states[task_id] is AuthoritativeRuntimeState.PASSED
                for task_id in checkpoint.task_order
            ):
                checkpoint = checkpoint.model_copy(update={"status": "succeeded"})
                await self._store.save(checkpoint)
                break

            projected = tuple(
                RuntimeTaskProjection(
                    task_id=task_id,
                    plan_version=checkpoint.plan_version,
                    lane="codex",
                    depends_on=dependencies[task_id],
                    context=RuntimeToolContext(
                        state=checkpoint.task_states[task_id],
                        project_id=checkpoint.project_id,
                        correlation_id=checkpoint.correlation_id,
                        subject_id=task_id,
                        subject_version=checkpoint.plan_version,
                        batch_id=task_to_batch[task_id].batch_id,
                        subtask_id=task_id,
                        workspace_ref=request.workspace_refs[task_id],
                        prompt_ref=checkpoint.prompt_refs[task_id],
                        integration_intent=(
                            IntegrationIntent.N8N
                            if "n8n-builder"
                            in task_to_batch[task_id].capability_tags
                            else IntegrationIntent.NONE
                        ),
                        wall_seconds=request.wall_seconds,
                        max_iterations=request.max_iterations,
                    ),
                )
                for task_id in checkpoint.task_order
            )
            actions = await self._swarm.run_once(
                projected,
                authoritative_plan_version=checkpoint.plan_version,
            )
            if not actions:
                raise RuntimeError("control-plane task DAG has no ready action")
            for action in actions:
                checkpoint = await self._apply_action(
                    checkpoint,
                    action.result,
                    task_to_batch[action.task_id],
                    request.max_iterations,
                    request.max_infrastructure_failures,
                )
                await self._store.save(checkpoint)
                if checkpoint.status != "running":
                    break
        return self._result(checkpoint, request.hermes_result, batches)

    async def _apply_action(
        self,
        checkpoint: ControlPlaneCheckpoint,
        result: AgentRuntimeResult,
        batch: WorkBatch,
        max_iterations: int,
        max_infrastructure_failures: int,
    ) -> ControlPlaneCheckpoint:
        task_id = result.subject_id
        history = (*checkpoint.result_history, result)
        states = dict(checkpoint.task_states)
        prompts = dict(checkpoint.prompt_refs)
        iterations = dict(checkpoint.behavioral_iterations)
        validations = checkpoint.validations
        if result.status is RuntimeStatus.INFRASTRUCTURE_FAILED:
            task_failures = sum(
                item.subject_id == task_id
                and item.status is RuntimeStatus.INFRASTRUCTURE_FAILED
                for item in history
            )
            if task_failures >= max_infrastructure_failures:
                return checkpoint.model_copy(
                    update={"status": "failed", "result_history": history}
                )
            states[task_id] = AuthoritativeRuntimeState.REDO
            return checkpoint.model_copy(
                update={"task_states": states, "result_history": history}
            )
        if result.status is not RuntimeStatus.SUCCEEDED:
            return checkpoint.model_copy(
                update={"status": "failed", "result_history": history}
            )

        validation = await self._validator.validate(batch, result)
        if validation.task_id != task_id:
            raise RuntimeError("validation task does not match runtime result")
        expected_assertion_ids = tuple(
            assertion.assertion_id for assertion in batch.acceptance_criteria
        )
        if validation.assertion_ids != expected_assertion_ids:
            raise RuntimeError("validation assertion IDs do not match Captain release")
        validations = (*validations, validation)
        if validation.disposition is ValidationDisposition.PASSED:
            states[task_id] = AuthoritativeRuntimeState.PASSED
        elif validation.disposition is ValidationDisposition.REDO:
            iterations[task_id] += 1
            if iterations[task_id] >= max_iterations:
                return checkpoint.model_copy(
                    update={
                        "status": "failed",
                        "behavioral_iterations": iterations,
                        "result_history": history,
                        "validations": validations,
                    }
                )
            states[task_id] = AuthoritativeRuntimeState.REDO
            prompts[task_id] = validation.artifact_ref
        else:
            return checkpoint.model_copy(
                update={
                    "status": "replanning",
                    "result_history": history,
                    "validations": validations,
                }
            )
        return checkpoint.model_copy(
            update={
                "task_states": states,
                "prompt_refs": prompts,
                "behavioral_iterations": iterations,
                "result_history": history,
                "validations": validations,
            }
        )

    def _result(
        self,
        checkpoint: ControlPlaneCheckpoint,
        hermes: HermesPlanResult,
        batches: tuple[WorkBatch, ...],
    ) -> ControlPlaneRunResult:
        status: Literal["succeeded", "replanning", "failed"] = (
            checkpoint.status
            if checkpoint.status in {"succeeded", "replanning", "failed"}
            else "failed"
        )
        manifest = _build_manifest(checkpoint, hermes, batches, status)
        return ControlPlaneRunResult(status=status, manifest=manifest)

    @staticmethod
    def _validate_request_bindings(
        request: ControlPlaneRunRequest, task_order: tuple[str, ...]
    ) -> None:
        expected = set(task_order)
        if set(request.workspace_refs) != expected or set(request.prompt_refs) != expected:
            raise ValueError("workspace and prompt bindings must match compiled subtasks")

    @staticmethod
    def _validate_checkpoint(
        checkpoint: ControlPlaneCheckpoint,
        compilation: PlanCompilationResult,
        task_order: tuple[str, ...],
    ) -> None:
        if (
            checkpoint.project_id != compilation.source_project_id
            or checkpoint.plan_version != compilation.source_plan_version
            or checkpoint.plan_digest != compilation.source_plan_digest
            or checkpoint.compiled_batch_digest
            != _compiled_batch_digest(compilation.compiled)
            or checkpoint.task_order != task_order
        ):
            raise RuntimeError("control-plane checkpoint conflicts with Hermes plan")

    @staticmethod
    def _task_dependencies(
        batches: tuple[WorkBatch, ...],
    ) -> dict[str, tuple[str, ...]]:
        subtasks_by_batch = {
            batch.batch_id: tuple(batch.subtask_ids) for batch in batches
        }
        return {
            task_id: tuple(
                dependency_task
                for dependency_batch in batch.depends_on
                for dependency_task in subtasks_by_batch[dependency_batch]
            )
            for batch in batches
            for task_id in batch.subtask_ids
        }


def _build_manifest(
    checkpoint: ControlPlaneCheckpoint,
    hermes: HermesPlanResult,
    batches: tuple[WorkBatch, ...],
    status: Literal["succeeded", "replanning", "failed"],
) -> ControlPlaneEvidenceManifest:
    batch_by_task = {
        task_id: batch for batch in batches for task_id in batch.subtask_ids
    }
    observations: list[EvidenceObservation] = [
        EvidenceObservation(
            observation_id=uuid5(checkpoint.correlation_id, "evidence|hermes"),
            boundary="hermes",
            subject_id=checkpoint.project_id,
            subject_version=checkpoint.plan_version,
            status="succeeded",
            operation="hermes.plan",
            session_id=hermes.planner_id,
            artifact_refs=(hermes.plan_ref, *hermes.blueprint_refs),
            evidence_refs=(hermes.decision_log_ref,),
        )
    ]
    for batch in batches:
        profile = _batch_profile(batch)
        observations.append(
            EvidenceObservation(
                observation_id=uuid5(
                    checkpoint.correlation_id, f"evidence|captain|{batch.batch_id}"
                ),
                boundary="captain",
                subject_id=batch.batch_id,
                subject_version=checkpoint.plan_version,
                status="released",
                batch_id=batch.batch_id,
                capability_profile=profile,
                mcp_servers=("n8n-mcp",) if profile is CapabilityProfile.N8N_BUILDER else (),
            )
        )
    for index, result in enumerate(checkpoint.result_history):
        batch = batch_by_task[result.subject_id]
        profile = _batch_profile(batch)
        observations.append(
            EvidenceObservation(
                observation_id=uuid5(
                    checkpoint.correlation_id,
                    f"evidence|runtime|{index}|{result.event_id}",
                ),
                boundary=(
                    "n8n" if profile is CapabilityProfile.N8N_BUILDER else "codex"
                ),
                subject_id=result.subject_id,
                subject_version=result.subject_version,
                status=result.status.value,
                batch_id=batch.batch_id,
                operation=result.operation.value,
                grant_id=result.grant_id,
                capability_profile=profile,
                mcp_servers=("n8n-mcp",) if profile is CapabilityProfile.N8N_BUILDER else (),
                session_id=result.session_id,
                artifact_refs=result.artifact_refs,
                evidence_refs=result.evidence_refs,
            )
        )
    for index, validation in enumerate(checkpoint.validations):
        batch = batch_by_task[validation.task_id]
        observations.append(
            EvidenceObservation(
                observation_id=uuid5(
                    checkpoint.correlation_id,
                    f"evidence|validation|{index}|{validation.artifact_ref.sha256}",
                ),
                boundary="validation",
                subject_id=validation.task_id,
                subject_version=checkpoint.plan_version,
                status=validation.disposition.value,
                batch_id=batch.batch_id,
                evidence_refs=(validation.artifact_ref,),
                validation_disposition=validation.disposition,
            )
        )
    return ControlPlaneEvidenceManifest(
        schema_name="captain.control-plane-evidence.v1",
        correlation_id=checkpoint.correlation_id,
        project_id=checkpoint.project_id,
        plan_version=checkpoint.plan_version,
        plan_digest=checkpoint.plan_digest,
        generated_at=checkpoint.created_at,
        status=status,
        minibook_project_id=hermes.minibook.project_id,
        minibook_post_id=hermes.minibook.post_id,
        batch_order=checkpoint.batch_order,
        completed_tasks=tuple(
            task_id
            for task_id in checkpoint.task_order
            if checkpoint.task_states[task_id] is AuthoritativeRuntimeState.PASSED
        ),
        behavioral_redos=sum(checkpoint.behavioral_iterations.values()),
        infrastructure_failures=sum(
            result.status is RuntimeStatus.INFRASTRUCTURE_FAILED
            for result in checkpoint.result_history
        ),
        observations=tuple(observations),
    )


def _batch_profile(batch: WorkBatch) -> CapabilityProfile:
    if "n8n-builder" in batch.capability_tags:
        return CapabilityProfile.N8N_BUILDER
    return CapabilityProfile.CODE_BUILDER


def _compiled_batch_digest(compiled: CaptainCompiledPlan) -> str:
    public_batches = [batch.model_dump(mode="json") for batch in compiled.batches]
    canonical = json.dumps(
        public_batches,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _reject_private(value: Any) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            key_text = str(key)
            if _PRIVATE_VALUE.search(f"{key_text}="):
                raise ValueError("private field is forbidden in evidence manifests")
            _reject_private(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_private(nested)
    elif isinstance(value, str):
        if (
            _PRIVATE_VALUE.search(value)
            or _TOKEN_LIKE_VALUE.search(value)
            or _ABSOLUTE_PATH.search(value)
        ):
            raise ValueError("private value is forbidden in evidence manifests")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("control-plane timestamps must be timezone-aware")
    if value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError("control-plane timestamps must use UTC")
    return value.astimezone(timezone.utc)


__all__ = [
    "AgentRuntimeControlPlane",
    "ControlPlaneCheckpoint",
    "ControlPlaneEvidenceManifest",
    "ControlPlaneRunRequest",
    "ControlPlaneRunResult",
    "EvidenceObservation",
    "InMemoryControlPlaneRunStore",
    "JsonControlPlaneRunStore",
    "ValidationDisposition",
    "ValidationRecord",
]
