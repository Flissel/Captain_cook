"""Captain-owned Codex execution and sealing for Agent Factory builds."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID

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
    canonical_factory_codex_model,
)
from agenten.agent_factory.contracts import AgentFactoryJobV3
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
    CodexRunResult,
    CodexRunRequest,
    CodexRunner,
)


_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40,64}$")
_CODEX_THREAD_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_OUTPUT_PATHS = (
    "factory-candidate.json",
    "candidate.zip",
    "test-evidence.json",
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


FactoryCodexBuildInterruptionReason = Literal[
    "codex_timed_out",
    "runtime_cancelled",
    "resume_authorization_required",
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


def _interruption_references(
    checkpoint: FactoryCodexBuildCheckpointV1,
) -> dict[str, object]:
    receipt_sha256 = checkpoint.terminal_receipt_sha256
    if checkpoint.phase != "implementation_interrupted" or receipt_sha256 is None:
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

    def __post_init__(self) -> None:
        if self.maximum_runtime_seconds < 1 or self.maximum_runtime_seconds > 900:
            raise ValueError("Factory Codex runtime bound is invalid")


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

    def _seal_completed(
        self,
        request: FactoryDispatch,
        invocation: FactorySkillInvocationV1,
        brief: CodexBuildBriefV1,
        completed: CompletedCodexBuild,
    ) -> CodexBuildEvidenceV1:
        self._executor.validate_completed_outputs(invocation, completed)
        receipt = self._issuer.issue(
            job=request.job,
            build_brief=brief,
            workspace_root=completed.workspace_root,
            codex_session_receipt=completed.codex_session_receipt,
            seal_idempotency_key=invocation.idempotency_key,
            candidate_manifest_path=completed.candidate_manifest_path,
            source_archive_path=completed.source_archive_path,
            test_evidence_paths=completed.test_evidence_paths,
            completed_at=completed.completed_at,
        )
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
        if (
            not isinstance(resume_ordinal, int)
            or isinstance(resume_ordinal, bool)
            or resume_ordinal != checkpoint.resume_ordinal + 1
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
        if self._resume_authorizer is None:
            raise FactoryDispatchError(
                "Factory Codex resume authorization validator is not configured"
            )
        checkpoint = self._checkpoint_store.load(invocation)
        if checkpoint is None or checkpoint.phase != "implementation_interrupted":
            raise FactoryDispatchError("Factory Codex build is not interrupted")
        resume_ordinal = self._resume_authorizer.authorize_resume(
            request,
            invocation,
            checkpoint,
        )
        authorization = request.runtime_retry_authorization
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
        if (
            prior.payload["status"] not in {"timed_out", "cancelled"}
            or prior.payload["process_cleanup_status"] == "unresolved"
            or prior.completed_at > checkpoint.updated_at
        ):
            raise FactoryDispatchError(
                "Factory Codex prior terminal evidence is not resumable"
            )
        return authorization

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
        if checkpoint.phase in {"implementation_running", "implementation_complete"}:
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
            if checkpoint.phase == "implementation_interrupted":
                if authorized_resume_ordinal is None:
                    raise FactoryCodexBuildInterrupted(
                        reason="resume_authorization_required",
                        exit_code=None,
                        **_interruption_details(request, invocation, checkpoint),
                    )
                prior = self._load_terminal_evidence(
                    request=request,
                    invocation=invocation,
                    brief=brief,
                    prepared=prepared,
                    checkpoint=checkpoint,
                )
                if (
                    prior.payload["status"] not in {"timed_out", "cancelled"}
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
    ) -> None:
        expected = (
            checkpoint.runtime_retry_authorization_uri,
            checkpoint.runtime_retry_authorization_sha256,
            checkpoint.runtime_retry_authorization_binding_sha256,
        )
        actual = self._runtime_retry_checkpoint_binding(
            request.runtime_retry_authorization
        )
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
        result = await runner.run(authorized)
        _validate_factory_codex_run_result(
            result=result,
            expected_journal_path=journal_path,
            journals_root=self._settings.state_root.resolve() / "journals",
        )
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
        receipt_path = self._persist_session_receipt(
            invocation,
            session_receipt,
            checkpoint.resume_ordinal,
        )
        receipt_sha256 = hashlib.sha256(session_receipt).hexdigest()
        if result.process_cleanup_status == "unresolved":
            raise FactoryDispatchError(
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
            raise FactoryDispatchError(
                f"Codex build process failed (exit {result.exit_code})"
            )
        if not invocation.lease.issued_at <= completed_at < authority_deadline:
            raise FactoryDispatchError("Codex build completed outside Captain authority")
        output_manifest_uri, output_manifest_sha256 = self._persist_output_manifest(
            invocation=invocation,
            prepared=prepared,
            resume_ordinal=checkpoint.resume_ordinal,
            terminal_receipt_sha256=receipt_sha256,
        )
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
        receipt_content, receipt = self._load_reconciliation_receipt(
            request=request,
            invocation=invocation,
            brief=brief,
            prepared=prepared,
            checkpoint=checkpoint,
        )
        cleanup_status = receipt["process_cleanup_status"]
        if cleanup_status == "unresolved":
            raise FactoryDispatchError(
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
        if receipt["status"] in {"timed_out", "cancelled"}:
            phase: FactoryCodexBuildPhase = "implementation_interrupted"
            output_manifest_uri = None
            output_manifest_sha256 = None
        elif receipt["status"] == "succeeded":
            phase = "implementation_complete"
            output_manifest_uri, output_manifest_sha256 = self._persist_output_manifest(
                invocation=invocation,
                prepared=prepared,
                resume_ordinal=checkpoint.resume_ordinal,
                terminal_receipt_sha256=receipt_sha256,
            )
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
        receipt_path = self._session_receipt_path(
            invocation, checkpoint.resume_ordinal
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
            or receipt["command_sha256"]
            != hashlib.sha256(
                "\0".join(run_request.command).encode("utf-8")
            ).hexdigest()
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
            130: ("cancelled", {"verified_cancelled", "unresolved"}),
        }.get(exit_code)
        if (
            expected_terminal is None
            or status != expected_terminal[0]
            or cleanup not in expected_terminal[1]
        ):
            raise FactoryDispatchError("Factory Codex terminal receipt is inconsistent")

        journal_path = self._journal_path(invocation, checkpoint.resume_ordinal)
        try:
            journal = journal_path.read_bytes()
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
            for line in journal.splitlines():
                if not line.strip():
                    continue
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError
                events.append(event)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise FactoryDispatchError("Factory Codex JSONL journal is invalid") from None
        event_types = sorted(
            {
                value
                for event in events
                for value in (event.get("type"),)
                if isinstance(value, str) and value.strip()
            }
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
        self._validate_output_manifest(
            invocation=invocation,
            checkpoint=checkpoint,
            workspace_root=prepared.root,
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
                workspace_root=checkpoint.workspace_root,
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
                workspace_root=checkpoint.workspace_root,
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
        self._validate_output_manifest(
            invocation=invocation,
            checkpoint=checkpoint,
            workspace_root=completed.workspace_root,
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
            workspace_root=checkpoint.workspace_root,
        )
        if (
            checkpoint.terminal_receipt_sha256
            != evidence.build_receipt.codex_session_ref.sha256
        ):
            raise FactoryDispatchError(
                "Factory Codex original sealed evidence session changed"
            )
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

    def _persist_output_manifest(
        self,
        *,
        invocation: FactorySkillInvocationV1,
        prepared: PreparedFactoryWorkspace,
        resume_ordinal: int,
        terminal_receipt_sha256: str,
    ) -> tuple[str, str]:
        manifest = FactoryCodexOutputManifestV1(
            schema="captain.factory-codex-output-manifest.v1",
            job_id=invocation.job_id,
            correlation_id=invocation.correlation_id,
            attempt=invocation.attempt,
            invocation_id=invocation.invocation_id,
            workspace_ref=invocation.lease.workspace_ref,
            resume_ordinal=resume_ordinal,
            terminal_receipt_sha256=terminal_receipt_sha256,
            artifacts=self._read_output_artifacts(prepared.root),
        )
        return self._output_manifest_store.persist(manifest)

    def _validate_output_manifest(
        self,
        *,
        invocation: FactorySkillInvocationV1,
        checkpoint: FactoryCodexBuildCheckpointV1,
        workspace_root: Path,
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
        if (
            manifest.resume_ordinal != checkpoint.resume_ordinal
            or manifest.terminal_receipt_sha256
            != checkpoint.terminal_receipt_sha256
        ):
            raise FactoryDispatchError(
                "Factory Codex output manifest checkpoint binding changed"
            )
        current = self._read_output_artifacts(workspace_root)
        if current != manifest.artifacts:
            raise FactoryDispatchError(
                "Codex output artifact binding changed"
            )

    @staticmethod
    def _read_output_artifacts(
        workspace_root: Path,
    ) -> tuple[FactoryCodexOutputArtifactV1, ...]:
        try:
            root = workspace_root.resolve(strict=True)
        except OSError:
            raise FactoryDispatchError(
                "Codex output workspace is unavailable"
            ) from None
        artifacts: list[FactoryCodexOutputArtifactV1] = []
        for relative in sorted(_OUTPUT_PATHS):
            target = root / relative
            try:
                resolved = target.resolve(strict=True)
                if (
                    target.is_symlink()
                    or not target.is_file()
                    or resolved.parent != root
                ):
                    raise OSError
                content = target.read_bytes()
            except OSError:
                raise FactoryDispatchError(
                    f"Codex omitted required build artifact: {relative}"
                ) from None
            artifacts.append(
                FactoryCodexOutputArtifactV1(
                    relative_path=relative,
                    sha256=hashlib.sha256(content).hexdigest(),
                    size=len(content),
                )
            )
        return tuple(artifacts)

    def _scaffold_files(
        self,
        request: FactoryDispatch,
        brief: CodexBuildBriefV1,
    ) -> dict[str, bytes]:
        bindings = (
            ("job-input.md", request.job.input_ref),
            ("compiled-spec.json", request.job.compiled_spec_ref),
            ("dependency-graph.json", request.job.dependency_graph_ref),
        )
        files: dict[str, bytes] = {}
        for name, reference in bindings:
            content = self._artifact_reader.read_bytes(reference)
            if hashlib.sha256(content).hexdigest() != reference.sha256:
                raise FactoryDispatchError("Factory build input digest changed")
            files[name] = content
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
    return (
        "Implement the Captain-authorized AutoGen agent team in this isolated "
        "worktree. Read .captain-inputs/job-input.md, compiled-spec.json, "
        "dependency-graph.json, and codex-build-brief.json. Reuse the repository "
        "architecture and released skills. Do not read secrets, write outside "
        "this worktree, push Git changes, activate workflows, or weaken assertions. "
        "Use n8n only when the integration contract below requires it. Build-time "
        "verification is candidate-scoped: use `python -m compileall -q "
        "generated-candidate` for python.compileall and `python -m pytest -q "
        "--no-cov generated-candidate/tests` for pytest.not-live. Do not run the "
        "repository-wide test suite. pytest.live.demo is deferred to Captain's "
        "sealed holdout gate; record it as deferred, never as passed. Finish only "
        "after creating: "
        "factory-candidate.json (the complete candidate manifest), candidate.zip "
        "(the runnable source with factory-candidate.json at archive root and byte-"
        "identical to the external manifest), and test-evidence.json (JSON object "
        "listing commands, exit codes, assertion IDs, and status). Do not place "
        "candidate.zip inside itself. Keep generated source separate from "
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
        journal_bytes = journal_path.read_bytes()
    except OSError as exc:
        raise FactoryDispatchError("Codex JSONL journal is unavailable") from exc
    journal_sha256 = hashlib.sha256(journal_bytes).hexdigest()
    if journal_sha256 != result.journal_sha256:
        raise FactoryDispatchError("Codex JSONL journal digest does not match result")
    journal_lines = tuple(
        line.decode("utf-8", errors="replace")
        for line in journal_bytes.splitlines()
        if line.strip()
    )
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
        "event_types": sorted(
            {
                event_type
                for event in events
                for event_type in (event.get("type"),)
                if isinstance(event_type, str) and event_type.strip()
            }
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
