"""Restart-safe composition root for one Captain capability-factory release.

External execution stays behind injected ports.  The composition itself owns
Captain parsing, compilation, lifecycle policy, package validation, release
records, and the redacted projection hand-off.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import importlib
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping, Protocol
from urllib.parse import urlsplit
from uuid import UUID, uuid5

# ``python -m`` executes this module as ``__main__``.  Static production
# adapters import its canonical package name, so bind both names to the same
# module before an adapter can import the entrypoint base class.
if __name__ == "__main__":
    sys.modules.setdefault(
        "agenten.agent_factory.capability_factory_entrypoint", sys.modules[__name__]
    )

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from agenten.agent_factory.capability_resolution import (
    CapabilityCatalogPort,
    CapabilityResolver,
)
from agenten.agent_factory.contracts import (
    AgentFactoryJobV2,
    FactoryBlockStatus,
    FactoryEvidenceBlock,
    FactoryPhase,
    PromotedCapability,
)
from agenten.agent_factory.forge_contracts import (
    CreationJobV1,
    CreationResultV1,
    CreationSubmissionReceipt,
    ReleasedSkillRefV1,
)
from agenten.agent_factory.holdout_store import (
    InMemoryPrivateHoldoutStore,
    PrivateHoldoutStore,
)
from agenten.agent_factory.input_compiler import (
    CompiledFactorySpecification,
    FactoryInputCompiler,
)
from agenten.agent_factory.input_document import load_factory_input
from agenten.agent_factory.job_builder import build_factory_job
from agenten.agent_factory.outcome_contracts import (
    CapabilityPackageManifestV1,
    CapabilityReleaseEvidenceV1,
    ExecutionOutcomeV1,
    FactoryTerminalDecision,
    FactoryTerminalState,
    ForgeCapabilityPackageCandidateV1,
    validate_execution_outcome_binding,
)
from agenten.agent_factory.outcome_validation import (
    CapabilityPackageValidationError,
    CapabilityPackageValidator,
    CapabilitySandboxRequest,
    CapabilitySandboxResult,
    CapabilitySandboxTermination,
    ReadOnlyCapabilityContentStore,
    TrustedCapabilitySandboxRunner,
)
from agenten.agent_factory.release_gate import (
    CapabilityValidationFailure,
    E2EKind,
    E2EOutcome,
    E2ERunEvidence,
    FactoryReleaseDecision,
    FactoryTerminalReasonCode,
    derive_terminal_decision,
    evaluate_factory_release,
)
from agenten.agent_factory.service import (
    FactoryCoordinator,
    FactoryRepository,
    FactoryRepositoryError,
)
from agenten.agent_factory.skill_store import StoredSkillEvaluation
from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeResult,
    ArtifactRef,
    CapabilityGrant,
    ProviderEffectReceipt,
)
from agenten.delivery.minibook_events import MinibookProjectionEvent
from agenten.delivery.projector import MinibookProjector
from gateway.contracts import (
    CapabilityExecutionRecord,
    CapabilityExecutionRequest,
    CapabilityReleaseRequest,
    CapabilityWriteReceipt,
    FactoryReleaseDecisionSubmission,
    RuntimeExecutionClaimReceipt,
    RuntimeExecutionClaimRequest,
    RuntimeExecutionClaim,
    RuntimeOperationProjection,
    RuntimeResultRecoveryObservation,
    RuntimeResultRecoveryRequest,
    canonical_contract_sha256,
)
from gateway.capability_catalog import (
    CapabilityCatalogRecord,
    compatibility_request_for_authority,
)


class CapabilityFactoryInputMutation(ValueError):
    """The same run identity was presented with different immutable input."""


class CapabilityFactoryConfigurationError(ValueError):
    """Production composition was not given a safe, complete configuration."""


class CapabilityFactoryDeadlineExceeded(RuntimeError):
    """A post-publication effect was stopped because the immutable budget expired."""


class CapabilitySandboxIsolationError(RuntimeError):
    """Docker could not prove every required isolation property."""


class CapabilityProviderIdempotencyError(RuntimeError):
    """The provider adapter cannot prove durable, replay-safe execution."""


class CapabilityFactoryCheckpoint(BaseModel):
    """Minimum immutable identity needed before any external factory effect."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    correlation_id: UUID
    factory_job_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    occurred_at: datetime
    deadline_at: datetime

    @model_validator(mode="after")
    def require_immutable_utc_timing(self) -> "CapabilityFactoryCheckpoint":
        for value in (self.occurred_at, self.deadline_at):
            if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
                raise ValueError("factory checkpoint timing must be UTC")
        if self.deadline_at <= self.occurred_at:
            raise ValueError("factory checkpoint deadline must follow occurred_at")
        return self


class CapabilityFactoryCliConfig(BaseModel):
    """Redaction-safe CLI configuration; credential values never come from argv."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_root: Path
    input_path: Path
    artifact_dir: Path
    checkpoint_dir: Path
    gateway_url: str
    runtime_url: str
    minibook_url: str
    sandbox_image: str
    adapter_manifest_path: Path
    adapter_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    correlation_id: UUID
    subject_version: int = Field(default=1, ge=1, strict=True)
    wall_clock_budget_seconds: int = Field(default=600, ge=1, le=86_400, strict=True)
    preflight_only: bool = False
    gateway_token: SecretStr
    runtime_token: SecretStr
    minibook_projection_api_key: SecretStr


class CapabilityReleaseRunReceipt(BaseModel):
    """One Captain-owned recovery or normal E2E record and its exact bytes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    record: CapabilityReleaseEvidenceV1
    reference: ArtifactRef

    @model_validator(mode="after")
    def require_canonical_reference(self) -> "CapabilityReleaseRunReceipt":
        content = self.record.model_dump_json(by_alias=True).encode("utf-8")
        if (
            self.reference.media_type != "application/json"
            or self.reference.sha256 != hashlib.sha256(content).hexdigest()
            or self.reference.uri.rsplit("/", 1)[-1] != self.reference.sha256
        ):
            raise ValueError("release run reference is not canonical")
        return self


class CapabilityExecutionBundle(BaseModel):
    """Typed runtime evidence returned before Captain records an execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    command: AgentRuntimeCommand
    grant: CapabilityGrant
    result: AgentRuntimeResult
    outcome: ExecutionOutcomeV1
    claim_owner_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    claim_fencing_token: int = Field(ge=1, strict=True)


class CapabilityExecutionPlan(BaseModel):
    """Deterministic command and least-privilege grant prepared before claiming."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    command: AgentRuntimeCommand
    grant: CapabilityGrant
    claim_owner_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class CapabilityRuntimeExecution(BaseModel):
    """Provider result produced only after Gateway issued the execution claim."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    result: AgentRuntimeResult
    outcome: ExecutionOutcomeV1
    provider_receipt: ProviderEffectReceipt


class CapabilityExecutionRetryPending(BaseModel):
    """A committed active claim that cannot safely expose its credential again."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    command_id: UUID
    expires_at: datetime

    @model_validator(mode="after")
    def require_utc_expiry(self) -> "CapabilityExecutionRetryPending":
        if (
            self.expires_at.tzinfo is None
            or self.expires_at.utcoffset() != timezone.utc.utcoffset(self.expires_at)
        ):
            raise ValueError("runtime retry expiry must be UTC")
        return self


class CapabilityExecutionCompleted(BaseModel):
    """A fully recorded runtime outcome and its exact public projection event."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    bundle: CapabilityExecutionBundle
    projection_events: tuple[MinibookProjectionEvent, ...] = Field(min_length=1)
    minibook_projection_verified: Literal[True]


class CapabilityFactoryRunSummary(BaseModel):
    """Redacted stable identity projection for one factory invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    correlation_id: UUID
    factory_job_id: UUID
    invocation_job_id: UUID
    release_authority_job_id: UUID | None = None
    execution_mode: Literal["created", "reused"]
    execution_state: Literal["not_started", "retry_pending", "completed"]
    retry_expires_at: datetime | None = None
    creation_job_id: UUID | None = None
    terminal_decision_id: UUID
    terminal_state: FactoryTerminalState
    capability_id: str
    capability_version: int | None = Field(default=None, ge=1)
    recovery_id: str | None = None
    e2e_batch_ids: tuple[str, ...] = ()
    execution_command_id: UUID | None = None
    execution_result_id: UUID | None = None
    projection_event_ids: tuple[UUID, ...] = ()
    minibook_projection_verified: bool = False
    package_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    release_evidence_sha256: tuple[str, ...] = ()
    unresolved_required_tool_gaps: tuple[str, ...] = ()
    unresolved_optional_tool_gaps: tuple[str, ...] = ()

    @model_validator(mode="after")
    def require_consistent_execution_identity(self) -> "CapabilityFactoryRunSummary":
        if self.factory_job_id != self.invocation_job_id:
            raise ValueError("factory_job_id must identify the current invocation")
        if self.execution_mode == "reused" and self.creation_job_id is not None:
            raise ValueError("reused execution cannot expose a creation job")
        if self.execution_state == "retry_pending":
            if (
                self.execution_command_id is None
                or self.execution_result_id is not None
                or self.retry_expires_at is None
            ):
                raise ValueError("retry-pending execution identity is incomplete")
        elif self.retry_expires_at is not None:
            raise ValueError("only retry-pending execution may expose retry expiry")
        if self.execution_state == "completed" and (
            self.execution_command_id is None or self.execution_result_id is None
        ):
            raise ValueError("completed execution identity is incomplete")
        return self


class CapabilityCreationPort(Protocol):
    async def preparation_blocks(
        self,
        job: AgentFactoryJobV2,
        creation_job: CreationJobV1,
    ) -> tuple[FactoryEvidenceBlock, FactoryEvidenceBlock]: ...

    async def submit(self, creation_job: CreationJobV1) -> CreationSubmissionReceipt: ...

    async def result(self, creation_job_id: UUID) -> CreationResultV1: ...

    async def completion_block(
        self,
        job: AgentFactoryJobV2,
        result: CreationResultV1,
    ) -> FactoryEvidenceBlock: ...


class HermesCreationAnalysisPort(Protocol):
    """Materialize Hermes creation evidence before Minibook accepts the job."""

    async def analyze(
        self,
        job: AgentFactoryJobV2,
        creation_job: CreationJobV1,
    ) -> object: ...


class CaptainEvidenceIssuerPort(Protocol):
    """Captain-owned issuer for canonical recovery and normal E2E records."""

    async def run(
        self,
        job: AgentFactoryJobV2,
        creation_result: CreationResultV1,
        candidate: ForgeCapabilityPackageCandidateV1,
        run_number: int,
    ) -> CapabilityReleaseRunReceipt | None: ...

    async def lifecycle_blocks(
        self,
        job: AgentFactoryJobV2,
        receipts: tuple[CapabilityReleaseRunReceipt, ...],
    ) -> tuple[FactoryEvidenceBlock, FactoryEvidenceBlock, FactoryEvidenceBlock]: ...


