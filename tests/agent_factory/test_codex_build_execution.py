from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agenten.agent_factory.codex_build_execution import (
    CaptainCodexBuildSealer,
    CodexCliFactoryBuildExecutor,
    CodexCliFactoryBuildSettings,
    CompletedCodexBuild,
    GitDetachedFactoryWorkspacePreparer,
    PreparedFactoryWorkspace,
    _session_receipt,
)
from agenten.agent_factory.codex_build_provenance import (
    CaptainCodexBuildReceiptIssuer,
    CodexBuildArtifactCas,
)
from agenten.agent_factory.forge_contracts import ArtifactRef as ForgeArtifactRef
from agenten.agent_factory.orchestration import FactoryDispatch, FactoryDispatchError
from agenten.agent_factory.skill_workflow_contracts import (
    CodexBuildEvidenceV1,
    FactorySkillInvocationV1,
)
from agenten.agent_factory.state_machine import FactoryAction, FactoryActionKind
from agenten.execution.codex_policy import AuthorizedCodexRun, FrozenEnvironment
from agenten.execution.codex_supervisor import CodexRunResult
from agenten.agent_runtime.contracts import ArtifactRef
from tests.agent_factory.test_codex_build_provenance import (
    _bound_job_and_brief,
    _workspace,
)
from tests.agent_factory.test_codex_build_provenance_contracts import (
    seal_invocation_payload,
)


NOW = datetime(2026, 7, 21, 10, 5, tzinfo=timezone.utc)


class FakeBuildExecutor:
    def __init__(self, completed: CompletedCodexBuild) -> None:
        self.completed = completed
        self.calls: list[tuple[object, object, object]] = []

    async def execute(self, request, invocation, brief) -> CompletedCodexBuild:
        self.calls.append((request, invocation, brief))
        return self.completed


class RecordingAuthorizer:
    def __init__(self) -> None:
        self.requests = []

    def authorize(self, request):
        self.requests.append(request)
        return AuthorizedCodexRun(
            workspace=request.workspace,
            command=request.command,
            environment=FrozenEnvironment({}),
        )


class SuccessfulRunner:
    def __init__(self, workspace: Path, journal_path: Path) -> None:
        self.workspace = workspace
        self.journal_path = journal_path
        self.calls: list[AuthorizedCodexRun] = []

    async def run(self, authorized: AuthorizedCodexRun) -> CodexRunResult:
        self.calls.append(authorized)
        candidate_manifest = json.dumps(
            {
                "schema": "captain.factory-candidate.v1",
                "candidate_id": "generated-team-v1",
            },
            sort_keys=True,
        ).encode("utf-8")
        (self.workspace / "factory-candidate.json").write_bytes(candidate_manifest)
        from io import BytesIO
        from zipfile import ZIP_STORED, ZipFile, ZipInfo

        output = BytesIO()
        with ZipFile(output, "w", compression=ZIP_STORED) as archive:
            for name, content in (
                ("factory-candidate.json", candidate_manifest),
                ("src/team.py", b"TEAM = 'generated'\n"),
            ):
                info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                archive.writestr(info, content)
        (self.workspace / "candidate.zip").write_bytes(output.getvalue())
        (self.workspace / "test-evidence.json").write_text(
            json.dumps({"status": "passed", "command_ids": ["pytest.not-live"]}),
            encoding="utf-8",
        )
        jsonl_lines = (
            json.dumps(
                {
                    "type": "thread.started",
                    "thread_id": "codex-thread-123",
                }
            ),
            json.dumps({"type": "turn.completed"}),
        )
        journal = "".join(f"{line}\n" for line in jsonl_lines).encode("utf-8")
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        self.journal_path.write_bytes(journal)
        return CodexRunResult(
            exit_code=0,
            terminal_status="succeeded",
            process_cleanup_status="not_required",
            journal_path=self.journal_path,
            journal_sha256=hashlib.sha256(journal).hexdigest(),
            artifact_references=(),
            jsonl_lines=jsonl_lines,
        )


class StaticArtifactReader:
    def __init__(self, contents: dict[str, bytes]) -> None:
        self._contents = contents

    def read_bytes(self, reference) -> bytes:
        return self._contents[reference.sha256]


