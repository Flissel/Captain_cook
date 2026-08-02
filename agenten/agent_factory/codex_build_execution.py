"""Captain-owned Codex execution and sealing for Agent Factory builds."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID, uuid4

from agenten.agent_factory.codex_build_provenance import (
    CaptainCodexBuildReceiptIssuer,
)
from agenten.agent_factory.codex_build_recovery import (
    FactoryCodexBuildCheckpointV1,
    FactoryCodexBuildPhase,
    FactoryCodexOutputArtifactV1,
    FactoryCodexOutputManifestV1,
    FactoryCodexScaffoldFileV1,
    FactoryCodexScaffoldManifestV1,
    FilesystemFactoryCodexBuildCheckpointStore,
    FilesystemFactoryCodexOutputManifestStore,
    FilesystemFactoryCodexScaffoldManifestStore,
    FilesystemFactoryCodexSealedEvidenceStore,
    MAX_FACTORY_CODEX_CANDIDATE_ARCHIVE_BYTES,
    MAX_FACTORY_CODEX_JSON_ARTIFACT_BYTES,
    MAX_FACTORY_CODEX_OUTPUT_BYTES,
    canonical_factory_codex_model,
)
from agenten.agent_factory.contracts import AgentFactoryJobV3
from agenten.agent_factory.forge_contracts import CodexBuildReceiptV1
from agenten.agent_factory.orchestration import FactoryDispatch, FactoryDispatchError
from agenten.agent_factory.skill_sequence import (
    FactoryRuntimeRetryAuthorizationV1,
    validate_factory_runtime_retry_authorization,
)
from agenten.agent_factory.skill_workflow_contracts import (
    CodexBuildBriefV1,
    CodexBuildEvidenceV1,
    FactorySkillInvocationV1,
    FactorySkillStep,
    factory_runtime_retry_evidence_binding,
)
from agenten.agent_runtime.contracts import ArtifactRef
from agenten.execution.codex_policy import AuthorizedCodexRun
from agenten.execution.codex_supervisor import (
    DEFAULT_MAX_CODEX_JOURNAL_BYTES,
    DEFAULT_MAX_CODEX_JOURNAL_RECORDS,
    DEFAULT_MAX_CODEX_JSONL_RECORD_BYTES,
    CodexJsonlInvalidObjectError,
    CodexOutputEvidenceError,
    CodexRunResult,
    CodexRunRequest,
    CodexRunner,
    canonical_codex_event_type,
    canonical_codex_event_types,
)


_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40,64}$")
_CODEX_THREAD_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_OUTPUT_PATHS = (
    "factory-candidate.json",
    "candidate.zip",
    "test-evidence.json",
)
_CODEX_OUTPUT_FAILURE_KINDS = frozenset(
    {
        "journal_persistence_failed",
        "output_read_failed",
        "invalid_json_object",
        "record_size_limit_exceeded",
        "unterminated_record",
        "journal_size_limit_exceeded",
        "journal_record_count_exceeded",
    }
)


@dataclass(frozen=True)
class CompletedCodexBuild:
    """Concrete local result ready for the Captain provenance issuer."""

    workspace_root: Path
    codex_session_receipt: bytes
    candidate_manifest_path: str
    source_archive_path: str
    test_evidence_paths: tuple[str, ...]
    completed_at: datetime
    output_snapshot_root: Path | None = None


FactoryCodexBuildInterruptionReason = Literal[
    "codex_timed_out",
    "runtime_cancelled",
    "resume_authorization_required",
]

FactoryCodexBuildFailureReason = Literal[
    "required_output_invalid",
    "output_size_limit_exceeded",
    "runtime_failed",
    "authority_expired",
    "evidence_failure",
]

FactoryCodexProcessState = Literal["active", "lost", "identity_mismatch"]


@dataclass(frozen=True)
class FactoryCodexBuildInterruptionBindings:
    """Redacted immutable inputs Captain must bind before a runtime resume."""

    job_id: UUID
    correlation_id: UUID
    subject_version: int
    attempt: int
    invocation_id: UUID
    idempotency_key: str
    lease_id: str
    workspace_ref: str
    base_revision: str
    scaffold_manifest_sha256: str
    brief_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "job_id": str(self.job_id),
            "correlation_id": str(self.correlation_id),
            "subject_version": self.subject_version,
            "attempt": self.attempt,
            "invocation_id": str(self.invocation_id),
            "idempotency_key": self.idempotency_key,
            "lease_id": self.lease_id,
            "workspace_ref": self.workspace_ref,
            "base_revision": self.base_revision,
            "scaffold_manifest_sha256": self.scaffold_manifest_sha256,
            "brief_sha256": self.brief_sha256,
        }


class FactoryCodexBuildInterrupted(FactoryDispatchError):
    """One terminal Codex runtime interruption retained for Captain recovery."""

    def __init__(
        self,
        *,
        reason: FactoryCodexBuildInterruptionReason,
        exit_code: int | None,
        checkpoint_ref: ArtifactRef,
        terminal_receipt_ref: ArtifactRef,
        resume_ordinal: int,
        authorization_binding: FactoryCodexBuildInterruptionBindings | None = None,
    ) -> None:
        if reason not in {
            "codex_timed_out",
            "runtime_cancelled",
            "resume_authorization_required",
        }:
            raise ValueError("Factory Codex interruption reason is invalid")
        if reason == "codex_timed_out" and exit_code != 124:
            raise ValueError("Factory Codex timeout interruption requires exit 124")
        if reason == "runtime_cancelled" and exit_code != 130:
            raise ValueError("Factory Codex cancellation requires exit 130")
        if reason == "resume_authorization_required" and exit_code is not None:
            raise ValueError("Factory Codex resume interruption cannot carry an exit code")
        super().__init__("Factory Codex build interrupted")
        self.reason = reason
        self.exit_code = exit_code
        if isinstance(resume_ordinal, bool) or not 0 <= resume_ordinal <= 2:
            raise ValueError("Factory Codex interruption ordinal is invalid")
        self.checkpoint_ref = checkpoint_ref
        self.terminal_receipt_ref = terminal_receipt_ref
        self.resume_ordinal = resume_ordinal
        self.authorization_binding = authorization_binding


class FactoryCodexCleanupUnresolved(FactoryDispatchError):
    """A provisional runtime state that must keep the seal replay pending."""


class FactoryCodexBuildFailed(FactoryDispatchError):
    """Redacted terminal failure recovered from exact durable build evidence."""

    def __init__(
        self,
        *,
        reason: FactoryCodexBuildFailureReason,
        checkpoint_ref: ArtifactRef,
        terminal_receipt_ref: ArtifactRef,
    ) -> None:
        if reason not in {
            "required_output_invalid",
            "output_size_limit_exceeded",
            "runtime_failed",
            "authority_expired",
            "evidence_failure",
        }:
            raise ValueError("Factory Codex failure reason is invalid")
        super().__init__("Factory Codex build failed")
        self.reason = reason
        self.checkpoint_ref = checkpoint_ref
        self.terminal_receipt_ref = terminal_receipt_ref


class FactoryCodexOutputCaptureError(FactoryDispatchError):
    """A successful Codex process produced outputs that cannot ever be sealed."""

    def __init__(
        self,
        message: str,
        *,
        reason: Literal[
            "required_output_invalid",
            "output_size_limit_exceeded",
        ],
    ) -> None:
        super().__init__(message)
        self.reason = reason


class FactoryCodexEvidenceFailure(FactoryDispatchError):
    """A redacted durable Factory failure for untrustworthy Codex output."""

    def __init__(
        self,
        *,
        process_cleanup_status: Literal[
            "not_required", "verified_cancelled", "unresolved"
        ],
        checkpoint_ref: ArtifactRef,
        terminal_receipt_ref: ArtifactRef,
    ) -> None:
        super().__init__("Factory Codex output evidence failed")
        self.process_cleanup_status = process_cleanup_status
        self.checkpoint_ref = checkpoint_ref
        self.terminal_receipt_ref = terminal_receipt_ref


def _interruption_references(
    checkpoint: FactoryCodexBuildCheckpointV1,
) -> dict[str, object]:
    receipt_sha256 = checkpoint.terminal_receipt_sha256
    resumable_phase = checkpoint.phase == "implementation_interrupted" or (
        checkpoint.phase == "implementation_failed"
        and checkpoint.implementation_failure_reason
        in {"evidence_failure", "required_output_invalid"}
    )
    if not resumable_phase or receipt_sha256 is None:
        raise FactoryDispatchError("Factory Codex interruption checkpoint is invalid")
    checkpoint_sha256 = hashlib.sha256(
        canonical_factory_codex_model(checkpoint)
    ).hexdigest()
    return {
        "checkpoint_ref": ArtifactRef(
            uri=f"artifact://factory/codex-checkpoint/{checkpoint_sha256}",
            sha256=checkpoint_sha256,
            media_type="application/json",
        ),
        "terminal_receipt_ref": ArtifactRef(
            uri=f"artifact://factory/codex-terminal-receipt/{receipt_sha256}",
            sha256=receipt_sha256,
            media_type="application/json",
        ),
        "resume_ordinal": checkpoint.resume_ordinal,
    }


def _interruption_details(
    request: FactoryDispatch,
    invocation: FactorySkillInvocationV1,
    checkpoint: FactoryCodexBuildCheckpointV1,
) -> dict[str, object]:
    details = _interruption_references(checkpoint)
    details["authorization_binding"] = FactoryCodexBuildInterruptionBindings(
        job_id=request.job.job_id,
        correlation_id=request.job.correlation_id,
        subject_version=request.job.subject_version,
        attempt=invocation.attempt,
        invocation_id=invocation.invocation_id,
        idempotency_key=invocation.idempotency_key,
        lease_id=invocation.lease.lease_id,
        workspace_ref=checkpoint.workspace_ref,
        base_revision=checkpoint.base_revision,
        scaffold_manifest_sha256=checkpoint.scaffold_manifest_sha256,
        brief_sha256=checkpoint.brief_sha256,
    )
    return details


@dataclass(frozen=True)
class PreparedFactoryWorkspace:
    """One clean detached worktree and its immutable base revision."""

    root: Path
    base_revision: str


@dataclass(frozen=True)
class _CodexResumeLineage:
    terminal_receipt_sha256: str
    journal_sha256: str
    codex_thread_id: str | None


@dataclass(frozen=True)
class _LoadedCodexTerminalEvidence:
    content: bytes
    payload: dict[str, object]
    receipt_sha256: str
    journal_sha256: str
    codex_thread_id: str | None
    completed_at: datetime


@dataclass(frozen=True)
class CodexCliFactoryBuildSettings:
    """Non-secret bounds for one Factory-specific Codex execution."""

    state_root: Path
    maximum_runtime_seconds: int = 600
    # Conservative private-capture ceilings. They bound every streamed read
    # before hashing, copying, checkpointing, or invoking the provenance CAS.
    maximum_candidate_archive_bytes: int = (
        MAX_FACTORY_CODEX_CANDIDATE_ARCHIVE_BYTES
    )
    maximum_json_artifact_bytes: int = MAX_FACTORY_CODEX_JSON_ARTIFACT_BYTES
    maximum_output_bytes: int = MAX_FACTORY_CODEX_OUTPUT_BYTES

    def __post_init__(self) -> None:
        if self.maximum_runtime_seconds < 1 or self.maximum_runtime_seconds > 900:
            raise ValueError("Factory Codex runtime bound is invalid")
        if not (
            1
            <= self.maximum_candidate_archive_bytes
            <= MAX_FACTORY_CODEX_CANDIDATE_ARCHIVE_BYTES
            and 1
            <= self.maximum_json_artifact_bytes
            <= MAX_FACTORY_CODEX_JSON_ARTIFACT_BYTES
            and 1
            <= self.maximum_output_bytes
            <= MAX_FACTORY_CODEX_OUTPUT_BYTES
        ):
            raise ValueError("Factory Codex output size bound is invalid")


class CaptainCodexBuildExecutorPort(Protocol):
    async def execute(
        self,
        request: FactoryDispatch,
        invocation: FactorySkillInvocationV1,
        brief: CodexBuildBriefV1,
    ) -> CompletedCodexBuild: ...

    def validate_authorized_resume(
        self,
        request: FactoryDispatch,
        invocation: FactorySkillInvocationV1,
        brief: CodexBuildBriefV1,
    ) -> FactoryRuntimeRetryAuthorizationV1: ...

    async def execute_authorized_resume(
        self,
        request: FactoryDispatch,
        invocation: FactorySkillInvocationV1,
        brief: CodexBuildBriefV1,
    ) -> CompletedCodexBuild: ...

    async def reconcile_pending(
        self,
        request: FactoryDispatch,
        invocation: FactorySkillInvocationV1,
        brief: CodexBuildBriefV1,
    ) -> CompletedCodexBuild: ...

    def reconcile_failed(
        self,
        request: FactoryDispatch,
        invocation: FactorySkillInvocationV1,
        brief: CodexBuildBriefV1,
        *,
        persisted_resume_ordinal: int | None = None,
        persisted_retry_authorization_ref: ArtifactRef | None = None,
        persisted_retry_authorization_binding_sha256: str | None = None,
    ) -> FactoryCodexBuildFailed: ...

    def replay_sealed(
        self,
        invocation: FactorySkillInvocationV1,
    ) -> CodexBuildEvidenceV1 | None: ...

    def validate_replay_authority(
        self,
        request: FactoryDispatch,
        invocation: FactorySkillInvocationV1,
    ) -> None: ...

    def validate_completed_outputs(
        self,
        invocation: FactorySkillInvocationV1,
        completed: CompletedCodexBuild,
    ) -> None: ...

    def validate_issued_receipt(
        self,
        invocation: FactorySkillInvocationV1,
        completed: CompletedCodexBuild,
        receipt: CodexBuildReceiptV1,
    ) -> None: ...

    def persist_sealed(
        self,
        invocation: FactorySkillInvocationV1,
        completed: CompletedCodexBuild,
        evidence: CodexBuildEvidenceV1,
    ) -> CodexBuildEvidenceV1: ...


class FactoryCodexWorkspacePreparerPort(Protocol):
    def prepare_or_recover(
        self,
        request: FactoryDispatch,
        invocation: FactorySkillInvocationV1,
        brief: CodexBuildBriefV1,
        checkpoint: FactoryCodexBuildCheckpointV1 | None,
    ) -> PreparedFactoryWorkspace: ...


class FactoryCodexResumeAuthorizerPort(Protocol):
    def authorize_resume(
        self,
        request: FactoryDispatch,
        invocation: FactorySkillInvocationV1,
        checkpoint: FactoryCodexBuildCheckpointV1,
    ) -> int: ...


class CaptainFactoryCodexResumeAuthorizer:
    """Validate already-issued Captain authority; never mint retry evidence."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def authorize_resume(
        self,
        request: FactoryDispatch,
        invocation: FactorySkillInvocationV1,
        checkpoint: FactoryCodexBuildCheckpointV1,
    ) -> int:
        authorization = request.runtime_retry_authorization
        if authorization is None:
            raise FactoryDispatchError(
                "Factory Codex resume requires Captain runtime retry authority"
            )
        references = _interruption_references(checkpoint)
        now = self._clock()
        authority_deadline = min(authorization.expires_at, request.job.deadline_at)
        remaining_seconds = int((authority_deadline - now).total_seconds())
        try:
            validate_factory_runtime_retry_authorization(
                authorization,
                job_id=request.job.job_id,
                correlation_id=request.job.correlation_id,
                subject_version=request.job.subject_version,
                attempt=invocation.attempt,
                invocation_id=invocation.invocation_id,
                idempotency_key=invocation.idempotency_key,
                lease_id=invocation.lease.lease_id,
                checkpoint_ref=references["checkpoint_ref"],
                terminal_receipt_ref=references["terminal_receipt_ref"],
                workspace_ref=checkpoint.workspace_ref,
                base_revision=checkpoint.base_revision,
                scaffold_manifest_sha256=checkpoint.scaffold_manifest_sha256,
                brief_sha256=checkpoint.brief_sha256,
                current_resume_ordinal=checkpoint.resume_ordinal,
                remaining_runtime_seconds=remaining_seconds,
                now=now,
            )
        except ValueError as exc:
            raise FactoryDispatchError("Factory Codex runtime retry authority is invalid") from exc
        return authorization.resume_ordinal