class CapabilityRuntimePort(Protocol):
    async def prepare(
        self,
        job: AgentFactoryJobV2,
        authority: CapabilityCatalogRecord,
    ) -> CapabilityExecutionPlan: ...

    async def execute(
        self,
        plan: CapabilityExecutionPlan,
        authority: CapabilityCatalogRecord,
        claim: RuntimeExecutionClaimReceipt,
        *,
        effect_id: UUID,
    ) -> CapabilityRuntimeExecution: ...

    def guarantees_durable_idempotency(
        self,
        plan: CapabilityExecutionPlan,
        authority: CapabilityCatalogRecord,
    ) -> bool: ...

    async def lookup_effect(
        self,
        *,
        command_id: UUID,
        effect_id: UUID,
    ) -> CapabilityRuntimeExecution | None: ...

    async def derive_outcome(
        self,
        plan: CapabilityExecutionPlan,
        authority: CapabilityCatalogRecord,
        result: AgentRuntimeResult,
    ) -> ExecutionOutcomeV1: ...


class CapabilityAuthorityCatalogPort(CapabilityCatalogPort, Protocol):
    def compatible_record(
        self,
        job: AgentFactoryJobV2,
    ) -> CapabilityCatalogRecord | None: ...


class CapabilityFactoryGatewayPort(Protocol):
    def record_factory_release_decision(
        self,
        submission: FactoryReleaseDecisionSubmission,
    ) -> object: ...

    def publish_capability_release(
        self,
        request: CapabilityReleaseRequest,
    ) -> CapabilityWriteReceipt: ...

    def factory_terminal_decision(
        self,
        job_id: UUID,
    ) -> FactoryTerminalDecision | None: ...

    def capability(
        self,
        capability_id: str,
        *,
        version: int | None = None,
    ) -> CapabilityCatalogRecord | None: ...

    def accept_runtime_command(self, command: AgentRuntimeCommand) -> object: ...

    def record_capability_grant(self, grant: CapabilityGrant) -> object: ...

    def claim_runtime_execution(
        self,
        request: RuntimeExecutionClaimRequest,
    ) -> RuntimeExecutionClaimReceipt: ...

    def record_runtime_result(
        self,
        result: AgentRuntimeResult,
        *,
        execution_owner_id: str,
        execution_fencing_token: int,
        execution_claim_credential: str,
    ) -> object: ...

    def recover_runtime_result(
        self,
        request: RuntimeResultRecoveryRequest,
        *,
        execution_owner_id: str,
        execution_fencing_token: int,
        execution_claim_credential: str,
    ) -> object: ...

    def runtime_result_recovery(
        self,
        operation_id: UUID,
    ) -> RuntimeResultRecoveryObservation | None: ...

    def find_runtime_operation(
        self,
        operation_id: UUID,
    ) -> RuntimeOperationProjection | None: ...

    def runtime_execution_claim(
        self,
        operation_id: UUID,
    ) -> RuntimeExecutionClaim | None: ...

    def record_capability_execution(
        self,
        request: CapabilityExecutionRequest,
    ) -> CapabilityWriteReceipt: ...

    def capability_execution(
        self,
        command_id: UUID,
    ) -> CapabilityExecutionRecord | None: ...

    def projection_events(
        self,
        correlation_id: UUID,
    ) -> tuple[MinibookProjectionEvent, ...]: ...


class CapabilityFactoryClock(Protocol):
    def now(self) -> datetime: ...


class CapabilityFactoryCheckpointStore(Protocol):
    def load(
        self,
        correlation_id: UUID,
    ) -> CapabilityFactoryCheckpoint | None: ...

    def bind(
        self,
        checkpoint: CapabilityFactoryCheckpoint,
    ) -> CapabilityFactoryCheckpoint: ...


class InMemoryCapabilityFactoryCheckpointStore:
    """Append-only deterministic checkpoint adapter used by offline composition."""

    def __init__(self) -> None:
        self._by_correlation: dict[UUID, CapabilityFactoryCheckpoint] = {}

    def load(self, correlation_id: UUID) -> CapabilityFactoryCheckpoint | None:
        return self._by_correlation.get(correlation_id)

    def bind(self, checkpoint: CapabilityFactoryCheckpoint) -> CapabilityFactoryCheckpoint:
        existing = self._by_correlation.get(checkpoint.correlation_id)
        if existing is None:
            self._by_correlation[checkpoint.correlation_id] = checkpoint
            return checkpoint
        return _require_same_checkpoint(existing, checkpoint)


class FileCapabilityFactoryCheckpointStore:
    """Exclusive, append-only checkpoints that survive interpreter restarts."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory.resolve()
        self._directory.mkdir(parents=True, exist_ok=True)

    def load(self, correlation_id: UUID) -> CapabilityFactoryCheckpoint | None:
        return self._read(self._directory / f"{correlation_id}.json")

    def bind(self, checkpoint: CapabilityFactoryCheckpoint) -> CapabilityFactoryCheckpoint:
        target = self._directory / f"{checkpoint.correlation_id}.json"
        existing = self._read(target)
        if existing is not None:
            return _require_same_checkpoint(existing, checkpoint)
        content = _canonical_json_bytes(checkpoint)
        try:
            with target.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            existing = self._read(target)
            if existing is None:
                raise CapabilityFactoryInputMutation(
                    "factory checkpoint could not be read after a concurrent bind"
                )
            return _require_same_checkpoint(existing, checkpoint)
        return checkpoint

    @staticmethod
    def _read(target: Path) -> CapabilityFactoryCheckpoint | None:
        try:
            content = target.read_bytes()
        except FileNotFoundError:
            return None
        try:
            return CapabilityFactoryCheckpoint.model_validate_json(content)
        except ValueError as exc:
            raise CapabilityFactoryInputMutation(
                "factory checkpoint is incomplete or invalid"
            ) from exc


@dataclass(frozen=True)
class DockerCommandResult:
    returncode: int
    stdout: str
    stderr: str


class DockerCommandRunner(Protocol):
    async def run(
        self,
        arguments: tuple[str, ...],
        *,
        timeout_seconds: float | None = None,
    ) -> DockerCommandResult: ...


class DockerCliCommandRunner:
    """Bounded, argument-vector-only Docker CLI adapter."""

    async def run(
        self,
        arguments: tuple[str, ...],
        *,
        timeout_seconds: float | None = None,
    ) -> DockerCommandResult:
        limit = 30.0 if timeout_seconds is None else timeout_seconds
        if not math.isfinite(limit) or limit <= 0:
            raise CapabilitySandboxIsolationError(
                "docker command timeout must be a positive finite value"
            )
        process = await asyncio.create_subprocess_exec(
            "docker",
            *arguments,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        communication = asyncio.create_task(process.communicate())
        try:
            stdout, stderr = await asyncio.wait_for(
                asyncio.shield(communication),
                timeout=limit,
            )
        except TimeoutError as exc:
            if process.returncode is None:
                process.terminate()
            try:
                await asyncio.wait_for(asyncio.shield(communication), timeout=5.0)
            except TimeoutError:
                if process.returncode is None:
                    process.kill()
                try:
                    await asyncio.wait_for(
                        asyncio.shield(communication),
                        timeout=5.0,
                    )
                except TimeoutError:
                    communication.cancel()
            raise CapabilitySandboxIsolationError(
                "docker command timed out"
            ) from exc
        except asyncio.CancelledError:
            if process.returncode is None:
                process.terminate()
            try:
                await asyncio.wait_for(asyncio.shield(communication), timeout=5.0)
            except TimeoutError:
                if process.returncode is None:
                    process.kill()
                communication.cancel()
            raise
        if len(stdout) > 1_048_576 or len(stderr) > 1_048_576:
            raise CapabilitySandboxIsolationError(
                "docker returned more output than the sandbox control limit"
            )
        return DockerCommandResult(
            returncode=process.returncode or 0,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
        )


@dataclass(frozen=True)
class _DockerExecution:
    request: CapabilitySandboxRequest
    container_id: str


_CAPTAIN_SANDBOX_IMAGE = re.compile(
    r"^(?:(?:[a-z0-9.-]+(?::[0-9]+)?)/)?"
    r"captain-[a-z0-9._/-]+@sha256:[0-9a-f]{64}$"
)
_DOCKER_CONTAINER_ID = re.compile(r"^[0-9a-f]{64}$")
_SANDBOX_SCRIPT = """
import importlib
import importlib.util
import json
import pathlib
import sys

modules = tuple(json.loads(sys.argv[1]))
tests = tuple(json.loads(sys.argv[2]))
sys.path.insert(0, "/workspace")
try:
    for module_name in modules:
        spec = importlib.util.find_spec(module_name)
        if spec is None or spec.origin is None:
            raise ImportError(module_name)
        source = pathlib.Path(spec.origin).read_text(encoding="utf-8")
        compile(source, spec.origin, "exec")
except Exception:
    raise SystemExit(20)
try:
    for module_name in modules:
        importlib.import_module(module_name)
except Exception:
    raise SystemExit(30)
try:
    import pytest
    status = pytest.main(("-q", "-p", "no:cacheprovider", "--basetemp=/tmp/pytest", *tests))
except Exception:
    raise SystemExit(40)