def _executor_job_and_brief():
    job, brief = _bound_job_and_brief()
    content_by_name = {
        "input": b'{"input":"claims team"}',
        "compiled": b'{"spec":"compiled"}',
        "graph": b'{"nodes":["build"]}',
    }
    references = {
        name: ArtifactRef(
            uri=f"artifact://test/{hashlib.sha256(content).hexdigest()}",
            sha256=hashlib.sha256(content).hexdigest(),
            media_type="application/json",
        )
        for name, content in content_by_name.items()
    }
    job = job.model_copy(
        update={
            "input_ref": references["input"],
            "compiled_spec_ref": references["compiled"],
            "dependency_graph_ref": references["graph"],
        }
    )
    assignment = brief.build_assignment.model_copy(
        update={
            "compiled_spec_ref": ForgeArtifactRef.model_validate(
                references["compiled"].model_dump(mode="json")
            ),
            "dependency_graph_ref": ForgeArtifactRef.model_validate(
                references["graph"].model_dump(mode="json")
            ),
        }
    )
    brief = brief.model_copy(update={"build_assignment": assignment})
    contents = {
        hashlib.sha256(content).hexdigest(): content
        for content in content_by_name.values()
    }
    return job, brief, StaticArtifactReader(contents)


def _seal_invocation(job, brief) -> FactorySkillInvocationV1:
    payload = seal_invocation_payload()
    lease = payload["lease"]
    assert isinstance(lease, dict)
    payload.update(
        {
            "job_id": str(job.job_id),
            "correlation_id": str(job.correlation_id),
            "subject_version": job.subject_version,
            "attempt": brief.attempt,
            "input_ref": brief.artifact_ref.model_dump(mode="json"),
            "input_sha256": brief.artifact_ref.sha256,
            "acceptance_assertion_ids": list(job.acceptance_assertion_ids),
        }
    )
    lease.update(
        {
            "job_id": str(job.job_id),
            "correlation_id": str(job.correlation_id),
            "subject_version": job.subject_version,
            "attempt": brief.attempt,
            "workspace_ref": brief.build_assignment.workspace_ref,
        }
    )
    return FactorySkillInvocationV1.model_validate(payload)


def _dispatch(job, invocation: FactorySkillInvocationV1) -> FactoryDispatch:
    return FactoryDispatch(
        job=job,
        action=FactoryAction(
            kind=FactoryActionKind.DISPATCH_TOOL_INTEGRATOR,
            attempt=invocation.attempt,
        ),
        role=invocation.lease.role,
        lease=invocation.lease,
    )


@pytest.mark.asyncio
async def test_captain_sealer_issues_and_persists_exact_build_evidence(
    tmp_path: Path,
) -> None:
    cas = CodexBuildArtifactCas(tmp_path / "cas")
    workspace = _workspace(tmp_path, cas)
    job, brief, artifact_reader = _executor_job_and_brief()
    invocation = _seal_invocation(job, brief)
    executor = FakeBuildExecutor(
        CompletedCodexBuild(
            workspace_root=workspace,
            codex_session_receipt=b'{"session_id":"codex-session-123"}',
            candidate_manifest_path="factory-candidate.json",
            source_archive_path="candidate.zip",
            test_evidence_paths=("test-evidence.json",),
            completed_at=NOW,
        )
    )
    sealer = CaptainCodexBuildSealer(
        executor=executor,
        issuer=CaptainCodexBuildReceiptIssuer(cas),
    )

    evidence = await sealer.seal(_dispatch(job, invocation), invocation, brief)

    assert isinstance(evidence, CodexBuildEvidenceV1)
    assert evidence.invocation == invocation
    assert evidence.artifact_ref == evidence.build_receipt_ref
    assert evidence.evidence_refs == (evidence.build_receipt_ref,)
    assert evidence.build_receipt.producer == "captain"
    assert evidence.build_receipt.seal_idempotency_key == invocation.idempotency_key
    assert cas.read_bytes(evidence.build_receipt_ref)


