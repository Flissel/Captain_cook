"""Captain-owned Codex execution and sealing for Agent Factory builds."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from agenten.agent_factory.codex_build_provenance import (
    CaptainCodexBuildReceiptIssuer,
)
from agenten.agent_factory.contracts import AgentFactoryJobV3
from agenten.agent_factory.orchestration import FactoryDispatch, FactoryDispatchError
from agenten.agent_factory.skill_workflow_contracts import (
    CodexBuildBriefV1,
    CodexBuildEvidenceV1,
    FactorySkillInvocationV1,
    FactorySkillStep,
)
from agenten.agent_runtime.contracts import ArtifactRef
from agenten.execution.codex_policy import AuthorizedCodexRun
from agenten.execution.codex_supervisor import (
    CodexRunResult,
    CodexRunRequest,
    CodexRunner,
)


_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40,64}$")
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


@dataclass(frozen=True)
class PreparedFactoryWorkspace:
    """One clean detached worktree and its immutable base revision."""

    root: Path
    base_revision: str


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


class FactoryCodexWorkspacePreparerPort(Protocol):
    def prepare(
        self,
        request: FactoryDispatch,
        invocation: FactorySkillInvocationV1,
        brief: CodexBuildBriefV1,
    ) -> PreparedFactoryWorkspace: ...


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
    ) -> CodexRunner: ...


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
        completed = await self._executor.execute(request, invocation, brief)
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
        return CodexBuildEvidenceV1(
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
            evidence_refs=(workflow_receipt_ref,),
            acceptance_assertion_ids=invocation.acceptance_assertion_ids,
            build_receipt_ref=workflow_receipt_ref,
            build_receipt=receipt,
            status="sealed",
        )


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
        if target.exists():
            raise FactoryDispatchError(
                "Codex workspace already exists; recovery is required before retry"
            )
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
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._workspace_preparer = workspace_preparer
        self._artifact_reader = artifact_reader
        self._authorizer = authorizer
        self._runner_factory = runner_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def execute(
        self,
        request: FactoryDispatch,
        invocation: FactorySkillInvocationV1,
        brief: CodexBuildBriefV1,
    ) -> CompletedCodexBuild:
        if not isinstance(request.job, AgentFactoryJobV3):
            raise FactoryDispatchError("Codex execution requires a V3 Factory job")
        started_at = self._clock()
        authority_deadline = min(invocation.lease.expires_at, request.job.deadline_at)
        remaining_seconds = (authority_deadline - started_at).total_seconds()
        if (
            started_at < invocation.lease.issued_at
            or remaining_seconds < 1
        ):
            raise FactoryDispatchError("Codex build lease is not active")
        runtime_seconds = min(
            self._settings.maximum_runtime_seconds,
            max(1, int(remaining_seconds)),
        )
        prepared = self._workspace_preparer.prepare(request, invocation, brief)
        session_id = f"factory-{invocation.invocation_id.hex[:24]}"
        state_path = (
            self._settings.state_root.resolve()
            / "processes"
            / f"{invocation.idempotency_key}.json"
        )
        journal_path = (
            self._settings.state_root.resolve()
            / "journals"
            / f"{invocation.idempotency_key}.jsonl"
        )
        state_path.parent.mkdir(parents=True, exist_ok=True)
        prompt = _codex_prompt(request, invocation, brief)
        run_request = CodexRunRequest(
            run_id=invocation.invocation_id.hex,
            trace_id=request.job.correlation_id.hex,
            batch_id=f"factory-{request.job.job_id.hex[:24]}",
            worker_id="factory-tool-integrator",
            session_id=session_id,
            claim_token=invocation.lease.lease_id,
            iteration=invocation.attempt,
            command=("codex", "exec", "--json", prompt),
            workspace=prepared.root,
            project_id=request.job.job_id.hex,
            claim_id=invocation.invocation_id.hex,
            fencing_token=invocation.subject_version,
            project_root=prepared.root,
        )
        # Authorization includes the clean-worktree check. Captain writes only
        # the three already-released inputs after this point and before Codex.
        authorized = self._authorizer.authorize(run_request)
        self._materialize_inputs(request, brief, prepared.root)
        runner = self._runner_factory(
            session_id=session_id,
            state_path=state_path,
            journal_path=journal_path,
            maximum_runtime_seconds=runtime_seconds,
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
        )
        self._persist_session_receipt(invocation, session_receipt)
        if result.terminal_status == "timed_out":
            raise FactoryDispatchError(
                f"Codex build process timed out (exit {result.exit_code})"
            )
        if result.terminal_status == "cancelled":
            raise FactoryDispatchError(
                f"Codex build process was cancelled (exit {result.exit_code})"
            )
        if result.exit_code != 0:
            raise FactoryDispatchError(
                f"Codex build process failed (exit {result.exit_code})"
            )
        if not (
            invocation.lease.issued_at <= completed_at < invocation.lease.expires_at
            and completed_at <= request.job.deadline_at
        ):
            raise FactoryDispatchError("Codex build completed outside Captain authority")
        for relative in _OUTPUT_PATHS:
            target = prepared.root / relative
            if not target.is_file():
                raise FactoryDispatchError(
                    f"Codex omitted required build artifact: {relative}"
                )
        return CompletedCodexBuild(
            workspace_root=prepared.root,
            codex_session_receipt=session_receipt,
            candidate_manifest_path=_OUTPUT_PATHS[0],
            source_archive_path=_OUTPUT_PATHS[1],
            test_evidence_paths=(_OUTPUT_PATHS[2],),
            completed_at=completed_at,
        )

    def _materialize_inputs(
        self,
        request: FactoryDispatch,
        brief: CodexBuildBriefV1,
        workspace: Path,
    ) -> None:
        destination = workspace / ".captain-inputs"
        destination.mkdir(parents=True, exist_ok=False)
        bindings = (
            ("job-input.md", request.job.input_ref),
            ("compiled-spec.json", request.job.compiled_spec_ref),
            ("dependency-graph.json", request.job.dependency_graph_ref),
        )
        for name, reference in bindings:
            content = self._artifact_reader.read_bytes(reference)
            if hashlib.sha256(content).hexdigest() != reference.sha256:
                raise FactoryDispatchError("Factory build input digest changed")
            (destination / name).write_bytes(content)
        (destination / "codex-build-brief.json").write_text(
            brief.model_dump_json(by_alias=True),
            encoding="utf-8",
        )

    def _persist_session_receipt(
        self,
        invocation: FactorySkillInvocationV1,
        content: bytes,
    ) -> None:
        target = (
            self._settings.state_root.resolve()
            / "sessions"
            / f"{invocation.idempotency_key}.json"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_once(target, content)


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
    thread_ids = {
        value
        for event in events
        for value in (event.get("thread_id"),)
        if isinstance(value, str) and value.strip()
    }
    if len(thread_ids) > 1:
        raise FactoryDispatchError("Codex JSONL contains conflicting thread IDs")
    payload = {
        "schema": "captain.codex-session-receipt.v1",
        "provider": "codex-cli",
        "session_id": session_id,
        "codex_thread_id": next(iter(thread_ids), None),
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
        "completed_at": completed_at.isoformat(),
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


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

    if result.terminal_status == "cancelled":
        raise FactoryDispatchError(
            "Codex runner terminal status has no defined cancellation exit code"
        )
    if result.exit_code == 0:
        expected_status = "succeeded"
        cleanup_is_valid = result.process_cleanup_status == "not_required"
    elif result.exit_code == 124:
        expected_status = "timed_out"
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


__all__ = [
    "CaptainCodexBuildExecutorPort",
    "CaptainCodexBuildSealer",
    "CodexCliFactoryBuildExecutor",
    "CodexCliFactoryBuildSettings",
    "CompletedCodexBuild",
    "FactoryBuildArtifactReaderPort",
    "FactoryCodexRunnerFactory",
    "FactoryCodexWorkspacePreparerPort",
    "GitDetachedFactoryWorkspacePreparer",
    "PreparedFactoryWorkspace",
]