raise SystemExit(0 if status == 0 else 40)
""".strip()


class DockerCapabilitySandboxRunner:
    """Run untrusted candidates only in an inspected, disposable Captain container."""

    def __init__(
        self,
        *,
        image: str,
        command_runner: DockerCommandRunner | None = None,
    ) -> None:
        if _CAPTAIN_SANDBOX_IMAGE.fullmatch(image) is None:
            raise CapabilityFactoryConfigurationError(
                "sandbox image must be Captain-owned and digest-pinned"
            )
        self._image = image
        self._commands = command_runner or DockerCliCommandRunner()
        self._active: dict[UUID, _DockerExecution] = {}
        self._terminations: dict[UUID, CapabilitySandboxTermination] = {}

    async def validate(
        self,
        request: CapabilitySandboxRequest,
    ) -> CapabilitySandboxResult:
        workspace = request.workspace.resolve()
        if (
            not workspace.is_dir()
            or request.python_path_root.resolve() != workspace
            or request.process_identity != f"sandbox-handle://{request.execution_id}"
        ):
            raise CapabilitySandboxIsolationError(
                "sandbox workspace or process identity is invalid"
            )
        if _workspace_tree_sha256(workspace) != request.extracted_tree_sha256:
            raise CapabilitySandboxIsolationError(
                "sandbox workspace tree digest does not match the request"
            )
        await self._verify_image(request.timeout_seconds)
        name = f"captain-capability-{request.execution_id}"
        mount = (
            "type=bind,source="
            f"{workspace},target=/workspace,readonly"
        )
        if "," in str(workspace) or "\n" in str(workspace) or "\r" in str(workspace):
            raise CapabilitySandboxIsolationError(
                "sandbox workspace cannot be encoded as a Docker bind mount"
            )
        create = await self._commands.run(
            (
                "create",
                "--name",
                name,
                "--label",
                "captain.owner=capability-factory",
                "--label",
                "captain.disposable=true",
                "--pull",
                "never",
                "--network",
                "none",
                "--read-only",
                "--init",
                "--memory",
                str(request.max_memory_bytes),
                "--pids-limit",
                str(request.max_processes),
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--user",
                "65532:65532",
                "--mount",
                mount,
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=67108864",
                "--workdir",
                "/workspace",
                "--env",
                "PYTHONDONTWRITEBYTECODE=1",
                "--entrypoint",
                "python",
                self._image,
                "-I",
                "-B",
                "-c",
                _SANDBOX_SCRIPT,
                json.dumps(request.module_names, separators=(",", ":")),
                json.dumps(request.test_paths, separators=(",", ":")),
            ),
            timeout_seconds=min(float(request.timeout_seconds), 30.0),
        )
        container_id = create.stdout.strip()
        if create.returncode != 0 or _DOCKER_CONTAINER_ID.fullmatch(container_id) is None:
            raise CapabilitySandboxIsolationError(
                "Docker could not create the disposable sandbox container"
            )
        execution = _DockerExecution(request=request, container_id=container_id)
        self._active[request.execution_id] = execution
        try:
            created = await self._inspect(container_id, request.timeout_seconds)
            self._require_isolation(created, execution, expected_name=name)
            start = await self._commands.run(
                ("start", "--attach", container_id),
                timeout_seconds=float(request.timeout_seconds),
            )
            finished = await self._inspect(container_id, request.timeout_seconds)
            self._require_isolation(finished, execution, expected_name=name)
            state = _mapping(finished, "State")
            if state.get("Running") is not False or state.get("Status") != "exited":
                raise CapabilitySandboxIsolationError(
                    "sandbox process termination was not inspectable"
                )
            exit_code = state.get("ExitCode")
            if not isinstance(exit_code, int):
                raise CapabilitySandboxIsolationError(
                    "sandbox exit status was not inspectable"
                )
            if start.returncode not in {0, exit_code}:
                raise CapabilitySandboxIsolationError(
                    "Docker start status disagrees with container state"
                )
            status = "passed" if exit_code == 0 else "failed"
            failure_stage = {20: "compile", 30: "import", 40: "test"}.get(exit_code)
            if exit_code != 0 and failure_stage is None:
                status = "isolation_failed"
                failure_stage = "isolation"
            return CapabilitySandboxResult(
                execution_id=request.execution_id,
                request_digest=request.request_digest,
                status=status,
                failure_stage=failure_stage,
                imported_modules=request.module_names,
                executed_test_paths=request.test_paths,
                sandbox_identity=f"sandbox://docker/{container_id}",
                process_identity=request.process_identity,
                process_identity_verified=True,
                extracted_tree_sha256=request.extracted_tree_sha256,
                workspace_was_read_only=True,
                network_was_disabled=True,
                resource_limits_were_enforced=True,
                process_tree_termination_capable=True,
            )
        finally:
            await self._remove(container_id)
            self._active.pop(request.execution_id, None)

    async def cancel(self, execution_id: UUID) -> None:
        execution = self._active.get(execution_id)
        if execution is None:
            if execution_id in self._terminations:
                return
            raise CapabilitySandboxIsolationError(
                "sandbox cancellation has no verified process identity"
            )
        killed = await self._commands.run(
            ("kill", execution.container_id),
            timeout_seconds=5.0,
        )
        if killed.returncode != 0:
            raise CapabilitySandboxIsolationError(
                "Docker could not terminate the verified sandbox process tree"
            )
        inspected = await self._inspect(execution.container_id, 5.0)
        state = _mapping(inspected, "State")
        if state.get("Running") is not False:
            raise CapabilitySandboxIsolationError(
                "sandbox process tree remained active after cancellation"
            )
        self._terminations[execution_id] = CapabilitySandboxTermination(
            execution_id=execution_id,
            request_digest=execution.request.request_digest,
            sandbox_identity=f"sandbox://docker/{execution.container_id}",
            process_identity=execution.request.process_identity,
            process_identity_verified=True,
            extracted_tree_sha256=execution.request.extracted_tree_sha256,
            terminated=True,
            process_tree_terminated=True,
        )
        await self._remove(execution.container_id)
        self._active.pop(execution_id, None)

    async def await_termination(
        self,
        execution_id: UUID,
    ) -> CapabilitySandboxTermination:
        try:
            return self._terminations[execution_id]
        except KeyError as exc:
            raise CapabilitySandboxIsolationError(
                "sandbox termination has not been inspectably attested"
            ) from exc

    async def _verify_image(self, timeout_seconds: int) -> None:
        result = await self._commands.run(
            ("image", "inspect", self._image),
            timeout_seconds=min(float(timeout_seconds), 30.0),
        )
        if result.returncode != 0:
            raise CapabilitySandboxIsolationError(
                "digest-pinned Captain sandbox image is not available locally"
            )
        inspected = _single_inspect_document(result.stdout)
        repo_digests = inspected.get("RepoDigests")
        if not isinstance(repo_digests, list) or self._image not in repo_digests:
            raise CapabilitySandboxIsolationError(
                "local sandbox image does not match its configured digest"
            )

    async def _inspect(self, container_id: str, timeout_seconds: float) -> Mapping[str, object]:
        result = await self._commands.run(
            ("inspect", container_id),
            timeout_seconds=min(timeout_seconds, 30.0),
        )
        if result.returncode != 0:
            raise CapabilitySandboxIsolationError(
                "Docker could not inspect the sandbox container"
            )
        return _single_inspect_document(result.stdout)

    def _require_isolation(
        self,
        inspected: Mapping[str, object],
        execution: _DockerExecution,
        *,
        expected_name: str,
    ) -> None:
        request = execution.request
        if (
            inspected.get("Id") != execution.container_id
            or inspected.get("Name") != f"/{expected_name}"
        ):
            raise CapabilitySandboxIsolationError(
                "sandbox process identity was not verified by Docker"
            )
        config = _mapping(inspected, "Config")
        labels = _mapping(config, "Labels")
        if (
            config.get("Image") != self._image
            or config.get("User") != "65532:65532"
            or config.get("WorkingDir") != "/workspace"
            or config.get("Entrypoint") != ["python"]
            or labels.get("captain.owner") != "capability-factory"
            or labels.get("captain.disposable") != "true"
        ):
            raise CapabilitySandboxIsolationError(
                "sandbox image, identity, or non-root execution was not attested"
            )
        host = _mapping(inspected, "HostConfig")
        if host.get("NetworkMode") != "none":
            raise CapabilitySandboxIsolationError(
                "sandbox network isolation was not attested"
            )
        if host.get("ReadonlyRootfs") is not True:
            raise CapabilitySandboxIsolationError(
                "sandbox read-only root filesystem was not attested"
            )
        if (
            host.get("Memory") != request.max_memory_bytes
            or host.get("PidsLimit") != request.max_processes
            or host.get("Init") is not True
        ):
            raise CapabilitySandboxIsolationError(
                "sandbox resource or process-tree limits were not attested"
            )
        cap_drop = host.get("CapDrop")
        security = host.get("SecurityOpt")
        if (
            not isinstance(cap_drop, list)
            or "ALL" not in {str(item).upper() for item in cap_drop}
            or not isinstance(security, list)
            or not any(str(item).startswith("no-new-privileges") for item in security)
        ):
            raise CapabilitySandboxIsolationError(
                "sandbox Linux privilege restrictions were not attested"
            )
        tmpfs = _mapping(host, "Tmpfs")
        tmp_options = tmpfs.get("/tmp")
        if (
            not isinstance(tmp_options, str)
            or set(tmp_options.split(","))
            != {"rw", "noexec", "nosuid", "size=67108864"}
        ):
            raise CapabilitySandboxIsolationError(
                "sandbox writable temporary storage was not safely bounded"
            )
        mounts = inspected.get("Mounts")
        if not isinstance(mounts, list):
            raise CapabilitySandboxIsolationError(
                "sandbox workspace mount was not inspectable"
            )
        workspace_mounts = [
            item
            for item in mounts
            if isinstance(item, dict) and item.get("Destination") == "/workspace"
        ]
        if len(workspace_mounts) != 1:
            raise CapabilitySandboxIsolationError(
                "sandbox workspace mount identity was not unique"
            )
        mount = workspace_mounts[0]
        source = mount.get("Source")
        if (
            mount.get("Type") != "bind"
            or mount.get("RW") is not False
            or not isinstance(source, str)
            or Path(source).resolve() != request.workspace.resolve()
        ):
            raise CapabilitySandboxIsolationError(
                "sandbox workspace was not attested as the exact read-only bind"
            )

    async def _remove(self, container_id: str) -> None:
        result = await self._commands.run(
            ("rm", "-f", container_id),
            timeout_seconds=5.0,
        )
        if result.returncode != 0:
            raise CapabilitySandboxIsolationError(
                "disposable sandbox container cleanup was not verified"
            )


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CapabilityFactoryConfigurationError(
            "invalid capability factory arguments"
        )


def parse_capability_factory_args(
    arguments: tuple[str, ...],
    *,
    environ: Mapping[str, str] | None = None,
    workspace_root: Path | None = None,
) -> CapabilityFactoryCliConfig:
    """Parse non-secret CLI values and resolve every filesystem path in-bounds."""

    for item in arguments:
        lowered = item.casefold()
        if item.startswith("--") and any(
            marker in lowered
            for marker in ("token", "secret", "password", "credential", "api-key")
        ):
            raise CapabilityFactoryConfigurationError(
                "credentials are accepted only through environment aliases"
            )
    parser = _SafeArgumentParser(prog="capability-factory")
    parser.add_argument("--input", default="TO_BE_BUILT.md")
    parser.add_argument("--artifact-dir", default="artifacts/capability-factory")
    parser.add_argument(
        "--checkpoint-dir",
        default=".superpowers/sdd/capability-factory-checkpoints",
    )
    parser.add_argument("--gateway-url", default="http://127.0.0.1:8080")
    parser.add_argument("--runtime-url", default="http://127.0.0.1:8081")
    parser.add_argument("--minibook-url", default="http://127.0.0.1:8082")
    parser.add_argument(
        "--sandbox-image",
        default="captain-capability-sandbox@sha256:" + "0" * 64,
    )
    parser.add_argument("--correlation-id")
    parser.add_argument("--subject-version", type=int, default=1)
    parser.add_argument("--wall-clock-budget-seconds", type=int, default=600)
    parser.add_argument("--preflight-only", action="store_true")
    namespace = parser.parse_args(arguments)

    root = (workspace_root or Path.cwd()).resolve()
    input_path = _safe_workspace_path(root, namespace.input, "input")
    artifact_dir = _safe_workspace_path(root, namespace.artifact_dir, "artifact")
    checkpoint_dir = _safe_workspace_path(root, namespace.checkpoint_dir, "checkpoint")
    if not input_path.is_file():
        raise CapabilityFactoryConfigurationError(
            "factory input path is not a readable workspace file"
        )
    gateway_url = _safe_service_url(namespace.gateway_url, "Gateway")
    runtime_url = _safe_service_url(namespace.runtime_url, "runtime")
    minibook_url = _safe_service_url(namespace.minibook_url, "Minibook")
    if _CAPTAIN_SANDBOX_IMAGE.fullmatch(namespace.sandbox_image) is None:
        raise CapabilityFactoryConfigurationError(
            "sandbox image must be Captain-owned and digest-pinned"
        )
    if namespace.correlation_id is None:
        raise CapabilityFactoryConfigurationError(
            "a correlation identity is required for restart-safe execution"
        )
    try:
        correlation_id = UUID(namespace.correlation_id)
    except (TypeError, ValueError) as exc:
        raise CapabilityFactoryConfigurationError(
            "correlation identity is invalid"
        ) from exc
    source = os.environ if environ is None else environ
    adapter_manifest_value = source.get(
        "CAPABILITY_FACTORY_ENTRYPOINT_ADAPTER_MANIFEST",
        "",
    ).strip()
    adapter_manifest_sha256 = source.get(
        "CAPABILITY_FACTORY_ENTRYPOINT_ADAPTER_SHA256",
        "",
    ).strip()
    if not adapter_manifest_value or not adapter_manifest_sha256:
        raise CapabilityFactoryConfigurationError(
            "required static adapter manifest aliases are missing: "
            "CAPABILITY_FACTORY_ENTRYPOINT_ADAPTER_MANIFEST, "
            "CAPABILITY_FACTORY_ENTRYPOINT_ADAPTER_SHA256"
        )
    adapter_manifest_path = _safe_workspace_path(
        root,
        adapter_manifest_value,
        "adapter manifest",
    )
    if not adapter_manifest_path.is_file():
        raise CapabilityFactoryConfigurationError(
            "static adapter manifest is not a readable workspace file"
        )
    aliases = {
        "gateway_token": "CAPTAIN_GATEWAY_TOKEN",
        "runtime_token": "CAPTAIN_RUNTIME_TOKEN",
        "minibook_projection_api_key": "MINIBOOK_PROJECTION_API_KEY",
    }
    secret_values: dict[str, SecretStr] = {}
    missing: list[str] = []
    for field_name, alias in aliases.items():
        value = source.get(alias)
        if value is None or not value.strip():
            missing.append(alias)
        else:
            secret_values[field_name] = SecretStr(value)
    if missing:
        raise CapabilityFactoryConfigurationError(
            "required credential aliases are missing: " + ", ".join(sorted(missing))
        )
    try:
        return CapabilityFactoryCliConfig(
            workspace_root=root,
            input_path=input_path,
            artifact_dir=artifact_dir,
            checkpoint_dir=checkpoint_dir,
            gateway_url=gateway_url,
            runtime_url=runtime_url,
            minibook_url=minibook_url,
            sandbox_image=namespace.sandbox_image,
            adapter_manifest_path=adapter_manifest_path,
            adapter_manifest_sha256=adapter_manifest_sha256,
            correlation_id=correlation_id,
            subject_version=namespace.subject_version,
            wall_clock_budget_seconds=namespace.wall_clock_budget_seconds,
            preflight_only=namespace.preflight_only,
            **secret_values,
        )
    except ValueError as exc:
        raise CapabilityFactoryConfigurationError(
            "capability factory numeric limits are invalid"
        ) from exc


def write_redacted_evidence_manifest(
    summary: CapabilityFactoryRunSummary,
    target_directory: Path,
) -> Path:
    """Persist only stable IDs/digests in canonical content-addressed JSON."""

    payload = {
        "schema": "captain.capability-factory-evidence-manifest.v1",
        "summary": summary.model_dump(mode="json", by_alias=True),
    }
    content = _canonical_json_bytes(payload)
    digest = hashlib.sha256(content).hexdigest()
    directory = target_directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{digest}.json"
    try:
        with target.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        if target.read_bytes() != content:
            raise CapabilityFactoryInputMutation(
                "content-addressed evidence manifest has conflicting bytes"
            )
    return target


def _load_production_entrypoint(
    config: CapabilityFactoryCliConfig,
    *,
    attestation: _StaticAdapterAttestation,
) -> CapabilityFactoryEntrypoint:
    try:
        current_content = attestation.module_path.read_bytes()
    except OSError as exc:
        raise CapabilityFactoryConfigurationError(
            "production adapter module could not be read"
        ) from exc
    if hashlib.sha256(current_content).hexdigest() != attestation.module_sha256:
        raise CapabilityFactoryConfigurationError(
            "production adapter module changed after static attestation"
        )
    try:
        module_name = "_captain_capability_adapter_" + attestation.module_sha256
        spec = importlib.util.spec_from_file_location(
            module_name,
            attestation.module_path,
        )
        if spec is None or spec.loader is None:
            raise ImportError("adapter module has no file loader")
        module = importlib.util.module_from_spec(spec)
        previous_module = sys.modules.get(module_name)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            if previous_module is None:
                sys.modules.pop(module_name, None)
            else:
                sys.modules[module_name] = previous_module
            raise
        factory = getattr(module, attestation.factory_symbol)
        entrypoint = factory(config)
    except (ImportError, AttributeError, TypeError, ValueError) as exc:
        raise CapabilityFactoryConfigurationError(
            "production capability-factory adapter bundle could not be loaded"
        ) from exc
    if not isinstance(entrypoint, CapabilityFactoryEntrypoint):
        raise CapabilityFactoryConfigurationError(
            "production adapter factory returned an invalid entrypoint"
        )
    return entrypoint


@dataclass(frozen=True)
class _StaticAdapterAttestation:
    module_path: Path
    module_sha256: str
    factory_symbol: str


def _validate_static_adapter_manifest(
    config: CapabilityFactoryCliConfig,
) -> _StaticAdapterAttestation:
    """Verify a side-effect-free, digest-pinned adapter declaration."""

    try:
        content = config.adapter_manifest_path.read_bytes()
    except OSError as exc:
        raise CapabilityFactoryConfigurationError(
            "static adapter manifest could not be read"
        ) from exc
    if len(content) > 65_536:
        raise CapabilityFactoryConfigurationError(
            "static adapter manifest exceeds the control-plane size limit"
        )
    if hashlib.sha256(content).hexdigest() != config.adapter_manifest_sha256:
        raise CapabilityFactoryConfigurationError(
            "static adapter manifest digest does not match"
        )
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CapabilityFactoryConfigurationError(
            "static adapter manifest is not valid JSON"
        ) from exc
    if (
        not isinstance(payload, dict)
        or set(payload)
        != {"schema", "module_path", "module_sha256", "factory_symbol"}
        or payload.get("schema")
        != "captain.capability-factory-entrypoint-adapter-manifest.v1"
        or not isinstance(payload.get("module_path"), str)
        or not payload["module_path"].strip()
        or not isinstance(payload.get("module_sha256"), str)
        or re.fullmatch(
            r"[0-9a-f]{64}",
            payload["module_sha256"],
        )
        is None
        or not isinstance(payload.get("factory_symbol"), str)
        or re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*",
            payload["factory_symbol"],
        )
        is None
    ):
        raise CapabilityFactoryConfigurationError(
            "static adapter manifest contract is invalid"
        )
    module_path = _safe_workspace_path(
        config.workspace_root,
        payload["module_path"],
        "adapter module",
    )
    if module_path.suffix.casefold() != ".py" or not module_path.is_file():
        raise CapabilityFactoryConfigurationError(
            "static adapter module is not a readable Python workspace file"
        )
    try:
        module_content = module_path.read_bytes()
    except OSError as exc:
        raise CapabilityFactoryConfigurationError(
            "static adapter module could not be read"
        ) from exc
    if len(module_content) > 1_048_576:
        raise CapabilityFactoryConfigurationError(
            "static adapter module exceeds the control-plane size limit"
        )
    if hashlib.sha256(module_content).hexdigest() != payload["module_sha256"]:
        raise CapabilityFactoryConfigurationError(
            "static adapter module digest does not match"
        )
    try:
        syntax_tree = ast.parse(
            module_content.decode("utf-8"),
            filename=str(module_path),
        )
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise CapabilityFactoryConfigurationError(
            "static adapter module is not valid UTF-8 Python"
        ) from exc
    matching_symbols = tuple(
        node
        for node in syntax_tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == payload["factory_symbol"]
    )
    if len(matching_symbols) != 1:
        raise CapabilityFactoryConfigurationError(
            "static adapter factory symbol is missing or ambiguous"
        )
    return _StaticAdapterAttestation(
        module_path=module_path,
        module_sha256=payload["module_sha256"],
        factory_symbol=payload["factory_symbol"],
    )


async def run_capability_factory_cli(
    config: CapabilityFactoryCliConfig,
    *,
    environ: Mapping[str, str] | None = None,
) -> Mapping[str, object]:
    """Run the configured production composition and return redacted evidence only."""

    document = load_factory_input(config.input_path)
    FactoryInputCompiler(
        holdout_store=InMemoryPrivateHoldoutStore(),
    ).compile(document, config.subject_version)
    adapter_attestation = _validate_static_adapter_manifest(config)
    if config.preflight_only:
        return {
            "schema": "captain.capability-factory-cli-result.v1",
            "status": "preflight_ok",
            "targets": {
                "gateway": config.gateway_url,
                "runtime": config.runtime_url,
                "minibook": config.minibook_url,
            },
        }
    entrypoint = _load_production_entrypoint(
        config,
        attestation=adapter_attestation,
    )
    started_at = datetime.now(timezone.utc)
    started = time.perf_counter()
    summary = await entrypoint.run(
        input_path=config.input_path,
        correlation_id=config.correlation_id,
        subject_version=config.subject_version,
        wall_clock_budget_seconds=config.wall_clock_budget_seconds,
    )
    manifest_path = write_redacted_evidence_manifest(summary, config.artifact_dir)
    finished_at = datetime.now(timezone.utc)
    return {
        "schema": "captain.capability-factory-cli-result.v1",
        "status": summary.terminal_state.value,
        "summary": summary.model_dump(mode="json", by_alias=True),
        "manifest": str(manifest_path.relative_to(config.workspace_root)),
        "targets": {
            "gateway": config.gateway_url,
            "runtime": config.runtime_url,
            "minibook": config.minibook_url,
        },
        "timings": {
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "duration_seconds": round(time.perf_counter() - started, 6),
        },
        "digests": {
            "input_sha256": hashlib.sha256(config.input_path.read_bytes()).hexdigest(),
            "manifest_sha256": manifest_path.stem,
        },
    }


def main(arguments: tuple[str, ...] | None = None) -> int:
    try:
        config = parse_capability_factory_args(
            tuple(sys.argv[1:] if arguments is None else arguments)
        )
        result = asyncio.run(run_capability_factory_cli(config))
    except CapabilityFactoryConfigurationError as exc:
        sys.stderr.write(f"capability factory blocked: {exc}\n")
        return 2
    except Exception:
        sys.stderr.write(
            "capability factory failed closed; inspect redacted service logs for details\n"
        )
        return 1
    sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n")
    return 0


def _safe_workspace_path(root: Path, raw: str, label: str) -> Path:
    candidate = Path(raw)
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if not resolved.is_relative_to(root):
        raise CapabilityFactoryConfigurationError(
            f"{label} path must remain inside the workspace"
        )
    return resolved


def _safe_service_url(raw: str, label: str) -> str:
    parsed = urlsplit(raw)
    if (
        any(character.isspace() for character in raw)
        or raw != raw.strip()
        or parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise CapabilityFactoryConfigurationError(
            f"{label} URL must be an HTTP service URL without credentials or query data"
        )
    return raw.rstrip("/")


def _canonical_json_bytes(value: BaseModel | Mapping[str, object]) -> bytes:
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


def _require_same_checkpoint(
    existing: CapabilityFactoryCheckpoint,
    proposed: CapabilityFactoryCheckpoint,
) -> CapabilityFactoryCheckpoint:
    if existing == proposed:
        return existing
    if existing.input_sha256 != proposed.input_sha256:
        raise CapabilityFactoryInputMutation(
            "factory input bytes changed for the existing correlation"
        )
    if (
        existing.occurred_at != proposed.occurred_at
        or existing.deadline_at != proposed.deadline_at
    ):
        raise CapabilityFactoryInputMutation(
            "factory job timing changed for the existing correlation"
        )
    raise CapabilityFactoryInputMutation(
        "factory job identity changed for the existing correlation"
    )


def _workspace_tree_sha256(workspace: Path) -> str:
    entries: list[tuple[str, str, int]] = []
    for path in workspace.rglob("*"):
        if path.is_symlink():
            raise CapabilitySandboxIsolationError(
                "sandbox workspace contains a symbolic link"
            )
        if path.is_file():
            content = path.read_bytes()
            entries.append(
                (
                    path.relative_to(workspace).as_posix(),
                    hashlib.sha256(content).hexdigest(),
                    len(content),
                )
            )
    return hashlib.sha256(
        json.dumps(sorted(entries), separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _single_inspect_document(content: str) -> Mapping[str, object]:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise CapabilitySandboxIsolationError(
            "Docker inspection did not return valid JSON"
        ) from exc
    if (
        not isinstance(payload, list)
        or len(payload) != 1
        or not isinstance(payload[0], dict)
    ):
        raise CapabilitySandboxIsolationError(
            "Docker inspection did not identify exactly one object"
        )
    return payload[0]


def _mapping(parent: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise CapabilitySandboxIsolationError(
            f"Docker inspection omitted required {key} isolation data"
        )
    return value


class CapabilityFactoryEntrypoint:
    """Compose one deterministic Captain-owned create/validate/release chain."""

    def __init__(
        self,
        *,
        checkpoint_store: CapabilityFactoryCheckpointStore,
        holdout_store: PrivateHoldoutStore,
        repository: FactoryRepository,
        catalog: CapabilityAuthorityCatalogPort,
        released_skill: ReleasedSkillRefV1,
        creation: CapabilityCreationPort,
        content_store: ReadOnlyCapabilityContentStore,
        sandbox_runner: TrustedCapabilitySandboxRunner,
        evidence_issuer: CaptainEvidenceIssuerPort,
        gateway: CapabilityFactoryGatewayPort,
        runtime: CapabilityRuntimePort,
        projector: MinibookProjector,
        clock: CapabilityFactoryClock,
        creation_analysis: HermesCreationAnalysisPort | None = None,
    ) -> None:
        self._checkpoint_store = checkpoint_store
        self._holdout_store = holdout_store
        self._repository = repository
        self._catalog = catalog
        self._released_skill = released_skill
        self._creation = creation
        self._content_store = content_store
        self._sandbox_runner = sandbox_runner
        self._evidence_issuer = evidence_issuer
        self._gateway = gateway
        self._runtime = runtime
        self._projector = projector
        self._clock = clock
        self._creation_analysis = creation_analysis

    async def run(
        self,
        *,
        input_path: Path,
        correlation_id: UUID,
        subject_version: int,
        wall_clock_budget_seconds: int,
    ) -> CapabilityFactoryRunSummary:
        document = load_factory_input(input_path)
        compiled = FactoryInputCompiler(holdout_store=self._holdout_store).compile(
            document,
            subject_version,
        )
        checkpoint = self._checkpoint_store.load(correlation_id)
        registration_time = self._clock.now()
        if checkpoint is not None:
            stored_budget_seconds = (
                checkpoint.deadline_at - checkpoint.occurred_at
            ).total_seconds()
            if (
                checkpoint.subject_version != subject_version
                or not stored_budget_seconds.is_integer()
                or int(stored_budget_seconds) != wall_clock_budget_seconds
            ):
                raise CapabilityFactoryInputMutation(
                    "factory job identity changed for the existing correlation"
                )
            registration_time = checkpoint.occurred_at
        proposed_job = build_factory_job(
            compiled,
            correlation_id=correlation_id,
            now=registration_time,
            wall_clock_budget_seconds=wall_clock_budget_seconds,
        )
        job = self._resume_job(proposed_job)
        self._checkpoint_store.bind(
            CapabilityFactoryCheckpoint(
                correlation_id=job.correlation_id,
                factory_job_id=job.job_id,
                subject_version=job.subject_version,
                input_sha256=job.input_ref.sha256,
                occurred_at=job.occurred_at,
                deadline_at=job.deadline_at,
            )
        )
        coordinator = FactoryCoordinator(self._repository, clock=self._clock)
        coordinator.register(job)
        authority = self._catalog.compatible_record(job)
        if authority is not None:
            _require_catalog_authority(job, authority)
            execution = await self._execute_authority(job, authority)
            return _reuse_summary(
                job,
                authority,
                execution=execution,
            )

        resolution = CapabilityResolver(self._catalog).resolve(job)
        if resolution.kind != "create" or resolution.creation_key is None:
            raise RuntimeError("released capability reuse execution is not configured")

        creation_job = self._build_creation_job(
            job,
            compiled=compiled,
            creation_key=resolution.creation_key,
            released_skill=self._released_skill,
        )
        if self._creation_analysis is not None:
            await self._creation_analysis.analyze(job, creation_job)
        coordinator.record(_captain_forge_requested(job))
        receipt = await self._creation.submit(creation_job)
        if (
            receipt.creation_job_id != creation_job.creation_job_id
            or receipt.subject_version != job.subject_version
        ):
            raise ValueError("creation submission receipt does not match the factory job")
        preparation = await self._creation.preparation_blocks(job, creation_job)
        if len(preparation) != 2:
            raise ValueError("creation preparation must return blueprint and tool evidence")
        for block in preparation:
            coordinator.record(block)

        creation_result = await self._creation.result(creation_job.creation_job_id)
        _require_creation_result(job, creation_job, creation_result)
        if self._clock.now() >= job.deadline_at:
            return self._seal_nonready(
                coordinator,
                job,
                creation_job.creation_job_id,
                validation=None,
                evaluation=coordinator.evaluation_for_job(job.job_id),
                evidence=(),
            )
        coordinator.record(await self._creation.completion_block(job, creation_result))

        candidate = await self._read_candidate(creation_result)
        run_receipts: list[CapabilityReleaseRunReceipt] = []
        for run_number in range(1, 5):
            if self._clock.now() >= job.deadline_at:
                return self._seal_nonready(
                    coordinator,
                    job,
                    creation_job.creation_job_id,
                    validation=None,
                    evaluation=coordinator.evaluation_for_job(job.job_id),
                    evidence=tuple(item.record for item in run_receipts),
                )
            run = await self._evidence_issuer.run(
                job,
                creation_result,
                candidate,
                run_number,
            )
            if run is None:
                return self._seal_nonready(
                    coordinator,
                    job,
                    creation_job.creation_job_id,
                    validation=None,
                    evaluation=coordinator.evaluation_for_job(job.job_id),
                    evidence=tuple(item.record for item in run_receipts),
                )
            _require_captain_release_receipt(
                job,
                creation_result,
                candidate,
                run,
                run_number,
            )
            run_receipts.append(run)

        if self._clock.now() >= job.deadline_at:
            return self._seal_nonready(
                coordinator,
                job,
                creation_job.creation_job_id,
                validation=None,
                evaluation=coordinator.evaluation_for_job(job.job_id),
                evidence=tuple(item.record for item in run_receipts),
            )

        evidence = tuple(item.record for item in run_receipts)
        try:
            package = await CapabilityPackageValidator(
                content_store=self._content_store,
                sandbox_runner=self._sandbox_runner,
            ).validate(
                job=job,
                creation_result=creation_result,
                candidate=candidate,
                release_evidence_refs=tuple(item.reference for item in run_receipts),
            )
        except CapabilityPackageValidationError:
            failure = CapabilityValidationFailure(
                reason_code=FactoryTerminalReasonCode.DIGEST_VIOLATION,
                evidence_ref=_fixed_evidence_ref(
                    "capability-package-validation-failed",
                    "validation-failure",
                ),
            )
            return self._seal_nonready(
                coordinator,
                job,
                creation_job.creation_job_id,
                validation=failure,
                evaluation=coordinator.evaluation_for_job(job.job_id),
                evidence=evidence,
            )

        if self._clock.now() >= job.deadline_at:
            return self._seal_nonready(
                coordinator,
                job,
                creation_job.creation_job_id,
                validation=package,
                evaluation=coordinator.evaluation_for_job(job.job_id),
                evidence=evidence,
            )

        evaluation = coordinator.evaluation_for_job(job.job_id)
        provisional = derive_terminal_decision(
            job,
            coordinator.projection(job.job_id),
            package,
            evaluation,
            evidence,
            self._clock.now(),
        )
        if provisional is None or provisional.state is not FactoryTerminalState.READY_TO_USE:
            return self._seal_nonready(
                coordinator,
                job,
                creation_job.creation_job_id,
                validation=package,
                evaluation=evaluation,
                evidence=evidence,
            )

        if self._clock.now() >= job.deadline_at:
            return self._seal_nonready(
                coordinator,
                job,
                creation_job.creation_job_id,
                validation=package,
                evaluation=evaluation,
                evidence=evidence,
            )
        lifecycle_blocks = await self._evidence_issuer.lifecycle_blocks(
            job,
            tuple(run_receipts),
        )
        for block in lifecycle_blocks:
            if self._clock.now() >= job.deadline_at:
                return self._seal_nonready(
                    coordinator,
                    job,
                    creation_job.creation_job_id,
                    validation=package,
                    evaluation=evaluation,
                    evidence=evidence,
                )
            coordinator.record(block)
        old_evidence = tuple(_legacy_e2e(item) for item in run_receipts)
        release_decision = evaluate_factory_release(job, old_evidence, evaluation)
        if self._clock.now() >= job.deadline_at:
            return self._seal_nonready(
                coordinator,
                job,
                creation_job.creation_job_id,
                validation=package,
                evaluation=evaluation,
                evidence=evidence,
            )
        self._gateway.record_factory_release_decision(
            FactoryReleaseDecisionSubmission(
                decision=release_decision,
                e2e_evidence=old_evidence,
            )
        )
        promotion = _promotion_block(job, package, evaluation, self._clock.now())
        if self._clock.now() >= job.deadline_at:
            return self._seal_nonready(
                coordinator,
                job,
                creation_job.creation_job_id,
                validation=package,
                evaluation=evaluation,
                evidence=evidence,
            )
        coordinator.record(promotion)
        terminal = derive_terminal_decision(
            job,
            coordinator.projection(job.job_id),
            package,
            evaluation,
            evidence,
            self._clock.now(),
        )
        if terminal is None or terminal.state is not FactoryTerminalState.READY_TO_USE:
            raise RuntimeError("ready_to_use was not derivable after promotion")

        release = _release_request(job, terminal, package, evidence, promotion)
        if self._clock.now() >= job.deadline_at:
            return self._seal_nonready(
                coordinator,
                job,
                creation_job.creation_job_id,
                validation=package,
                evaluation=evaluation,
                evidence=evidence,
            )
        self._gateway.publish_capability_release(release)
        self._require_effect_budget(job, "post-publication runtime continuation")
        terminal = self._gateway.factory_terminal_decision(job.job_id)
        authority = self._gateway.capability(
            package.capability_id,
            version=package.capability_version,
        )
        if (
            terminal is None
            or terminal.model_copy(update={"decided_at": release.decision.decided_at})
            != release.decision
        ):
            raise RuntimeError("Gateway terminal readback disagrees with atomic publication")
        if (
            authority is None
            or authority.terminal_decision_id != terminal.decision_id
            or authority.package_ref != release.package_ref
            or authority.promoted_capability != release.promoted_capability
            or authority.status != "ready_to_use"
        ):
            raise RuntimeError("Gateway catalog readback disagrees with atomic publication")
        _require_catalog_authority(job, authority)
        execution = await self._execute_authority(job, authority)
        return _summary(
            job,
            terminal,
            creation_job_id=creation_job.creation_job_id,
            package=package,
            evidence=evidence,
            execution=execution,
        )

    async def _execute_authority(
        self,
        job: AgentFactoryJobV2,
        authority: CapabilityCatalogRecord,
    ) -> CapabilityExecutionCompleted | CapabilityExecutionRetryPending:
        self._require_effect_budget(job, "runtime plan preparation")
        plan = await self._runtime.prepare(job, authority)
        if (
            plan.command.correlation_id != job.correlation_id
            or plan.grant.command_id != plan.command.event_id
            or plan.grant.batch_id != plan.command.payload.batch_id
            or plan.grant.subtask_id != plan.command.payload.subtask_id
            or plan.grant.workspace_ref != plan.command.payload.workspace_ref
            or plan.grant.profile != plan.command.payload.capability_profile
        ):
            raise ValueError("runtime execution plan does not match the factory invocation")

        if not self._runtime.guarantees_durable_idempotency(plan, authority):
            raise CapabilityProviderIdempotencyError(
                "runtime provider does not guarantee durable idempotency"
            )
        self._require_effect_budget(job, "provider effect lookup")
        provider_effect_id = uuid5(
            plan.command.event_id,
            "durable-provider-effect",
        )
        provider_execution = await self._runtime.lookup_effect(
            command_id=plan.command.event_id,
            effect_id=provider_effect_id,
        )
        if provider_execution is not None:
            self._validate_provider_execution(plan, provider_execution)

        operation = self._gateway.find_runtime_operation(plan.command.event_id)
        existing = self._gateway.capability_execution(plan.command.event_id)
        if operation is not None:
            if operation.command != plan.command:
                raise RuntimeError("Gateway runtime command disagrees with the execution plan")
            if operation.grant is not None and operation.grant != plan.grant:
                raise RuntimeError("Gateway runtime grant disagrees with the execution plan")
            if operation.revocation is not None:
                raise RuntimeError("Gateway runtime grant was revoked before recovery")

        if existing is not None:
            claim = self._gateway.runtime_execution_claim(plan.command.event_id)
            if (
                provider_execution is None
                or operation is None
                or operation.result is None
                or claim is None
                or claim.status != "completed"
                or existing.outcome.result_id != operation.result.event_id
                or existing.claim_owner_id != claim.owner_id
                or existing.claim_fencing_token != claim.fencing_token
                or provider_execution.outcome != existing.outcome
            ):
                raise RuntimeError("Gateway execution readback is incomplete")
            self._validate_gateway_result_readback(
                provider_execution,
                operation.result,
                claim,
            )
            bundle = CapabilityExecutionBundle(
                command=operation.command,
                grant=operation.grant or plan.grant,
                result=operation.result,
                outcome=existing.outcome,
                claim_owner_id=existing.claim_owner_id,
                claim_fencing_token=existing.claim_fencing_token,
            )
            return self._project_execution(job, authority, bundle)

        if operation is not None and operation.result is not None:
            claim = self._gateway.runtime_execution_claim(plan.command.event_id)
            if (
                claim is None
                or claim.status != "completed"
                or provider_execution is None
            ):
                raise RuntimeError("Gateway runtime result has no completed execution claim")
            self._validate_gateway_result_readback(
                provider_execution,
                operation.result,
                claim,
            )
            bundle = CapabilityExecutionBundle(
                command=operation.command,
                grant=operation.grant or plan.grant,
                result=operation.result,
                outcome=provider_execution.outcome,
                claim_owner_id=claim.owner_id,
                claim_fencing_token=claim.fencing_token,
            )
            self._validate_execution_bundle(job, authority, bundle)
            self._record_execution(job, bundle)
            return self._project_execution(job, authority, bundle)

        claim = (
            self._gateway.runtime_execution_claim(plan.command.event_id)
            if operation is not None
            else None
        )
        if provider_execution is not None and claim is None:
            raise CapabilityProviderIdempotencyError(
                "durable provider effect has no Gateway execution claim"
            )
        if claim is not None:
            if claim.status == "completed":
                raise RuntimeError("completed runtime claim is missing its result")
            if claim.expires_at > self._clock.now():
                return CapabilityExecutionRetryPending(
                    command_id=plan.command.event_id,
                    expires_at=claim.expires_at,
                )
        if operation is None:
            self._require_effect_budget(job, "runtime command admission")
            self._gateway.accept_runtime_command(plan.command)
        if operation is None or operation.grant is None:
            self._require_effect_budget(job, "runtime grant recording")
            self._gateway.record_capability_grant(plan.grant)

        self._require_effect_budget(job, "runtime execution claim")
        try:
            claim_receipt = self._gateway.claim_runtime_execution(
                RuntimeExecutionClaimRequest(
                    schema_name="captain.runtime-execution-claim-request.v1",
                    command_id=plan.command.event_id,
                    owner_id=plan.claim_owner_id,
                    lease_seconds=min(plan.command.payload.limits.wall_seconds, 900),
                    capability_id=authority.capability_id,
                    capability_version=authority.capability_version,
                )
            )
        except Exception:
            # Store and HTTP adapters expose different conflict types. Treat
            # one as a completion race only after exact authoritative readback.
            if provider_execution is None:
                raise
            raced_bundle = self._completed_gateway_bundle(
                plan,
                provider_execution,
            )
            if raced_bundle is None:
                raise
            self._validate_execution_bundle(job, authority, raced_bundle)
            self._record_execution(job, raced_bundle)
            return self._project_execution(job, authority, raced_bundle)
        if claim_receipt.claim_credential is None:
            if (
                claim_receipt.claim.status == "active"
                and claim_receipt.claim.expires_at > self._clock.now()
            ):
                return CapabilityExecutionRetryPending(
                    command_id=plan.command.event_id,
                    expires_at=claim_receipt.claim.expires_at,
                )
            raise RuntimeError("Gateway returned an unusable runtime execution claim")

        self._require_effect_budget(job, "provider execution")
        runtime = provider_execution
        if runtime is None:
            runtime = await self._runtime.execute(
                plan,
                authority,
                claim_receipt,
                effect_id=provider_effect_id,
            )
        self._validate_provider_execution(
            plan,
            runtime,
            effect_claim=(
                claim_receipt.claim if provider_execution is None else None
            ),
        )
        bundle = CapabilityExecutionBundle(
            command=plan.command,
            grant=plan.grant,
            result=runtime.result,
            outcome=runtime.outcome,
            claim_owner_id=claim_receipt.claim.owner_id,
            claim_fencing_token=claim_receipt.claim.fencing_token,
        )
        self._validate_execution_bundle(job, authority, bundle)
        self._require_effect_budget(job, "runtime result recording")
        if provider_execution is not None:
            recovery = self._runtime_result_recovery_request(
                runtime,
                recovery_claim=claim_receipt.claim,
            )
            self._gateway.recover_runtime_result(
                recovery,
                execution_owner_id=claim_receipt.claim.owner_id,
                execution_fencing_token=claim_receipt.claim.fencing_token,
                execution_claim_credential=claim_receipt.claim_credential,
            )
        else:
            self._gateway.record_runtime_result(
                bundle.result,
                execution_owner_id=claim_receipt.claim.owner_id,
                execution_fencing_token=claim_receipt.claim.fencing_token,
                execution_claim_credential=claim_receipt.claim_credential,
            )
        readback_bundle = self._completed_gateway_bundle(plan, runtime)
        if readback_bundle is None:
            raise RuntimeError("Gateway runtime result readback is incomplete")
        bundle = readback_bundle
        self._validate_execution_bundle(job, authority, bundle)
        self._record_execution(job, bundle)
        return self._project_execution(job, authority, bundle)

    def _validate_provider_execution(
        self,
        plan: CapabilityExecutionPlan,
        runtime: CapabilityRuntimeExecution,
        *,
        effect_claim: RuntimeExecutionClaim | None = None,
    ) -> None:
        receipt = runtime.provider_receipt
        expected_effect_id = uuid5(
            plan.command.event_id,
            "durable-provider-effect",
        )
        if (
            not receipt.idempotency_guaranteed
            or receipt.effect_id != expected_effect_id
            or receipt.command_id != plan.command.event_id
            or receipt.request_digest != canonical_contract_sha256(plan)
            or receipt.result_digest != canonical_contract_sha256(runtime.result)
            or receipt.status != runtime.result.status.value
            or runtime.result.command_id != plan.command.event_id
        ):
            raise CapabilityProviderIdempotencyError(
                "runtime provider receipt does not prove durable idempotency"
            )
        if effect_claim is not None and (
            receipt.origin_claim_id != effect_claim.claim_id
            or receipt.origin_claim_fencing_token != effect_claim.fencing_token
            or receipt.origin_claim_digest
            != canonical_contract_sha256(effect_claim)
        ):
            raise CapabilityProviderIdempotencyError(
                "runtime provider receipt does not bind its execution claim"
            )

    @staticmethod
    def _runtime_result_recovery_request(
        runtime: CapabilityRuntimeExecution,
        *,
        recovery_claim: RuntimeExecutionClaim,
    ) -> RuntimeResultRecoveryRequest:
        result = runtime.result
        receipt = runtime.provider_receipt
        observation = RuntimeResultRecoveryObservation(
            schema_name="captain.runtime-result-recovery-observation.v1",
            event_id=uuid5(
                result.event_id,
                (
                    "runtime-result-recovery:"
                    f"{receipt.origin_claim_fencing_token}:"
                    f"{recovery_claim.fencing_token}"
                ),
            ),
            observed_at=recovery_claim.claimed_at,
            command_id=result.command_id,
            original_result_id=result.event_id,
            original_result_digest=canonical_contract_sha256(result),
            original_claim_id=receipt.origin_claim_id,
            original_claim_digest=receipt.origin_claim_digest,
            provider_effect_id=receipt.effect_id,
            provider_receipt_digest=canonical_contract_sha256(receipt),
            original_claim_fence=receipt.origin_claim_fencing_token,
            recovery_claim_fence=recovery_claim.fencing_token,
            correlation_id=result.correlation_id,
            causation_id=result.event_id,
        )
        return RuntimeResultRecoveryRequest(
            schema_name="captain.runtime-result-recovery-request.v1",
            result=result,
            provider_receipt=receipt,
            observation=observation,
        )

    def _validate_gateway_result_readback(
        self,
        runtime: CapabilityRuntimeExecution,
        observed: AgentRuntimeResult,
        claim: RuntimeExecutionClaim,
    ) -> None:
        if observed != runtime.result:
            raise CapabilityProviderIdempotencyError(
                "Gateway changed the immutable provider runtime result"
            )
        recovery = self._gateway.runtime_result_recovery(observed.command_id)
        if recovery is None:
            origin_claim = claim.model_copy(
                update={"status": "active", "completed_at": None}
            )
            receipt = runtime.provider_receipt
            if (
                receipt.origin_claim_id != origin_claim.claim_id
                or receipt.origin_claim_fencing_token
                != origin_claim.fencing_token
                or receipt.origin_claim_digest
                != canonical_contract_sha256(origin_claim)
            ):
                raise CapabilityProviderIdempotencyError(
                    "Gateway runtime result receipt does not bind its execution claim"
                )
            if not origin_claim.claimed_at <= observed.occurred_at < origin_claim.expires_at:
                raise CapabilityProviderIdempotencyError(
                    "Gateway runtime result is outside its execution claim"
                )
            return
        receipt = runtime.provider_receipt
        if (
            recovery.event_id == observed.event_id
            or recovery.command_id != observed.command_id
            or recovery.original_result_id != observed.event_id
            or recovery.original_result_digest != canonical_contract_sha256(observed)
            or recovery.original_claim_id != receipt.origin_claim_id
            or recovery.original_claim_digest != receipt.origin_claim_digest
            or recovery.provider_effect_id != receipt.effect_id
            or recovery.provider_receipt_digest != canonical_contract_sha256(receipt)
            or recovery.recovery_claim_fence != claim.fencing_token
            or recovery.original_claim_fence
            != receipt.origin_claim_fencing_token
            or recovery.original_claim_fence >= recovery.recovery_claim_fence
            or recovery.correlation_id != observed.correlation_id
            or recovery.causation_id != observed.event_id
            or not claim.claimed_at <= recovery.observed_at < claim.expires_at
        ):
            raise CapabilityProviderIdempotencyError(
                "Gateway recovery observation does not bind the immutable provider result"
            )

    def _completed_gateway_bundle(
        self,
        plan: CapabilityExecutionPlan,
        runtime: CapabilityRuntimeExecution,
    ) -> CapabilityExecutionBundle | None:
        operation = self._gateway.find_runtime_operation(plan.command.event_id)
        claim = self._gateway.runtime_execution_claim(plan.command.event_id)
        if (
            operation is None
            or operation.result is None
            or claim is None
            or claim.status != "completed"
        ):
            return None
        if (
            operation.command != plan.command
            or operation.grant is None
            or operation.grant != plan.grant
            or operation.revocation is not None
        ):
            raise RuntimeError("Gateway runtime readback disagrees with the execution plan")
        self._validate_gateway_result_readback(runtime, operation.result, claim)
        return CapabilityExecutionBundle(
            command=operation.command,
            grant=operation.grant,
            result=operation.result,
            outcome=runtime.outcome,
            claim_owner_id=claim.owner_id,
            claim_fencing_token=claim.fencing_token,
        )

    def _validate_execution_bundle(
        self,
        job: AgentFactoryJobV2,
        authority: CapabilityCatalogRecord,
        bundle: CapabilityExecutionBundle,
    ) -> None:
        validate_execution_outcome_binding(
            bundle.outcome,
            command=bundle.command,
            result=bundle.result,
            expected_capability_id=authority.capability_id,
            expected_capability_version=authority.capability_version,
            expected_team_version=authority.team_version,
        )
        if bundle.command.correlation_id != job.correlation_id:
            raise ValueError("runtime command correlation does not match factory invocation")

    def _record_execution(
        self,
        job: AgentFactoryJobV2,
        bundle: CapabilityExecutionBundle,
    ) -> None:
        self._require_effect_budget(job, "capability execution recording")
        execution = CapabilityExecutionRequest(
            schema_name="captain.capability-execution-request.v1",
            event_id=uuid5(bundle.command.event_id, "capability-execution-record"),
            causation_id=bundle.result.event_id,
            occurred_at=bundle.result.occurred_at,
            producer="captain",
            outcome=bundle.outcome,
            outcome_ref=_canonical_ref(bundle.outcome, "execution-outcome"),
            claim_owner_id=bundle.claim_owner_id,
            claim_fencing_token=bundle.claim_fencing_token,
        )
        self._gateway.record_capability_execution(execution)

    def _project_execution(
        self,
        job: AgentFactoryJobV2,
        authority: CapabilityCatalogRecord,
        bundle: CapabilityExecutionBundle,
    ) -> CapabilityExecutionCompleted:
        self._validate_execution_bundle(job, authority, bundle)
        self._require_effect_budget(job, "Minibook projection")
        projection_events = tuple(
            event
            for event in self._gateway.projection_events(job.correlation_id)
            if (
                event.event_id == bundle.result.event_id
                and event.correlation_id == job.correlation_id
                and event.causation_id == bundle.command.event_id
                and event.event_type == "codex.result"
                and event.payload.status_id == "built"
            )
        )
        if len(projection_events) != 1:
            raise RuntimeError("Gateway projection feed lacks the exact successful result event")
        projection_results = tuple(self._projector.rebuild(projection_events))
        expected_event_ids = tuple(str(event.event_id) for event in projection_events)
        if (
            len(projection_results) != len(projection_events)
            or tuple(result.event_id for result in projection_results)
            != expected_event_ids
            or any(
                result.outcome not in {"projected", "duplicate"}
                for result in projection_results
            )
        ):
            raise RuntimeError("Minibook projection rebuild did not commit every event")
        return CapabilityExecutionCompleted(
            bundle=bundle,
            projection_events=projection_events,
            minibook_projection_verified=True,
        )

    def _require_effect_budget(self, job: AgentFactoryJobV2, effect: str) -> None:
        if self._clock.now() >= job.deadline_at:
            raise CapabilityFactoryDeadlineExceeded(
                f"factory deadline expired before {effect}"
            )

    def _resume_job(self, proposed: AgentFactoryJobV2) -> AgentFactoryJobV2:
        try:
            stored = self._repository.job(proposed.job_id)
        except (FactoryRepositoryError, KeyError):
            return proposed
        if not isinstance(stored, AgentFactoryJobV2):
            raise CapabilityFactoryInputMutation("factory job schema changed on resume")
        if (
            stored.correlation_id != proposed.correlation_id
            or stored.subject_version != proposed.subject_version
            or stored.input_ref != proposed.input_ref
            or stored.compiled_spec_ref != proposed.compiled_spec_ref
            or stored.dependency_graph_ref != proposed.dependency_graph_ref
        ):
            raise CapabilityFactoryInputMutation("factory input bytes changed on resume")
        return stored

    async def _read_candidate(
        self,
        result: CreationResultV1,
    ) -> ForgeCapabilityPackageCandidateV1:
        reference = result.package_manifest_ref
        if reference is None:
            raise ValueError("creation result is missing its package manifest")
        runtime_reference = ArtifactRef.model_validate(reference.model_dump(mode="json"))
        content = await self._content_store.read(runtime_reference)
        if hashlib.sha256(content).hexdigest() != runtime_reference.sha256:
            raise CapabilityPackageValidationError("candidate manifest digest mismatch")
        return ForgeCapabilityPackageCandidateV1.model_validate_json(content)

    def _seal_nonready(
        self,
        coordinator: FactoryCoordinator,
        job: AgentFactoryJobV2,
        creation_job_id: UUID,
        *,
        validation: CapabilityPackageManifestV1 | CapabilityValidationFailure | None,
        evaluation: StoredSkillEvaluation | None,
        evidence: tuple[CapabilityReleaseEvidenceV1, ...],
    ) -> CapabilityFactoryRunSummary:
        coordinator.record_terminal_decision(
            job.job_id,
            validation=validation,
            evaluation=evaluation,
            e2e=evidence,
        )
        terminal = coordinator.terminal_decision_for_job(job.job_id)
        if terminal is None:
            raise RuntimeError("Gateway did not persist the terminal decision")
        package = validation if isinstance(validation, CapabilityPackageManifestV1) else None
        return _summary(
            job,
            terminal,
            creation_job_id=creation_job_id,
            package=package,
            evidence=evidence,
            execution=None,
        )

    def _build_creation_job(
        self,
        job: AgentFactoryJobV2,
        *,
        compiled: CompiledFactorySpecification,
        creation_key: str,
        released_skill: ReleasedSkillRefV1,
    ) -> CreationJobV1:
        del compiled
        return _creation_job(
            job,
            creation_key=creation_key,
            released_skill=released_skill,
        )


def _creation_job(
    job: AgentFactoryJobV2,
    *,
    creation_key: str,
    released_skill: ReleasedSkillRefV1,
) -> CreationJobV1:
    idempotency_key = hashlib.sha256(
        f"{job.job_id}:{creation_key}:1".encode("utf-8")
    ).hexdigest()
    return CreationJobV1.model_validate(
        {
            "schema": "minibook.creation-job.v1",
            "creation_job_id": uuid5(job.job_id, f"creation:{creation_key}:1"),
            "factory_job_id": job.job_id,
            "correlation_id": job.correlation_id,
            "causation_id": job.event_id,
            "subject_version": job.subject_version,
            "attempt": 1,
            "idempotency_key": idempotency_key,
            "input_ref": job.input_ref.model_dump(mode="json"),
            "compiled_spec_ref": job.compiled_spec_ref.model_dump(mode="json"),
            "dependency_graph_ref": job.dependency_graph_ref.model_dump(mode="json"),
            "released_skill": released_skill.model_dump(mode="json"),
            "public_assertion_ids": job.acceptance_assertion_ids,
            "deadline_at": job.deadline_at,
        }
    )


def _captain_forge_requested(job: AgentFactoryJobV2) -> FactoryEvidenceBlock:
    return FactoryEvidenceBlock(
        schema_name="captain.agent-factory-block.v1",
        event_id=uuid5(job.event_id, "factory-stage:forge_requested:1"),
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        causation_id=job.event_id,
        occurred_at=job.occurred_at,
        producer="captain",
        subject_version=job.subject_version,
        attempt=1,
        phase=FactoryPhase.FORGE_REQUESTED,
        status=FactoryBlockStatus.SUCCEEDED,
        evidence_refs=(_fixed_evidence_ref(str(job.job_id), "forge-requested"),),
    )


def _require_creation_result(
    job: AgentFactoryJobV2,
    creation_job: CreationJobV1,
    result: CreationResultV1,
) -> None:
    if (
        result.creation_job_id != creation_job.creation_job_id
        or result.correlation_id != job.correlation_id
        or result.subject_version != job.subject_version
        or result.attempt != creation_job.attempt
    ):
        raise ValueError("creation result does not match the immutable factory job")


def _require_captain_release_receipt(
    job: AgentFactoryJobV2,
    creation_result: CreationResultV1,
    candidate: ForgeCapabilityPackageCandidateV1,
    receipt: CapabilityReleaseRunReceipt,
    run_number: int,
) -> None:
    record = receipt.record
    expected_kind = "recovery" if run_number == 1 else "normal"
    expected_outcome = "expected_failure_recovered" if run_number == 1 else "succeeded"
    if (
        record.producer != "captain"
        or record.run_number != run_number
        or record.factory_job_id != job.job_id
        or record.creation_job_id != creation_result.creation_job_id
        or record.correlation_id != job.correlation_id
        or record.subject_version != job.subject_version
        or record.capability_id != job.required_capability
        or record.capability_version != candidate.capability_version
        or record.candidate_manifest_sha256
        != creation_result.package_manifest_ref.sha256
        or record.package_archive_sha256 != candidate.source_ref.sha256
        or record.kind != expected_kind
        or record.outcome != expected_outcome
    ):
        raise ValueError(
            "Captain Evidence Issuer returned a foreign or non-canonical release record"
        )


def _legacy_e2e(receipt: CapabilityReleaseRunReceipt) -> E2ERunEvidence:
    record = receipt.record
    return E2ERunEvidence(
        run_number=record.run_number,
        correlation_id=record.correlation_id,
        kind=E2EKind.RECOVERY if record.kind == "recovery" else E2EKind.NORMAL,
        outcome=(
            E2EOutcome.EXPECTED_FAILURE
            if record.kind == "recovery"
            else E2EOutcome.SUCCEEDED
        ),
        evidence_ref=receipt.reference,
    )


def _promotion_block(
    job: AgentFactoryJobV2,
    package: CapabilityPackageManifestV1,
    evaluation: StoredSkillEvaluation | None,
    occurred_at: datetime,
) -> FactoryEvidenceBlock:
    if evaluation is None:
        raise RuntimeError("capability promotion requires accepted evaluation evidence")
    return FactoryEvidenceBlock(
        schema_name="captain.agent-factory-block.v1",
        event_id=uuid5(job.event_id, "factory-stage:capability_promoted:1"),
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        causation_id=job.event_id,
        occurred_at=occurred_at,
        producer="captain",
        subject_version=job.subject_version,
        attempt=1,
        phase=FactoryPhase.CAPABILITY_PROMOTED,
        status=FactoryBlockStatus.SUCCEEDED,
        artifact_refs=(package.source_ref,),
        evidence_refs=(*package.release_evidence_refs, evaluation.evidence_ref),
        assertion_ids=tuple(item.assertion_id for item in package.assertion_outcomes),
    )


def _release_request(
    job: AgentFactoryJobV2,
    terminal: FactoryTerminalDecision,
    package: CapabilityPackageManifestV1,
    evidence: tuple[CapabilityReleaseEvidenceV1, ...],
    promotion: FactoryEvidenceBlock,
) -> CapabilityReleaseRequest:
    code_ref = next(
        item.reference for item in package.artifacts if item.kind == "autogen_source"
    )
    tool_refs = tuple(
        item.reference
        for item in package.artifacts
        if item.kind in {"n8n_workflow", "local_adapter"}
    )
    promotion_ref = ArtifactRef(
        uri=f"artifact://gateway/factory-promotion/{promotion.event_id}",
        sha256=canonical_contract_sha256(promotion),
        media_type="application/json",
    )
    intents = tuple(
        sorted(
            {
                item.integration_intent
                for item in package.assertion_outcomes
                if item.integration_intent.value != "none"
            },
            key=lambda item: item.value,
        )
    )
    return CapabilityReleaseRequest(
        schema_name="captain.capability-release-request.v1",
        event_id=uuid5(job.event_id, "capability-release-request"),
        causation_id=promotion.event_id,
        occurred_at=terminal.decided_at,
        producer="captain",
        decision=terminal,
        decision_ref=_canonical_ref(terminal, "terminal-decision"),
        package=package,
        package_ref=_canonical_ref(package, "capability-package"),
        release_evidence=evidence,
        promoted_capability=PromotedCapability(
            capability_id=package.capability_id,
            version=package.capability_version,
            status="ready_to_use",
            blueprint_ref=package.team_manifest_ref,
            code_ref=code_ref,
            tool_refs=tool_refs,
            promotion_block_ref=promotion_ref,
        ),
        schema_major=1,
        team_version=package.capability_version,
        accepted_assertion_ids=tuple(
            item.assertion_id for item in package.assertion_outcomes
        ),
        integration_intents=intents,
        tool_contracts=(),
    )


def _canonical_ref(model: BaseModel, name: str) -> ArtifactRef:
    digest = canonical_contract_sha256(model)
    return ArtifactRef(
        uri=f"artifact://capability-factory/{name}/{digest}",
        sha256=digest,
        media_type="application/json",
    )


def _fixed_evidence_ref(material: str, name: str) -> ArtifactRef:
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return ArtifactRef(
        uri=f"artifact://capability-factory/{name}/{digest}",
        sha256=digest,
        media_type="application/json",
    )


def _summary(
    job: AgentFactoryJobV2,
    terminal: FactoryTerminalDecision,
    *,
    creation_job_id: UUID,
    package: CapabilityPackageManifestV1 | None,
    evidence: tuple[CapabilityReleaseEvidenceV1, ...],
    execution: CapabilityExecutionCompleted | CapabilityExecutionRetryPending | None,
) -> CapabilityFactoryRunSummary:
    required_gaps = ()
    optional_gaps = ()
    if package is not None:
        required_gaps = tuple(
            gap.gap_id
            for gap in package.tool_gaps
            if gap.severity == "required" and gap.status == "unresolved"
        )
        optional_gaps = tuple(
            gap.gap_id
            for gap in package.tool_gaps
            if gap.severity == "optional" and gap.status == "unresolved"
        )
    recovery = next((item for item in evidence if item.kind == "recovery"), None)
    normals = tuple(item for item in evidence if item.kind == "normal")
    completed = execution if isinstance(execution, CapabilityExecutionCompleted) else None
    pending = execution if isinstance(execution, CapabilityExecutionRetryPending) else None
    return CapabilityFactoryRunSummary(
        correlation_id=job.correlation_id,
        factory_job_id=job.job_id,
        invocation_job_id=job.job_id,
        release_authority_job_id=(
            job.job_id if terminal.state is FactoryTerminalState.READY_TO_USE else None
        ),
        execution_mode="created",
        execution_state=(
            "completed"
            if completed is not None
            else "retry_pending"
            if pending is not None
            else "not_started"
        ),
        retry_expires_at=(pending.expires_at if pending is not None else None),
        creation_job_id=creation_job_id,
        terminal_decision_id=terminal.decision_id,
        terminal_state=terminal.state,
        capability_id=job.required_capability,
        capability_version=(package.capability_version if package is not None else None),
        recovery_id=(recovery.recovery_id if recovery is not None else None),
        e2e_batch_ids=tuple(item.run_id for item in normals),
        execution_command_id=(
            completed.bundle.command.event_id
            if completed is not None
            else pending.command_id
            if pending is not None
            else None
        ),
        execution_result_id=(
            completed.bundle.result.event_id if completed is not None else None
        ),
        projection_event_ids=(
            tuple(item.event_id for item in completed.projection_events)
            if completed is not None
            else ()
        ),
        minibook_projection_verified=(
            completed.minibook_projection_verified if completed is not None else False
        ),
        package_sha256=(canonical_contract_sha256(package) if package is not None else None),
        release_evidence_sha256=tuple(
            hashlib.sha256(item.model_dump_json(by_alias=True).encode("utf-8")).hexdigest()
            for item in evidence
        ),
        unresolved_required_tool_gaps=required_gaps,
        unresolved_optional_tool_gaps=optional_gaps,
    )


def _require_catalog_authority(
    job: AgentFactoryJobV2,
    authority: CapabilityCatalogRecord,
) -> None:
    promoted = authority.promoted_capability
    try:
        compatibility = compatibility_request_for_authority(job, authority)
    except ValueError as exc:
        raise RuntimeError(
            "Gateway returned incompatible capability catalog authority"
        ) from exc
    if (
        not authority.satisfies(compatibility)
        or promoted.capability_id != authority.capability_id
        or promoted.version != authority.capability_version
        or promoted.status != "ready_to_use"
        or promoted.promotion_block_ref is None
    ):
        raise RuntimeError("Gateway returned incompatible capability catalog authority")


def _reuse_summary(
    job: AgentFactoryJobV2,
    authority: CapabilityCatalogRecord,
    *,
    execution: CapabilityExecutionCompleted | CapabilityExecutionRetryPending,
) -> CapabilityFactoryRunSummary:
    completed = execution if isinstance(execution, CapabilityExecutionCompleted) else None
    pending = execution if isinstance(execution, CapabilityExecutionRetryPending) else None
    return CapabilityFactoryRunSummary(
        correlation_id=job.correlation_id,
        factory_job_id=job.job_id,
        invocation_job_id=job.job_id,
        release_authority_job_id=authority.release_authority_job_id,
        execution_mode="reused",
        execution_state=("completed" if completed is not None else "retry_pending"),
        retry_expires_at=(pending.expires_at if pending is not None else None),
        creation_job_id=None,
        terminal_decision_id=authority.terminal_decision_id,
        terminal_state=FactoryTerminalState.READY_TO_USE,
        capability_id=authority.capability_id,
        capability_version=authority.capability_version,
        execution_command_id=(
            completed.bundle.command.event_id
            if completed is not None
            else pending.command_id
            if pending is not None
            else None
        ),
        execution_result_id=(
            completed.bundle.result.event_id if completed is not None else None
        ),
        projection_event_ids=(
            tuple(item.event_id for item in completed.projection_events)
            if completed is not None
            else ()
        ),
        minibook_projection_verified=(
            completed.minibook_projection_verified if completed is not None else False
        ),
        package_sha256=authority.package_ref.sha256,
        unresolved_required_tool_gaps=authority.unresolved_required_gap_ids,
    )


if __name__ == "__main__":
    raise SystemExit(main())