@pytest.mark.asyncio
async def test_cli_executor_authorizes_before_materializing_and_records_redacted_session(
    tmp_path: Path,
) -> None:
    job, brief, artifact_reader = _executor_job_and_brief()
    invocation = _seal_invocation(job, brief)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    preparer_calls: list[object] = []

    class Preparer:
        def prepare(self, request, seal_invocation, build_brief):
            preparer_calls.append((request, seal_invocation, build_brief))
            return PreparedFactoryWorkspace(
                root=workspace,
                base_revision="a" * 40,
            )

    authorizer = RecordingAuthorizer()
    runners: list[SuccessfulRunner] = []

    def runner_factory(**kwargs) -> SuccessfulRunner:
        runner = SuccessfulRunner(workspace, kwargs["journal_path"])
        runners.append(runner)
        return runner

    state_root = tmp_path / "state"
    executor = CodexCliFactoryBuildExecutor(
        settings=CodexCliFactoryBuildSettings(
            state_root=state_root,
            maximum_runtime_seconds=120,
        ),
        workspace_preparer=Preparer(),
        artifact_reader=artifact_reader,
        authorizer=authorizer,
        runner_factory=runner_factory,
        clock=lambda: NOW,
    )

    completed = await executor.execute(_dispatch(job, invocation), invocation, brief)

    assert len(preparer_calls) == 1
    assert len(authorizer.requests) == 1
    assert len(runners) == 1
    runner = runners[0]
    assert len(runner.calls) == 1
    assert authorizer.requests[0].workspace == workspace
    assert authorizer.requests[0].command[:3] == ("codex", "exec", "--json")
    prompt = authorizer.requests[0].command[3]
    assert "python -m compileall -q generated-candidate" in prompt
    assert "python -m pytest -q --no-cov generated-candidate/tests" in prompt
    assert "Do not run the repository-wide test suite" in prompt
    assert "pytest.live.demo is deferred to Captain" in prompt
    assert (workspace / ".captain-inputs" / "job-input.md").is_file()
    assert (workspace / ".captain-inputs" / "compiled-spec.json").is_file()
    assert (workspace / ".captain-inputs" / "dependency-graph.json").is_file()
    session = json.loads(completed.codex_session_receipt)
    assert session["status"] == "succeeded"
    assert session["codex_thread_id"] == "codex-thread-123"
    assert session["jsonl_sha256"] == hashlib.sha256(
        ("\n".join(runner.calls[0] and (
            json.dumps({"type": "thread.started", "thread_id": "codex-thread-123"}),
            json.dumps({"type": "turn.completed"}),
        )) + "\n").encode("utf-8")
    ).hexdigest()
    assert "OPENAI_API_KEY" not in completed.codex_session_receipt.decode("utf-8")
    assert completed.test_evidence_paths == ("test-evidence.json",)
    assert tuple(state_root.glob("sessions/*.json"))
    assert runner.journal_path == (
        state_root / "journals" / f"{invocation.idempotency_key}.jsonl"
    )


@pytest.mark.asyncio
async def test_cli_executor_fails_closed_when_codex_omits_required_outputs(
    tmp_path: Path,
) -> None:
    job, brief, artifact_reader = _executor_job_and_brief()
    invocation = _seal_invocation(job, brief)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    class Preparer:
        def prepare(self, *_args):
            return PreparedFactoryWorkspace(root=workspace, base_revision="a" * 40)

    class IncompleteRunner:
        def __init__(self, journal_path: Path) -> None:
            self.journal_path = journal_path

        async def run(self, _authorized):
            line = json.dumps({"type": "thread.started"})
            journal = f"{line}\n".encode("utf-8")
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            self.journal_path.write_bytes(journal)
            return CodexRunResult(
                exit_code=0,
                terminal_status="succeeded",
                process_cleanup_status="not_required",
                journal_path=self.journal_path,
                journal_sha256=hashlib.sha256(journal).hexdigest(),
                artifact_references=(),
                jsonl_lines=(line,),
            )

    executor = CodexCliFactoryBuildExecutor(
        settings=CodexCliFactoryBuildSettings(
            state_root=tmp_path / "state",
            maximum_runtime_seconds=120,
        ),
        workspace_preparer=Preparer(),
        artifact_reader=artifact_reader,
        authorizer=RecordingAuthorizer(),
        runner_factory=lambda **kwargs: IncompleteRunner(kwargs["journal_path"]),
        clock=lambda: NOW,
    )

    with pytest.raises(FactoryDispatchError, match="required build artifact"):
        await executor.execute(_dispatch(job, invocation), invocation, brief)


def _run_result(
    tmp_path: Path,
    *,
    exit_code: int,
    terminal_status: str,
    jsonl_lines: tuple[str, ...],
    process_cleanup_status: str = "not_required",
) -> CodexRunResult:
    journal_path = tmp_path / "journal.jsonl"
    journal = "".join(f"{line}\n" for line in jsonl_lines).encode("utf-8")
    journal_path.write_bytes(journal)
    return CodexRunResult(
        exit_code=exit_code,
        terminal_status=terminal_status,
        process_cleanup_status=process_cleanup_status,
        journal_path=journal_path,
        journal_sha256=hashlib.sha256(journal).hexdigest(),
        artifact_references=(),
        jsonl_lines=jsonl_lines,
    )


