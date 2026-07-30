from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agenten.agent_factory.codex_build_execution import (
    CaptainCodexBuildSealer,
    CaptainFactoryCodexResumeAuthorizer,
    CodexCliFactoryBuildExecutor,
    CodexCliFactoryBuildSettings,
    CompletedCodexBuild,
    FactoryCodexBuildInterrupted,
    GitDetachedFactoryWorkspacePreparer,
    PreparedFactoryWorkspace,
    _session_receipt,
)
from agenten.agent_factory.codex_build_recovery import (
    FactoryCodexBuildCheckpointV1,
    FilesystemFactoryCodexBuildCheckpointStore,
    canonical_factory_codex_model,
)
from agenten.agent_factory.codex_build_provenance import (
    CaptainCodexBuildReceiptIssuer,
    CodexBuildArtifactCas,
)
from agenten.agent_factory.forge_contracts import ArtifactRef as ForgeArtifactRef
from agenten.agent_factory.orchestration import FactoryDispatch, FactoryDispatchError
from agenten.agent_factory.skill_sequence import FactoryRuntimeRetryAuthorizationV1
from agenten.agent_factory.skill_workflow_contracts import (
    CodexBuildBriefV1,
    CodexBuildEvidenceV1,
    FactorySkillInvocationV1,
    factory_runtime_retry_evidence_binding,
    factory_runtime_retry_evidence_binding_sha256,
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
        self.sealed: list[tuple[object, object, object]] = []
        self.sealed_evidence = None

    async def execute(self, request, invocation, brief) -> CompletedCodexBuild:
        self.calls.append((request, invocation, brief))
        return self.completed

    async def execute_authorized_resume(
        self, request, invocation, brief
    ) -> CompletedCodexBuild:
        self.calls.append((request, invocation, brief))
        return self.completed

    def replay_sealed(self, _invocation):
        return self.sealed_evidence

    def persist_sealed(self, invocation, completed, evidence):
        self.sealed.append((invocation, completed, evidence))
        self.sealed_evidence = evidence
        return evidence


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


def _authorized_runtime_retry_dispatch(
    dispatch: FactoryDispatch,
    invocation: FactorySkillInvocationV1,
    checkpoint: FactoryCodexBuildCheckpointV1,
    *,
    maximum_runtime_seconds: int = 60,
    issued_at: datetime = NOW,
    expires_at: datetime | None = None,
) -> FactoryDispatch:
    checkpoint_sha256 = hashlib.sha256(
        canonical_factory_codex_model(checkpoint)
    ).hexdigest()
    assert checkpoint.terminal_receipt_sha256 is not None
    authorization = FactoryRuntimeRetryAuthorizationV1(
        schema_name="captain.factory-runtime-retry-authorization.v1",
        authorization_ref=ArtifactRef(
            uri=f"artifact://factory/runtime-retry/{'9' * 64}",
            sha256="9" * 64,
            media_type="application/json",
        ),
        producer="captain",
        status="succeeded",
        job_id=dispatch.job.job_id,
        correlation_id=dispatch.job.correlation_id,
        subject_version=dispatch.job.subject_version,
        attempt=invocation.attempt,
        invocation_id=invocation.invocation_id,
        idempotency_key=invocation.idempotency_key,
        lease_id=invocation.lease.lease_id,
        checkpoint_ref=ArtifactRef(
            uri=f"artifact://factory/codex-checkpoint/{checkpoint_sha256}",
            sha256=checkpoint_sha256,
            media_type="application/json",
        ),
        terminal_receipt_ref=ArtifactRef(
            uri=(
                "artifact://factory/codex-terminal-receipt/"
                f"{checkpoint.terminal_receipt_sha256}"
            ),
            sha256=checkpoint.terminal_receipt_sha256,
            media_type="application/json",
        ),
        workspace_ref=checkpoint.workspace_ref,
        base_revision=checkpoint.base_revision,
        scaffold_manifest_sha256=checkpoint.scaffold_manifest_sha256,
        brief_sha256=checkpoint.brief_sha256,
        resume_ordinal=checkpoint.resume_ordinal + 1,
        maximum_runtime_seconds=maximum_runtime_seconds,
        issued_at=issued_at,
        expires_at=expires_at or issued_at + timedelta(minutes=2),
    )
    binding = factory_runtime_retry_evidence_binding(authorization)
    binding_sha256 = factory_runtime_retry_evidence_binding_sha256(binding)
    authorization = authorization.model_copy(
        update={
            "authorization_ref": ArtifactRef(
                uri=f"artifact://factory/runtime-retry/{binding_sha256}",
                sha256=binding_sha256,
                media_type="application/json",
            )
        }
    )
    return replace(dispatch, runtime_retry_authorization=authorization)


def _seed_git_repository(root: Path) -> Path:
    root.mkdir()
    subprocess.run(("git", "init", str(root)), check=True, capture_output=True)
    (root / "README.md").write_text("factory seed\n", encoding="utf-8")
    subprocess.run(("git", "-C", str(root), "add", "README.md"), check=True)
    subprocess.run(
        (
            "git",
            "-C",
            str(root),
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
    return root


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
    assert executor.sealed == [(invocation, executor.completed, evidence)]


@pytest.mark.asyncio
async def test_captain_sealer_records_successor_authority_after_original_lease_expiry(
    tmp_path: Path,
) -> None:
    cas = CodexBuildArtifactCas(tmp_path / "cas")
    workspace = _workspace(tmp_path, cas)
    job, brief, _artifact_reader = _executor_job_and_brief()
    invocation = _seal_invocation(job, brief)
    resumed_at = invocation.lease.expires_at + timedelta(seconds=1)
    executor = FakeBuildExecutor(
        CompletedCodexBuild(
            workspace_root=workspace,
            codex_session_receipt=b'{"session_id":"codex-session-resumed"}',
            candidate_manifest_path="factory-candidate.json",
            source_archive_path="candidate.zip",
            test_evidence_paths=("test-evidence.json",),
            completed_at=resumed_at,
        )
    )
    sealer = CaptainCodexBuildSealer(
        executor=executor,
        issuer=CaptainCodexBuildReceiptIssuer(cas),
    )
    authorization = FactoryRuntimeRetryAuthorizationV1(
        schema_name="captain.factory-runtime-retry-authorization.v1",
        authorization_ref=ArtifactRef(
            uri=f"artifact://factory/runtime-retry/{'9' * 64}",
            sha256="9" * 64,
            media_type="application/json",
        ),
        producer="captain",
        status="succeeded",
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        subject_version=job.subject_version,
        attempt=invocation.attempt,
        invocation_id=invocation.invocation_id,
        idempotency_key=invocation.idempotency_key,
        lease_id=invocation.lease.lease_id,
        checkpoint_ref=ArtifactRef(
            uri=f"artifact://factory/codex-checkpoint/{'c' * 64}",
            sha256="c" * 64,
            media_type="application/json",
        ),
        terminal_receipt_ref=ArtifactRef(
            uri=f"artifact://factory/codex-terminal-receipt/{'d' * 64}",
            sha256="d" * 64,
            media_type="application/json",
        ),
        workspace_ref=invocation.lease.workspace_ref,
        base_revision="e" * 40,
        scaffold_manifest_sha256="f" * 64,
        brief_sha256="1" * 64,
        resume_ordinal=1,
        maximum_runtime_seconds=60,
        issued_at=resumed_at - timedelta(seconds=1),
        expires_at=resumed_at + timedelta(minutes=1),
    )
    retry_binding = factory_runtime_retry_evidence_binding(authorization)
    retry_digest = factory_runtime_retry_evidence_binding_sha256(retry_binding)
    authorization = authorization.model_copy(
        update={
            "authorization_ref": ArtifactRef(
                uri=f"artifact://factory/runtime-retry/{retry_digest}",
                sha256=retry_digest,
                media_type="application/json",
            )
        }
    )
    request = replace(
        _dispatch(job, invocation),
        runtime_retry_authorization=authorization,
    )

    evidence = await sealer.seal(request, invocation, brief)

    assert evidence.invocation == invocation
    assert evidence.occurred_at == resumed_at
    assert evidence.runtime_retry_ref == authorization.authorization_ref
    assert evidence.evidence_refs == (
        evidence.build_receipt_ref,
        authorization.authorization_ref,
    )
    serialized = evidence.model_dump(mode="json", by_alias=True)
    mutated = json.loads(json.dumps(serialized))
    mutated["runtime_retry_binding"]["invocation_id"] = str(job.event_id)
    with pytest.raises(ValueError, match="recovery binding"):
        CodexBuildEvidenceV1.model_validate(mutated)
    mutated = json.loads(json.dumps(serialized))
    mutated["runtime_retry_binding"]["checkpoint_ref"]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="binding digest"):
        CodexBuildEvidenceV1.model_validate(mutated)
    mutated = json.loads(json.dumps(serialized))
    mutated["evidence_refs"] = [mutated["build_receipt_ref"]]
    with pytest.raises(ValueError, match="recovery authority ref"):
        CodexBuildEvidenceV1.model_validate(mutated)


@pytest.mark.asyncio
async def test_sealed_replay_returns_original_evidence_without_resnapshotting_workspace(
    tmp_path: Path,
) -> None:
    cas = CodexBuildArtifactCas(tmp_path / "cas")
    workspace = _workspace(tmp_path, cas)
    job, brief, artifact_reader = _executor_job_and_brief()
    invocation = _seal_invocation(job, brief)
    state_root = tmp_path / "state"

    class Preparer:
        def prepare(self, *_args):
            return PreparedFactoryWorkspace(root=workspace, base_revision="a" * 40)

        def prepare_or_recover(self, *_args):
            return PreparedFactoryWorkspace(root=workspace, base_revision="a" * 40)

    class EvidenceOnlyRunner:
        def __init__(self, journal_path: Path) -> None:
            self.journal_path = journal_path

        async def run(self, _authorized) -> CodexRunResult:
            line = json.dumps(
                {"type": "thread.started", "thread_id": "sealed-replay-thread"}
            )
            content = f"{line}\n".encode("utf-8")
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            self.journal_path.write_bytes(content)
            return CodexRunResult(
                exit_code=0,
                terminal_status="succeeded",
                process_cleanup_status="not_required",
                journal_path=self.journal_path,
                journal_sha256=hashlib.sha256(content).hexdigest(),
                artifact_references=(),
                jsonl_lines=(line,),
            )

    class CountingIssuer(CaptainCodexBuildReceiptIssuer):
        def __init__(self, artifact_cas: CodexBuildArtifactCas) -> None:
            super().__init__(artifact_cas)
            self.issue_calls = 0
            self.persist_calls = 0

        def issue(self, **kwargs):
            self.issue_calls += 1
            return super().issue(**kwargs)

        def persist_receipt(self, receipt):
            self.persist_calls += 1
            return super().persist_receipt(receipt)

    issuer = CountingIssuer(cas)
    executor = CodexCliFactoryBuildExecutor(
        settings=CodexCliFactoryBuildSettings(
            state_root=state_root,
            maximum_runtime_seconds=120,
        ),
        workspace_preparer=Preparer(),
        artifact_reader=artifact_reader,
        authorizer=RecordingAuthorizer(),
        runner_factory=lambda **kwargs: EvidenceOnlyRunner(kwargs["journal_path"]),
        clock=lambda: NOW,
    )
    sealer = CaptainCodexBuildSealer(executor=executor, issuer=issuer)
    dispatch = _dispatch(job, invocation)

    first = await sealer.seal(dispatch, invocation, brief)
    first_bytes = json.dumps(
        first.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    (workspace / "src" / "team.py").write_text(
        "TEAM = 'mutated-after-seal'\n", encoding="utf-8"
    )

    replay = await sealer.seal(dispatch, invocation, brief)

    assert json.dumps(
        replay.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") == first_bytes
    assert issuer.issue_calls == 1
    assert issuer.persist_calls == 1

    (state_root / "sealed-evidence" / f"{invocation.invocation_id.hex}.json").unlink()
    with pytest.raises(
        FactoryDispatchError,
        match="original sealed evidence is missing",
    ):
        await sealer.seal(dispatch, invocation, brief)
    assert issuer.issue_calls == 1
    assert issuer.persist_calls == 1


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
async def test_authorization_failure_retries_same_uncheckpointed_detached_workspace(
    tmp_path: Path,
) -> None:
    repository = _seed_git_repository(tmp_path / "repo")
    workspaces_root = repository / ".factory-workspaces"
    state_root = tmp_path / "state"
    job, brief, artifact_reader = _executor_job_and_brief()
    invocation = _seal_invocation(job, brief)
    preparer = GitDetachedFactoryWorkspacePreparer(
        repository_root=repository,
        workspaces_root=workspaces_root,
    )
    runner_calls = 0

    class RejectingAuthorizer:
        def authorize(self, _request):
            raise FactoryDispatchError("authorization rejected scaffold")

    def forbidden_runner(**_kwargs):
        raise AssertionError("runner must not start before authorization")

    first = CodexCliFactoryBuildExecutor(
        settings=CodexCliFactoryBuildSettings(
            state_root=state_root,
            maximum_runtime_seconds=120,
        ),
        workspace_preparer=preparer,
        artifact_reader=artifact_reader,
        authorizer=RejectingAuthorizer(),
        runner_factory=forbidden_runner,
        clock=lambda: NOW,
    )

    with pytest.raises(FactoryDispatchError, match="authorization rejected scaffold"):
        await first.execute(_dispatch(job, invocation), invocation, brief)

    assert FilesystemFactoryCodexBuildCheckpointStore(
        state_root / "checkpoints"
    ).load(invocation) is None
    target = next(workspaces_root.rglob("attempt-*"))
    assert not (target / ".captain-inputs").exists()

    def successful_runner(**kwargs):
        nonlocal runner_calls
        runner_calls += 1
        return SuccessfulRunner(target, kwargs["journal_path"])

    second = CodexCliFactoryBuildExecutor(
        settings=CodexCliFactoryBuildSettings(
            state_root=state_root,
            maximum_runtime_seconds=120,
        ),
        workspace_preparer=preparer,
        artifact_reader=artifact_reader,
        authorizer=RecordingAuthorizer(),
        runner_factory=successful_runner,
        clock=lambda: NOW,
    )

    completed = await second.execute(_dispatch(job, invocation), invocation, brief)

    assert completed.workspace_root == target
    assert runner_calls == 1


@pytest.mark.asyncio
async def test_partial_scaffold_materialization_retries_without_runner_duplication(
    tmp_path: Path,
) -> None:
    repository = _seed_git_repository(tmp_path / "repo")
    workspaces_root = repository / ".factory-workspaces"
    state_root = tmp_path / "state"
    job, brief, artifact_reader = _executor_job_and_brief()
    invocation = _seal_invocation(job, brief)
    preparer = GitDetachedFactoryWorkspacePreparer(
        repository_root=repository,
        workspaces_root=workspaces_root,
    )
    runner_calls = 0

    def forbidden_runner(**_kwargs):
        raise AssertionError("runner must not start before scaffold checkpoint")

    first = CodexCliFactoryBuildExecutor(
        settings=CodexCliFactoryBuildSettings(
            state_root=state_root,
            maximum_runtime_seconds=120,
        ),
        workspace_preparer=preparer,
        artifact_reader=artifact_reader,
        authorizer=RecordingAuthorizer(),
        runner_factory=forbidden_runner,
        clock=lambda: NOW,
    )

    def interrupt_materialization(files, workspace):
        destination = workspace / ".captain-inputs"
        destination.mkdir()
        name = sorted(files)[0]
        (destination / name).write_bytes(files[name])
        raise FactoryDispatchError("simulated scaffold interruption")

    first._materialize_inputs = interrupt_materialization

    with pytest.raises(FactoryDispatchError, match="simulated scaffold interruption"):
        await first.execute(_dispatch(job, invocation), invocation, brief)

    assert FilesystemFactoryCodexBuildCheckpointStore(
        state_root / "checkpoints"
    ).load(invocation) is None
    target = next(workspaces_root.rglob("attempt-*"))
    assert (target / ".captain-inputs").is_dir()

    def successful_runner(**kwargs):
        nonlocal runner_calls
        runner_calls += 1
        return SuccessfulRunner(target, kwargs["journal_path"])

    second = CodexCliFactoryBuildExecutor(
        settings=CodexCliFactoryBuildSettings(
            state_root=state_root,
            maximum_runtime_seconds=120,
        ),
        workspace_preparer=preparer,
        artifact_reader=artifact_reader,
        authorizer=RecordingAuthorizer(),
        runner_factory=successful_runner,
        clock=lambda: NOW,
    )

    completed = await second.execute(_dispatch(job, invocation), invocation, brief)

    assert completed.workspace_root == target
    assert runner_calls == 1


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

    checkpoint = FilesystemFactoryCodexBuildCheckpointStore(
        tmp_path / "state" / "checkpoints"
    ).load(invocation)
    assert checkpoint is not None
    assert checkpoint.phase == "implementation_complete"


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


def test_session_receipt_reports_empty_succeeded_journal_truthfully(tmp_path: Path) -> None:
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


@pytest.mark.parametrize("exit_code", (0, 1, 124))
def test_factory_interruption_rejects_noncanonical_cancellation_exit_code(
    exit_code: int,
) -> None:
    checkpoint_ref = ArtifactRef(
        uri="artifact://factory/codex-checkpoint/" + "a" * 64,
        sha256="a" * 64,
        media_type="application/json",
    )
    receipt_ref = ArtifactRef(
        uri="artifact://factory/codex-terminal-receipt/" + "b" * 64,
        sha256="b" * 64,
        media_type="application/json",
    )

    with pytest.raises(ValueError, match="requires exit 130"):
        FactoryCodexBuildInterrupted(
            reason="runtime_cancelled",
            exit_code=exit_code,
            checkpoint_ref=checkpoint_ref,
            terminal_receipt_ref=receipt_ref,
            resume_ordinal=0,
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

    with pytest.raises(FactoryCodexBuildInterrupted) as captured:
        await executor.execute(_dispatch(job, invocation), invocation, brief)

    assert str(captured.value) == "Factory Codex build interrupted"
    assert captured.value.reason == "codex_timed_out"
    assert captured.value.exit_code == 124
    binding = captured.value.authorization_binding
    assert binding is not None
    assert binding.job_id == job.job_id
    assert binding.correlation_id == job.correlation_id
    assert binding.subject_version == job.subject_version
    assert binding.attempt == invocation.attempt
    assert binding.invocation_id == invocation.invocation_id
    assert binding.idempotency_key == invocation.idempotency_key
    assert binding.lease_id == invocation.lease.lease_id
    assert binding.workspace_ref == invocation.lease.workspace_ref
    assert binding.base_revision == "a" * 40
    assert set(binding.as_dict()) == {
        "job_id",
        "correlation_id",
        "subject_version",
        "attempt",
        "invocation_id",
        "idempotency_key",
        "lease_id",
        "workspace_ref",
        "base_revision",
        "scaffold_manifest_sha256",
        "brief_sha256",
    }

    receipt_path = state_root / "sessions" / f"{invocation.idempotency_key}.json"
    receipt = json.loads(receipt_path.read_bytes())
    assert receipt["status"] == "timed_out"
    assert receipt["exit_code"] == 124
    assert receipt["event_count"] == 0
    checkpoint = FilesystemFactoryCodexBuildCheckpointStore(
        state_root / "checkpoints"
    ).load(invocation)
    assert checkpoint is not None
    assert checkpoint.phase == "implementation_interrupted"
    assert checkpoint.terminal_receipt_sha256 == hashlib.sha256(
        receipt_path.read_bytes()
    ).hexdigest()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exit_code", "terminal_status"),
    (
        (124, "timed_out"),
        (130, "cancelled"),
    ),
)
async def test_cli_executor_persists_unresolved_cleanup_but_never_makes_it_resumable(
    tmp_path: Path,
    exit_code: int,
    terminal_status: str,
) -> None:
    job, brief, artifact_reader = _executor_job_and_brief()
    invocation = _seal_invocation(job, brief)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    runner_calls = 0

    class RecoveringPreparer:
        def prepare_or_recover(self, _request, _invocation, _brief, _checkpoint):
            return PreparedFactoryWorkspace(root=workspace, base_revision="a" * 40)

    class UnresolvedRunner:
        def __init__(self, journal_path: Path) -> None:
            self.journal_path = journal_path

        async def run(self, _authorized) -> CodexRunResult:
            line = json.dumps({"type": "turn.started"})
            journal = f"{line}\n".encode("utf-8")
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            self.journal_path.write_bytes(journal)
            return CodexRunResult(
                exit_code=exit_code,
                terminal_status=terminal_status,
                process_cleanup_status="unresolved",
                journal_path=self.journal_path,
                journal_sha256=hashlib.sha256(journal).hexdigest(),
                artifact_references=(),
                jsonl_lines=(line,),
            )

    def runner_factory(**kwargs):
        nonlocal runner_calls
        runner_calls += 1
        return UnresolvedRunner(kwargs["journal_path"])

    executor = CodexCliFactoryBuildExecutor(
        settings=CodexCliFactoryBuildSettings(
            state_root=state_root,
            maximum_runtime_seconds=120,
        ),
        workspace_preparer=RecoveringPreparer(),
        artifact_reader=artifact_reader,
        authorizer=RecordingAuthorizer(),
        runner_factory=runner_factory,
        resume_authorizer=CaptainFactoryCodexResumeAuthorizer(clock=lambda: NOW),
        clock=lambda: NOW,
    )
    dispatch = _dispatch(job, invocation)

    with pytest.raises(FactoryDispatchError, match="cleanup is unresolved"):
        await executor.execute(dispatch, invocation, brief)

    receipt_path = state_root / "sessions" / f"{invocation.idempotency_key}.json"
    receipt = json.loads(receipt_path.read_bytes())
    assert receipt["status"] == terminal_status
    assert receipt["exit_code"] == exit_code
    assert receipt["process_cleanup_status"] == "unresolved"
    assert receipt["event_count"] == 1
    checkpoint = FilesystemFactoryCodexBuildCheckpointStore(
        state_root / "checkpoints"
    ).load(invocation)
    assert checkpoint is not None
    assert checkpoint.phase == "implementation_running"
    assert checkpoint.terminal_receipt_sha256 is None

    with pytest.raises(FactoryDispatchError, match="already running or unresolved"):
        await executor.execute(dispatch, invocation, brief)
    with pytest.raises(FactoryDispatchError, match="not interrupted"):
        executor.validate_authorized_resume(dispatch, invocation)
    assert runner_calls == 1


@pytest.mark.asyncio
async def test_cli_executor_makes_verified_controlled_cancellation_resumable(
    tmp_path: Path,
) -> None:
    job, brief, artifact_reader = _executor_job_and_brief()
    invocation = _seal_invocation(job, brief)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"

    class Preparer:
        def prepare(self, *_args):
            return PreparedFactoryWorkspace(root=workspace, base_revision="a" * 40)

    class CancelledRunner:
        def __init__(self, journal_path: Path) -> None:
            self.journal_path = journal_path

        async def run(self, _authorized) -> CodexRunResult:
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            self.journal_path.write_bytes(b"")
            return CodexRunResult(
                exit_code=130,
                terminal_status="cancelled",
                process_cleanup_status="verified_cancelled",
                journal_path=self.journal_path,
                journal_sha256=hashlib.sha256(b"").hexdigest(),
                artifact_references=(),
                jsonl_lines=(),
            )

    executor = CodexCliFactoryBuildExecutor(
        settings=CodexCliFactoryBuildSettings(
            state_root=state_root,
            maximum_runtime_seconds=120,
        ),
        workspace_preparer=Preparer(),
        artifact_reader=artifact_reader,
        authorizer=RecordingAuthorizer(),
        runner_factory=lambda **kwargs: CancelledRunner(kwargs["journal_path"]),
        clock=lambda: NOW,
    )

    with pytest.raises(FactoryCodexBuildInterrupted) as captured:
        await executor.execute(_dispatch(job, invocation), invocation, brief)

    assert captured.value.reason == "runtime_cancelled"
    assert captured.value.exit_code == 130
    receipt_path = state_root / "sessions" / f"{invocation.idempotency_key}.json"
    receipt = json.loads(receipt_path.read_bytes())
    assert receipt["status"] == "cancelled"
    assert receipt["process_cleanup_status"] == "verified_cancelled"
    checkpoint = FilesystemFactoryCodexBuildCheckpointStore(
        state_root / "checkpoints"
    ).load(invocation)
    assert checkpoint is not None
    assert checkpoint.phase == "implementation_interrupted"
    assert checkpoint.terminal_receipt_sha256 == hashlib.sha256(
        receipt_path.read_bytes()
    ).hexdigest()


@pytest.mark.asyncio
async def test_interrupted_ordinary_redispatch_revalidates_workspace_and_never_runs(
    tmp_path: Path,
) -> None:
    job, brief, artifact_reader = _executor_job_and_brief()
    invocation = _seal_invocation(job, brief)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    preparer_calls: list[object | None] = []

    class RecoveringPreparer:
        def prepare_or_recover(self, _request, _invocation, _brief, checkpoint):
            preparer_calls.append(checkpoint)
            return PreparedFactoryWorkspace(root=workspace, base_revision="a" * 40)

    class TimedOutRunner:
        async def run(self, _authorized) -> CodexRunResult:
            journal_path = state_root / "journals" / f"{invocation.idempotency_key}.jsonl"
            journal_path.parent.mkdir(parents=True, exist_ok=True)
            journal_path.write_bytes(b"")
            return CodexRunResult(
                exit_code=124,
                terminal_status="timed_out",
                process_cleanup_status="verified_cancelled",
                journal_path=journal_path,
                journal_sha256=hashlib.sha256(b"").hexdigest(),
                artifact_references=(),
                jsonl_lines=(),
            )

    runner_calls: list[object] = []

    def runner_factory(**_kwargs):
        runner_calls.append(object())
        return TimedOutRunner()

    executor = CodexCliFactoryBuildExecutor(
        settings=CodexCliFactoryBuildSettings(
            state_root=state_root,
            maximum_runtime_seconds=120,
        ),
        workspace_preparer=RecoveringPreparer(),
        artifact_reader=artifact_reader,
        authorizer=RecordingAuthorizer(),
        runner_factory=runner_factory,
        clock=lambda: NOW,
    )

    with pytest.raises(FactoryCodexBuildInterrupted):
        await executor.execute(_dispatch(job, invocation), invocation, brief)
    input_digests = {
        item.name: hashlib.sha256(item.read_bytes()).hexdigest()
        for item in (workspace / ".captain-inputs").iterdir()
    }

    with pytest.raises(FactoryCodexBuildInterrupted) as captured:
        await executor.execute(_dispatch(job, invocation), invocation, brief)
    assert captured.value.reason == "resume_authorization_required"
    assert captured.value.exit_code is None
    checkpoint = FilesystemFactoryCodexBuildCheckpointStore(
        state_root / "checkpoints"
    ).load(invocation)
    assert checkpoint is not None
    authorized_dispatch = _authorized_runtime_retry_dispatch(
        _dispatch(job, invocation),
        invocation,
        checkpoint,
    )
    with pytest.raises(FactoryDispatchError, match="validator is not configured"):
        await executor.execute_authorized_resume(
            authorized_dispatch,
            invocation,
            brief,
        )

    assert len(runner_calls) == 1
    assert len(preparer_calls) == 2
    assert preparer_calls[0] is None
    assert preparer_calls[1].phase == "implementation_interrupted"
    assert input_digests == {
        item.name: hashlib.sha256(item.read_bytes()).hexdigest()
        for item in (workspace / ".captain-inputs").iterdir()
    }
    (workspace / ".captain-inputs" / "job-input.md").write_bytes(b"tampered")
    with pytest.raises(FactoryDispatchError, match="input digest changed"):
        await executor.execute(_dispatch(job, invocation), invocation, brief)
    assert len(runner_calls) == 1


@pytest.mark.asyncio
async def test_setup_elapsed_authority_fails_before_runner_construction(
    tmp_path: Path,
) -> None:
    job, brief, artifact_reader = _executor_job_and_brief()
    invocation = _seal_invocation(job, brief)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    current = NOW
    runner_calls: list[int] = []

    class AdvancingPreparer:
        def prepare(self, *_args):
            nonlocal current
            current = min(invocation.lease.expires_at, job.deadline_at) - timedelta(
                milliseconds=500
            )
            return PreparedFactoryWorkspace(root=workspace, base_revision="a" * 40)

    def forbidden_runner(**_kwargs):
        runner_calls.append(1)
        raise AssertionError("runner must not be constructed outside authority")

    executor = CodexCliFactoryBuildExecutor(
        settings=CodexCliFactoryBuildSettings(
            state_root=tmp_path / "state",
            maximum_runtime_seconds=120,
        ),
        workspace_preparer=AdvancingPreparer(),
        artifact_reader=artifact_reader,
        authorizer=RecordingAuthorizer(),
        runner_factory=forbidden_runner,
        clock=lambda: current,
    )

    with pytest.raises(FactoryDispatchError, match="remaining runtime"):
        await executor.execute(_dispatch(job, invocation), invocation, brief)

    assert runner_calls == []


@pytest.mark.asyncio
async def test_checkpoint_binds_canonical_brief_and_rejects_coordinated_replacement(
    tmp_path: Path,
) -> None:
    job, brief, artifact_reader = _executor_job_and_brief()
    invocation = _seal_invocation(job, brief)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"

    class RecoveringPreparer:
        def prepare_or_recover(self, _request, _invocation, _brief, _checkpoint):
            return PreparedFactoryWorkspace(root=workspace, base_revision="a" * 40)

    class TimedOutRunner:
        def __init__(self, journal_path: Path) -> None:
            self.journal_path = journal_path

        async def run(self, _authorized) -> CodexRunResult:
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            self.journal_path.write_bytes(b"")
            return CodexRunResult(
                exit_code=124,
                terminal_status="timed_out",
                process_cleanup_status="verified_cancelled",
                journal_path=self.journal_path,
                journal_sha256=hashlib.sha256(b"").hexdigest(),
                artifact_references=(),
                jsonl_lines=(),
            )

    executor = CodexCliFactoryBuildExecutor(
        settings=CodexCliFactoryBuildSettings(
            state_root=state_root,
            maximum_runtime_seconds=120,
        ),
        workspace_preparer=RecoveringPreparer(),
        artifact_reader=artifact_reader,
        authorizer=RecordingAuthorizer(),
        runner_factory=lambda **kwargs: TimedOutRunner(kwargs["journal_path"]),
        clock=lambda: NOW,
    )
    dispatch = _dispatch(job, invocation)
    with pytest.raises(FactoryCodexBuildInterrupted):
        await executor.execute(dispatch, invocation, brief)

    checkpoint = FilesystemFactoryCodexBuildCheckpointStore(
        state_root / "checkpoints"
    ).load(invocation)
    assert checkpoint is not None
    canonical_brief = json.dumps(
        brief.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert checkpoint.brief_sha256 == hashlib.sha256(canonical_brief).hexdigest()
    assert checkpoint.scaffold_manifest_sha256

    changed_input = b'{"input":"coordinated replacement"}'
    changed_ref = ArtifactRef(
        uri=f"artifact://test/{hashlib.sha256(changed_input).hexdigest()}",
        sha256=hashlib.sha256(changed_input).hexdigest(),
        media_type="application/json",
    )
    changed_job = job.model_copy(update={"input_ref": changed_ref})
    payload = brief.model_dump(mode="json", by_alias=True)
    payload["required_test_command_ids"] = ["pytest.changed"]
    changed_brief = CodexBuildBriefV1.model_validate(payload)
    changed_contents = dict(artifact_reader._contents)
    changed_contents[changed_ref.sha256] = changed_input
    changed_reader = StaticArtifactReader(changed_contents)
    (workspace / ".captain-inputs" / "job-input.md").write_bytes(changed_input)
    (workspace / ".captain-inputs" / "codex-build-brief.json").write_text(
        changed_brief.model_dump_json(by_alias=True), encoding="utf-8"
    )
    changed_executor = CodexCliFactoryBuildExecutor(
        settings=CodexCliFactoryBuildSettings(
            state_root=state_root,
            maximum_runtime_seconds=120,
        ),
        workspace_preparer=RecoveringPreparer(),
        artifact_reader=changed_reader,
        authorizer=RecordingAuthorizer(),
        runner_factory=lambda **kwargs: TimedOutRunner(kwargs["journal_path"]),
        clock=lambda: NOW,
    )

    with pytest.raises(FactoryDispatchError, match="original scaffold manifest"):
        await changed_executor.execute(
            _dispatch(changed_job, invocation), invocation, changed_brief
        )


@pytest.mark.asyncio
async def test_authorized_resume_uses_next_ordinal_without_replacing_timeout_receipt(
    tmp_path: Path,
) -> None:
    job, brief, artifact_reader = _executor_job_and_brief()
    invocation = _seal_invocation(job, brief)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    current = NOW
    observed_deadlines: list[datetime] = []

    class RecoveringPreparer:
        def prepare_or_recover(self, _request, _invocation, _brief, _checkpoint):
            return PreparedFactoryWorkspace(root=workspace, base_revision="a" * 40)

    class TimedOutRunner:
        def __init__(self, journal_path: Path) -> None:
            self.journal_path = journal_path

        async def run(self, _authorized) -> CodexRunResult:
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            self.journal_path.write_bytes(b"")
            return CodexRunResult(
                exit_code=124,
                terminal_status="timed_out",
                process_cleanup_status="verified_cancelled",
                journal_path=self.journal_path,
                journal_sha256=hashlib.sha256(b"").hexdigest(),
                artifact_references=(),
                jsonl_lines=(),
            )

    runner_count = 0

    def runner_factory(**kwargs):
        nonlocal runner_count
        runner_count += 1
        observed_deadlines.append(kwargs["deadline_at"])
        if runner_count == 1:
            return TimedOutRunner(kwargs["journal_path"])
        return SuccessfulRunner(workspace, kwargs["journal_path"])

    executor = CodexCliFactoryBuildExecutor(
        settings=CodexCliFactoryBuildSettings(
            state_root=state_root,
            maximum_runtime_seconds=120,
        ),
        workspace_preparer=RecoveringPreparer(),
        artifact_reader=artifact_reader,
        authorizer=RecordingAuthorizer(),
        runner_factory=runner_factory,
        resume_authorizer=CaptainFactoryCodexResumeAuthorizer(clock=lambda: current),
        clock=lambda: current,
    )
    dispatch = _dispatch(job, invocation)

    with pytest.raises(FactoryCodexBuildInterrupted):
        await executor.execute(dispatch, invocation, brief)
    checkpoint = FilesystemFactoryCodexBuildCheckpointStore(
        state_root / "checkpoints"
    ).load(invocation)
    assert checkpoint is not None
    current = invocation.lease.expires_at + timedelta(seconds=1)
    authorized_dispatch = _authorized_runtime_retry_dispatch(
        dispatch,
        invocation,
        checkpoint,
        issued_at=current,
        expires_at=current + timedelta(minutes=2),
    )
    timeout_receipt = (
        state_root / "sessions" / f"{invocation.idempotency_key}.json"
    ).read_bytes()
    assert authorized_dispatch.runtime_retry_authorization is not None
    invalid_authorization = authorized_dispatch.runtime_retry_authorization.model_copy(
        update={
            "authorization_ref": ArtifactRef(
                uri=f"artifact://factory/runtime-retry/{'0' * 64}",
                sha256="0" * 64,
                media_type="application/json",
            )
        }
    )
    with pytest.raises(FactoryDispatchError, match="runtime retry authority is invalid"):
        await executor.execute_authorized_resume(
            replace(
                authorized_dispatch,
                runtime_retry_authorization=invalid_authorization,
            ),
            invocation,
            brief,
        )
    assert runner_count == 1

    class RejectingAuthorizer:
        def authorize(self, _request):
            raise FactoryDispatchError("execution policy rejected resume")

    rejecting_executor = CodexCliFactoryBuildExecutor(
        settings=CodexCliFactoryBuildSettings(
            state_root=state_root,
            maximum_runtime_seconds=120,
        ),
        workspace_preparer=RecoveringPreparer(),
        artifact_reader=artifact_reader,
        authorizer=RejectingAuthorizer(),
        runner_factory=runner_factory,
        resume_authorizer=CaptainFactoryCodexResumeAuthorizer(clock=lambda: current),
        clock=lambda: current,
    )
    with pytest.raises(FactoryDispatchError, match="execution policy rejected"):
        await rejecting_executor.execute_authorized_resume(
            authorized_dispatch,
            invocation,
            brief,
        )
    still_interrupted = FilesystemFactoryCodexBuildCheckpointStore(
        state_root / "checkpoints"
    ).load(invocation)
    assert still_interrupted is not None
    assert still_interrupted.phase == "implementation_interrupted"

    completed = await executor.execute_authorized_resume(
        authorized_dispatch,
        invocation,
        brief,
    )

    checkpoint = FilesystemFactoryCodexBuildCheckpointStore(
        state_root / "checkpoints"
    ).load(invocation)
    assert checkpoint is not None
    assert checkpoint.phase == "implementation_complete"
    assert checkpoint.resume_ordinal == 1
    assert runner_count == 2
    assert observed_deadlines == [
        min(invocation.lease.expires_at, job.deadline_at),
        min(authorized_dispatch.runtime_retry_authorization.expires_at, job.deadline_at),
    ]
    assert (
        state_root / "sessions" / f"{invocation.idempotency_key}.json"
    ).read_bytes() == timeout_receipt
    assert completed.codex_session_receipt == (
        state_root
        / "sessions"
        / f"{invocation.idempotency_key}.resume-1.json"
    ).read_bytes()


@pytest.mark.asyncio
async def test_authorized_resume_rejects_completion_after_authorization_expiry(
    tmp_path: Path,
) -> None:
    job, brief, artifact_reader = _executor_job_and_brief()
    invocation = _seal_invocation(job, brief)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    current = NOW
    observed_timeouts: list[int] = []

    class RecoveringPreparer:
        def prepare_or_recover(self, _request, _invocation, _brief, _checkpoint):
            return PreparedFactoryWorkspace(root=workspace, base_revision="a" * 40)

    class TimedOutRunner:
        def __init__(self, journal_path: Path) -> None:
            self.journal_path = journal_path

        async def run(self, _authorized) -> CodexRunResult:
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            self.journal_path.write_bytes(b"")
            return CodexRunResult(
                exit_code=124,
                terminal_status="timed_out",
                process_cleanup_status="verified_cancelled",
                journal_path=self.journal_path,
                journal_sha256=hashlib.sha256(b"").hexdigest(),
                artifact_references=(),
                jsonl_lines=(),
            )

    class ExpiringSuccessfulRunner(SuccessfulRunner):
        async def run(self, authorized: AuthorizedCodexRun) -> CodexRunResult:
            nonlocal current
            result = await super().run(authorized)
            current = NOW + timedelta(seconds=3)
            return result

    runner_count = 0

    def runner_factory(**kwargs):
        nonlocal runner_count
        runner_count += 1
        observed_timeouts.append(kwargs["maximum_runtime_seconds"])
        if runner_count == 1:
            return TimedOutRunner(kwargs["journal_path"])
        return ExpiringSuccessfulRunner(workspace, kwargs["journal_path"])

    executor = CodexCliFactoryBuildExecutor(
        settings=CodexCliFactoryBuildSettings(
            state_root=state_root,
            maximum_runtime_seconds=120,
        ),
        workspace_preparer=RecoveringPreparer(),
        artifact_reader=artifact_reader,
        authorizer=RecordingAuthorizer(),
        runner_factory=runner_factory,
        resume_authorizer=CaptainFactoryCodexResumeAuthorizer(
            clock=lambda: current
        ),
        clock=lambda: current,
    )
    dispatch = _dispatch(job, invocation)
    with pytest.raises(FactoryCodexBuildInterrupted):
        await executor.execute(dispatch, invocation, brief)
    checkpoint = FilesystemFactoryCodexBuildCheckpointStore(
        state_root / "checkpoints"
    ).load(invocation)
    assert checkpoint is not None
    authorized = _authorized_runtime_retry_dispatch(
        dispatch,
        invocation,
        checkpoint,
        maximum_runtime_seconds=2,
        expires_at=NOW + timedelta(seconds=2),
    )

    with pytest.raises(FactoryDispatchError, match="outside Captain authority"):
        await executor.execute_authorized_resume(authorized, invocation, brief)

    assert observed_timeouts[-1] <= 2


@pytest.mark.asyncio
async def test_authorized_resume_revalidates_deadline_after_runner_construction(
    tmp_path: Path,
) -> None:
    job, brief, artifact_reader = _executor_job_and_brief()
    invocation = _seal_invocation(job, brief)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    current = NOW

    class RecoveringPreparer:
        def prepare_or_recover(self, _request, _invocation, _brief, _checkpoint):
            return PreparedFactoryWorkspace(root=workspace, base_revision="a" * 40)

    class TimedOutRunner:
        def __init__(self, journal_path: Path) -> None:
            self.journal_path = journal_path

        async def run(self, _authorized) -> CodexRunResult:
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            self.journal_path.write_bytes(b"")
            return CodexRunResult(
                exit_code=124,
                terminal_status="timed_out",
                process_cleanup_status="verified_cancelled",
                journal_path=self.journal_path,
                journal_sha256=hashlib.sha256(b"").hexdigest(),
                artifact_references=(),
                jsonl_lines=(),
            )

    runner_count = 0
    resume_runner: SuccessfulRunner | None = None

    def runner_factory(**kwargs):
        nonlocal current, resume_runner, runner_count
        runner_count += 1
        if runner_count == 1:
            return TimedOutRunner(kwargs["journal_path"])
        current = NOW + timedelta(seconds=3)
        resume_runner = SuccessfulRunner(workspace, kwargs["journal_path"])
        return resume_runner

    executor = CodexCliFactoryBuildExecutor(
        settings=CodexCliFactoryBuildSettings(
            state_root=state_root,
            maximum_runtime_seconds=120,
        ),
        workspace_preparer=RecoveringPreparer(),
        artifact_reader=artifact_reader,
        authorizer=RecordingAuthorizer(),
        runner_factory=runner_factory,
        resume_authorizer=CaptainFactoryCodexResumeAuthorizer(
            clock=lambda: current
        ),
        clock=lambda: current,
    )
    dispatch = _dispatch(job, invocation)
    with pytest.raises(FactoryCodexBuildInterrupted):
        await executor.execute(dispatch, invocation, brief)
    interrupted = FilesystemFactoryCodexBuildCheckpointStore(
        state_root / "checkpoints"
    ).load(invocation)
    assert interrupted is not None
    authorized = _authorized_runtime_retry_dispatch(
        dispatch,
        invocation,
        interrupted,
        maximum_runtime_seconds=2,
        expires_at=NOW + timedelta(seconds=2),
    )

    with pytest.raises(FactoryDispatchError, match="remaining runtime"):
        await executor.execute_authorized_resume(authorized, invocation, brief)

    assert resume_runner is not None
    assert resume_runner.calls == []
    recoverable = FilesystemFactoryCodexBuildCheckpointStore(
        state_root / "checkpoints"
    ).load(invocation)
    assert recoverable == interrupted
    assert not (
        state_root / "sessions" / f"{invocation.idempotency_key}.resume-1.json"
    ).exists()


@pytest.mark.asyncio
async def test_authorized_resume_deadline_survives_checkpoint_fsync_delay(
    tmp_path: Path,
) -> None:
    job, brief, artifact_reader = _executor_job_and_brief()
    invocation = _seal_invocation(job, brief)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    current = NOW
    authorization_deadline = NOW + timedelta(seconds=2)
    checkpoint_store = FilesystemFactoryCodexBuildCheckpointStore(
        state_root / "checkpoints"
    )

    class DelayedCheckpointStore:
        def load(self, current_invocation):
            return checkpoint_store.load(current_invocation)

        def advance(self, previous, checkpoint):
            nonlocal current
            persisted = checkpoint_store.advance(previous, checkpoint)
            if (
                previous is not None
                and previous.phase == "implementation_interrupted"
                and persisted.phase == "implementation_running"
            ):
                current = authorization_deadline + timedelta(milliseconds=1)
            return persisted

    class RecoveringPreparer:
        def prepare_or_recover(self, _request, _invocation, _brief, _checkpoint):
            return PreparedFactoryWorkspace(root=workspace, base_revision="a" * 40)

    class TimedOutRunner:
        def __init__(self, journal_path: Path) -> None:
            self.journal_path = journal_path

        async def run(self, _authorized) -> CodexRunResult:
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            self.journal_path.write_bytes(b"")
            return CodexRunResult(
                exit_code=124,
                terminal_status="timed_out",
                process_cleanup_status="verified_cancelled",
                journal_path=self.journal_path,
                journal_sha256=hashlib.sha256(b"").hexdigest(),
                artifact_references=(),
                jsonl_lines=(),
            )

    child_calls: list[AuthorizedCodexRun] = []

    class DeadlineAwareRunner:
        def __init__(self, *, deadline_at: datetime, journal_path: Path) -> None:
            self.deadline_at = deadline_at
            self.journal_path = journal_path

        async def run(self, authorized_run: AuthorizedCodexRun) -> CodexRunResult:
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            self.journal_path.write_bytes(b"")
            if current < self.deadline_at:
                child_calls.append(authorized_run)
            return CodexRunResult(
                exit_code=124,
                terminal_status="timed_out",
                process_cleanup_status="not_required",
                journal_path=self.journal_path,
                journal_sha256=hashlib.sha256(b"").hexdigest(),
                artifact_references=(),
                jsonl_lines=(),
            )

    runner_count = 0

    def runner_factory(**kwargs):
        nonlocal runner_count
        runner_count += 1
        deadline_at = kwargs["deadline_at"]
        if runner_count == 1:
            assert deadline_at == min(invocation.lease.expires_at, job.deadline_at)
            return TimedOutRunner(kwargs["journal_path"])
        assert deadline_at == authorization_deadline
        return DeadlineAwareRunner(
            deadline_at=deadline_at,
            journal_path=kwargs["journal_path"],
        )

    executor = CodexCliFactoryBuildExecutor(
        settings=CodexCliFactoryBuildSettings(
            state_root=state_root,
            maximum_runtime_seconds=120,
        ),
        workspace_preparer=RecoveringPreparer(),
        artifact_reader=artifact_reader,
        authorizer=RecordingAuthorizer(),
        runner_factory=runner_factory,
        checkpoint_store=DelayedCheckpointStore(),
        resume_authorizer=CaptainFactoryCodexResumeAuthorizer(
            clock=lambda: current
        ),
        clock=lambda: current,
    )
    dispatch = _dispatch(job, invocation)
    with pytest.raises(FactoryCodexBuildInterrupted):
        await executor.execute(dispatch, invocation, brief)
    interrupted = checkpoint_store.load(invocation)
    assert interrupted is not None
    authorized = _authorized_runtime_retry_dispatch(
        dispatch,
        invocation,
        interrupted,
        maximum_runtime_seconds=2,
        expires_at=authorization_deadline,
    )

    with pytest.raises(FactoryCodexBuildInterrupted) as captured:
        await executor.execute_authorized_resume(authorized, invocation, brief)

    assert captured.value.reason == "codex_timed_out"
    assert captured.value.exit_code == 124
    assert child_calls == []
    recoverable = checkpoint_store.load(invocation)
    assert recoverable is not None
    assert recoverable.phase == "implementation_interrupted"
    assert recoverable.resume_ordinal == 1
    assert recoverable.terminal_receipt_sha256 is not None
    receipt = json.loads(
        (
            state_root
            / "sessions"
            / f"{invocation.idempotency_key}.resume-1.json"
        ).read_bytes()
    )
    assert receipt["status"] == "timed_out"
    assert receipt["exit_code"] == 124
    assert receipt["process_cleanup_status"] == "not_required"


def test_git_workspace_preparer_recovers_exact_head_and_rejects_missing_workspace(
    tmp_path: Path,
) -> None:
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
    prepared = preparer.prepare_or_recover(
        _dispatch(job, invocation), invocation, brief, None
    )
    checkpoint = FactoryCodexBuildCheckpointV1(
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        attempt=invocation.attempt,
        invocation_id=invocation.invocation_id,
        workspace_ref=brief.build_assignment.workspace_ref,
        workspace_root=prepared.root,
        base_revision=prepared.base_revision,
        brief_sha256=hashlib.sha256(
            json.dumps(
                brief.model_dump(mode="json", by_alias=True),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        scaffold_manifest_sha256="f" * 64,
        phase="scaffold_ready",
        resume_ordinal=0,
        updated_at=NOW,
    )

    assert preparer.prepare_or_recover(
        _dispatch(job, invocation), invocation, brief, checkpoint
    ) == prepared
    subprocess.run(
        ("git", "-C", str(prepared.root), "switch", "-c", "attached-recovery"),
        check=True,
        capture_output=True,
    )
    with pytest.raises(FactoryDispatchError, match="detached HEAD"):
        preparer.prepare_or_recover(
            _dispatch(job, invocation), invocation, brief, checkpoint
        )
    subprocess.run(
        ("git", "-C", str(prepared.root), "switch", "--detach", checkpoint.base_revision),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        (
            "git",
            "-C",
            str(prepared.root),
            "-c",
            "user.name=Captain",
            "-c",
            "user.email=captain@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "test: move head",
        ),
        check=True,
        capture_output=True,
    )
    with pytest.raises(FactoryDispatchError, match="HEAD changed"):
        preparer.prepare_or_recover(
            _dispatch(job, invocation), invocation, brief, checkpoint
        )
    prepared.root.rename(prepared.root.with_name(prepared.root.name + "-missing"))
    with pytest.raises(FactoryDispatchError, match="missing"):
        preparer.prepare_or_recover(
            _dispatch(job, invocation), invocation, brief, checkpoint
        )


@pytest.mark.asyncio
async def test_cli_executor_rejects_runner_journal_path_other_than_its_receipt_path(
    tmp_path: Path,
) -> None:
    job, brief, artifact_reader = _executor_job_and_brief()
    invocation = _seal_invocation(job, brief)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"

    class Preparer:
        def prepare(self, *_args):
            return PreparedFactoryWorkspace(root=workspace, base_revision="a" * 40)

    runner = SuccessfulRunner(
        workspace,
        state_root / "journals" / "different-invocation.jsonl",
    )
    executor = CodexCliFactoryBuildExecutor(
        settings=CodexCliFactoryBuildSettings(
            state_root=state_root,
            maximum_runtime_seconds=120,
        ),
        workspace_preparer=Preparer(),
        artifact_reader=artifact_reader,
        authorizer=RecordingAuthorizer(),
        runner_factory=lambda **_kwargs: runner,
        clock=lambda: NOW,
    )

    with pytest.raises(FactoryDispatchError, match="journal path"):
        await executor.execute(_dispatch(job, invocation), invocation, brief)

    assert not (state_root / "sessions" / f"{invocation.idempotency_key}.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exit_code", "terminal_status", "process_cleanup_status"),
    (
        (0, "failed", "not_required"),
        (124, "failed", "verified_cancelled"),
        (0, "cancelled", "verified_cancelled"),
        (1, "cancelled", "verified_cancelled"),
        (124, "cancelled", "verified_cancelled"),
        (130, "failed", "not_required"),
    ),
)
async def test_cli_executor_rejects_terminal_status_exit_code_mismatch_before_receipt(
    tmp_path: Path,
    exit_code: int,
    terminal_status: str,
    process_cleanup_status: str,
) -> None:
    job, brief, artifact_reader = _executor_job_and_brief()
    invocation = _seal_invocation(job, brief)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"

    class Preparer:
        def prepare(self, *_args):
            return PreparedFactoryWorkspace(root=workspace, base_revision="a" * 40)

    class MismatchedRunner(SuccessfulRunner):
        async def run(self, authorized: AuthorizedCodexRun) -> CodexRunResult:
            result = await super().run(authorized)
            return result.model_copy(
                update={
                    "exit_code": exit_code,
                    "terminal_status": terminal_status,
                    "process_cleanup_status": process_cleanup_status,
                }
            )

    runners: list[MismatchedRunner] = []

    def runner_factory(**kwargs) -> MismatchedRunner:
        runner = MismatchedRunner(workspace, kwargs["journal_path"])
        runners.append(runner)
        return runner

    executor = CodexCliFactoryBuildExecutor(
        settings=CodexCliFactoryBuildSettings(
            state_root=state_root,
            maximum_runtime_seconds=120,
        ),
        workspace_preparer=Preparer(),
        artifact_reader=artifact_reader,
        authorizer=RecordingAuthorizer(),
        runner_factory=runner_factory,
        clock=lambda: NOW,
    )

    with pytest.raises(FactoryDispatchError, match="terminal status"):
        await executor.execute(_dispatch(job, invocation), invocation, brief)

    assert len(runners) == 1
    assert not (state_root / "sessions" / f"{invocation.idempotency_key}.json").exists()


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
    retried = preparer.prepare(_dispatch(job, invocation), invocation, brief)
    assert retried == prepared