class FactoryBuildArtifactReaderPort(Protocol):
    def read_bytes(self, reference: ArtifactRef) -> bytes: ...


class CodexExecutionAuthorizerPort(Protocol):
    def authorize(self, request: CodexRunRequest) -> AuthorizedCodexRun: ...


class FactoryCodexRunnerFactory(Protocol):
    def __call__(
        self,
        *,
        session_id: str,
        state_path: Path,
        journal_path: Path,
        maximum_runtime_seconds: int,
        deadline_at: datetime,
    ) -> CodexRunner: ...


class FactoryCodexProcessInspectorPort(Protocol):
    async def inspect(
        self,
        *,
        session_id: str,
        state_path: Path,
    ) -> FactoryCodexProcessState: ...


class PowerShellFactoryCodexProcessInspector:
    """Inspect the exact persisted Codex child identity without mutating it."""

    def __init__(self, *, pwsh_path: Path, script_path: Path) -> None:
        self._pwsh_path = pwsh_path.resolve(strict=True)
        self._script_path = script_path.resolve(strict=True)

    async def inspect(
        self,
        *,
        session_id: str,
        state_path: Path,
    ) -> FactoryCodexProcessState:
        if not state_path.is_file():
            raise FactoryDispatchError("Factory Codex process state is missing")
        process = await asyncio.create_subprocess_exec(
            str(self._pwsh_path),
            "-NoProfile",
            "-File",
            str(self._script_path),
            "-InspectStatePath",
            str(state_path),
            "-SessionId",
            session_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await process.communicate()
        if process.returncode != 0:
            raise FactoryDispatchError("Factory Codex process inspection failed")
        try:
            payload = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise FactoryDispatchError(
                "Factory Codex process inspection returned invalid evidence"
            ) from None
        if not isinstance(payload, dict):
            raise FactoryDispatchError(
                "Factory Codex process inspection returned invalid evidence"
            )
        status = payload.get("status")
        if (
            payload.get("session_id") != session_id
            or status not in {"active", "lost", "identity_mismatch"}
        ):
            raise FactoryDispatchError(
                "Factory Codex process inspection returned invalid evidence"
            )
        return status


class CaptainCodexBuildSealer:
    """Run one exact assignment, then issue only Captain-owned build evidence."""

    def __init__(
        self,
        *,
        executor: CaptainCodexBuildExecutorPort,
        issuer: CaptainCodexBuildReceiptIssuer,
    ) -> None:
        self._executor = executor
        self._issuer = issuer

    def validate_runtime_retry(
        self,
        request: FactoryDispatch,
        invocation: FactorySkillInvocationV1,
        brief: CodexBuildBriefV1,
    ) -> FactoryRuntimeRetryAuthorizationV1:
        return self._executor.validate_authorized_resume(request, invocation, brief)

    async def seal(
        self,
        request: FactoryDispatch,
        invocation: FactorySkillInvocationV1,
        brief: CodexBuildBriefV1,
    ) -> CodexBuildEvidenceV1:
        if not isinstance(request.job, AgentFactoryJobV3):
            raise FactoryDispatchError("Codex build sealing requires a V3 Factory job")
        if (
            invocation.step is not FactorySkillStep.SEAL_CODEX_BUILD
            or invocation.input_ref != brief.artifact_ref
            or request.job.job_id != invocation.job_id
        ):
            raise FactoryDispatchError("Codex build sealing authority does not match")
        self._executor.validate_replay_authority(request, invocation)
        replay = self._executor.replay_sealed(invocation)
        if replay is not None:
            return replay
        if request.runtime_retry_authorization is None:
            completed = await self._executor.execute(request, invocation, brief)
        else:
            completed = await self._executor.execute_authorized_resume(
                request,
                invocation,
                brief,
            )
        return self._seal_completed(request, invocation, brief, completed)

    async def reconcile_pending(
        self,
        request: FactoryDispatch,
        invocation: FactorySkillInvocationV1,
        brief: CodexBuildBriefV1,
    ) -> CodexBuildEvidenceV1:
        if not isinstance(request.job, AgentFactoryJobV3):
            raise FactoryDispatchError("Codex reconciliation requires a V3 Factory job")
        self._executor.validate_replay_authority(request, invocation)
        replay = self._executor.replay_sealed(invocation)
        if replay is not None:
            return replay
        completed = await self._executor.reconcile_pending(
            request,
            invocation,
            brief,
        )
        return self._seal_completed(request, invocation, brief, completed)

    def reconcile_failed(
        self,
        request: FactoryDispatch,
        invocation: FactorySkillInvocationV1,
        brief: CodexBuildBriefV1,
        *,
        persisted_resume_ordinal: int | None = None,
        persisted_retry_authorization_ref: ArtifactRef | None = None,
        persisted_retry_authorization_binding_sha256: str | None = None,
    ) -> FactoryCodexBuildFailed:
        if not isinstance(request.job, AgentFactoryJobV3):
            raise FactoryDispatchError(
                "Codex failure reconciliation requires a V3 Factory job"
            )
        self._executor.validate_replay_authority(request, invocation)
        if self._executor.replay_sealed(invocation) is not None:
            raise FactoryDispatchError(
                "Factory Codex sealed replay is not a terminal failure"
            )
        return self._executor.reconcile_failed(
            request,
            invocation,
            brief,
            persisted_resume_ordinal=persisted_resume_ordinal,
            persisted_retry_authorization_ref=(
                persisted_retry_authorization_ref
            ),
            persisted_retry_authorization_binding_sha256=(
                persisted_retry_authorization_binding_sha256
            ),
        )

    def _seal_completed(
        self,
        request: FactoryDispatch,
        invocation: FactorySkillInvocationV1,
        brief: CodexBuildBriefV1,
        completed: CompletedCodexBuild,
    ) -> CodexBuildEvidenceV1:
        if completed.output_snapshot_root is None:
            raise FactoryDispatchError(
                "Codex build sealing requires an immutable output snapshot"
            )
        self._executor.validate_completed_outputs(invocation, completed)
        receipt = self._issuer.issue(
            job=request.job,
            build_brief=brief,
            workspace_root=completed.output_snapshot_root,
            codex_session_receipt=completed.codex_session_receipt,
            seal_idempotency_key=invocation.idempotency_key,
            candidate_manifest_path=completed.candidate_manifest_path,
            source_archive_path=completed.source_archive_path,
            test_evidence_paths=completed.test_evidence_paths,
            completed_at=completed.completed_at,
        )
        self._executor.validate_issued_receipt(invocation, completed, receipt)
        receipt_ref = self._issuer.persist_receipt(receipt)
        workflow_receipt_ref = ArtifactRef.model_validate(
            receipt_ref.model_dump(mode="json")
        )
        runtime_retry = request.runtime_retry_authorization
        runtime_retry_ref = (
            runtime_retry.authorization_ref if runtime_retry is not None else None
        )
        runtime_retry_binding = (
            factory_runtime_retry_evidence_binding(runtime_retry)
            if runtime_retry is not None
            else None
        )
        evidence = CodexBuildEvidenceV1(
            schema_name="hermes.factory-codex-build-evidence.v1",
            invocation=invocation,
            invocation_id=invocation.invocation_id,
            job_id=invocation.job_id,
            correlation_id=invocation.correlation_id,
            subject_version=invocation.subject_version,
            attempt=invocation.attempt,
            occurred_at=completed.completed_at,
            producer="hermes",
            artifact_ref=workflow_receipt_ref,
            evidence_refs=(
                (workflow_receipt_ref, runtime_retry_ref)
                if runtime_retry_ref is not None
                else (workflow_receipt_ref,)
            ),
            acceptance_assertion_ids=invocation.acceptance_assertion_ids,
            build_receipt_ref=workflow_receipt_ref,
            build_receipt=receipt,
            runtime_retry_ref=runtime_retry_ref,
            runtime_retry_binding=runtime_retry_binding,
            status="sealed",
        )
        return self._executor.persist_sealed(invocation, completed, evidence)


class GitDetachedFactoryWorkspacePreparer:
    """Create one clean detached Git worktree for an exact seal invocation."""

    def __init__(self, *, repository_root: Path, workspaces_root: Path) -> None:
        self._repository_root = repository_root.resolve(strict=True)
        self._workspaces_root = workspaces_root.resolve()
        if not _is_relative_to(self._workspaces_root, self._repository_root):
            raise ValueError("Factory Codex workspaces must stay below the repository")

    def prepare(
        self,
        request: FactoryDispatch,
        invocation: FactorySkillInvocationV1,
        brief: CodexBuildBriefV1,
    ) -> PreparedFactoryWorkspace:
        return self.prepare_or_recover(request, invocation, brief, None)

    def prepare_or_recover(
        self,
        request: FactoryDispatch,
        invocation: FactorySkillInvocationV1,
        brief: CodexBuildBriefV1,
        checkpoint: FactoryCodexBuildCheckpointV1 | None,
    ) -> PreparedFactoryWorkspace:
        if (
            request.job.job_id != invocation.job_id
            or brief.invocation.job_id != invocation.job_id
            or brief.build_assignment.workspace_ref
            != invocation.lease.workspace_ref
        ):
            raise FactoryDispatchError("Codex workspace authority does not match")
        digest = hashlib.sha256(
            brief.build_assignment.workspace_ref.encode("utf-8")
        ).hexdigest()
        target = (
            self._workspaces_root
            / str(request.job.job_id)
            / f"attempt-{invocation.attempt}-{digest[:16]}"
        ).resolve()
        if not _is_relative_to(target, self._workspaces_root):
            raise FactoryDispatchError("Codex workspace path escaped its authority root")
        if checkpoint is not None:
            if (
                checkpoint.workspace_root != target
                or checkpoint.workspace_ref != brief.build_assignment.workspace_ref
                or checkpoint.brief_sha256
                != hashlib.sha256(canonical_factory_codex_model(brief)).hexdigest()
            ):
                raise FactoryDispatchError(
                    "Codex recovery workspace binding does not match checkpoint"
                )
            if not target.is_dir():
                raise FactoryDispatchError("Codex recovery workspace is missing")
            revision = self._git_in(target, "rev-parse", "HEAD")
            if revision != checkpoint.base_revision:
                raise FactoryDispatchError("Codex recovery workspace HEAD changed")
            self._require_detached_head(target)
            return PreparedFactoryWorkspace(root=target, base_revision=revision)
        if target.exists():
            if not target.is_dir():
                raise FactoryDispatchError("Codex retry workspace is invalid")
            revision = self._git_in(target, "rev-parse", "HEAD")
            if revision != self._git("rev-parse", "HEAD"):
                raise FactoryDispatchError("Codex retry workspace HEAD changed")
            self._require_detached_head(target)
            status = self._git_in(target, "status", "--porcelain")
            if status and any(
                not _is_captain_scaffold_status_line(line)
                for line in status.splitlines()
            ):
                raise FactoryDispatchError("Codex retry workspace contains changes")
            return PreparedFactoryWorkspace(root=target, base_revision=revision)
        status = self._git("status", "--porcelain")
        if status:
            raise FactoryDispatchError(
                "Codex base repository must be clean before worktree creation"
            )
        revision = self._git("rev-parse", "HEAD")
        if _REVISION_PATTERN.fullmatch(revision) is None:
            raise FactoryDispatchError("Codex base revision is invalid")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(
                (
                    "git",
                    "-C",
                    str(self._repository_root),
                    "worktree",
                    "add",
                    "--detach",
                    str(target),
                    revision,
                ),
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise FactoryDispatchError(
                "detached Codex workspace could not be created"
            ) from exc
        if self._git_in(target, "status", "--porcelain"):
            raise FactoryDispatchError("new Codex workspace is not clean")
        self._require_detached_head(target)
        return PreparedFactoryWorkspace(root=target, base_revision=revision)

    def _git(self, *arguments: str) -> str:
        return self._git_in(self._repository_root, *arguments)

    @staticmethod
    def _git_in(root: Path, *arguments: str) -> str:
        try:
            completed = subprocess.run(
                ("git", "-C", str(root), *arguments),
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise FactoryDispatchError("Git workspace inspection failed") from exc
        return completed.stdout.strip()

    @staticmethod
    def _require_detached_head(root: Path) -> None:
        try:
            completed = subprocess.run(
                ("git", "-C", str(root), "symbolic-ref", "-q", "HEAD"),
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            raise FactoryDispatchError("Git workspace inspection failed") from exc
        if completed.returncode == 0:
            raise FactoryDispatchError("Codex recovery workspace requires detached HEAD")
        if completed.returncode != 1:
            raise FactoryDispatchError("Git workspace inspection failed")


class CodexCliFactoryBuildExecutor:
    """Execute a brief with Codex and retain a redacted private session receipt."""

    def __init__(
        self,
        *,
        settings: CodexCliFactoryBuildSettings,
        workspace_preparer: FactoryCodexWorkspacePreparerPort,
        artifact_reader: FactoryBuildArtifactReaderPort,
        authorizer: CodexExecutionAuthorizerPort,
        runner_factory: FactoryCodexRunnerFactory,
        checkpoint_store: FilesystemFactoryCodexBuildCheckpointStore | None = None,
        scaffold_manifest_store: FilesystemFactoryCodexScaffoldManifestStore | None = None,
        output_manifest_store: FilesystemFactoryCodexOutputManifestStore | None = None,
        sealed_evidence_store: FilesystemFactoryCodexSealedEvidenceStore | None = None,
        resume_authorizer: FactoryCodexResumeAuthorizerPort | None = None,
        process_inspector: FactoryCodexProcessInspectorPort | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._workspace_preparer = workspace_preparer
        self._artifact_reader = artifact_reader
        self._authorizer = authorizer
        self._runner_factory = runner_factory
        self._checkpoint_store = checkpoint_store or FilesystemFactoryCodexBuildCheckpointStore(
            settings.state_root.resolve() / "checkpoints"
        )
        self._scaffold_manifest_store = (
            scaffold_manifest_store
            or FilesystemFactoryCodexScaffoldManifestStore(
                settings.state_root.resolve() / "scaffold-manifests"
            )
        )
        self._output_manifest_store = (
            output_manifest_store
            or FilesystemFactoryCodexOutputManifestStore(
                settings.state_root.resolve() / "output-manifests"
            )
        )
        self._sealed_evidence_store = (
            sealed_evidence_store
            or FilesystemFactoryCodexSealedEvidenceStore(
                settings.state_root.resolve() / "sealed-evidence"
            )
        )
        self._resume_authorizer = resume_authorizer
        self._process_inspector = process_inspector
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def execute(
        self,
        request: FactoryDispatch,
        invocation: FactorySkillInvocationV1,
        brief: CodexBuildBriefV1,
    ) -> CompletedCodexBuild:
        return await self._execute(
            request,
            invocation,
            brief,
            authorized_resume_ordinal=None,
        )

    async def execute_authorized_resume(
        self,
        request: FactoryDispatch,
        invocation: FactorySkillInvocationV1,
        brief: CodexBuildBriefV1,
    ) -> CompletedCodexBuild:
        authorization = self.validate_authorized_resume(request, invocation, brief)
        resume_ordinal = authorization.resume_ordinal
        checkpoint = self._checkpoint_store.load(invocation)
        assert checkpoint is not None
        expected_resume_ordinal = (
            checkpoint.resume_ordinal
            if checkpoint.phase == "implementation_complete"
            else checkpoint.resume_ordinal + 1
        )
        if (
            not isinstance(resume_ordinal, int)
            or isinstance(resume_ordinal, bool)
            or resume_ordinal != expected_resume_ordinal
        ):
            raise FactoryDispatchError(
                "Factory Codex resume authorization decision is invalid"
            )
        return await self._execute(
            request,
            invocation,
            brief,
            authorized_resume_ordinal=resume_ordinal,
        )

    def validate_authorized_resume(
        self,
        request: FactoryDispatch,
        invocation: FactorySkillInvocationV1,
        brief: CodexBuildBriefV1,
    ) -> FactoryRuntimeRetryAuthorizationV1:
        checkpoint = self._checkpoint_store.load(invocation)
        authorization = request.runtime_retry_authorization
        if checkpoint is not None and checkpoint.phase == "implementation_complete":
            if authorization is None:
                raise FactoryDispatchError(
                    "Factory Codex completed seal retry requires Captain runtime retry authority"
                )
            self._require_checkpoint_runtime_retry_authority(request, checkpoint)
            if authorization.resume_ordinal != checkpoint.resume_ordinal:
                raise FactoryDispatchError(
                    "Factory Codex completed seal retry ordinal changed"
                )
            prepared = self._prepare_or_recover(
                request,
                invocation,
                brief,
                checkpoint,
            )
            self._validate_original_scaffold(
                request,
                invocation,
                brief,
                prepared.root,
                checkpoint,
            )
            return authorization
        if self._resume_authorizer is None:
            raise FactoryDispatchError(
                "Factory Codex resume authorization validator is not configured"
            )
        if checkpoint is None or checkpoint.phase not in {
            "implementation_interrupted",
            "implementation_failed",
        }:
            raise FactoryDispatchError(
                "Factory Codex build is not interrupted or resumable"
            )
        if (
            checkpoint.phase == "implementation_failed"
            and checkpoint.implementation_failure_reason
            not in {"evidence_failure", "required_output_invalid"}
        ):
            raise FactoryDispatchError("Factory Codex build failure is not resumable")
        resume_ordinal = self._resume_authorizer.authorize_resume(
            request,
            invocation,
            checkpoint,
        )
        if authorization is None:
            raise FactoryDispatchError(
                "Factory Codex resume requires Captain runtime retry authority"
            )
        if resume_ordinal != authorization.resume_ordinal:
            raise FactoryDispatchError(
                "Factory Codex resume authorization decision is invalid"
            )
        prepared = self._prepare_or_recover(
            request,
            invocation,
            brief,
            checkpoint,
        )
        self._validate_original_scaffold(
            request,
            invocation,
            brief,
            prepared.root,
            checkpoint,
        )
        prior = self._load_terminal_evidence(
            request=request,
            invocation=invocation,
            brief=brief,
            prepared=prepared,
            checkpoint=checkpoint,
        )
        interruption_is_resumable = (
            checkpoint.phase == "implementation_interrupted"
            and prior.payload["status"] in {"timed_out", "cancelled"}
        )
        evidence_failure_is_resumable = (
            checkpoint.phase == "implementation_failed"
            and prior.payload["status"] == "evidence_failed"
            and prior.payload.get("failure_kind") == "record_size_limit_exceeded"
        )
        required_output_failure_is_resumable = (
            checkpoint.phase == "implementation_failed"
            and checkpoint.implementation_failure_reason
            == "required_output_invalid"
            and prior.payload["status"] == "succeeded"
            and prior.payload.get("exit_code") == 0
        )
        if (
            not (
                interruption_is_resumable
                or evidence_failure_is_resumable
                or required_output_failure_is_resumable
            )
            or prior.payload["process_cleanup_status"] == "unresolved"
            or prior.completed_at > checkpoint.updated_at
        ):
            raise FactoryDispatchError(
                "Factory Codex prior terminal evidence is not resumable"
            )
        return authorization

    def reconcile_failed(
        self,
        request: FactoryDispatch,
        invocation: FactorySkillInvocationV1,
        brief: CodexBuildBriefV1,
        *,
        persisted_resume_ordinal: int | None = None,
        persisted_retry_authorization_ref: ArtifactRef | None = None,
        persisted_retry_authorization_binding_sha256: str | None = None,
    ) -> FactoryCodexBuildFailed:
        """Validate only an already-terminal failure; never inspect or run work."""

        if not isinstance(request.job, AgentFactoryJobV3):
            raise FactoryDispatchError(
                "Codex failure reconciliation requires a V3 Factory job"
            )
        checkpoint = self._checkpoint_store.load(invocation)
        if checkpoint is not None and checkpoint.phase == "implementation_running":
            raise FactoryCodexCleanupUnresolved(
                "Factory Codex checkpoint is not a terminal failure; pending "
                "reconciliation is required"
            )
        if checkpoint is None or checkpoint.phase != "implementation_failed":
            raise FactoryDispatchError(
                "Factory Codex checkpoint is not a terminal failure"
            )
        prepared = self._prepare_or_recover(
            request,
            invocation,
            brief,
            checkpoint,
        )
        self._validate_original_scaffold(
            request,
            invocation,
            brief,
            prepared.root,
            checkpoint,
        )
        self._require_checkpoint_runtime_retry_authority(
            request,
            checkpoint,
            persisted_resume_ordinal=persisted_resume_ordinal,
            persisted_retry_authorization_ref=(
                persisted_retry_authorization_ref
            ),
            persisted_retry_authorization_binding_sha256=(
                persisted_retry_authorization_binding_sha256
            ),
        )
        return self._reconcile_failed(
            request=request,
            invocation=invocation,
            brief=brief,
            prepared=prepared,
            checkpoint=checkpoint,
        )

    async def reconcile_pending(
        self,
        request: FactoryDispatch,
        invocation: FactorySkillInvocationV1,
        brief: CodexBuildBriefV1,
    ) -> CompletedCodexBuild:
        """Reconcile only a previously claimed seal from durable local evidence."""

        if not isinstance(request.job, AgentFactoryJobV3):
            raise FactoryDispatchError("Codex reconciliation requires a V3 Factory job")
        checkpoint = self._checkpoint_store.load(invocation)
        if checkpoint is None:
            raise FactoryDispatchError(
                "Factory Codex pending seal has no checkpoint; inspection is required"
            )
        prepared = self._prepare_or_recover(
            request,
            invocation,
            brief,
            checkpoint,
        )
        self._validate_original_scaffold(
            request,
            invocation,
            brief,
            prepared.root,
            checkpoint,
        )
        if checkpoint.phase in {
            "implementation_running",
            "implementation_complete",
            "implementation_failed",
        }:
            self._require_checkpoint_runtime_retry_authority(request, checkpoint)
        if checkpoint.phase == "sealed":
            raise FactoryDispatchError(
                "Factory Codex sealed replay requires original persisted evidence"
            )
        if checkpoint.phase == "implementation_complete":
            self._load_reconciliation_receipt(
                request=request,
                invocation=invocation,
                brief=brief,
                prepared=prepared,
                checkpoint=checkpoint,
            )
            return self._seal_phase(invocation, prepared, checkpoint)
        if checkpoint.phase == "implementation_interrupted":
            _, receipt = self._load_reconciliation_receipt(
                request=request,
                invocation=invocation,
                brief=brief,
                prepared=prepared,
                checkpoint=checkpoint,
            )
            reason: FactoryCodexBuildInterruptionReason = (
                "codex_timed_out"
                if receipt["status"] == "timed_out"
                else "runtime_cancelled"
            )
            raise FactoryCodexBuildInterrupted(
                reason=reason,
                exit_code=int(receipt["exit_code"]),
                **_interruption_details(request, invocation, checkpoint),
            )
        if checkpoint.phase == "implementation_failed":
            raise self._reconcile_failed(
                request=request,
                invocation=invocation,
                brief=brief,
                prepared=prepared,
                checkpoint=checkpoint,
            )
        if checkpoint.phase != "implementation_running":
            raise FactoryDispatchError(
                "Factory Codex pending seal checkpoint is not reconcilable"
            )
        checkpoint = await self._reconcile_running(
            request=request,
            invocation=invocation,
            brief=brief,
            prepared=prepared,
            checkpoint=checkpoint,
        )
        if checkpoint.phase == "implementation_interrupted":
            receipt = self._load_reconciliation_receipt(
                request=request,
                invocation=invocation,
                brief=brief,
                prepared=prepared,
                checkpoint=checkpoint,
            )[1]
            reason: FactoryCodexBuildInterruptionReason = (
                "codex_timed_out"
                if receipt["status"] == "timed_out"
                else "runtime_cancelled"
            )
            raise FactoryCodexBuildInterrupted(
                reason=reason,
                exit_code=int(receipt["exit_code"]),
                **_interruption_details(request, invocation, checkpoint),
            )
        return self._seal_phase(invocation, prepared, checkpoint)

    def _reconcile_failed(
        self,
        *,
        request: FactoryDispatch,
        invocation: FactorySkillInvocationV1,
        brief: CodexBuildBriefV1,
        prepared: PreparedFactoryWorkspace,
        checkpoint: FactoryCodexBuildCheckpointV1,
    ) -> FactoryCodexBuildFailed:
        reason = checkpoint.implementation_failure_reason
        receipt_sha256 = checkpoint.terminal_receipt_sha256
        if reason is None or receipt_sha256 is None:
            raise FactoryDispatchError(
                "Factory Codex failed checkpoint evidence is incomplete"
            )
        terminal = self._load_terminal_evidence(
            request=request,
            invocation=invocation,
            brief=brief,
            prepared=prepared,
            checkpoint=checkpoint,
        )
        if self._output_manifest_store.load_pending(invocation) is not None:
            raise FactoryDispatchError(
                "Factory Codex failed checkpoint conflicts with output capture"
            )
        authority_start = invocation.lease.issued_at
        authority_deadline = min(
            invocation.lease.expires_at,
            request.job.deadline_at,
        )
        if checkpoint.resume_ordinal > 0:
            authorization = request.runtime_retry_authorization
            if authorization is not None:
                authority_start = authorization.issued_at
                authority_deadline = min(
                    authorization.expires_at,
                    request.job.deadline_at,
                )
            else:
                issued_at = checkpoint.runtime_retry_authorization_issued_at
                expires_at = checkpoint.runtime_retry_authorization_expires_at
                if issued_at is None or expires_at is None:
                    raise FactoryDispatchError(
                        "Factory Codex failed retry authority window is missing"
                    )
                authority_start = issued_at
                authority_deadline = min(expires_at, request.job.deadline_at)
        within_authority = (
            authority_start <= terminal.completed_at < authority_deadline
        )
        status = terminal.payload["status"]
        cleanup = terminal.payload["process_cleanup_status"]
        shape_is_valid = {
            "runtime_failed": status == "failed",
            "authority_expired": (
                status == "succeeded"
                and terminal.completed_at >= authority_deadline
            ),
            "required_output_invalid": status == "succeeded" and within_authority,
            "output_size_limit_exceeded": status == "succeeded" and within_authority,
            "evidence_failure": (
                status == "evidence_failed"
                and cleanup in {"not_required", "verified_cancelled"}
            ),
        }[reason]
        if not shape_is_valid:
            raise FactoryDispatchError(
                "Factory Codex failed checkpoint terminal evidence conflicts"
            )
        checkpoint_sha256 = hashlib.sha256(
            canonical_factory_codex_model(checkpoint)
        ).hexdigest()
        return FactoryCodexBuildFailed(
            reason=reason,
            checkpoint_ref=ArtifactRef(
                uri=f"artifact://factory/codex-checkpoint/{checkpoint_sha256}",
                sha256=checkpoint_sha256,
                media_type="application/json",
            ),
            terminal_receipt_ref=ArtifactRef(
                uri=(
                    "artifact://factory/codex-terminal-receipt/"
                    f"{receipt_sha256}"
                ),
                sha256=receipt_sha256,
                media_type="application/json",
            ),
        )

    async def _execute(
        self,
        request: FactoryDispatch,
        invocation: FactorySkillInvocationV1,
        brief: CodexBuildBriefV1,
        *,
        authorized_resume_ordinal: int | None,
    ) -> CompletedCodexBuild:
        if not isinstance(request.job, AgentFactoryJobV3):
            raise FactoryDispatchError("Codex execution requires a V3 Factory job")
        started_at = self._clock()
        authority_deadline = self._authority_deadline(
            request,
            invocation,
            authorized_resume=authorized_resume_ordinal is not None,
        )
        remaining_seconds = (authority_deadline - started_at).total_seconds()
        if (
            started_at < invocation.lease.issued_at
            or remaining_seconds < 1
        ):
            raise FactoryDispatchError("Codex build lease is not active")
        resume_lineage: _CodexResumeLineage | None = None
        checkpoint = self._checkpoint_store.load(invocation)
        if checkpoint is None:
            scaffold_files = self._scaffold_files(request, brief)
            manifest = self._scaffold_manifest(
                request, invocation, brief, scaffold_files
            )
            scaffold_manifest_sha256 = self._scaffold_manifest_store.persist(manifest)
            prepared = self._prepare_or_recover(
                request,
                invocation,
                brief,
                checkpoint,
            )
            self._clean_uncheckpointed_scaffold(scaffold_files, prepared.root)
            run_request = self._run_request(request, invocation, brief, prepared)
            authorized = self._authorizer.authorize(run_request)
            self._materialize_inputs(scaffold_files, prepared.root)
            checkpoint = self._checkpoint_store.advance(
                None,
                self._checkpoint(
                    request,
                    invocation,
                    brief,
                    prepared,
                    phase="scaffold_ready",
                    resume_ordinal=0,
                    terminal_receipt_sha256=None,
                    scaffold_manifest_sha256=scaffold_manifest_sha256,
                    previous=None,
                ),
            )
        else:
            prepared = self._prepare_or_recover(
                request,
                invocation,
                brief,
                checkpoint,
            )
            self._validate_original_scaffold(
                request,
                invocation,
                brief,
                prepared.root,
                checkpoint,
            )
            if checkpoint.phase == "sealed":
                raise FactoryDispatchError(
                    "Factory Codex sealed replay requires original persisted evidence"
                )
            if checkpoint.phase == "implementation_complete":
                return self._seal_phase(invocation, prepared, checkpoint)
            if checkpoint.phase == "implementation_running":
                raise FactoryDispatchError(
                    "Factory Codex implementation is already running or unresolved"
                )
            if checkpoint.phase in {
                "implementation_interrupted",
                "implementation_failed",
            }:
                if authorized_resume_ordinal is None:
                    if checkpoint.phase == "implementation_interrupted":
                        raise FactoryCodexBuildInterrupted(
                            reason="resume_authorization_required",
                            exit_code=None,
                            **_interruption_details(request, invocation, checkpoint),
                        )
                    raise FactoryDispatchError(
                        "Factory Codex evidence failure requires runtime retry authority"
                    )
                prior = self._load_terminal_evidence(
                    request=request,
                    invocation=invocation,
                    brief=brief,
                    prepared=prepared,
                    checkpoint=checkpoint,
                )
                interruption_is_resumable = (
                    checkpoint.phase == "implementation_interrupted"
                    and prior.payload["status"] in {"timed_out", "cancelled"}
                )
                evidence_failure_is_resumable = (
                    checkpoint.phase == "implementation_failed"
                    and checkpoint.implementation_failure_reason == "evidence_failure"
                    and prior.payload["status"] == "evidence_failed"
                    and prior.payload.get("failure_kind")
                    == "record_size_limit_exceeded"
                )
                required_output_failure_is_resumable = (
                    checkpoint.phase == "implementation_failed"
                    and checkpoint.implementation_failure_reason
                    == "required_output_invalid"
                    and prior.payload["status"] == "succeeded"
                    and prior.payload.get("exit_code") == 0
                )
                if (
                    not (
                        interruption_is_resumable
                        or evidence_failure_is_resumable
                        or required_output_failure_is_resumable
                    )
                    or prior.payload["process_cleanup_status"] == "unresolved"
                ):
                    raise FactoryDispatchError(
                        "Factory Codex prior terminal evidence is not resumable"
                    )
                resume_lineage = _CodexResumeLineage(
                    terminal_receipt_sha256=prior.receipt_sha256,
                    journal_sha256=prior.journal_sha256,
                    codex_thread_id=prior.codex_thread_id,
                )
                run_request = self._run_request(
                    request,
                    invocation,
                    brief,
                    prepared,
                    resume_thread_id=resume_lineage.codex_thread_id,
                )
                authorized = self._authorizer.authorize(run_request)
            else:
                run_request = self._run_request(request, invocation, brief, prepared)
                authorized = self._authorizer.authorize(run_request)
        checkpoint = await self._implementation_phase(
            request=request,
            invocation=invocation,
            brief=brief,
            prepared=prepared,
            checkpoint=checkpoint,
            run_request=run_request,
            authorized=authorized,
            authorized_resume_ordinal=authorized_resume_ordinal,
            resume_lineage=resume_lineage,
        )
        return self._seal_phase(invocation, prepared, checkpoint)

    def _authority_deadline(
        self,
        request: FactoryDispatch,
        invocation: FactorySkillInvocationV1,
        *,
        authorized_resume: bool,
    ) -> datetime:
        deadlines = [request.job.deadline_at]
        if authorized_resume:
            authorization = request.runtime_retry_authorization
            if authorization is None:
                raise FactoryDispatchError(
                    "Factory Codex resume requires Captain runtime retry authority"
                )
            deadlines.append(authorization.expires_at)
        else:
            deadlines.append(invocation.lease.expires_at)
        return min(deadlines)

    @staticmethod
    def _runtime_retry_checkpoint_binding(
        authorization: FactoryRuntimeRetryAuthorizationV1 | None,
    ) -> tuple[str | None, str | None, str | None]:
        if authorization is None:
            return None, None, None
        return (
            authorization.authorization_ref.uri,
            authorization.authorization_ref.sha256,
            hashlib.sha256(canonical_factory_codex_model(authorization)).hexdigest(),
        )

    def _require_checkpoint_runtime_retry_authority(
        self,
        request: FactoryDispatch,
        checkpoint: FactoryCodexBuildCheckpointV1,
        *,
        persisted_resume_ordinal: int | None = None,
        persisted_retry_authorization_ref: ArtifactRef | None = None,
        persisted_retry_authorization_binding_sha256: str | None = None,
    ) -> None:
        expected = (
            checkpoint.runtime_retry_authorization_uri,
            checkpoint.runtime_retry_authorization_sha256,
            checkpoint.runtime_retry_authorization_binding_sha256,
        )
        authorization = request.runtime_retry_authorization
        if authorization is not None:
            if (
                persisted_resume_ordinal is not None
                or persisted_retry_authorization_ref is not None
                or persisted_retry_authorization_binding_sha256 is not None
            ):
                raise FactoryDispatchError(
                    "Factory Codex retry authority source is ambiguous"
                )
            actual = self._runtime_retry_checkpoint_binding(authorization)
        else:
            expected_ordinal = checkpoint.resume_ordinal
            effective_ordinal = (
                0
                if persisted_resume_ordinal is None and expected_ordinal == 0
                else persisted_resume_ordinal
            )
            if effective_ordinal != expected_ordinal:
                raise FactoryDispatchError(
                    "Factory Codex checkpoint retry authority resume ordinal changed"
                )
        if authorization is None and persisted_retry_authorization_ref is not None:
            actual = (
                persisted_retry_authorization_ref.uri,
                persisted_retry_authorization_ref.sha256,
                persisted_retry_authorization_binding_sha256,
            )
        elif authorization is None:
            actual = (None, None, persisted_retry_authorization_binding_sha256)
        if actual != expected:
            raise FactoryDispatchError(
                "Factory Codex checkpoint runtime retry authority changed"
            )

    def _prepare_or_recover(
        self,
        request: FactoryDispatch,
        invocation: FactorySkillInvocationV1,
        brief: CodexBuildBriefV1,
        checkpoint: FactoryCodexBuildCheckpointV1 | None,
    ) -> PreparedFactoryWorkspace:
        recover = getattr(self._workspace_preparer, "prepare_or_recover", None)
        if callable(recover):
            return recover(request, invocation, brief, checkpoint)
        if checkpoint is not None:
            raise FactoryDispatchError(
                "Codex workspace preparer cannot recover an existing checkpoint"
            )
        prepare = getattr(self._workspace_preparer, "prepare", None)
        if not callable(prepare):
            raise FactoryDispatchError("Codex workspace preparer is invalid")
        return prepare(request, invocation, brief)

    def _run_request(
        self,
        request: FactoryDispatch,
        invocation: FactorySkillInvocationV1,
        brief: CodexBuildBriefV1,
        prepared: PreparedFactoryWorkspace,
        *,
        resume_thread_id: str | None = None,
    ) -> CodexRunRequest:
        session_id = f"factory-{invocation.invocation_id.hex[:24]}"
        prompt = _codex_prompt(request, invocation, brief)
        if resume_thread_id is not None:
            prompt += (
                "\n\nCAPTAIN RESUME REPAIR:\n"
                "Continue from the existing workspace and thread. Do not repeat broad "
                "repository inspection. Shell startup may be delayed on Windows; wait for "
                "each bounded command to finish and retry a transient shell probe before "
                "declaring infrastructure failure. Inspect which required output artifacts "
                "are missing, complete the candidate-scoped implementation and tests, then "
                "regenerate all three final artifacts. Do not finish with a blocked report "
                "while bounded shell commands eventually complete or required artifacts "
                "remain absent."
            )
        command = (
            ("codex", "exec", "resume", "--json", resume_thread_id, prompt)
            if resume_thread_id is not None
            else ("codex", "exec", "--json", prompt)
        )
        return CodexRunRequest(
            run_id=invocation.invocation_id.hex,
            trace_id=request.job.correlation_id.hex,
            batch_id=f"factory-{request.job.job_id.hex[:24]}",
            worker_id="factory-tool-integrator",
            session_id=session_id,
            claim_token=invocation.lease.lease_id,
            iteration=invocation.attempt,
            command=command,
            workspace=prepared.root,
            project_id=request.job.job_id.hex,
            claim_id=invocation.invocation_id.hex,
            fencing_token=invocation.subject_version,
            project_root=prepared.root,
        )

    async def _implementation_phase(
        self,
        *,
        request: FactoryDispatch,
        invocation: FactorySkillInvocationV1,
        brief: CodexBuildBriefV1,
        prepared: PreparedFactoryWorkspace,
        checkpoint: FactoryCodexBuildCheckpointV1,
        run_request: CodexRunRequest,
        authorized: AuthorizedCodexRun,
        authorized_resume_ordinal: int | None,
        resume_lineage: _CodexResumeLineage | None,
    ) -> FactoryCodexBuildCheckpointV1:
        authority_deadline = self._authority_deadline(
            request,
            invocation,
            authorized_resume=authorized_resume_ordinal is not None,
        )
        before_run = self._clock()
        remaining_whole_seconds = int(
            (authority_deadline - before_run).total_seconds()
        )
        if (
            before_run < invocation.lease.issued_at
            or remaining_whole_seconds < 1
        ):
            raise FactoryDispatchError(
                "Factory Codex build has less than one second remaining runtime"
            )
        next_ordinal = (
            checkpoint.resume_ordinal
            if authorized_resume_ordinal is None
            else authorized_resume_ordinal
        )
        session_id = f"factory-{invocation.invocation_id.hex[:24]}"
        state_path = self._process_state_path(invocation, next_ordinal)
        journal_path = self._journal_path(invocation, next_ordinal)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        runtime_seconds = min(
            self._settings.maximum_runtime_seconds,
            remaining_whole_seconds,
        )
        if authorized_resume_ordinal is not None:
            authorization = request.runtime_retry_authorization
            assert authorization is not None
            if resume_lineage is None:
                raise FactoryDispatchError(
                    "Factory Codex resume lineage is unavailable"
                )
            runtime_seconds = min(
                runtime_seconds,
                authorization.maximum_runtime_seconds,
            )
        runner = self._runner_factory(
            session_id=session_id,
            state_path=state_path,
            journal_path=journal_path,
            maximum_runtime_seconds=runtime_seconds,
            deadline_at=authority_deadline,
        )
        authority_deadline = self._authority_deadline(
            request,
            invocation,
            authorized_resume=authorized_resume_ordinal is not None,
        )
        before_runner_run = self._clock()
        final_remaining_whole_seconds = int(
            (authority_deadline - before_runner_run).total_seconds()
        )
        if (
            before_runner_run < invocation.lease.issued_at
            or final_remaining_whole_seconds < 1
            or runtime_seconds > final_remaining_whole_seconds
        ):
            raise FactoryDispatchError(
                "Factory Codex build has insufficient remaining runtime"
            )
        checkpoint = self._checkpoint_store.advance(
            checkpoint,
            self._checkpoint(
                request,
                invocation,
                brief,
                prepared,
                phase="implementation_running",
                resume_ordinal=next_ordinal,
                terminal_receipt_sha256=None,
                scaffold_manifest_sha256=checkpoint.scaffold_manifest_sha256,
                resume_lineage=resume_lineage,
                previous=checkpoint,
            ),
        )
        try:
            result = await runner.run(authorized)
        except CodexOutputEvidenceError as exc:
            self._raise_persisted_evidence_failure(
                error=exc,
                request=request,
                invocation=invocation,
                brief=brief,
                prepared=prepared,
                checkpoint=checkpoint,
                command=run_request.command,
                session_id=session_id,
                resume_lineage=resume_lineage,
            )
            raise AssertionError("unreachable")
        _validate_factory_codex_run_result(
            result=result,
            expected_journal_path=journal_path,
            journals_root=self._settings.state_root.resolve() / "journals",
        )
        malformed_output = _factory_codex_jsonl_evidence_error(result)
        if malformed_output is not None:
            self._raise_persisted_evidence_failure(
                error=malformed_output,
                request=request,
                invocation=invocation,
                brief=brief,
                prepared=prepared,
                checkpoint=checkpoint,
                command=run_request.command,
                session_id=session_id,
                resume_lineage=resume_lineage,
            )
            raise AssertionError("unreachable")
        completed_at = self._clock()
        session_receipt = _session_receipt(
            result=result,
            session_id=session_id,
            workspace_ref=brief.build_assignment.workspace_ref,
            base_revision=prepared.base_revision,
            command=run_request.command,
            completed_at=completed_at,
            resume_ordinal=checkpoint.resume_ordinal,
            parent_lineage=resume_lineage,
        )
        receipt_sha256 = hashlib.sha256(session_receipt).hexdigest()
        self._persist_session_receipt(
            invocation,
            session_receipt,
            checkpoint.resume_ordinal,
        )
        if result.process_cleanup_status == "unresolved":
            raise FactoryCodexCleanupUnresolved(
                "Codex process cleanup is unresolved; runtime resume is denied"
            )
        if result.terminal_status in {"timed_out", "cancelled"}:
            interrupted = self._checkpoint_store.advance(
                checkpoint,
                self._checkpoint(
                    request,
                    invocation,
                    brief,
                    prepared,
                    phase="implementation_interrupted",
                    resume_ordinal=checkpoint.resume_ordinal,
                    terminal_receipt_sha256=receipt_sha256,
                    scaffold_manifest_sha256=checkpoint.scaffold_manifest_sha256,
                    resume_lineage=resume_lineage,
                    previous=checkpoint,
                ),
            )
        if result.terminal_status == "timed_out":
            raise FactoryCodexBuildInterrupted(
                reason="codex_timed_out",
                exit_code=result.exit_code,
                **_interruption_details(request, invocation, interrupted),
            )
        if result.terminal_status == "cancelled":
            raise FactoryCodexBuildInterrupted(
                reason="runtime_cancelled",
                exit_code=result.exit_code,
                **_interruption_details(request, invocation, interrupted),
            )
        if result.exit_code != 0:
            self._checkpoint_store.advance(
                checkpoint,
                self._checkpoint(
                    request,
                    invocation,
                    brief,
                    prepared,
                    phase="implementation_failed",
                    resume_ordinal=checkpoint.resume_ordinal,
                    terminal_receipt_sha256=receipt_sha256,
                    scaffold_manifest_sha256=checkpoint.scaffold_manifest_sha256,
                    implementation_failure_reason="runtime_failed",
                    resume_lineage=resume_lineage,
                    previous=checkpoint,
                ),
            )
            raise FactoryDispatchError(
                f"Codex build process failed (exit {result.exit_code})"
            )
        if not invocation.lease.issued_at <= completed_at < authority_deadline:
            self._checkpoint_store.advance(
                checkpoint,
                self._checkpoint(
                    request,
                    invocation,
                    brief,
                    prepared,
                    phase="implementation_failed",
                    resume_ordinal=checkpoint.resume_ordinal,
                    terminal_receipt_sha256=receipt_sha256,
                    scaffold_manifest_sha256=checkpoint.scaffold_manifest_sha256,
                    implementation_failure_reason="authority_expired",
                    resume_lineage=resume_lineage,
                    previous=checkpoint,
                ),
            )
            raise FactoryDispatchError("Codex build completed outside Captain authority")
        try:
            output_manifest_uri, output_manifest_sha256 = (
                self._capture_and_persist_output_manifest(
                    invocation=invocation,
                    prepared=prepared,
                    resume_ordinal=checkpoint.resume_ordinal,
                    terminal_receipt_sha256=receipt_sha256,
                )
            )
        except FactoryCodexOutputCaptureError as exc:
            self._checkpoint_store.advance(
                checkpoint,
                self._checkpoint(
                    request,
                    invocation,
                    brief,
                    prepared,
                    phase="implementation_failed",
                    resume_ordinal=checkpoint.resume_ordinal,
                    terminal_receipt_sha256=receipt_sha256,
                    scaffold_manifest_sha256=checkpoint.scaffold_manifest_sha256,
                    implementation_failure_reason=exc.reason,
                    resume_lineage=resume_lineage,
                    previous=checkpoint,
                ),
            )
            raise
        return self._checkpoint_store.advance(
            checkpoint,
            self._checkpoint(
                request,
                invocation,
                brief,
                prepared,
                phase="implementation_complete",
                resume_ordinal=checkpoint.resume_ordinal,
                terminal_receipt_sha256=receipt_sha256,
                scaffold_manifest_sha256=checkpoint.scaffold_manifest_sha256,
                output_manifest_uri=output_manifest_uri,
                output_manifest_sha256=output_manifest_sha256,
                resume_lineage=resume_lineage,
                previous=checkpoint,
            ),
        )

    def _raise_persisted_evidence_failure(
        self,
        *,
        error: CodexOutputEvidenceError,
        request: FactoryDispatch,
        invocation: FactorySkillInvocationV1,
        brief: CodexBuildBriefV1,
        prepared: PreparedFactoryWorkspace,
        checkpoint: FactoryCodexBuildCheckpointV1,
        command: tuple[str, ...],
        session_id: str,
        resume_lineage: _CodexResumeLineage | None,
    ) -> None:
        if (
            error.process_cleanup_status is None
            or error.journal_path is None
            or error.journal_sha256 is None
            or error.journal_byte_count is None
            or error.event_count is None
            or error.event_types is None
        ):
            raise FactoryDispatchError(
                "Factory Codex output failure evidence is incomplete"
            ) from None
        expected_journal = self._journal_path(invocation, checkpoint.resume_ordinal)
        if error.journal_path != expected_journal:
            raise FactoryDispatchError(
                "Factory Codex output failure journal binding changed"
            ) from None
        completed_at = self._clock()
        receipt = _evidence_failure_receipt(
            error=error,
            session_id=session_id,
            workspace_ref=brief.build_assignment.workspace_ref,
            base_revision=prepared.base_revision,
            command=command,
            completed_at=completed_at,
            resume_ordinal=checkpoint.resume_ordinal,
        )
        receipt_sha256 = hashlib.sha256(receipt).hexdigest()
        self._persist_session_receipt(
            invocation,
            receipt,
            checkpoint.resume_ordinal,
        )
        terminal_checkpoint = checkpoint
        if error.process_cleanup_status in {
            "not_required",
            "verified_cancelled",
        }:
            terminal_checkpoint = self._checkpoint_store.advance(
                checkpoint,
                self._checkpoint(
                    request,
                    invocation,
                    brief,
                    prepared,
                    phase="implementation_failed",
                    resume_ordinal=checkpoint.resume_ordinal,
                    terminal_receipt_sha256=receipt_sha256,
                    scaffold_manifest_sha256=checkpoint.scaffold_manifest_sha256,
                    implementation_failure_reason="evidence_failure",
                    resume_lineage=resume_lineage,
                    previous=checkpoint,
                ),
            )
        checkpoint_bytes = canonical_factory_codex_model(terminal_checkpoint)
        checkpoint_sha256 = hashlib.sha256(checkpoint_bytes).hexdigest()
        raise FactoryCodexEvidenceFailure(
            process_cleanup_status=error.process_cleanup_status,
            checkpoint_ref=ArtifactRef(
                uri=f"artifact://factory/codex-checkpoint/{checkpoint_sha256}",
                sha256=checkpoint_sha256,
                media_type="application/json",
            ),
            terminal_receipt_ref=ArtifactRef(
                uri=f"artifact://factory/codex-terminal-receipt/{receipt_sha256}",
                sha256=receipt_sha256,
                media_type="application/json",
            ),
        ) from None

    async def _reconcile_running(
        self,
        *,
        request: FactoryDispatch,
        invocation: FactorySkillInvocationV1,
        brief: CodexBuildBriefV1,
        prepared: PreparedFactoryWorkspace,
        checkpoint: FactoryCodexBuildCheckpointV1,
    ) -> FactoryCodexBuildCheckpointV1:
        state_path = self._process_state_path(invocation, checkpoint.resume_ordinal)
        inspected_state: FactoryCodexProcessState | None = None
        if state_path.is_file():
            if self._process_inspector is None:
                raise FactoryDispatchError(
                    "Factory Codex process inspection is not configured"
                )
            inspected_state = await self._process_inspector.inspect(
                session_id=f"factory-{invocation.invocation_id.hex[:24]}",
                state_path=state_path,
            )
            if inspected_state == "active":
                raise FactoryDispatchError(
                    "Factory Codex process is active; inspection or cancellation is required"
                )
            if inspected_state != "lost":
                raise FactoryDispatchError(
                    "Factory Codex process identity is unresolved; inspection is required"
                )
        receipt_path = self._session_receipt_path(
            invocation, checkpoint.resume_ordinal
        )
        if inspected_state == "lost" and not receipt_path.is_file():
            self._persist_lost_process_interruption_receipt(
                request=request,
                invocation=invocation,
                brief=brief,
                prepared=prepared,
                checkpoint=checkpoint,
            )
        receipt_content, receipt = self._load_reconciliation_receipt(
            request=request,
            invocation=invocation,
            brief=brief,
            prepared=prepared,
            checkpoint=checkpoint,
        )
        cleanup_status = receipt["process_cleanup_status"]
        if cleanup_status == "unresolved" and inspected_state == "lost":
            receipt_content, receipt = (
                self._persist_verified_cleanup_session_receipt(
                    invocation=invocation,
                    checkpoint=checkpoint,
                    original_content=receipt_content,
                    original_receipt=receipt,
                )
            )
            cleanup_status = receipt["process_cleanup_status"]
        if cleanup_status == "unresolved":
            raise FactoryCodexCleanupUnresolved(
                "Factory Codex process cleanup is unresolved; inspection is required"
            )
        if inspected_state is None and not (
            receipt["status"] == "timed_out"
            and cleanup_status == "not_required"
        ):
            raise FactoryDispatchError(
                "Factory Codex process state is missing; inspection is required"
            )

        receipt_sha256 = hashlib.sha256(receipt_content).hexdigest()
        if receipt["status"] == "evidence_failed":
            failed = self._checkpoint_store.advance(
                checkpoint,
                self._checkpoint(
                    request,
                    invocation,
                    brief,
                    prepared,
                    phase="implementation_failed",
                    resume_ordinal=checkpoint.resume_ordinal,
                    terminal_receipt_sha256=receipt_sha256,
                    scaffold_manifest_sha256=checkpoint.scaffold_manifest_sha256,
                    implementation_failure_reason="evidence_failure",
                    previous=checkpoint,
                ),
            )
            checkpoint_sha256 = hashlib.sha256(
                canonical_factory_codex_model(failed)
            ).hexdigest()
            raise FactoryCodexEvidenceFailure(
                process_cleanup_status=cleanup_status,
                checkpoint_ref=ArtifactRef(
                    uri=f"artifact://factory/codex-checkpoint/{checkpoint_sha256}",
                    sha256=checkpoint_sha256,
                    media_type="application/json",
                ),
                terminal_receipt_ref=ArtifactRef(
                    uri=(
                        "artifact://factory/codex-terminal-receipt/"
                        f"{receipt_sha256}"
                    ),
                    sha256=receipt_sha256,
                    media_type="application/json",
                ),
            )
        if receipt["status"] in {"timed_out", "cancelled"}:
            phase: FactoryCodexBuildPhase = "implementation_interrupted"
            output_manifest_uri = None
            output_manifest_sha256 = None
        elif receipt["status"] == "succeeded":
            pending = self._output_manifest_store.load_pending(invocation)
            if pending is None:
                failed = self._checkpoint(
                    request,
                    invocation,
                    brief,
                    prepared,
                    phase="implementation_failed",
                    resume_ordinal=checkpoint.resume_ordinal,
                    terminal_receipt_sha256=receipt_sha256,
                    scaffold_manifest_sha256=checkpoint.scaffold_manifest_sha256,
                    implementation_failure_reason="required_output_invalid",
                    previous=checkpoint,
                )
                self._checkpoint_store.advance(checkpoint, failed)
                raise FactoryDispatchError(
                    "Factory Codex successful receipt has no immutable output capture"
                )
            manifest, output_manifest_uri, output_manifest_sha256 = pending
            self._validate_captured_outputs(
                invocation=invocation,
                manifest=manifest,
                manifest_sha256=output_manifest_sha256,
                resume_ordinal=checkpoint.resume_ordinal,
                terminal_receipt_sha256=receipt_sha256,
            )
            phase = "implementation_complete"
        else:
            raise FactoryDispatchError(
                "Factory Codex failed terminal receipt requires operator inspection"
            )
        return self._checkpoint_store.advance(
            checkpoint,
            self._checkpoint(
                request,
                invocation,
                brief,
                prepared,
                phase=phase,
                resume_ordinal=checkpoint.resume_ordinal,
                terminal_receipt_sha256=receipt_sha256,
                scaffold_manifest_sha256=checkpoint.scaffold_manifest_sha256,
                output_manifest_uri=output_manifest_uri,
                output_manifest_sha256=output_manifest_sha256,
                previous=checkpoint,
            ),
        )

    def _persist_lost_process_interruption_receipt(
        self,
        *,
        request: FactoryDispatch,
        invocation: FactorySkillInvocationV1,
        brief: CodexBuildBriefV1,
        prepared: PreparedFactoryWorkspace,
        checkpoint: FactoryCodexBuildCheckpointV1,
    ) -> None:
        """Materialize a bounded cancellation receipt after exact process loss."""

        journal_path = self._journal_path(invocation, checkpoint.resume_ordinal)
        try:
            journal = _read_bounded_codex_journal(journal_path)
            journal_lines = _bounded_codex_journal_lines(journal)
            completed_at = datetime.fromtimestamp(
                journal_path.stat().st_mtime,
                tz=timezone.utc,
            )
        except (OSError, UnicodeDecodeError) as exc:
            raise FactoryDispatchError(
                "Factory Codex lost process journal is unavailable"
            ) from exc
        run_request = self._run_request(
            request,
            invocation,
            brief,
            prepared,
            resume_thread_id=(
                checkpoint.parent_codex_thread_id
                if checkpoint.resume_ordinal > 0
                else None
            ),
        )
        parent_lineage: _CodexResumeLineage | None = None
        if checkpoint.resume_ordinal > 0:
            if (
                checkpoint.parent_terminal_receipt_sha256 is None
                or checkpoint.parent_journal_sha256 is None
            ):
                raise FactoryDispatchError(
                    "Factory Codex lost resumed process lineage is incomplete"
                )
            parent_lineage = _CodexResumeLineage(
                terminal_receipt_sha256=(
                    checkpoint.parent_terminal_receipt_sha256
                ),
                journal_sha256=checkpoint.parent_journal_sha256,
                codex_thread_id=checkpoint.parent_codex_thread_id,
            )
        receipt = _session_receipt(
            result=CodexRunResult(
                exit_code=130,
                terminal_status="cancelled",
                process_cleanup_status="not_required",
                journal_path=journal_path,
                journal_sha256=hashlib.sha256(journal).hexdigest(),
                artifact_references=(),
                jsonl_lines=journal_lines,
            ),
            session_id=f"factory-{invocation.invocation_id.hex[:24]}",
            workspace_ref=brief.build_assignment.workspace_ref,
            base_revision=prepared.base_revision,
            command=run_request.command,
            completed_at=completed_at,
            resume_ordinal=checkpoint.resume_ordinal,
            parent_lineage=parent_lineage,
        )
        self._persist_session_receipt(
            invocation,
            receipt,
            checkpoint.resume_ordinal,
        )

    def _persist_verified_cleanup_session_receipt(
        self,
        *,
        invocation: FactorySkillInvocationV1,
        checkpoint: FactoryCodexBuildCheckpointV1,
        original_content: bytes,
        original_receipt: dict[str, object],
    ) -> tuple[bytes, dict[str, object]]:
        if (
            original_receipt.get("process_cleanup_status") != "unresolved"
            or original_receipt.get("status") not in {"timed_out", "cancelled"}
            or checkpoint.terminal_receipt_sha256 is not None
            or hashlib.sha256(original_content).hexdigest()
            != hashlib.sha256(
                self._session_receipt_path(
                    invocation,
                    checkpoint.resume_ordinal,
                ).read_bytes()
            ).hexdigest()
        ):
            raise FactoryDispatchError(
                "Factory Codex cleanup verification source is invalid"
            )
        verified_receipt = dict(original_receipt)
        verified_receipt["process_cleanup_status"] = "verified_cancelled"
        content = json.dumps(
            verified_receipt,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        target = self._verified_session_receipt_path(
            invocation,
            checkpoint.resume_ordinal,
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_once(target, content)
        return content, verified_receipt

    def _load_reconciliation_receipt(
        self,
        *,
        request: FactoryDispatch,
        invocation: FactorySkillInvocationV1,
        brief: CodexBuildBriefV1,
        prepared: PreparedFactoryWorkspace,
        checkpoint: FactoryCodexBuildCheckpointV1,
    ) -> tuple[bytes, dict[str, object]]:
        terminal = self._load_terminal_evidence(
            request=request,
            invocation=invocation,
            brief=brief,
            prepared=prepared,
            checkpoint=checkpoint,
        )
        receipt = terminal.payload
        completed_at = terminal.completed_at
        authorized_resume = checkpoint.resume_ordinal > 0
        authority_deadline = self._authority_deadline(
            request,
            invocation,
            authorized_resume=authorized_resume,
        )
        authority_start = invocation.lease.issued_at
        if authorized_resume:
            authorization = request.runtime_retry_authorization
            if authorization is None or authorization.resume_ordinal != checkpoint.resume_ordinal:
                raise FactoryDispatchError(
                    "Factory Codex reconciliation requires original retry authority"
                )
            authority_start = authorization.issued_at
        if not authority_start <= completed_at < authority_deadline:
            raise FactoryDispatchError(
                "Factory Codex terminal receipt is outside Captain authority"
            )
        return terminal.content, receipt

    def _load_terminal_evidence(
        self,
        *,
        request: FactoryDispatch,
        invocation: FactorySkillInvocationV1,
        brief: CodexBuildBriefV1,
        prepared: PreparedFactoryWorkspace,
        checkpoint: FactoryCodexBuildCheckpointV1,
    ) -> _LoadedCodexTerminalEvidence:
        receipt_path = self._terminal_receipt_path(
            invocation,
            checkpoint,
        )
        try:
            content = receipt_path.read_bytes()
            receipt = json.loads(content)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise FactoryDispatchError(
                "Factory Codex terminal session receipt is missing or invalid"
            ) from None
        if not isinstance(receipt, dict) or content != json.dumps(
            receipt,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"):
            raise FactoryDispatchError(
                "Factory Codex terminal session receipt is not canonical"
            )
        if receipt.get("schema") == "captain.codex-session-error-receipt.v1":
            return self._load_error_terminal_evidence(
                request=request,
                invocation=invocation,
                brief=brief,
                prepared=prepared,
                checkpoint=checkpoint,
                content=content,
                receipt=receipt,
            )
        expected_keys = {
            "schema",
            "provider",
            "session_id",
            "codex_thread_id",
            "status",
            "exit_code",
            "process_cleanup_status",
            "workspace_ref",
            "base_revision",
            "command_sha256",
            "jsonl_sha256",
            "journal_sha256",
            "event_count",
            "event_types",
            "resume_ordinal",
            "parent_terminal_receipt_sha256",
            "parent_journal_sha256",
            "parent_codex_thread_id",
            "completed_at",
        }
        run_request = self._run_request(
            request,
            invocation,
            brief,
            prepared,
            resume_thread_id=(
                checkpoint.parent_codex_thread_id
                if checkpoint.resume_ordinal > 0
                else None
            ),
        )
        command_sha256 = receipt.get("command_sha256")
        command_binding_is_valid = (
            command_sha256
            == hashlib.sha256(
                "\0".join(run_request.command).encode("utf-8")
            ).hexdigest()
            if checkpoint.terminal_receipt_sha256 is None
            else isinstance(command_sha256, str)
            and re.fullmatch(r"[0-9a-f]{64}", command_sha256) is not None
        )
        if (
            set(receipt) != expected_keys
            or receipt["schema"] != "captain.codex-session-receipt.v1"
            or receipt["provider"] != "codex-cli"
            or receipt["session_id"]
            != f"factory-{invocation.invocation_id.hex[:24]}"
            or receipt["workspace_ref"] != checkpoint.workspace_ref
            or receipt["base_revision"] != checkpoint.base_revision
            or receipt["resume_ordinal"] != checkpoint.resume_ordinal
            or receipt["parent_terminal_receipt_sha256"]
            != checkpoint.parent_terminal_receipt_sha256
            or receipt["parent_journal_sha256"]
            != checkpoint.parent_journal_sha256
            or receipt["parent_codex_thread_id"]
            != checkpoint.parent_codex_thread_id
            or not command_binding_is_valid
        ):
            raise FactoryDispatchError(
                "Factory Codex terminal session receipt binding changed"
            )
        exit_code = receipt["exit_code"]
        status = receipt["status"]
        cleanup = receipt["process_cleanup_status"]
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            raise FactoryDispatchError("Factory Codex terminal receipt is inconsistent")
        expected_terminal = {
            0: ("succeeded", {"not_required"}),
            124: ("timed_out", {"not_required", "verified_cancelled", "unresolved"}),
            130: ("cancelled", {"not_required", "verified_cancelled", "unresolved"}),
        }.get(exit_code)
        if expected_terminal is None and exit_code not in {0, 124, 130}:
            expected_terminal = ("failed", {"not_required"})
        if (
            expected_terminal is None
            or status != expected_terminal[0]
            or cleanup not in expected_terminal[1]
        ):
            raise FactoryDispatchError("Factory Codex terminal receipt is inconsistent")

        journal_path = self._journal_path(invocation, checkpoint.resume_ordinal)
        try:
            journal = _read_bounded_codex_journal(journal_path)
        except OSError:
            raise FactoryDispatchError("Factory Codex JSONL journal is missing") from None
        journal_sha256 = hashlib.sha256(journal).hexdigest()
        if (
            receipt["jsonl_sha256"] != journal_sha256
            or receipt["journal_sha256"] != journal_sha256
        ):
            raise FactoryDispatchError("Factory Codex JSONL journal digest changed")
        events: list[dict[str, object]] = []
        try:
            for line in _bounded_codex_journal_lines(journal):
                if not line.strip():
                    continue
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError
                events.append(event)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise FactoryDispatchError("Factory Codex JSONL journal is invalid") from None
        event_types = list(
            canonical_codex_event_types(
                tuple(event.get("type") for event in events)
            )
        )
        observed_thread_id = _canonical_codex_thread_id(events)
        expected_thread_id = checkpoint.parent_codex_thread_id or observed_thread_id
        if (
            receipt["event_count"] != len(events)
            or receipt["event_types"] != event_types
            or receipt["codex_thread_id"] != expected_thread_id
            or (
                checkpoint.parent_codex_thread_id is not None
                and observed_thread_id is not None
                and observed_thread_id != checkpoint.parent_codex_thread_id
            )
            or (status == "succeeded" and not events)
        ):
            raise FactoryDispatchError("Factory Codex JSONL journal thread receipt changed")
        try:
            completed_at = datetime.fromisoformat(str(receipt["completed_at"]))
        except ValueError:
            raise FactoryDispatchError(
                "Factory Codex terminal receipt completion time is invalid"
            ) from None
        if (
            completed_at.tzinfo is None
            or completed_at.utcoffset() != timezone.utc.utcoffset(completed_at)
        ):
            raise FactoryDispatchError(
                "Factory Codex terminal receipt completion time is invalid"
            )
        digest = hashlib.sha256(content).hexdigest()
        if (
            checkpoint.terminal_receipt_sha256 is not None
            and checkpoint.terminal_receipt_sha256 != digest
        ):
            raise FactoryDispatchError(
                "Factory Codex terminal session receipt digest changed"
            )
        if (
            checkpoint.phase != "implementation_running"
            and checkpoint.terminal_receipt_sha256 is None
        ):
            raise FactoryDispatchError(
                "Factory Codex terminal session receipt digest is missing"
            )
        return _LoadedCodexTerminalEvidence(
            content=content,
            payload=receipt,
            receipt_sha256=digest,
            journal_sha256=journal_sha256,
            codex_thread_id=expected_thread_id,
            completed_at=completed_at,
        )

    def _load_error_terminal_evidence(
        self,
        *,
        request: FactoryDispatch,
        invocation: FactorySkillInvocationV1,
        brief: CodexBuildBriefV1,
        prepared: PreparedFactoryWorkspace,
        checkpoint: FactoryCodexBuildCheckpointV1,
        content: bytes,
        receipt: dict[str, object],
    ) -> _LoadedCodexTerminalEvidence:
        expected_keys = {
            "schema",
            "provider",
            "session_id",
            "status",
            "failure_kind",
            "process_cleanup_status",
            "workspace_ref",
            "base_revision",
            "command_sha256",
            "journal_sha256",
            "journal_byte_count",
            "event_count",
            "event_types",
            "resume_ordinal",
            "completed_at",
        }
        run_request = self._run_request(
            request,
            invocation,
            brief,
            prepared,
            resume_thread_id=(
                checkpoint.parent_codex_thread_id
                if checkpoint.resume_ordinal > 0
                else None
            ),
        )
        cleanup = receipt.get("process_cleanup_status")
        command_sha256 = receipt.get("command_sha256")
        command_binding_is_valid = (
            command_sha256
            == hashlib.sha256(
                "\0".join(run_request.command).encode("utf-8")
            ).hexdigest()
            if checkpoint.terminal_receipt_sha256 is None
            else isinstance(command_sha256, str)
            and re.fullmatch(r"[0-9a-f]{64}", command_sha256) is not None
        )
        if (
            set(receipt) != expected_keys
            or receipt.get("provider") != "codex-cli"
            or receipt.get("session_id")
            != f"factory-{invocation.invocation_id.hex[:24]}"
            or receipt.get("status") != "evidence_failed"
            or receipt.get("failure_kind") not in _CODEX_OUTPUT_FAILURE_KINDS
            or cleanup not in {"not_required", "verified_cancelled", "unresolved"}
            or receipt.get("workspace_ref") != checkpoint.workspace_ref
            or receipt.get("base_revision") != checkpoint.base_revision
            or receipt.get("resume_ordinal") != checkpoint.resume_ordinal
            or not command_binding_is_valid
        ):
            raise FactoryDispatchError(
                "Factory Codex error receipt binding changed"
            )
        journal_path = self._journal_path(invocation, checkpoint.resume_ordinal)
        journal = _read_bounded_codex_journal(journal_path)
        journal_sha256 = hashlib.sha256(journal).hexdigest()
        if (
            receipt.get("journal_sha256") != journal_sha256
            or isinstance(receipt.get("journal_byte_count"), bool)
            or receipt.get("journal_byte_count") != len(journal)
        ):
            raise FactoryDispatchError("Factory Codex error journal binding changed")
        try:
            lines = _bounded_codex_journal_lines(journal)
        except UnicodeDecodeError:
            raise FactoryDispatchError("Factory Codex error journal is invalid") from None
        events: list[dict[str, object]] = []
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                break
            if not isinstance(value, dict):
                break
            events.append(value)
        event_types = list(
            canonical_codex_event_types(
                tuple(event.get("type") for event in events)
            )
        )
        event_count = receipt.get("event_count")
        if (
            isinstance(event_count, bool)
            or event_count != len(events)
            or receipt.get("event_types") != event_types
        ):
            raise FactoryDispatchError("Factory Codex error event metadata changed")
        try:
            completed_at = datetime.fromisoformat(str(receipt.get("completed_at")))
        except ValueError:
            raise FactoryDispatchError(
                "Factory Codex terminal receipt completion time is invalid"
            ) from None
        if (
            completed_at.tzinfo is None
            or completed_at.utcoffset() != timezone.utc.utcoffset(completed_at)
        ):
            raise FactoryDispatchError(
                "Factory Codex terminal receipt completion time is invalid"
            )
        digest = hashlib.sha256(content).hexdigest()
        if (
            checkpoint.terminal_receipt_sha256 is not None
            and checkpoint.terminal_receipt_sha256 != digest
        ):
            raise FactoryDispatchError(
                "Factory Codex terminal session receipt digest changed"
            )
        if (
            checkpoint.phase != "implementation_running"
            and checkpoint.terminal_receipt_sha256 is None
        ):
            raise FactoryDispatchError(
                "Factory Codex terminal session receipt digest is missing"
            )
        return _LoadedCodexTerminalEvidence(
            content=content,
            payload=receipt,
            receipt_sha256=digest,
            journal_sha256=journal_sha256,
            codex_thread_id=_canonical_codex_thread_id(events),
            completed_at=completed_at,
        )

    def _seal_phase(
        self,
        invocation: FactorySkillInvocationV1,
        prepared: PreparedFactoryWorkspace,
        checkpoint: FactoryCodexBuildCheckpointV1,
    ) -> CompletedCodexBuild:
        receipt_path = self._session_receipt_path(
            invocation, checkpoint.resume_ordinal
        )
        try:
            session_receipt = receipt_path.read_bytes()
        except OSError as exc:
            raise FactoryDispatchError("Codex terminal session receipt is missing") from exc
        if hashlib.sha256(session_receipt).hexdigest() != checkpoint.terminal_receipt_sha256:
            raise FactoryDispatchError("Codex terminal session receipt digest changed")
        output_snapshot_root = self._validate_output_manifest(
            invocation=invocation,
            checkpoint=checkpoint,
        )
        try:
            receipt_payload = json.loads(session_receipt)
            completed_at = datetime.fromisoformat(receipt_payload["completed_at"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            raise FactoryDispatchError("Codex terminal session receipt is invalid") from None
        return CompletedCodexBuild(
            workspace_root=prepared.root,
            codex_session_receipt=session_receipt,
            candidate_manifest_path=_OUTPUT_PATHS[0],
            source_archive_path=_OUTPUT_PATHS[1],
            test_evidence_paths=(_OUTPUT_PATHS[2],),
            completed_at=completed_at,
            output_snapshot_root=output_snapshot_root,
        )

    def replay_sealed(
        self,
        invocation: FactorySkillInvocationV1,
    ) -> CodexBuildEvidenceV1 | None:
        checkpoint = self._checkpoint_store.load(invocation)
        if checkpoint is None:
            evidence = self._sealed_evidence_store.load(invocation)
            if evidence is not None:
                raise FactoryDispatchError(
                    "Factory Codex sealed evidence has no checkpoint"
                )
            return None
        if checkpoint.phase in {"implementation_complete", "sealed"}:
            self._validate_output_manifest(
                invocation=invocation,
                checkpoint=checkpoint,
            )
        evidence = self._sealed_evidence_store.load(invocation)
        if evidence is None:
            if checkpoint.phase == "sealed":
                raise FactoryDispatchError(
                    "Factory Codex original sealed evidence is missing"
                )
            return None
        if checkpoint.phase not in {"implementation_complete", "sealed"}:
            raise FactoryDispatchError(
                "Factory Codex sealed evidence appeared before implementation complete"
            )
        return self._bind_or_validate_sealed_checkpoint(
            invocation,
            checkpoint,
            evidence,
        )

    def validate_replay_authority(
        self,
        request: FactoryDispatch,
        invocation: FactorySkillInvocationV1,
    ) -> None:
        checkpoint = self._checkpoint_store.load(invocation)
        if checkpoint is not None and checkpoint.phase in {
            "implementation_complete",
            "sealed",
        }:
            self._validate_output_manifest(
                invocation=invocation,
                checkpoint=checkpoint,
            )
        evidence = self._sealed_evidence_store.load(invocation)
        if (
            checkpoint is None
            or evidence is None
            or checkpoint.phase not in {"implementation_complete", "sealed"}
        ):
            return
        self._require_checkpoint_runtime_retry_authority(request, checkpoint)

    def validate_completed_outputs(
        self,
        invocation: FactorySkillInvocationV1,
        completed: CompletedCodexBuild,
    ) -> None:
        checkpoint = self._checkpoint_store.load(invocation)
        if checkpoint is None or checkpoint.phase != "implementation_complete":
            raise FactoryDispatchError("Factory Codex build is not ready to seal")
        if (
            completed.workspace_root.resolve() != checkpoint.workspace_root
            or completed.candidate_manifest_path != _OUTPUT_PATHS[0]
            or completed.source_archive_path != _OUTPUT_PATHS[1]
            or completed.test_evidence_paths != (_OUTPUT_PATHS[2],)
            or hashlib.sha256(completed.codex_session_receipt).hexdigest()
            != checkpoint.terminal_receipt_sha256
        ):
            raise FactoryDispatchError(
                "Factory Codex completed output binding changed"
            )
        output_snapshot_root = self._validate_output_manifest(
            invocation=invocation,
            checkpoint=checkpoint,
        )
        if (
            completed.output_snapshot_root is None
            or completed.output_snapshot_root.resolve() != output_snapshot_root
        ):
            raise FactoryDispatchError(
                "Factory Codex completed snapshot binding changed"
            )

    def validate_issued_receipt(
        self,
        invocation: FactorySkillInvocationV1,
        completed: CompletedCodexBuild,
        receipt: CodexBuildReceiptV1,
    ) -> None:
        self.validate_completed_outputs(invocation, completed)
        checkpoint = self._checkpoint_store.load(invocation)
        if checkpoint is None or checkpoint.phase != "implementation_complete":
            raise FactoryDispatchError("Factory Codex build is not ready to seal")
        self._require_build_receipt_output_binding(
            invocation,
            checkpoint,
            receipt,
        )

    def persist_sealed(
        self,
        invocation: FactorySkillInvocationV1,
        completed: CompletedCodexBuild,
        evidence: CodexBuildEvidenceV1,
    ) -> CodexBuildEvidenceV1:
        checkpoint = self._checkpoint_store.load(invocation)
        if checkpoint is None or checkpoint.phase != "implementation_complete":
            raise FactoryDispatchError("Factory Codex build is not ready to seal")
        receipt_sha256 = hashlib.sha256(completed.codex_session_receipt).hexdigest()
        if checkpoint.terminal_receipt_sha256 != receipt_sha256:
            raise FactoryDispatchError("Factory Codex seal receipt binding changed")
        if evidence.build_receipt.codex_session_ref.sha256 != receipt_sha256:
            raise FactoryDispatchError("Factory Codex evidence session receipt changed")
        self.validate_completed_outputs(invocation, completed)
        self._require_receipt_output_binding(invocation, checkpoint, evidence)
        self._sealed_evidence_store.persist(evidence)
        return self._bind_or_validate_sealed_checkpoint(
            invocation,
            checkpoint,
            evidence,
        )

    def _bind_or_validate_sealed_checkpoint(
        self,
        invocation: FactorySkillInvocationV1,
        checkpoint: FactoryCodexBuildCheckpointV1,
        evidence: CodexBuildEvidenceV1,
    ) -> CodexBuildEvidenceV1:
        self._validate_output_manifest(
            invocation=invocation,
            checkpoint=checkpoint,
        )
        if (
            checkpoint.terminal_receipt_sha256
            != evidence.build_receipt.codex_session_ref.sha256
        ):
            raise FactoryDispatchError(
                "Factory Codex original sealed evidence session changed"
            )
        self._require_receipt_output_binding(invocation, checkpoint, evidence)
        evidence_sha256 = self._sealed_evidence_store.digest(evidence)
        receipt_ref = evidence.build_receipt_ref
        if checkpoint.phase == "sealed":
            if (
                checkpoint.sealed_evidence_sha256 != evidence_sha256
                or checkpoint.sealed_build_receipt_uri != receipt_ref.uri
                or checkpoint.sealed_build_receipt_sha256 != receipt_ref.sha256
            ):
                raise FactoryDispatchError(
                    "Factory Codex original sealed evidence binding changed"
                )
            return evidence
        target = checkpoint.model_copy(
            update={
                "phase": "sealed",
                "sealed_evidence_sha256": evidence_sha256,
                "sealed_build_receipt_uri": receipt_ref.uri,
                "sealed_build_receipt_sha256": receipt_ref.sha256,
                "updated_at": self._checkpoint_time(checkpoint),
            }
        )
        self._checkpoint_store.advance(checkpoint, target)
        return evidence

    def _capture_and_persist_output_manifest(
        self,
        *,
        invocation: FactorySkillInvocationV1,
        prepared: PreparedFactoryWorkspace,
        resume_ordinal: int,
        terminal_receipt_sha256: str,
    ) -> tuple[str, str]:
        invocation_sha256 = hashlib.sha256(
            canonical_factory_codex_model(invocation)
        ).hexdigest()
        artifacts, snapshot_staging = self._capture_output_artifacts(prepared.root)
        manifest = FactoryCodexOutputManifestV1(
            schema="captain.factory-codex-output-manifest.v1",
            job_id=invocation.job_id,
            correlation_id=invocation.correlation_id,
            attempt=invocation.attempt,
            invocation_id=invocation.invocation_id,
            workspace_ref=invocation.lease.workspace_ref,
            invocation_sha256=invocation_sha256,
            resume_ordinal=resume_ordinal,
            terminal_receipt_sha256=terminal_receipt_sha256,
            artifacts=artifacts,
        )
        try:
            return self._output_manifest_store.persist(
                manifest,
                snapshot_staging=snapshot_staging,
            )
        except BaseException:
            shutil.rmtree(snapshot_staging, ignore_errors=True)
            raise

    def _validate_output_manifest(
        self,
        *,
        invocation: FactorySkillInvocationV1,
        checkpoint: FactoryCodexBuildCheckpointV1,
    ) -> Path:
        uri = checkpoint.output_manifest_uri
        digest = checkpoint.output_manifest_sha256
        if uri is None or digest is None:
            raise FactoryDispatchError(
                "Factory Codex original output manifest binding is missing"
            )
        manifest = self._output_manifest_store.load(
            invocation,
            uri=uri,
            sha256=digest,
        )
        return self._validate_captured_outputs(
            invocation=invocation,
            manifest=manifest,
            manifest_sha256=digest,
            resume_ordinal=checkpoint.resume_ordinal,
            terminal_receipt_sha256=checkpoint.terminal_receipt_sha256,
        )

    def _capture_output_artifacts(
        self,
        workspace_root: Path,
    ) -> tuple[tuple[FactoryCodexOutputArtifactV1, ...], Path]:
        staging = (
            self._settings.state_root.resolve()
            / "output-capture-staging"
            / uuid4().hex
        )
        try:
            staging.mkdir(parents=True, exist_ok=False)
            artifacts = self._scan_output_artifacts(
                workspace_root,
                snapshot_destination=staging,
            )
            return artifacts, staging
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def _validate_captured_outputs(
        self,
        *,
        invocation: FactorySkillInvocationV1,
        manifest: FactoryCodexOutputManifestV1,
        manifest_sha256: str,
        resume_ordinal: int,
        terminal_receipt_sha256: str | None,
    ) -> Path:
        invocation_sha256 = hashlib.sha256(
            canonical_factory_codex_model(invocation)
        ).hexdigest()
        if (
            manifest.invocation_sha256 != invocation_sha256
            or manifest.resume_ordinal != resume_ordinal
            or terminal_receipt_sha256 is None
            or manifest.terminal_receipt_sha256 != terminal_receipt_sha256
        ):
            raise FactoryDispatchError(
                "Factory Codex output manifest checkpoint binding changed"
            )
        snapshot_root = self._output_manifest_store.snapshot_root(
            manifest,
            sha256=manifest_sha256,
        )
        snapshot = self._scan_output_artifacts(snapshot_root)
        if snapshot != manifest.artifacts:
            raise FactoryDispatchError(
                "Factory Codex output snapshot binding changed"
            )
        return snapshot_root.resolve()

    def _scan_output_artifacts(
        self,
        workspace_root: Path,
        *,
        snapshot_destination: Path | None = None,
    ) -> tuple[FactoryCodexOutputArtifactV1, ...]:
        try:
            root = workspace_root.resolve(strict=True)
            if workspace_root.is_symlink() or not root.is_dir():
                raise OSError
        except (OSError, RuntimeError):
            raise FactoryCodexOutputCaptureError(
                "Codex output workspace is unavailable",
                reason="required_output_invalid",
            ) from None
        artifacts: list[FactoryCodexOutputArtifactV1] = []
        aggregate_size = 0
        for relative in sorted(_OUTPUT_PATHS):
            maximum = (
                self._settings.maximum_candidate_archive_bytes
                if relative == "candidate.zip"
                else self._settings.maximum_json_artifact_bytes
            )
            destination = (
                snapshot_destination / relative
                if snapshot_destination is not None
                else None
            )
            artifact, aggregate_size = self._stream_output_artifact(
                root=root,
                relative=relative,
                destination=destination,
                maximum_size=maximum,
                aggregate_size=aggregate_size,
            )
            artifacts.append(artifact)
        return tuple(artifacts)

    def _stream_output_artifact(
        self,
        *,
        root: Path,
        relative: str,
        destination: Path | None,
        maximum_size: int,
        aggregate_size: int,
    ) -> tuple[FactoryCodexOutputArtifactV1, int]:
        target = root / relative
        descriptor: int | None = None
        destination_stream = None
        try:
            before_path = os.stat(target, follow_symlinks=False)
            if not self._is_plain_regular_file(before_path):
                raise OSError
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(target, flags)
            opened = os.fstat(descriptor)
            after_open_path = os.stat(target, follow_symlinks=False)
            if (
                not self._is_plain_regular_file(opened)
                or self._file_identity(before_path) != self._file_identity(opened)
                or self._file_identity(after_open_path) != self._file_identity(opened)
            ):
                raise OSError
            if destination is not None:
                destination_stream = destination.open("xb")
            digest = hashlib.sha256()
            size = 0
            with os.fdopen(descriptor, "rb", closefd=True) as source:
                descriptor = None
                while chunk := source.read(64 * 1024):
                    size += len(chunk)
                    if (
                        size > maximum_size
                        or aggregate_size + size
                        > self._settings.maximum_output_bytes
                    ):
                        raise FactoryCodexOutputCaptureError(
                            "Codex output exceeds configured size limit",
                            reason="output_size_limit_exceeded",
                        )
                    digest.update(chunk)
                    if destination_stream is not None:
                        destination_stream.write(chunk)
                after_read = os.fstat(source.fileno())
            if size == 0:
                raise FactoryCodexOutputCaptureError(
                    "Codex required output artifact is empty",
                    reason="required_output_invalid",
                )
            after_path = os.stat(target, follow_symlinks=False)
            if (
                self._file_identity(after_read) != self._file_identity(opened)
                or self._file_identity(after_path) != self._file_identity(opened)
                or self._file_version(after_read) != self._file_version(opened)
            ):
                raise OSError
            if destination_stream is not None:
                destination_stream.flush()
                os.fsync(destination_stream.fileno())
                destination_stream.close()
                destination_stream = None
            return (
                FactoryCodexOutputArtifactV1(
                    relative_path=relative,
                    sha256=digest.hexdigest(),
                    size=size,
                ),
                aggregate_size + size,
            )
        except FactoryCodexOutputCaptureError:
            raise
        except (OSError, RuntimeError):
            raise FactoryCodexOutputCaptureError(
                f"Codex omitted or changed required build artifact: {relative}",
                reason="required_output_invalid",
            ) from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if destination_stream is not None:
                destination_stream.close()
            if destination is not None and destination.exists():
                try:
                    if destination.stat().st_size == 0:
                        destination.unlink()
                except OSError:
                    pass

    @staticmethod
    def _is_plain_regular_file(metadata: os.stat_result) -> bool:
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        file_attributes = getattr(metadata, "st_file_attributes", 0)
        return (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_nlink == 1
            and not (reparse_flag and file_attributes & reparse_flag)
        )

    @staticmethod
    def _file_identity(metadata: os.stat_result) -> tuple[int, int, int]:
        return (
            metadata.st_dev,
            metadata.st_ino,
            stat.S_IFMT(metadata.st_mode),
        )

    @staticmethod
    def _file_version(metadata: os.stat_result) -> tuple[int, int, int]:
        return (
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )

    def _require_receipt_output_binding(
        self,
        invocation: FactorySkillInvocationV1,
        checkpoint: FactoryCodexBuildCheckpointV1,
        evidence: CodexBuildEvidenceV1,
    ) -> None:
        self._require_build_receipt_output_binding(
            invocation,
            checkpoint,
            evidence.build_receipt,
        )

    def _require_build_receipt_output_binding(
        self,
        invocation: FactorySkillInvocationV1,
        checkpoint: FactoryCodexBuildCheckpointV1,
        receipt: CodexBuildReceiptV1,
    ) -> None:
        uri = checkpoint.output_manifest_uri
        digest = checkpoint.output_manifest_sha256
        if uri is None or digest is None:
            raise FactoryDispatchError(
                "Factory Codex original output manifest binding is missing"
            )
        manifest = self._output_manifest_store.load(
            invocation,
            uri=uri,
            sha256=digest,
        )
        by_path = {
            item.relative_path: item.sha256
            for item in manifest.artifacts
        }
        candidate_sha256 = by_path["factory-candidate.json"]
        source_sha256 = by_path["candidate.zip"]
        test_sha256 = by_path["test-evidence.json"]
        if (
            self._artifact_ref_binding(receipt.candidate_manifest_ref)
            != (
                "artifact://captain-codex-build/"
                f"candidate-manifest/{candidate_sha256}",
                candidate_sha256,
                "application/json",
            )
            or self._artifact_ref_binding(receipt.source_archive_ref)
            != (
                f"artifact://captain-codex-build/codex-source/{source_sha256}",
                source_sha256,
                "application/zip",
            )
            or tuple(
                self._artifact_ref_binding(item)
                for item in receipt.test_evidence_refs
            )
            != (
                (
                    f"artifact://captain-codex-build/test-evidence/{test_sha256}",
                    test_sha256,
                    "application/json",
                ),
            )
        ):
            raise FactoryDispatchError(
                "Factory Codex issued receipt output binding changed"
            )

    @staticmethod
    def _artifact_ref_binding(reference: ArtifactRef) -> tuple[str, str, str]:
        return reference.uri, reference.sha256, reference.media_type

    def _scaffold_files(
        self,
        request: FactoryDispatch,
        brief: CodexBuildBriefV1,
    ) -> dict[str, bytes]:
        bindings = (
            ("job-input.md", request.job.input_ref),
            ("compiled-spec.json", request.job.compiled_spec_ref),
            ("dependency-graph.json", request.job.dependency_graph_ref),
            ("codex-build-instructions.md", brief.prompt_ref),
        )
        files: dict[str, bytes] = {}
        for name, reference in bindings:
            content = self._artifact_reader.read_bytes(reference)
            if hashlib.sha256(content).hexdigest() != reference.sha256:
                raise FactoryDispatchError("Factory build input digest changed")
            files[name] = content
        try:
            instructions = json.loads(files["codex-build-instructions.md"])
        except (UnicodeDecodeError, json.JSONDecodeError):
            instructions = None
        prior_uri = (
            instructions.get("prior candidate ref")
            if isinstance(instructions, dict)
            else None
        )
        if prior_uri is not None:
            matching = tuple(
                reference
                for reference in brief.context_refs
                if reference.uri == prior_uri
                and reference.media_type == "application/zip"
                and reference.uri.startswith(
                    "artifact://minibook-creation/forge-source/"
                )
            )
            if len(matching) != 1:
                raise FactoryDispatchError(
                    "Factory prior candidate reference is not uniquely bound"
                )
            prior_content = self._artifact_reader.read_bytes(matching[0])
            if hashlib.sha256(prior_content).hexdigest() != matching[0].sha256:
                raise FactoryDispatchError("Factory prior candidate digest changed")
            files["prior-candidate.zip"] = prior_content
        files["codex-build-brief.json"] = canonical_factory_codex_model(brief)
        return files

    @staticmethod
    def _scaffold_manifest(
        request: FactoryDispatch,
        invocation: FactorySkillInvocationV1,
        brief: CodexBuildBriefV1,
        files: Mapping[str, bytes],
    ) -> FactoryCodexScaffoldManifestV1:
        return FactoryCodexScaffoldManifestV1(
            schema="captain.factory-codex-scaffold-manifest.v1",
            job_id=request.job.job_id,
            correlation_id=request.job.correlation_id,
            attempt=invocation.attempt,
            invocation_id=invocation.invocation_id,
            workspace_ref=brief.build_assignment.workspace_ref,
            files=tuple(
                FactoryCodexScaffoldFileV1(
                    filename=name,
                    sha256=hashlib.sha256(files[name]).hexdigest(),
                )
                for name in sorted(files)
            ),
        )

    @staticmethod
    def _materialize_inputs(files: Mapping[str, bytes], workspace: Path) -> None:
        destination = workspace / ".captain-inputs"
        staging = workspace / ".captain-inputs.staging"
        staging.mkdir(parents=False, exist_ok=False)
        for name, content in files.items():
            target = staging / name
            with target.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        os.replace(staging, destination)

    @staticmethod
    def _clean_uncheckpointed_scaffold(
        files: Mapping[str, bytes],
        workspace: Path,
    ) -> None:
        workspace = workspace.resolve(strict=True)
        expected = {
            name: hashlib.sha256(content).hexdigest()
            for name, content in files.items()
        }
        for directory_name in (".captain-inputs", ".captain-inputs.staging"):
            directory = workspace / directory_name
            if not directory.exists():
                continue
            resolved = directory.resolve(strict=True)
            if (
                resolved.parent != workspace
                or directory.is_symlink()
                or not directory.is_dir()
            ):
                raise FactoryDispatchError("Codex retry scaffold path is invalid")
            actual_names = {item.name for item in directory.iterdir()}
            if not actual_names.issubset(expected):
                raise FactoryDispatchError("Codex retry scaffold contains extra files")
            for name in actual_names:
                target = directory / name
                if target.is_symlink() or not target.is_file():
                    raise FactoryDispatchError("Codex retry scaffold input is invalid")
                if hashlib.sha256(target.read_bytes()).hexdigest() != expected[name]:
                    raise FactoryDispatchError("Codex retry scaffold input changed")
            shutil.rmtree(directory)

    def _validate_original_scaffold(
        self,
        request: FactoryDispatch,
        invocation: FactorySkillInvocationV1,
        brief: CodexBuildBriefV1,
        workspace: Path,
        checkpoint: FactoryCodexBuildCheckpointV1,
    ) -> None:
        original = self._scaffold_manifest_store.load(invocation)
        if original is None:
            raise FactoryDispatchError(
                "Factory Codex original scaffold manifest is missing"
            )
        original_bytes = canonical_factory_codex_model(original)
        if hashlib.sha256(original_bytes).hexdigest() != checkpoint.scaffold_manifest_sha256:
            raise FactoryDispatchError(
                "Factory Codex original scaffold manifest digest changed"
            )
        current_files = self._scaffold_files(request, brief)
        current = self._scaffold_manifest(
            request, invocation, brief, current_files
        )
        if canonical_factory_codex_model(current) != original_bytes:
            raise FactoryDispatchError(
                "Factory Codex original scaffold manifest binding changed"
            )
        destination = workspace / ".captain-inputs"
        expected = {item.filename: item.sha256 for item in original.files}
        try:
            actual_names = {item.name for item in destination.iterdir()}
        except OSError as exc:
            raise FactoryDispatchError("Factory build materialized inputs are missing") from exc
        if actual_names != set(expected):
            raise FactoryDispatchError("Factory build materialized input set changed")
        for name, digest in expected.items():
            target = destination / name
            if target.is_symlink() or not target.is_file():
                raise FactoryDispatchError("Factory build materialized input changed")
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise FactoryDispatchError("Factory build materialized input digest changed")

    def _checkpoint(
        self,
        request: FactoryDispatch,
        invocation: FactorySkillInvocationV1,
        brief: CodexBuildBriefV1,
        prepared: PreparedFactoryWorkspace,
        *,
        phase: FactoryCodexBuildPhase,
        resume_ordinal: int,
        terminal_receipt_sha256: str | None,
        scaffold_manifest_sha256: str,
        output_manifest_uri: str | None = None,
        output_manifest_sha256: str | None = None,
        implementation_failure_reason: Literal[
            "required_output_invalid",
            "output_size_limit_exceeded",
            "runtime_failed",
            "authority_expired",
            "evidence_failure",
        ] | None = None,
        previous: FactoryCodexBuildCheckpointV1 | None,
        resume_lineage: _CodexResumeLineage | None = None,
    ) -> FactoryCodexBuildCheckpointV1:
        retry_uri, retry_sha256, retry_binding_sha256 = (
            self._runtime_retry_checkpoint_binding(
                request.runtime_retry_authorization
                if resume_ordinal > 0
                else None
            )
        )
        retry_authorization = (
            request.runtime_retry_authorization if resume_ordinal > 0 else None
        )
        return FactoryCodexBuildCheckpointV1(
            job_id=request.job.job_id,
            correlation_id=request.job.correlation_id,
            attempt=invocation.attempt,
            invocation_id=invocation.invocation_id,
            workspace_ref=brief.build_assignment.workspace_ref,
            workspace_root=prepared.root.resolve(),
            base_revision=prepared.base_revision,
            brief_sha256=hashlib.sha256(
                canonical_factory_codex_model(brief)
            ).hexdigest(),
            scaffold_manifest_sha256=scaffold_manifest_sha256,
            output_manifest_uri=output_manifest_uri,
            output_manifest_sha256=output_manifest_sha256,
            implementation_failure_reason=implementation_failure_reason,
            phase=phase,
            resume_ordinal=resume_ordinal,
            terminal_receipt_sha256=terminal_receipt_sha256,
            parent_terminal_receipt_sha256=(
                resume_lineage.terminal_receipt_sha256
                if resume_lineage is not None
                else (
                    previous.parent_terminal_receipt_sha256
                    if previous is not None
                    else None
                )
            ),
            parent_journal_sha256=(
                resume_lineage.journal_sha256
                if resume_lineage is not None
                else (
                    previous.parent_journal_sha256
                    if previous is not None
                    else None
                )
            ),
            parent_codex_thread_id=(
                resume_lineage.codex_thread_id
                if resume_lineage is not None
                else (
                    previous.parent_codex_thread_id
                    if previous is not None
                    else None
                )
            ),
            runtime_retry_authorization_uri=retry_uri,
            runtime_retry_authorization_sha256=retry_sha256,
            runtime_retry_authorization_binding_sha256=retry_binding_sha256,
            runtime_retry_authorization_issued_at=(
                retry_authorization.issued_at
                if retry_authorization is not None
                else None
            ),
            runtime_retry_authorization_expires_at=(
                retry_authorization.expires_at
                if retry_authorization is not None
                else None
            ),
            updated_at=self._checkpoint_time(previous),
        )

    def _checkpoint_time(
        self,
        previous: FactoryCodexBuildCheckpointV1 | None,
    ) -> datetime:
        current = self._clock()
        if previous is not None and current <= previous.updated_at:
            return previous.updated_at + timedelta(microseconds=1)
        return current

    def _persist_session_receipt(
        self,
        invocation: FactorySkillInvocationV1,
        content: bytes,
        resume_ordinal: int,
    ) -> Path:
        target = self._session_receipt_path(invocation, resume_ordinal)
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_once(target, content)
        return target

    def _session_receipt_path(
        self,
        invocation: FactorySkillInvocationV1,
        resume_ordinal: int,
    ) -> Path:
        ordinal_suffix = (
            "" if resume_ordinal == 0 else f".resume-{resume_ordinal}"
        )
        return self._settings.state_root.resolve() / "sessions" / (
            f"{invocation.idempotency_key}{ordinal_suffix}.json"
        )

    def _verified_session_receipt_path(
        self,
        invocation: FactorySkillInvocationV1,
        resume_ordinal: int,
    ) -> Path:
        ordinal_suffix = (
            "" if resume_ordinal == 0 else f".resume-{resume_ordinal}"
        )
        return self._settings.state_root.resolve() / "sessions" / (
            f"{invocation.idempotency_key}{ordinal_suffix}.cleanup-verified.json"
        )

    def _terminal_receipt_path(
        self,
        invocation: FactorySkillInvocationV1,
        checkpoint: FactoryCodexBuildCheckpointV1,
    ) -> Path:
        primary = self._session_receipt_path(
            invocation,
            checkpoint.resume_ordinal,
        )
        expected_sha256 = checkpoint.terminal_receipt_sha256
        if expected_sha256 is None:
            return primary
        verified = self._verified_session_receipt_path(
            invocation,
            checkpoint.resume_ordinal,
        )
        for candidate in (primary, verified):
            try:
                content = candidate.read_bytes()
            except OSError:
                continue
            if hashlib.sha256(content).hexdigest() == expected_sha256:
                return candidate
        return primary

    def _process_state_path(
        self,
        invocation: FactorySkillInvocationV1,
        resume_ordinal: int,
    ) -> Path:
        ordinal_suffix = (
            "" if resume_ordinal == 0 else f".resume-{resume_ordinal}"
        )
        return self._settings.state_root.resolve() / "processes" / (
            f"{invocation.idempotency_key}{ordinal_suffix}.json"
        )

    def _journal_path(
        self,
        invocation: FactorySkillInvocationV1,
        resume_ordinal: int,
    ) -> Path:
        ordinal_suffix = (
            "" if resume_ordinal == 0 else f".resume-{resume_ordinal}"
        )
        return self._settings.state_root.resolve() / "journals" / (
            f"{invocation.idempotency_key}{ordinal_suffix}.jsonl"
        )


def _codex_prompt(
    request: FactoryDispatch,
    invocation: FactorySkillInvocationV1,
    brief: CodexBuildBriefV1,
) -> str:
    contract = {
        "job_id": str(request.job.job_id),
        "correlation_id": str(request.job.correlation_id),
        "attempt": invocation.attempt,
        "workspace_ref": invocation.lease.workspace_ref,
        "acceptance_assertion_ids": list(invocation.acceptance_assertion_ids),
        "required_test_command_ids": list(brief.required_test_command_ids),
        "forbidden_effect_ids": list(brief.forbidden_effect_ids),
        "integrations": [
            item.model_dump(mode="json", by_alias=True)
            for item in brief.build_assignment.integrations
        ],
    }
    prior_candidate_guidance = (
        "Start from .captain-inputs/prior-candidate.zip: extract it into "
        "generated-candidate before editing, and do not search the repository for "
        "an older candidate. "
        if any(
            reference.media_type == "application/zip"
            and reference.uri.startswith(
                "artifact://minibook-creation/forge-source/"
            )
            for reference in brief.context_refs
        )
        else ""
    )
    return (
        "Implement the Captain-authorized AutoGen agent team in this isolated "
        "worktree. Read .captain-inputs/job-input.md, compiled-spec.json, "
        "dependency-graph.json, codex-build-brief.json, and "
        "codex-build-instructions.md. Treat codex-build-instructions.md as the "
        "authoritative bounded implementation and retry guidance. Reuse the repository "
        + prior_candidate_guidance
        + "architecture and released skills. Do not read secrets, write outside "
        "this worktree, push Git changes, activate workflows, or weaken assertions. "
        "Use n8n only when the integration contract below requires it. Build-time "
        "verification is candidate-scoped: use `python -m compileall -q "
        "generated-candidate` for python.compileall and `python -m pytest -q "
        "--no-cov generated-candidate/tests` for pytest.not-live. Do not run the "
        "repository-wide test suite. pytest.live.demo is deferred to Captain's "
        "Keep every shell command's combined output well below 256 KiB. Never dump "
        "whole directories or documents, and never use broad recursive `rg -A` or "
        "`rg -B` searches across multiple trees. Inspect with targeted `rg -n` queries "
        "and small bounded slices such as `Select-Object -First`. "
        "sealed holdout gate; record it as deferred, never as passed. Finish only "
        "after creating: "
        "factory-candidate.json (the complete candidate manifest), candidate.zip "
        "(the runnable source with factory-candidate.json at archive root and byte-"
        "identical to the external manifest), and test-evidence.json (JSON object "
        "listing commands, exit codes, assertion IDs, and status). Do not place "
        "candidate.zip inside itself. Write candidate.zip with canonical POSIX `/` "
        "entry separators on every platform; on Windows do not use Compress-Archive. "
        "factory-candidate.json MUST omit "
        "source_archive_ref; Captain adds source_archive_ref only after sealing "
        "candidate.zip, because a pre-seal archive digest would be self-referential. "
        "Keep generated source separate from "
        ".captain-inputs. Captain will independently validate every byte.\n\n"
        "CAPTAIN CONTRACT:\n"
        + json.dumps(contract, ensure_ascii=False, sort_keys=True, indent=2)
    )


def _session_receipt(
    *,
    result: CodexRunResult,
    session_id: str,
    workspace_ref: str,
    base_revision: str,
    command: tuple[str, ...],
    completed_at: datetime,
    resume_ordinal: int = 0,
    parent_lineage: _CodexResumeLineage | None = None,
) -> bytes:
    if completed_at.tzinfo is None or completed_at.utcoffset() != timezone.utc.utcoffset(
        completed_at
    ):
        raise FactoryDispatchError("Codex completion timestamp must be UTC")
    try:
        journal_path = result.journal_path.resolve(strict=True)
        journal_bytes = _read_bounded_codex_journal(journal_path)
    except OSError as exc:
        raise FactoryDispatchError("Codex JSONL journal is unavailable") from exc
    journal_sha256 = hashlib.sha256(journal_bytes).hexdigest()
    if journal_sha256 != result.journal_sha256:
        raise FactoryDispatchError("Codex JSONL journal digest does not match result")
    journal_lines = _bounded_codex_journal_lines(journal_bytes)
    if journal_lines != result.jsonl_lines:
        raise FactoryDispatchError("Codex JSONL journal snapshot does not match result")
    events: list[Mapping[str, object]] = []
    for line in result.jsonl_lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise FactoryDispatchError("Codex JSONL evidence is invalid") from exc
        if not isinstance(value, dict):
            raise FactoryDispatchError("Codex JSONL evidence must contain objects")
        events.append(value)
    if not events and result.terminal_status == "succeeded":
        raise FactoryDispatchError("Codex JSONL evidence is empty")
    observed_thread_id = _canonical_codex_thread_id(events)
    if isinstance(resume_ordinal, bool) or not 0 <= resume_ordinal <= 2:
        raise FactoryDispatchError("Codex resume ordinal is invalid")
    if resume_ordinal == 0 and parent_lineage is not None:
        raise FactoryDispatchError("Original Codex receipt cannot bind parent lineage")
    if resume_ordinal > 0 and parent_lineage is None:
        raise FactoryDispatchError("Resumed Codex receipt requires parent lineage")
    if (
        parent_lineage is not None
        and parent_lineage.codex_thread_id is not None
        and observed_thread_id is not None
        and observed_thread_id != parent_lineage.codex_thread_id
    ):
        raise FactoryDispatchError("Codex resumed thread ID changed")
    effective_thread_id = (
        parent_lineage.codex_thread_id
        if parent_lineage is not None
        and parent_lineage.codex_thread_id is not None
        else observed_thread_id
    )
    payload = {
        "schema": "captain.codex-session-receipt.v1",
        "provider": "codex-cli",
        "session_id": session_id,
        "codex_thread_id": effective_thread_id,
        "status": result.terminal_status,
        "exit_code": result.exit_code,
        "process_cleanup_status": result.process_cleanup_status,
        "workspace_ref": workspace_ref,
        "base_revision": base_revision,
        "command_sha256": hashlib.sha256(
            "\0".join(command).encode("utf-8")
        ).hexdigest(),
        "jsonl_sha256": journal_sha256,
        "journal_sha256": journal_sha256,
        "event_count": len(events),
        "event_types": list(
            canonical_codex_event_types(
                tuple(event.get("type") for event in events)
            )
        ),
        "resume_ordinal": resume_ordinal,
        "parent_terminal_receipt_sha256": (
            parent_lineage.terminal_receipt_sha256
            if parent_lineage is not None
            else None
        ),
        "parent_journal_sha256": (
            parent_lineage.journal_sha256
            if parent_lineage is not None
            else None
        ),
        "parent_codex_thread_id": (
            parent_lineage.codex_thread_id
            if parent_lineage is not None
            else None
        ),
        "completed_at": completed_at.isoformat(),
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _factory_codex_jsonl_evidence_error(
    result: CodexRunResult,
) -> CodexJsonlInvalidObjectError | None:
    """Classify invalid runner JSONL before normal receipt construction."""

    journal_path = result.journal_path.resolve(strict=True)
    journal_bytes = _read_bounded_codex_journal(journal_path)
    journal_sha256 = hashlib.sha256(journal_bytes).hexdigest()
    if journal_sha256 != result.journal_sha256:
        raise FactoryDispatchError("Codex JSONL journal digest does not match result")
    journal_lines = _bounded_codex_journal_lines(journal_bytes)
    if journal_lines != result.jsonl_lines:
        raise FactoryDispatchError("Codex JSONL journal snapshot does not match result")
    event_types: set[str] = set()
    event_count = 0
    for line in journal_lines:
        try:
            event = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            event = None
        if not isinstance(event, dict):
            error = CodexJsonlInvalidObjectError(
                "Codex JSONL record is not a valid JSON object"
            )
            error.bind_terminal_evidence(
                process_cleanup_status=result.process_cleanup_status,
                journal_path=journal_path,
                journal_sha256=journal_sha256,
                journal_byte_count=len(journal_bytes),
                event_count=event_count,
                event_types=tuple(sorted(event_types)),
            )
            return error
        event_count += 1
        event_types.add(canonical_codex_event_type(event.get("type")))
    return None


def _evidence_failure_receipt(
    *,
    error: CodexOutputEvidenceError,
    session_id: str,
    workspace_ref: str,
    base_revision: str,
    command: tuple[str, ...],
    completed_at: datetime,
    resume_ordinal: int,
) -> bytes:
    if completed_at.tzinfo is None or completed_at.utcoffset() != timezone.utc.utcoffset(
        completed_at
    ):
        raise FactoryDispatchError("Codex evidence failure timestamp must be UTC")
    if (
        error.process_cleanup_status is None
        or error.journal_sha256 is None
        or error.journal_byte_count is None
        or error.event_count is None
        or error.event_types is None
    ):
        raise FactoryDispatchError("Codex evidence failure metadata is incomplete")
    payload = {
        "schema": "captain.codex-session-error-receipt.v1",
        "provider": "codex-cli",
        "session_id": session_id,
        "status": "evidence_failed",
        "failure_kind": error.failure_kind,
        "process_cleanup_status": error.process_cleanup_status,
        "workspace_ref": workspace_ref,
        "base_revision": base_revision,
        "command_sha256": hashlib.sha256(
            "\0".join(command).encode("utf-8")
        ).hexdigest(),
        "journal_sha256": error.journal_sha256,
        "journal_byte_count": error.journal_byte_count,
        "event_count": error.event_count,
        "event_types": list(canonical_codex_event_types(error.event_types)),
        "resume_ordinal": resume_ordinal,
        "completed_at": completed_at.isoformat(),
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _canonical_codex_thread_id(
    events: Iterable[Mapping[str, object]],
) -> str | None:
    """Return the sole canonical Codex thread start, ignoring unrelated fields."""

    starts = tuple(event for event in events if event.get("type") == "thread.started")
    if not starts:
        return None
    if len(starts) != 1:
        raise FactoryDispatchError(
            "Codex JSONL must contain at most one thread.started event"
        )
    thread_id = starts[0].get("thread_id")
    if (
        not isinstance(thread_id, str)
        or _CODEX_THREAD_ID_PATTERN.fullmatch(thread_id) is None
    ):
        raise FactoryDispatchError(
            "Codex thread.started thread ID is invalid"
        )
    return thread_id


def _validate_factory_codex_run_result(
    *,
    result: CodexRunResult,
    expected_journal_path: Path,
    journals_root: Path,
) -> None:
    try:
        actual_journal_path = result.journal_path.resolve(strict=True)
    except OSError as exc:
        raise FactoryDispatchError("Codex runner journal path is unavailable") from exc
    expected_path = expected_journal_path.resolve()
    expected_root = journals_root.resolve()
    if (
        actual_journal_path != expected_path
        or not _is_relative_to(actual_journal_path, expected_root)
    ):
        raise FactoryDispatchError(
            "Codex runner journal path does not match the Factory invocation"
        )

    if result.exit_code == 0:
        expected_status = "succeeded"
        cleanup_is_valid = result.process_cleanup_status == "not_required"
    elif result.exit_code == 124:
        expected_status = "timed_out"
        cleanup_is_valid = result.process_cleanup_status in {
            "not_required",
            "verified_cancelled",
            "unresolved",
        }
    elif result.exit_code == 130:
        expected_status = "cancelled"
        cleanup_is_valid = result.process_cleanup_status in {
            "not_required",
            "verified_cancelled",
            "unresolved",
        }
    else:
        expected_status = "failed"
        cleanup_is_valid = result.process_cleanup_status == "not_required"
    if result.terminal_status != expected_status or not cleanup_is_valid:
        raise FactoryDispatchError(
            "Codex runner terminal status or process cleanup status is inconsistent"
        )


def _read_bounded_codex_journal(path: Path) -> bytes:
    try:
        with path.open("rb") as journal:
            size_before = os.fstat(journal.fileno()).st_size
            if size_before > DEFAULT_MAX_CODEX_JOURNAL_BYTES:
                raise FactoryDispatchError(
                    "Codex JSONL journal exceeds the Factory size limit"
                )
            content = journal.read(DEFAULT_MAX_CODEX_JOURNAL_BYTES + 1)
            size_after = os.fstat(journal.fileno()).st_size
    except OSError as exc:
        raise FactoryDispatchError("Codex JSONL journal is unavailable") from exc
    if (
        len(content) > DEFAULT_MAX_CODEX_JOURNAL_BYTES
        or size_before != size_after
        or len(content) != size_after
    ):
        raise FactoryDispatchError(
            "Codex JSONL journal snapshot changed or exceeds the Factory size limit"
        )
    return content


def _bounded_codex_journal_lines(content: bytes) -> tuple[str, ...]:
    records: list[str] = []
    stream = BytesIO(content)
    while True:
        raw_record = stream.readline(DEFAULT_MAX_CODEX_JSONL_RECORD_BYTES + 2)
        if not raw_record:
            break
        if not raw_record.endswith(b"\n"):
            if len(raw_record) > DEFAULT_MAX_CODEX_JSONL_RECORD_BYTES:
                raise FactoryDispatchError("Codex JSONL record exceeds the size limit")
            raise FactoryDispatchError("Codex JSONL journal has an incomplete record")
        record = raw_record[:-1]
        if record.endswith(b"\r"):
            record = record[:-1]
        if len(record) > DEFAULT_MAX_CODEX_JSONL_RECORD_BYTES:
            raise FactoryDispatchError("Codex JSONL record exceeds the size limit")
        if not record.strip():
            continue
        if len(records) >= DEFAULT_MAX_CODEX_JOURNAL_RECORDS:
            raise FactoryDispatchError("Codex JSONL journal exceeds the record limit")
        records.append(record.decode("utf-8", errors="replace"))
    return tuple(records)


def _write_once(target: Path, content: bytes) -> None:
    try:
        descriptor = os.open(
            target,
            getattr(os, "O_BINARY", 0) | os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
    except FileExistsError:
        if target.read_bytes() != content:
            raise FactoryDispatchError("Codex session receipt replay conflicts")
        return
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _is_captain_scaffold_status_line(line: str) -> bool:
    path = line[3:] if len(line) > 3 else ""
    return any(
        path == directory or path.startswith(f"{directory}/")
        for directory in (".captain-inputs", ".captain-inputs.staging")
    )


__all__ = [
    "CaptainCodexBuildExecutorPort",
    "CaptainCodexBuildSealer",
    "CodexCliFactoryBuildExecutor",
    "CodexCliFactoryBuildSettings",
    "CompletedCodexBuild",
    "FactoryCodexBuildInterrupted",
    "FactoryCodexCleanupUnresolved",
    "FactoryCodexEvidenceFailure",
    "FactoryCodexProcessInspectorPort",
    "FactoryCodexProcessState",
    "FactoryBuildArtifactReaderPort",
    "FactoryCodexRunnerFactory",
    "FactoryCodexResumeAuthorizerPort",
    "FactoryCodexWorkspacePreparerPort",
    "GitDetachedFactoryWorkspacePreparer",
    "PreparedFactoryWorkspace",
    "PowerShellFactoryCodexProcessInspector",
]