@pytest.mark.parametrize(
    ("jsonl_lines", "event_count", "thread_id", "event_types"),
    (
        ((), 0, None, []),
        (
            (
                json.dumps({"type": "thread.started", "thread_id": "thread-123"}),
                json.dumps({"type": "turn.started"}),
            ),
            2,
            "thread-123",
            ["thread.started", "turn.started"],
        ),
    ),
)
def test_session_receipt_retains_zero_or_partial_timeout_journal(
    tmp_path: Path,
    jsonl_lines: tuple[str, ...],
    event_count: int,
    thread_id: str | None,
    event_types: list[str],
) -> None:
    result = _run_result(
        tmp_path,
        exit_code=124,
        terminal_status="timed_out",
        jsonl_lines=jsonl_lines,
        process_cleanup_status="verified_cancelled",
    )

    receipt = json.loads(
        _session_receipt(
            result=result,
            session_id="factory-session-123",
            workspace_ref="workspace://factory/123",
            base_revision="a" * 40,
            command=("codex", "exec", "--json", "private prompt"),
            completed_at=NOW,
        )
    )

    assert receipt["status"] == "timed_out"
    assert receipt["exit_code"] == 124
    assert receipt["process_cleanup_status"] == "verified_cancelled"
    assert receipt["journal_sha256"] == result.journal_sha256
    assert receipt["event_count"] == event_count
    assert receipt["event_types"] == event_types
    assert receipt["codex_thread_id"] == thread_id
    assert "private prompt" not in json.dumps(receipt)


def test_session_receipt_rejects_empty_succeeded_journal(tmp_path: Path) -> None:
    result = _run_result(
        tmp_path,
        exit_code=0,
        terminal_status="succeeded",
        jsonl_lines=(),
    )

    with pytest.raises(FactoryDispatchError, match="Codex JSONL evidence is empty"):
        _session_receipt(
            result=result,
            session_id="factory-session-123",
            workspace_ref="workspace://factory/123",
            base_revision="a" * 40,
            command=("codex", "exec", "--json", "private prompt"),
            completed_at=NOW,
        )


@pytest.mark.asyncio
async def test_cli_executor_persists_timeout_receipt_before_raising_timeout_124(
    tmp_path: Path,
) -> None:
    job, brief, artifact_reader = _executor_job_and_brief()
    invocation = _seal_invocation(job, brief)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    class Preparer:
        def prepare(self, *_args):
            return PreparedFactoryWorkspace(root=workspace, base_revision="a" * 40)

    class TimedOutRunner:
        def __init__(self, journal_path: Path) -> None:
            self.journal_path = journal_path

        async def run(self, _authorized) -> CodexRunResult:
            journal = b""
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            self.journal_path.write_bytes(journal)
            return CodexRunResult(
                exit_code=124,
                terminal_status="timed_out",
                process_cleanup_status="verified_cancelled",
                journal_path=self.journal_path,
                journal_sha256=hashlib.sha256(journal).hexdigest(),
                artifact_references=(),
                jsonl_lines=(),
            )

    state_root = tmp_path / "state"
    executor = CodexCliFactoryBuildExecutor(
        settings=CodexCliFactoryBuildSettings(
            state_root=state_root,
            maximum_runtime_seconds=120,
        ),
        workspace_preparer=Preparer(),
        artifact_reader=artifact_reader,
        authorizer=RecordingAuthorizer(),
        runner_factory=lambda **kwargs: TimedOutRunner(kwargs["journal_path"]),
        clock=lambda: NOW,
    )

    with pytest.raises(FactoryDispatchError, match=r"timed out \(exit 124\)"):
        await executor.execute(_dispatch(job, invocation), invocation, brief)

    receipt_path = state_root / "sessions" / f"{invocation.idempotency_key}.json"
    receipt = json.loads(receipt_path.read_bytes())
    assert receipt["status"] == "timed_out"
    assert receipt["exit_code"] == 124
    assert receipt["event_count"] == 0


def test_git_workspace_preparer_creates_clean_detached_worktree(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(("git", "init", str(repository)), check=True, capture_output=True)
    (repository / "README.md").write_text("factory seed\n", encoding="utf-8")
    subprocess.run(("git", "-C", str(repository), "add", "README.md"), check=True)
    subprocess.run(
        (
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=Captain",
            "-c",
            "user.email=captain@example.invalid",
            "commit",
            "-m",
            "chore: seed",
        ),
        check=True,
        capture_output=True,
    )
    job, brief = _bound_job_and_brief()
    invocation = _seal_invocation(job, brief)
    preparer = GitDetachedFactoryWorkspacePreparer(
        repository_root=repository,
        workspaces_root=repository / ".captain-cook" / "private" / "codex-workspaces",
    )

    prepared = preparer.prepare(_dispatch(job, invocation), invocation, brief)

    assert prepared.root.is_dir()
    assert (prepared.root / "README.md").read_text(encoding="utf-8") == "factory seed\n"
    assert len(prepared.base_revision) == 40
    status = subprocess.run(
        ("git", "-C", str(prepared.root), "status", "--porcelain"),
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout == ""
    with pytest.raises(FactoryDispatchError, match="recovery"):
        preparer.prepare(_dispatch(job, invocation), invocation, brief)
