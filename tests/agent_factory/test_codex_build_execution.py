from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agenten.agent_factory import codex_build_execution
from agenten.agent_factory.codex_build_execution import (
    CaptainCodexBuildSealer,
    CaptainFactoryCodexResumeAuthorizer,
    CodexCliFactoryBuildExecutor,
    CodexCliFactoryBuildSettings,
    CompletedCodexBuild,
    FactoryCodexBuildFailed,
    FactoryCodexBuildInterrupted,
    FactoryCodexCleanupUnresolved,
    FactoryCodexEvidenceFailure,
    FactoryCodexProcessState,
    GitDetachedFactoryWorkspacePreparer,
    PreparedFactoryWorkspace,
    _evidence_failure_receipt,
    _session_receipt,
)
from agenten.agent_factory.codex_build_recovery import (
    FactoryCodexBuildCheckpointV1,
    FactoryCodexOutputArtifactV1,
    FactoryCodexOutputManifestV1,
    FilesystemFactoryCodexBuildCheckpointStore,
    FilesystemFactoryCodexOutputManifestStore,
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
from agenten.execution import codex_supervisor
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

    def validate_replay_authority(self, _request, _invocation):
        return None

    def validate_completed_outputs(self, _invocation, _completed):
        return None

    def validate_issued_receipt(self, _invocation, _completed, _receipt):
        return None

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
    def __init__(
        self,
        workspace: Path,
        journal_path: Path,
        *,
        thread_id: str = "codex-thread-123",
    ) -> None:
        self.workspace = workspace
        self.journal_path = journal_path
        self.thread_id = thread_id
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
                    "thread_id": self.thread_id,
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
    build_instructions = (
        b"Follow the bounded Captain repair diagnostics and real-case contract."
    )
    content_by_name = {
        "input": b'{"input":"claims team"}',
        "compiled": b'{"spec":"compiled"}',
        "graph": b'{"nodes":["build"]}',
        "instructions": build_instructions,
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
    brief = brief.model_copy(
        update={
            "artifact_ref": references["instructions"],
            "prompt_ref": references["instructions"],
        }
    )
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
    snapshot = tmp_path / "private-output-snapshot"
    shutil.copytree(workspace, snapshot)
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
            output_snapshot_root=snapshot,
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
async def test_captain_sealer_rejects_mutable_workspace_without_snapshot(
    tmp_path: Path,
) -> None:
    cas = CodexBuildArtifactCas(tmp_path / "cas")
    workspace = _workspace(tmp_path, cas)
    job, brief, _artifact_reader = _executor_job_and_brief()
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

    with pytest.raises(
        FactoryDispatchError,
        match="requires an immutable output snapshot",
    ):
        await sealer.seal(_dispatch(job, invocation), invocation, brief)

    assert executor.sealed == []


@pytest.mark.asyncio
async def test_captain_sealer_records_successor_authority_after_original_lease_expiry(
    tmp_path: Path,
) -> None:
    cas = CodexBuildArtifactCas(tmp_path / "cas")
    workspace = _workspace(tmp_path, cas)
    snapshot = tmp_path / "private-output-snapshot"
    shutil.copytree(workspace, snapshot)
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
            output_snapshot_root=snapshot,
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

    sealed_checkpoint = FilesystemFactoryCodexBuildCheckpointStore(
        state_root / "checkpoints"
    ).load(invocation)
    assert sealed_checkpoint is not None
    arbitrary_retry = _authorized_runtime_retry_dispatch(
        dispatch,
        invocation,
        sealed_checkpoint,
    )
    with pytest.raises(FactoryDispatchError, match="checkpoint.*retry authority"):
        await sealer.seal(arbitrary_retry, invocation, brief)

    (state_root / "sealed-evidence" / f"{invocation.invocation_id.hex}.json").unlink()
    with pytest.raises(
        FactoryDispatchError,
        match="original sealed evidence is missing",
    ):
        await sealer.seal(dispatch, invocation, brief)
    assert issuer.issue_calls == 1
    assert issuer.persist_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("authority_variant", ("absent", "different"))
async def test_replay_rejects_changed_retry_authority_before_advancing_checkpoint(
    tmp_path: Path,
    authority_variant: str,
) -> None:
    cas = CodexBuildArtifactCas(tmp_path / "cas")
    workspace = _workspace(tmp_path, cas)
    job, brief, artifact_reader = _executor_job_and_brief()
    invocation = _seal_invocation(job, brief)
    dispatch = _dispatch(job, invocation)
    state_root = tmp_path / "state"
    checkpoint_root = state_root / "checkpoints"
    checkpoint_store = FilesystemFactoryCodexBuildCheckpointStore(checkpoint_root)
    session_receipt = json.dumps(
        {
            "completed_at": NOW.isoformat(),
            "session_id": "persisted-before-checkpoint-advance",
        },
        sort_keys=True,
    ).encode("utf-8")
    receipt_sha256 = hashlib.sha256(session_receipt).hexdigest()
    initial = FactoryCodexBuildCheckpointV1(
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        attempt=invocation.attempt,
        invocation_id=invocation.invocation_id,
        workspace_ref=invocation.lease.workspace_ref,
        workspace_root=workspace,
        base_revision="a" * 40,
        brief_sha256=hashlib.sha256(canonical_factory_codex_model(brief)).hexdigest(),
        scaffold_manifest_sha256="b" * 64,
        phase="scaffold_ready",
        resume_ordinal=0,
        updated_at=NOW,
    )
    checkpoint_store.advance(None, initial)
    running = initial.model_copy(
        update={
            "phase": "implementation_running",
            "updated_at": NOW + timedelta(seconds=1),
        }
    )
    checkpoint_store.advance(initial, running)
    interrupted = running.model_copy(
        update={
            "phase": "implementation_interrupted",
            "terminal_receipt_sha256": receipt_sha256,
            "updated_at": NOW + timedelta(seconds=2),
        }
    )
    checkpoint_store.advance(running, interrupted)
    authorized_dispatch = _authorized_runtime_retry_dispatch(
        dispatch,
        invocation,
        interrupted,
    )
    authorization = authorized_dispatch.runtime_retry_authorization
    assert authorization is not None
    resumed = interrupted.model_copy(
        update={
            "phase": "implementation_running",
            "resume_ordinal": 1,
            "terminal_receipt_sha256": None,
            "runtime_retry_authorization_uri": authorization.authorization_ref.uri,
            "runtime_retry_authorization_sha256": (
                authorization.authorization_ref.sha256
            ),
                "runtime_retry_authorization_binding_sha256": hashlib.sha256(
                    canonical_factory_codex_model(authorization)
                ).hexdigest(),
                "runtime_retry_authorization_issued_at": authorization.issued_at,
                "runtime_retry_authorization_expires_at": authorization.expires_at,
                "parent_terminal_receipt_sha256": receipt_sha256,
                "parent_journal_sha256": "e" * 64,
                "updated_at": NOW + timedelta(seconds=3),
            }
        )
    checkpoint_store.advance(interrupted, resumed)
    output_manifest = FactoryCodexOutputManifestV1(
        schema="captain.factory-codex-output-manifest.v1",
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        attempt=invocation.attempt,
        invocation_id=invocation.invocation_id,
        workspace_ref=invocation.lease.workspace_ref,
        invocation_sha256=hashlib.sha256(
            canonical_factory_codex_model(invocation)
        ).hexdigest(),
        resume_ordinal=1,
        terminal_receipt_sha256=receipt_sha256,
        artifacts=tuple(
            FactoryCodexOutputArtifactV1(
                relative_path=relative,
                sha256=hashlib.sha256((workspace / relative).read_bytes()).hexdigest(),
                size=(workspace / relative).stat().st_size,
            )
            for relative in (
                "candidate.zip",
                "factory-candidate.json",
                "test-evidence.json",
            )
        ),
    )
    manual_snapshot = state_root / "manual-output-snapshot"
    manual_snapshot.mkdir()
    for relative in (
        "candidate.zip",
        "factory-candidate.json",
        "test-evidence.json",
    ):
        shutil.copyfile(workspace / relative, manual_snapshot / relative)
    output_manifest_uri, output_manifest_sha256 = (
        FilesystemFactoryCodexOutputManifestStore(
            state_root / "output-manifests"
        ).persist(
            output_manifest,
            snapshot_staging=manual_snapshot,
        )
    )
    implementation_complete = resumed.model_copy(
        update={
            "phase": "implementation_complete",
            "terminal_receipt_sha256": receipt_sha256,
            "output_manifest_uri": output_manifest_uri,
            "output_manifest_sha256": output_manifest_sha256,
            "updated_at": NOW + timedelta(seconds=4),
        }
    )
    checkpoint_store.advance(resumed, implementation_complete)

    class CrashBeforeSealedCheckpointStore(FilesystemFactoryCodexBuildCheckpointStore):
        def advance(
            self,
            previous: FactoryCodexBuildCheckpointV1 | None,
            next_checkpoint: FactoryCodexBuildCheckpointV1,
        ) -> FactoryCodexBuildCheckpointV1:
            if (
                previous is not None
                and previous.phase == "implementation_complete"
                and next_checkpoint.phase == "sealed"
            ):
                raise RuntimeError("simulated crash before checkpoint advance")
            return super().advance(previous, next_checkpoint)

    class Preparer:
        def prepare_or_recover(self, *_args):
            return PreparedFactoryWorkspace(root=workspace, base_revision="a" * 40)

    def build_executor(
        store: FilesystemFactoryCodexBuildCheckpointStore,
    ) -> CodexCliFactoryBuildExecutor:
        return CodexCliFactoryBuildExecutor(
            settings=CodexCliFactoryBuildSettings(
                state_root=state_root,
                maximum_runtime_seconds=120,
            ),
            workspace_preparer=Preparer(),
            artifact_reader=artifact_reader,
            authorizer=RecordingAuthorizer(),
            runner_factory=lambda **_kwargs: pytest.fail(
                "sealed replay must not launch Codex"
            ),
            checkpoint_store=store,
            clock=lambda: NOW + timedelta(seconds=5),
        )

    completed = CompletedCodexBuild(
        workspace_root=workspace,
        codex_session_receipt=session_receipt,
        candidate_manifest_path="factory-candidate.json",
        source_archive_path="candidate.zip",
        test_evidence_paths=("test-evidence.json",),
        completed_at=NOW,
        output_snapshot_root=(
            state_root
            / "output-manifests"
            / "snapshots"
            / output_manifest_sha256
        ),
    )

    class MismatchedReceiptIssuer(CaptainCodexBuildReceiptIssuer):
        def __init__(self, artifact_cas: CodexBuildArtifactCas) -> None:
            super().__init__(artifact_cas)
            self.persist_calls = 0

        def issue(self, **kwargs):
            receipt = super().issue(**kwargs)
            return receipt.model_copy(
                update={
                    "source_archive_ref": receipt.source_archive_ref.model_copy(
                        update={
                            "uri": (
                                "artifact://attacker/codex-source/"
                                f"{receipt.source_archive_ref.sha256}"
                            ),
                        }
                    )
                }
            )

        def persist_receipt(self, receipt):
            self.persist_calls += 1
            return super().persist_receipt(receipt)

    mismatched_issuer = MismatchedReceiptIssuer(cas)
    mismatch_sealer = CaptainCodexBuildSealer(
        executor=build_executor(checkpoint_store),
        issuer=mismatched_issuer,
    )
    checkpoint_before_mismatch = checkpoint_store.load(invocation)
    with pytest.raises(
        FactoryDispatchError,
        match="issued receipt output binding changed",
    ):
        mismatch_sealer._seal_completed(
            authorized_dispatch,
            invocation,
            brief,
            completed,
        )
    assert checkpoint_store.load(invocation) == checkpoint_before_mismatch
    assert not (
        state_root / "sealed-evidence" / f"{invocation.invocation_id.hex}.json"
    ).exists()
    assert mismatched_issuer.persist_calls == 0
    assert not (cas.root / "build-receipt").exists()

    crashing_sealer = CaptainCodexBuildSealer(
        executor=build_executor(CrashBeforeSealedCheckpointStore(checkpoint_root)),
        issuer=CaptainCodexBuildReceiptIssuer(cas),
    )
    with pytest.raises(RuntimeError, match="simulated crash"):
        crashing_sealer._seal_completed(
            authorized_dispatch,
            invocation,
            brief,
            completed,
        )

    checkpoint_path = checkpoint_root / f"{invocation.invocation_id.hex}.json"
    checkpoint_bytes = checkpoint_path.read_bytes()
    checkpoint_before_replay = checkpoint_store.load(invocation)
    assert checkpoint_before_replay == implementation_complete
    assert (
        state_root / "sealed-evidence" / f"{invocation.invocation_id.hex}.json"
    ).is_file()

    if authority_variant == "absent":
        replay_dispatch = replace(
            authorized_dispatch,
            runtime_retry_authorization=None,
        )
    else:
        replay_dispatch = replace(
            authorized_dispatch,
            runtime_retry_authorization=authorization.model_copy(
                update={
                    "authorization_ref": ArtifactRef(
                        uri=f"artifact://factory/runtime-retry/{'8' * 64}",
                        sha256="8" * 64,
                        media_type="application/json",
                    )
                }
            ),
        )
    replay_sealer = CaptainCodexBuildSealer(
        executor=build_executor(checkpoint_store),
        issuer=CaptainCodexBuildReceiptIssuer(cas),
    )

    with pytest.raises(FactoryDispatchError, match="checkpoint.*retry authority"):
        await replay_sealer.reconcile_pending(replay_dispatch, invocation, brief)

    assert checkpoint_path.read_bytes() == checkpoint_bytes
    assert checkpoint_store.load(invocation) == checkpoint_before_replay


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
    assert "combined output well below 256 KiB" in prompt
    assert "never use broad recursive `rg -A` or `rg -B`" in prompt
    assert "pytest.live.demo is deferred to Captain" in prompt
    assert "on Windows do not use Compress-Archive" in prompt
    assert "MUST omit source_archive_ref" in prompt
    assert "Captain adds source_archive_ref only after sealing candidate.zip" in prompt
    assert "FactoryAutoGenTeamManifestV1" in prompt
    assert "system_prompt_ref" in prompt
    assert "host_tools only in factory-candidate.json" in prompt
    assert "codex-build-instructions.md" in prompt
    assert (workspace / ".captain-inputs" / "job-input.md").is_file()
    assert (workspace / ".captain-inputs" / "compiled-spec.json").is_file()
    assert (workspace / ".captain-inputs" / "dependency-graph.json").is_file()
    assert (
        workspace / ".captain-inputs" / "codex-build-instructions.md"
    ).read_bytes() == (
        b"Follow the bounded Captain repair diagnostics and real-case contract."
    )
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
async def test_improvement_scaffold_materializes_digest_bound_prior_candidate(
    tmp_path: Path,
) -> None:
    job, brief, artifact_reader = _executor_job_and_brief()
    prior_candidate = b"PK\x03\x04bounded prior candidate bytes"
    prior_sha256 = hashlib.sha256(prior_candidate).hexdigest()
    prior_ref = ArtifactRef(
        uri=f"artifact://minibook-creation/forge-source/{prior_sha256}",
        sha256=prior_sha256,
        media_type="application/zip",
    )
    instructions = json.dumps(
        {"prior candidate ref": prior_ref.uri},
        sort_keys=True,
    ).encode("utf-8")
    instructions_sha256 = hashlib.sha256(instructions).hexdigest()
    instructions_ref = ArtifactRef(
        uri=f"artifact://test/{instructions_sha256}",
        sha256=instructions_sha256,
        media_type="application/json",
    )
    brief = brief.model_copy(
        update={
            "artifact_ref": instructions_ref,
            "prompt_ref": instructions_ref,
            "context_refs": (*brief.context_refs, prior_ref),
        }
    )
    invocation = _seal_invocation(job, brief)
    contents = dict(artifact_reader._contents)
    contents[instructions_sha256] = instructions
    contents[prior_sha256] = prior_candidate
    artifact_reader = StaticArtifactReader(contents)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    class Preparer:
        def prepare(self, *_args):
            return PreparedFactoryWorkspace(root=workspace, base_revision="a" * 40)

    authorizer = RecordingAuthorizer()
    executor = CodexCliFactoryBuildExecutor(
        settings=CodexCliFactoryBuildSettings(
            state_root=tmp_path / "state",
            maximum_runtime_seconds=120,
        ),
        workspace_preparer=Preparer(),
        artifact_reader=artifact_reader,
        authorizer=authorizer,
        runner_factory=lambda **kwargs: SuccessfulRunner(
            workspace,
            kwargs["journal_path"],
        ),
        clock=lambda: NOW,
    )

    await executor.execute(_dispatch(job, invocation), invocation, brief)

    assert (
        workspace / ".captain-inputs" / "prior-candidate.zip"
    ).read_bytes() == prior_candidate
    assert "Start from .captain-inputs/prior-candidate.zip" in (
        authorizer.requests[0].command[3]
    )


@pytest.mark.asyncio
async def test_initial_scaffold_ignores_explicit_null_prior_candidate(
    tmp_path: Path,
) -> None:
    job, brief, artifact_reader = _executor_job_and_brief()
    instructions = json.dumps(
        {"prior candidate ref": None},
        sort_keys=True,
    ).encode("utf-8")
    instructions_sha256 = hashlib.sha256(instructions).hexdigest()
    instructions_ref = ArtifactRef(
        uri=f"artifact://test/{instructions_sha256}",
        sha256=instructions_sha256,
        media_type="application/json",
    )
    brief = brief.model_copy(
        update={
            "artifact_ref": instructions_ref,
            "prompt_ref": instructions_ref,
        }
    )
    invocation = _seal_invocation(job, brief)
    contents = dict(artifact_reader._contents)
    contents[instructions_sha256] = instructions
    artifact_reader = StaticArtifactReader(contents)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    class Preparer:
        def prepare(self, *_args):
            return PreparedFactoryWorkspace(root=workspace, base_revision="a" * 40)

    authorizer = RecordingAuthorizer()
    executor = CodexCliFactoryBuildExecutor(
        settings=CodexCliFactoryBuildSettings(
            state_root=tmp_path / "state",
            maximum_runtime_seconds=120,
        ),
        workspace_preparer=Preparer(),
        artifact_reader=artifact_reader,
        authorizer=authorizer,
        runner_factory=lambda **kwargs: SuccessfulRunner(
            workspace,
            kwargs["journal_path"],
        ),
        clock=lambda: NOW,
    )

    await executor.execute(_dispatch(job, invocation), invocation, brief)

    assert not (workspace / ".captain-inputs" / "prior-candidate.zip").exists()
    assert "prior-candidate.zip" not in authorizer.requests[0].command[3]


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

        def prepare_or_recover(self, *_args):
            return PreparedFactoryWorkspace(root=workspace, base_revision="a" * 40)

    class IncompleteRunner:
        def __init__(self, journal_path: Path) -> None:
            self.journal_path = journal_path

        async def run(self, _authorized):
            line = json.dumps(
                {
                    "type": "thread.started",
                    "thread_id": "incomplete-output-thread",
                }
            )
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
    assert checkpoint.phase == "implementation_failed"
    assert checkpoint.terminal_receipt_sha256 is not None
    assert (
        tmp_path
        / "state"
        / "sessions"
        / f"{invocation.idempotency_key}.json"
    ).is_file()
    assert not (tmp_path / "state" / "output-manifests").exists()
    (workspace / "candidate.zip").write_bytes(b"late archive")
    (workspace / "factory-candidate.json").write_text("{}", encoding="utf-8")
    (workspace / "test-evidence.json").write_text("{}", encoding="utf-8")
    with pytest.raises(FactoryDispatchError, match="not reconcilable|failed"):
        await executor.reconcile_pending(
            _dispatch(job, invocation),
            invocation,
            brief,
        )


@pytest.mark.asyncio
async def test_authorized_resume_repairs_missing_outputs_in_fresh_policy_thread(
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

        def prepare_or_recover(self, *_args):
            return PreparedFactoryWorkspace(root=workspace, base_revision="a" * 40)

    class IncompleteRunner:
        def __init__(self, journal_path: Path) -> None:
            self.journal_path = journal_path

        async def run(self, _authorized):
            line = json.dumps(
                {
                    "type": "thread.started",
                    "thread_id": "codex-thread-123",
                }
            )
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

    runner_calls = 0

    def runner_factory(**kwargs):
        nonlocal runner_calls
        runner_calls += 1
        if runner_calls == 1:
            return IncompleteRunner(kwargs["journal_path"])
        return SuccessfulRunner(
            workspace,
            kwargs["journal_path"],
            thread_id="codex-thread-fresh",
        )

    authorizer = RecordingAuthorizer()
    executor = CodexCliFactoryBuildExecutor(
        settings=CodexCliFactoryBuildSettings(
            state_root=state_root,
            maximum_runtime_seconds=120,
        ),
        workspace_preparer=Preparer(),
        artifact_reader=artifact_reader,
        authorizer=authorizer,
        runner_factory=runner_factory,
        resume_authorizer=CaptainFactoryCodexResumeAuthorizer(clock=lambda: NOW),
        clock=lambda: NOW,
    )
    dispatch = _dispatch(job, invocation)

    with pytest.raises(FactoryDispatchError, match="required build artifact"):
        await executor.execute(dispatch, invocation, brief)
    failed = FilesystemFactoryCodexBuildCheckpointStore(
        state_root / "checkpoints"
    ).load(invocation)
    assert failed is not None
    assert failed.implementation_failure_reason == "required_output_invalid"
    authorized = _authorized_runtime_retry_dispatch(dispatch, invocation, failed)

    completed = await executor.execute_authorized_resume(
        authorized,
        invocation,
        brief,
    )

    assert completed.source_archive_path == "candidate.zip"
    assert runner_calls == 2
    assert authorizer.requests[1].command[:3] == (
        "codex",
        "exec",
        "--json",
    )
    assert "CAPTAIN RESUME REPAIR" in authorizer.requests[1].command[3]
    assert "Do not repeat broad repository inspection" in (
        authorizer.requests[1].command[3]
    )
    assert "Do not finish with a blocked report" in authorizer.requests[1].command[3]
    receipt = json.loads(completed.codex_session_receipt)
    assert receipt["codex_thread_id"] == "codex-thread-fresh"
    assert receipt["parent_codex_thread_id"] == "codex-thread-123"
    checkpoint = FilesystemFactoryCodexBuildCheckpointStore(
        state_root / "checkpoints"
    ).load(invocation)
    assert checkpoint is not None
    assert checkpoint.phase == "implementation_complete"
    assert checkpoint.resume_ordinal == 1


@pytest.mark.asyncio
async def test_second_resume_accepts_digest_bound_receipt_after_prompt_upgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, brief, artifact_reader = _executor_job_and_brief()
    invocation = _seal_invocation(job, brief)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"

    class Preparer:
        def prepare(self, *_args):
            return PreparedFactoryWorkspace(root=workspace, base_revision="a" * 40)

        prepare_or_recover = prepare

    class IncompleteRunner:
        def __init__(self, journal_path: Path) -> None:
            self.journal_path = journal_path

        async def run(self, _authorized):
            line = json.dumps(
                {"type": "thread.started", "thread_id": "codex-thread-123"}
            )
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

    runner_calls = 0

    def runner_factory(**kwargs):
        nonlocal runner_calls
        runner_calls += 1
        if runner_calls < 3:
            return IncompleteRunner(kwargs["journal_path"])
        return SuccessfulRunner(workspace, kwargs["journal_path"])

    executor = CodexCliFactoryBuildExecutor(
        settings=CodexCliFactoryBuildSettings(
            state_root=state_root,
            maximum_runtime_seconds=120,
        ),
        workspace_preparer=Preparer(),
        artifact_reader=artifact_reader,
        authorizer=RecordingAuthorizer(),
        runner_factory=runner_factory,
        resume_authorizer=CaptainFactoryCodexResumeAuthorizer(clock=lambda: NOW),
        clock=lambda: NOW,
    )
    dispatch = _dispatch(job, invocation)

    with pytest.raises(FactoryDispatchError, match="required build artifact"):
        await executor.execute(dispatch, invocation, brief)
    first_failed = FilesystemFactoryCodexBuildCheckpointStore(
        state_root / "checkpoints"
    ).load(invocation)
    assert first_failed is not None
    with pytest.raises(FactoryDispatchError, match="required build artifact"):
        await executor.execute_authorized_resume(
            _authorized_runtime_retry_dispatch(dispatch, invocation, first_failed),
            invocation,
            brief,
        )
    second_failed = FilesystemFactoryCodexBuildCheckpointStore(
        state_root / "checkpoints"
    ).load(invocation)
    assert second_failed is not None
    assert second_failed.resume_ordinal == 1

    original_prompt = codex_build_execution._codex_prompt
    monkeypatch.setattr(
        codex_build_execution,
        "_codex_prompt",
        lambda *args, **kwargs: original_prompt(*args, **kwargs)
        + "\nCaptain prompt release upgraded.",
    )

    completed = await executor.execute_authorized_resume(
        _authorized_runtime_retry_dispatch(dispatch, invocation, second_failed),
        invocation,
        brief,
    )

    assert completed.source_archive_path == "candidate.zip"
    assert runner_calls == 3
    checkpoint = FilesystemFactoryCodexBuildCheckpointStore(
        state_root / "checkpoints"
    ).load(invocation)
    assert checkpoint is not None
    assert checkpoint.phase == "implementation_complete"
    assert checkpoint.resume_ordinal == 2


@pytest.mark.asyncio
async def test_implementation_complete_seals_snapshot_after_archive_replacement(
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

        def prepare_or_recover(self, *_args):
            return PreparedFactoryWorkspace(root=workspace, base_revision="a" * 40)

    executor = CodexCliFactoryBuildExecutor(
        settings=CodexCliFactoryBuildSettings(
            state_root=state_root,
            maximum_runtime_seconds=120,
        ),
        workspace_preparer=Preparer(),
        artifact_reader=artifact_reader,
        authorizer=RecordingAuthorizer(),
        runner_factory=lambda **kwargs: SuccessfulRunner(
            workspace,
            kwargs["journal_path"],
        ),
        clock=lambda: NOW,
    )
    dispatch = _dispatch(job, invocation)

    completed = await executor.execute(dispatch, invocation, brief)

    checkpoint_store = FilesystemFactoryCodexBuildCheckpointStore(
        state_root / "checkpoints"
    )
    checkpoint = checkpoint_store.load(invocation)
    assert checkpoint is not None
    assert checkpoint.phase == "implementation_complete"
    assert checkpoint.output_manifest_sha256 is not None
    assert checkpoint.output_manifest_uri == (
        "artifact://factory/codex-output-manifest/"
        f"{checkpoint.output_manifest_sha256}"
    )
    manifest_path = (
        state_root
        / "output-manifests"
        / f"{checkpoint.output_manifest_sha256}.json"
    )
    manifest = json.loads(manifest_path.read_bytes())
    assert manifest["schema"] == "captain.factory-codex-output-manifest.v1"
    assert manifest["invocation_sha256"] == hashlib.sha256(
        canonical_factory_codex_model(invocation)
    ).hexdigest()
    assert [item["relative_path"] for item in manifest["artifacts"]] == [
        "candidate.zip",
        "factory-candidate.json",
        "test-evidence.json",
    ]
    assert all(item["size"] > 0 for item in manifest["artifacts"])
    assert str(workspace) not in json.dumps(manifest)
    changed_invocation = invocation.model_copy(
        update={"acceptance_assertion_ids": ("different_assertion",)}
    )
    with pytest.raises(
        FactoryDispatchError,
        match="output manifest binding changed",
    ):
        FilesystemFactoryCodexOutputManifestStore(
            state_root / "output-manifests"
        ).load(
            changed_invocation,
            uri=checkpoint.output_manifest_uri,
            sha256=checkpoint.output_manifest_sha256,
        )
    (workspace / "candidate.zip").write_bytes(b"replacement archive")
    cas = CodexBuildArtifactCas(tmp_path / "cas")
    sealer = CaptainCodexBuildSealer(
        executor=executor,
        issuer=CaptainCodexBuildReceiptIssuer(cas),
    )

    evidence = sealer._seal_completed(
        dispatch,
        invocation,
        brief,
        completed,
    )

    by_path = {
        item["relative_path"]: item["sha256"] for item in manifest["artifacts"]
    }
    assert evidence.build_receipt.source_archive_ref.sha256 == by_path["candidate.zip"]
    assert checkpoint_store.load(invocation).phase == "sealed"


@pytest.mark.asyncio
async def test_issuer_reads_private_snapshot_when_workspace_swaps_after_validation(
    tmp_path: Path,
) -> None:
    cas = CodexBuildArtifactCas(tmp_path / "cas")
    job, brief, artifact_reader = _executor_job_and_brief()
    invocation = _seal_invocation(job, brief)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"

    class Preparer:
        def prepare(self, *_args):
            return PreparedFactoryWorkspace(root=workspace, base_revision="a" * 40)

        def prepare_or_recover(self, *_args):
            return PreparedFactoryWorkspace(root=workspace, base_revision="a" * 40)

    executor = CodexCliFactoryBuildExecutor(
        settings=CodexCliFactoryBuildSettings(
            state_root=state_root,
            maximum_runtime_seconds=120,
        ),
        workspace_preparer=Preparer(),
        artifact_reader=artifact_reader,
        authorizer=RecordingAuthorizer(),
        runner_factory=lambda **kwargs: SuccessfulRunner(
            workspace,
            kwargs["journal_path"],
        ),
        clock=lambda: NOW,
    )
    dispatch = _dispatch(job, invocation)
    completed = await executor.execute(dispatch, invocation, brief)
    original = {
        relative: (workspace / relative).read_bytes()
        for relative in (
            "candidate.zip",
            "factory-candidate.json",
            "test-evidence.json",
        )
    }
    alternate_root = tmp_path / "alternate"
    alternate_root.mkdir()
    alternate_runner = SuccessfulRunner(
        alternate_root,
        tmp_path / "alternate-journal.jsonl",
    )
    await alternate_runner.run(
        AuthorizedCodexRun(
            workspace=alternate_root,
            command=("codex", "exec", "--json", "alternate"),
            environment=FrozenEnvironment({}),
        )
    )
    from io import BytesIO
    from zipfile import ZIP_STORED, ZipFile, ZipInfo

    alternate_zip = BytesIO()
    with ZipFile(alternate_zip, "w", compression=ZIP_STORED) as archive:
        for name, content in (
            (
                "factory-candidate.json",
                (alternate_root / "factory-candidate.json").read_bytes(),
            ),
            ("src/team.py", b"TEAM = 'alternate'\n"),
        ):
            info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    (alternate_root / "candidate.zip").write_bytes(alternate_zip.getvalue())
    alternate = {
        relative: (alternate_root / relative).read_bytes()
        for relative in original
    }

    class SwappingIssuer(CaptainCodexBuildReceiptIssuer):
        def __init__(self, artifact_cas: CodexBuildArtifactCas) -> None:
            super().__init__(artifact_cas)
            self.received_root: Path | None = None

        def issue(self, **kwargs):
            self.received_root = kwargs["workspace_root"]
            for relative, content in alternate.items():
                (workspace / relative).write_bytes(content)
            try:
                return super().issue(**kwargs)
            finally:
                for relative, content in original.items():
                    (workspace / relative).write_bytes(content)

    issuer = SwappingIssuer(cas)
    sealer = CaptainCodexBuildSealer(executor=executor, issuer=issuer)

    evidence = sealer._seal_completed(
        dispatch,
        invocation,
        brief,
        completed,
    )

    assert issuer.received_root is not None
    assert issuer.received_root != workspace
    assert evidence.build_receipt.candidate_manifest_ref.sha256 == hashlib.sha256(
        original["factory-candidate.json"]
    ).hexdigest()
    assert evidence.build_receipt.source_archive_ref.sha256 == hashlib.sha256(
        original["candidate.zip"]
    ).hexdigest()
    assert tuple(
        item.sha256 for item in evidence.build_receipt.test_evidence_refs
    ) == (hashlib.sha256(original["test-evidence.json"]).hexdigest(),)
    assert (
        FilesystemFactoryCodexBuildCheckpointStore(
            state_root / "checkpoints"
        ).load(invocation).phase
        == "sealed"
    )


@pytest.mark.asyncio
async def test_output_manifest_ignores_unrequired_workspace_files(
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

        def prepare_or_recover(self, *_args):
            return PreparedFactoryWorkspace(root=workspace, base_revision="a" * 40)

    executor = CodexCliFactoryBuildExecutor(
        settings=CodexCliFactoryBuildSettings(
            state_root=state_root,
            maximum_runtime_seconds=120,
        ),
        workspace_preparer=Preparer(),
        artifact_reader=artifact_reader,
        authorizer=RecordingAuthorizer(),
        runner_factory=lambda **kwargs: SuccessfulRunner(
            workspace,
            kwargs["journal_path"],
        ),
        clock=lambda: NOW,
    )
    dispatch = _dispatch(job, invocation)
    completed = await executor.execute(dispatch, invocation, brief)
    (workspace / "unrelated-debug.txt").write_text("irrelevant", encoding="utf-8")

    replay = await executor.reconcile_pending(dispatch, invocation, brief)

    assert replay == completed


@pytest.mark.asyncio
async def test_mutation_after_sealed_evidence_crash_cannot_advance_checkpoint(
    tmp_path: Path,
) -> None:
    cas = CodexBuildArtifactCas(tmp_path / "cas")
    job, brief, artifact_reader = _executor_job_and_brief()
    invocation = _seal_invocation(job, brief)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    checkpoint_root = state_root / "checkpoints"

    class Preparer:
        def prepare(self, *_args):
            return PreparedFactoryWorkspace(root=workspace, base_revision="a" * 40)

        def prepare_or_recover(self, *_args):
            return PreparedFactoryWorkspace(root=workspace, base_revision="a" * 40)

    class CrashBeforeSealedCheckpointStore(FilesystemFactoryCodexBuildCheckpointStore):
        def advance(
            self,
            previous: FactoryCodexBuildCheckpointV1 | None,
            next_checkpoint: FactoryCodexBuildCheckpointV1,
        ) -> FactoryCodexBuildCheckpointV1:
            if (
                previous is not None
                and previous.phase == "implementation_complete"
                and next_checkpoint.phase == "sealed"
            ):
                raise RuntimeError("simulated crash before checkpoint advance")
            return super().advance(previous, next_checkpoint)

    def build_executor(
        checkpoint_store: FilesystemFactoryCodexBuildCheckpointStore,
    ) -> CodexCliFactoryBuildExecutor:
        return CodexCliFactoryBuildExecutor(
            settings=CodexCliFactoryBuildSettings(
                state_root=state_root,
                maximum_runtime_seconds=120,
            ),
            workspace_preparer=Preparer(),
            artifact_reader=artifact_reader,
            authorizer=RecordingAuthorizer(),
            runner_factory=lambda **kwargs: SuccessfulRunner(
                workspace,
                kwargs["journal_path"],
            ),
            checkpoint_store=checkpoint_store,
            clock=lambda: NOW,
        )

    crashing_executor = build_executor(
        CrashBeforeSealedCheckpointStore(checkpoint_root)
    )
    dispatch = _dispatch(job, invocation)
    crashing_sealer = CaptainCodexBuildSealer(
        executor=crashing_executor,
        issuer=CaptainCodexBuildReceiptIssuer(cas),
    )

    with pytest.raises(RuntimeError, match="simulated crash"):
        await crashing_sealer.seal(dispatch, invocation, brief)

    checkpoint_store = FilesystemFactoryCodexBuildCheckpointStore(checkpoint_root)
    implementation_complete = checkpoint_store.load(invocation)
    assert implementation_complete is not None
    assert implementation_complete.phase == "implementation_complete"
    checkpoint_path = checkpoint_root / f"{invocation.invocation_id.hex}.json"
    checkpoint_bytes = checkpoint_path.read_bytes()
    assert (
        state_root / "sealed-evidence" / f"{invocation.invocation_id.hex}.json"
    ).is_file()
    (workspace / "test-evidence.json").write_text(
        json.dumps({"status": "passed", "command_ids": ["replacement"]}),
        encoding="utf-8",
    )
    replay_sealer = CaptainCodexBuildSealer(
        executor=build_executor(checkpoint_store),
        issuer=CaptainCodexBuildReceiptIssuer(cas),
    )

    replay = await replay_sealer.seal(dispatch, invocation, brief)

    assert replay.status == "sealed"
    assert checkpoint_path.read_bytes() != checkpoint_bytes
    assert checkpoint_store.load(invocation).phase == "sealed"


class FixedSizeOutputsRunner:
    def __init__(
        self,
        *,
        workspace: Path,
        journal_path: Path,
        candidate_archive: bytes,
        candidate_manifest: bytes,
        test_evidence: bytes,
    ) -> None:
        self._workspace = workspace
        self._journal_path = journal_path
        self._outputs = {
            "candidate.zip": candidate_archive,
            "factory-candidate.json": candidate_manifest,
            "test-evidence.json": test_evidence,
        }

    async def run(self, _authorized: AuthorizedCodexRun) -> CodexRunResult:
        for relative, content in self._outputs.items():
            (self._workspace / relative).write_bytes(content)
        line = json.dumps(
            {"type": "thread.started", "thread_id": "bounded-output-thread"}
        )
        journal = f"{line}\n".encode("utf-8")
        self._journal_path.parent.mkdir(parents=True, exist_ok=True)
        self._journal_path.write_bytes(journal)
        return CodexRunResult(
            exit_code=0,
            terminal_status="succeeded",
            process_cleanup_status="not_required",
            journal_path=self._journal_path,
            journal_sha256=hashlib.sha256(journal).hexdigest(),
            artifact_references=(),
            jsonl_lines=(line,),
        )


@pytest.mark.asyncio
async def test_nonzero_terminal_result_persists_receipt_and_fails_terminally(
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

        def prepare_or_recover(self, *_args):
            return PreparedFactoryWorkspace(root=workspace, base_revision="a" * 40)

    class FailedRunner(SuccessfulRunner):
        async def run(self, authorized: AuthorizedCodexRun) -> CodexRunResult:
            result = await super().run(authorized)
            return CodexRunResult(
                exit_code=1,
                terminal_status="failed",
                process_cleanup_status="not_required",
                journal_path=result.journal_path,
                journal_sha256=result.journal_sha256,
                artifact_references=result.artifact_references,
                jsonl_lines=result.jsonl_lines,
            )

    executor = CodexCliFactoryBuildExecutor(
        settings=CodexCliFactoryBuildSettings(
            state_root=state_root,
            maximum_runtime_seconds=120,
        ),
        workspace_preparer=Preparer(),
        artifact_reader=artifact_reader,
        authorizer=RecordingAuthorizer(),
        runner_factory=lambda **kwargs: FailedRunner(
            workspace,
            kwargs["journal_path"],
        ),
        clock=lambda: NOW,
    )

    with pytest.raises(FactoryDispatchError, match=r"failed \(exit 1\)"):
        await executor.execute(_dispatch(job, invocation), invocation, brief)

    checkpoint = FilesystemFactoryCodexBuildCheckpointStore(
        state_root / "checkpoints"
    ).load(invocation)
    assert checkpoint is not None
    assert checkpoint.phase == "implementation_failed"
    assert checkpoint.implementation_failure_reason == "runtime_failed"
    assert checkpoint.terminal_receipt_sha256 is not None
    assert (
        state_root / "sessions" / f"{invocation.idempotency_key}.json"
    ).is_file()

    caught = executor.reconcile_failed(
        _dispatch(job, invocation),
        invocation,
        brief,
    )

    assert str(caught) == "Factory Codex build failed"
    assert caught.reason == "runtime_failed"
    assert caught.checkpoint_ref.sha256 == hashlib.sha256(
        canonical_factory_codex_model(checkpoint)
    ).hexdigest()
    assert caught.terminal_receipt_ref.sha256 == checkpoint.terminal_receipt_sha256
    assert len(caught.args) == 1

    forbidden_retry = _authorized_runtime_retry_dispatch(
        _dispatch(job, invocation),
        invocation,
        checkpoint,
    )
    with pytest.raises(FactoryDispatchError, match="retry authority changed"):
        executor.reconcile_failed(forbidden_retry, invocation, brief)

    checkpoint_path = state_root / "checkpoints" / f"{invocation.invocation_id.hex}.json"
    checkpoint_bytes = checkpoint_path.read_bytes()
    receipt_path = state_root / "sessions" / f"{invocation.idempotency_key}.json"
    receipt_bytes = receipt_path.read_bytes()
    receipt_path.unlink()
    with pytest.raises(FactoryDispatchError, match="receipt.*missing|missing.*receipt"):
        executor.reconcile_failed(_dispatch(job, invocation), invocation, brief)
    assert checkpoint_path.read_bytes() == checkpoint_bytes

    receipt_path.write_bytes(receipt_bytes)
    journal_path = state_root / "journals" / f"{invocation.idempotency_key}.jsonl"
    journal_bytes = journal_path.read_bytes()
    journal_path.write_text('{"type":"tampered"}\n', encoding="utf-8")
    with pytest.raises(FactoryDispatchError, match="journal.*digest|digest.*journal"):
        executor.reconcile_failed(_dispatch(job, invocation), invocation, brief)
    assert checkpoint_path.read_bytes() == checkpoint_bytes
    journal_path.write_bytes(journal_bytes)


@pytest.mark.asyncio
async def test_output_capture_accepts_exact_per_file_and_aggregate_boundaries(
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

    executor = CodexCliFactoryBuildExecutor(
        settings=CodexCliFactoryBuildSettings(
            state_root=state_root,
            maximum_runtime_seconds=120,
            maximum_candidate_archive_bytes=10,
            maximum_json_artifact_bytes=10,
            maximum_output_bytes=30,
        ),
        workspace_preparer=Preparer(),
        artifact_reader=artifact_reader,
        authorizer=RecordingAuthorizer(),
        runner_factory=lambda **kwargs: FixedSizeOutputsRunner(
            workspace=workspace,
            journal_path=kwargs["journal_path"],
            candidate_archive=b"a" * 10,
            candidate_manifest=b"b" * 10,
            test_evidence=b"c" * 10,
        ),
        clock=lambda: NOW,
    )

    await executor.execute(_dispatch(job, invocation), invocation, brief)

    checkpoint = FilesystemFactoryCodexBuildCheckpointStore(
        state_root / "checkpoints"
    ).load(invocation)
    assert checkpoint is not None
    assert checkpoint.phase == "implementation_complete"
    assert checkpoint.output_manifest_sha256 is not None
    manifest = json.loads(
        (
            state_root
            / "output-manifests"
            / f"{checkpoint.output_manifest_sha256}.json"
        ).read_bytes()
    )
    assert sum(item["size"] for item in manifest["artifacts"]) == 30


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("candidate_archive", "candidate_manifest", "test_evidence", "aggregate_limit"),
    (
        (b"a" * 11, b"b" * 10, b"c" * 10, 31),
        (b"a" * 10, b"b" * 10, b"c" * 10, 29),
    ),
)
async def test_output_capture_fails_terminally_before_checkpoint_on_size_limit(
    tmp_path: Path,
    candidate_archive: bytes,
    candidate_manifest: bytes,
    test_evidence: bytes,
    aggregate_limit: int,
) -> None:
    job, brief, artifact_reader = _executor_job_and_brief()
    invocation = _seal_invocation(job, brief)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"

    class Preparer:
        def prepare(self, *_args):
            return PreparedFactoryWorkspace(root=workspace, base_revision="a" * 40)

        def prepare_or_recover(self, *_args):
            return PreparedFactoryWorkspace(root=workspace, base_revision="a" * 40)

    executor = CodexCliFactoryBuildExecutor(
        settings=CodexCliFactoryBuildSettings(
            state_root=state_root,
            maximum_runtime_seconds=120,
            maximum_candidate_archive_bytes=10,
            maximum_json_artifact_bytes=10,
            maximum_output_bytes=aggregate_limit,
        ),
        workspace_preparer=Preparer(),
        artifact_reader=artifact_reader,
        authorizer=RecordingAuthorizer(),
        runner_factory=lambda **kwargs: FixedSizeOutputsRunner(
            workspace=workspace,
            journal_path=kwargs["journal_path"],
            candidate_archive=candidate_archive,
            candidate_manifest=candidate_manifest,
            test_evidence=test_evidence,
        ),
        clock=lambda: NOW,
    )
    dispatch = _dispatch(job, invocation)

    with pytest.raises(FactoryDispatchError, match="output.*size limit"):
        await executor.execute(dispatch, invocation, brief)

    checkpoint_store = FilesystemFactoryCodexBuildCheckpointStore(
        state_root / "checkpoints"
    )
    failed = checkpoint_store.load(invocation)
    assert failed is not None
    assert failed.phase == "implementation_failed"
    assert failed.terminal_receipt_sha256 is not None
    assert (
        state_root / "sessions" / f"{invocation.idempotency_key}.json"
    ).is_file()
    for relative in (
        "candidate.zip",
        "factory-candidate.json",
        "test-evidence.json",
    ):
        (workspace / relative).write_bytes(b"late")
    with pytest.raises(FactoryDispatchError, match="not reconcilable|failed"):
        await executor.reconcile_pending(dispatch, invocation, brief)
    assert checkpoint_store.load(invocation) == failed


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


def test_session_receipt_rejects_thread_id_that_could_become_a_cli_option(
    tmp_path: Path,
) -> None:
    line = json.dumps({"type": "thread.started", "thread_id": "--last"})
    result = _run_result(
        tmp_path,
        exit_code=124,
        terminal_status="timed_out",
        jsonl_lines=(line,),
        process_cleanup_status="verified_cancelled",
    )

    with pytest.raises(FactoryDispatchError, match="thread ID"):
        _session_receipt(
            result=result,
            session_id="factory-safe-thread-test",
            workspace_ref="workspace://factory/safe-thread",
            base_revision="a" * 40,
            command=("codex", "exec", "--json", "bounded prompt"),
            completed_at=NOW,
        )


@pytest.mark.parametrize(
    "canonical_events",
    (
        (
            {"type": "thread.started", "thread_id": "thread-123"},
            {"type": "thread.started", "thread_id": "thread-123"},
        ),
        (
            {"type": "thread.started", "thread_id": "thread-123"},
            {"type": "thread.started", "thread_id": "thread-456"},
        ),
        ({"type": "thread.started"},),
        ({"type": "thread.started", "thread_id": 7},),
    ),
)
def test_session_receipt_rejects_duplicate_conflicting_or_malformed_thread_start(
    tmp_path: Path,
    canonical_events: tuple[dict[str, object], ...],
) -> None:
    lines = tuple(json.dumps(event) for event in canonical_events)
    result = _run_result(
        tmp_path,
        exit_code=124,
        terminal_status="timed_out",
        jsonl_lines=lines,
        process_cleanup_status="verified_cancelled",
    )

    with pytest.raises(FactoryDispatchError, match="thread.started"):
        _session_receipt(
            result=result,
            session_id="factory-canonical-thread-test",
            workspace_ref="workspace://factory/canonical-thread",
            base_revision="a" * 40,
            command=("codex", "exec", "--json", "bounded prompt"),
            completed_at=NOW,
        )


def test_session_receipt_ignores_thread_id_on_noncanonical_event(
    tmp_path: Path,
) -> None:
    lines = (
        json.dumps({"type": "turn.started", "thread_id": "injected-thread"}),
        json.dumps({"type": "turn.completed"}),
    )
    result = _run_result(
        tmp_path,
        exit_code=124,
        terminal_status="timed_out",
        jsonl_lines=lines,
        process_cleanup_status="verified_cancelled",
    )

    receipt = json.loads(
        _session_receipt(
            result=result,
            session_id="factory-no-canonical-thread-test",
            workspace_ref="workspace://factory/no-canonical-thread",
            base_revision="a" * 40,
            command=("codex", "exec", "--json", "bounded prompt"),
            completed_at=NOW,
        )
    )

    assert receipt["codex_thread_id"] is None


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


def test_session_receipt_normalizes_untrusted_event_types_without_inflation(
    tmp_path: Path,
) -> None:
    marker = "SENSITIVE_MARKER"
    lines = tuple(
        json.dumps({"type": f"{marker}_{index}"}) for index in range(128)
    )
    result = _run_result(
        tmp_path,
        jsonl_lines=lines,
        exit_code=1,
        terminal_status="failed",
    )

    receipt = _session_receipt(
        result=result,
        session_id="session-untrusted-types",
        workspace_ref="workspace://factory/demo",
        base_revision="a" * 40,
        command=("codex", "exec", "--json", "safe"),
        completed_at=NOW,
    )
    payload = json.loads(receipt)

    assert payload["event_count"] == 128
    assert payload["event_types"] == ["unknown"]
    assert marker.encode("utf-8") not in receipt


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

    with pytest.raises(FactoryCodexCleanupUnresolved, match="cleanup is unresolved"):
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
        executor.validate_authorized_resume(dispatch, invocation, brief)
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
                process_cleanup_status="not_required",
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
    assert receipt["process_cleanup_status"] == "not_required"
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
    seal_retry = await executor.execute_authorized_resume(
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
    assert checkpoint.output_manifest_sha256 is not None
    assert seal_retry == completed
    resumed_manifest = json.loads(
        (
            state_root
            / "output-manifests"
            / f"{checkpoint.output_manifest_sha256}.json"
        ).read_bytes()
    )
    assert resumed_manifest["resume_ordinal"] == 1
    assert (
        resumed_manifest["terminal_receipt_sha256"]
        == checkpoint.terminal_receipt_sha256
    )
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
    assert checkpoint.runtime_retry_authorization_uri == (
        authorized_dispatch.runtime_retry_authorization.authorization_ref.uri
    )
    assert checkpoint.runtime_retry_authorization_sha256 == (
        authorized_dispatch.runtime_retry_authorization.authorization_ref.sha256
    )
    assert checkpoint.runtime_retry_authorization_binding_sha256 is not None
    with pytest.raises(FactoryDispatchError, match="checkpoint.*retry authority"):
        await executor.reconcile_pending(
            replace(authorized_dispatch, runtime_retry_authorization=None),
            invocation,
            brief,
        )
    different_authorization = authorized_dispatch.runtime_retry_authorization.model_copy(
        update={
            "authorization_ref": ArtifactRef(
                uri=f"artifact://factory/runtime-retry/{'8' * 64}",
                sha256="8" * 64,
                media_type="application/json",
            )
        }
    )
    with pytest.raises(FactoryDispatchError, match="checkpoint.*retry authority"):
        await executor.reconcile_pending(
            replace(
                authorized_dispatch,
                runtime_retry_authorization=different_authorization,
            ),
            invocation,
            brief,
        )


async def _resume_lineage_fixture(
    tmp_path: Path,
    *,
    prior_thread_id: str | None,
    noncanonical_thread_id: str | None = None,
):
    job, brief, artifact_reader = _executor_job_and_brief()
    invocation = _seal_invocation(job, brief)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    current = NOW
    runner_calls = 0

    class RecoveringPreparer:
        def prepare_or_recover(self, _request, _invocation, _brief, _checkpoint):
            return PreparedFactoryWorkspace(root=workspace, base_revision="a" * 40)

    class TimedOutRunner:
        def __init__(self, journal_path: Path) -> None:
            self.journal_path = journal_path

        async def run(self, _authorized) -> CodexRunResult:
            events: list[dict[str, object]] = []
            if prior_thread_id is not None:
                events.append(
                    {
                        "type": "thread.started",
                        "thread_id": prior_thread_id,
                    }
                )
            if noncanonical_thread_id is not None:
                events.append(
                    {
                        "type": "turn.completed",
                        "thread_id": noncanonical_thread_id,
                    }
                )
            lines = tuple(json.dumps(event) for event in events)
            journal = b"".join(f"{line}\n".encode("utf-8") for line in lines)
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            self.journal_path.write_bytes(journal)
            return CodexRunResult(
                exit_code=124,
                terminal_status="timed_out",
                process_cleanup_status="verified_cancelled",
                journal_path=self.journal_path,
                journal_sha256=hashlib.sha256(journal).hexdigest(),
                artifact_references=(),
                jsonl_lines=lines,
            )

    resume_runners: list[SuccessfulRunner] = []

    def runner_factory(**kwargs):
        nonlocal runner_calls
        runner_calls += 1
        if runner_calls == 1:
            return TimedOutRunner(kwargs["journal_path"])
        runner = SuccessfulRunner(workspace, kwargs["journal_path"])
        resume_runners.append(runner)
        return runner

    authorizer = RecordingAuthorizer()
    executor = CodexCliFactoryBuildExecutor(
        settings=CodexCliFactoryBuildSettings(
            state_root=state_root,
            maximum_runtime_seconds=120,
        ),
        workspace_preparer=RecoveringPreparer(),
        artifact_reader=artifact_reader,
        authorizer=authorizer,
        runner_factory=runner_factory,
        resume_authorizer=CaptainFactoryCodexResumeAuthorizer(clock=lambda: current),
        clock=lambda: current,
    )
    dispatch = _dispatch(job, invocation)
    with pytest.raises(FactoryCodexBuildInterrupted):
        await executor.execute(dispatch, invocation, brief)
    checkpoint_store = FilesystemFactoryCodexBuildCheckpointStore(
        state_root / "checkpoints"
    )
    checkpoint = checkpoint_store.load(invocation)
    assert checkpoint is not None
    current = invocation.lease.expires_at + timedelta(seconds=1)
    authorized = _authorized_runtime_retry_dispatch(
        dispatch,
        invocation,
        checkpoint,
        issued_at=current,
        expires_at=current + timedelta(minutes=2),
    )
    return {
        "executor": executor,
        "job": job,
        "dispatch": dispatch,
        "authorized": authorized,
        "invocation": invocation,
        "brief": brief,
        "state_root": state_root,
        "workspace": workspace,
        "checkpoint_store": checkpoint_store,
        "checkpoint": checkpoint,
        "authorizer": authorizer,
        "runner_calls": lambda: runner_calls,
        "resume_runners": resume_runners,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ("missing_receipt", "journal_digest"))
async def test_authorized_resume_revalidates_prior_terminal_evidence_before_process(
    tmp_path: Path,
    tamper: str,
) -> None:
    fixture = await _resume_lineage_fixture(
        tmp_path,
        prior_thread_id="codex-thread-123",
    )
    state_root = fixture["state_root"]
    invocation = fixture["invocation"]
    receipt_path = (
        state_root / "sessions" / f"{invocation.idempotency_key}.json"
    )
    journal_path = (
        state_root / "journals" / f"{invocation.idempotency_key}.jsonl"
    )
    if tamper == "missing_receipt":
        receipt_path.unlink()
    else:
        journal_path.write_text(
            '{"type":"thread.started","thread_id":"tampered-thread"}\n',
            encoding="utf-8",
        )

    with pytest.raises(FactoryDispatchError, match="receipt|journal"):
        await fixture["executor"].execute_authorized_resume(
            fixture["authorized"],
            invocation,
            fixture["brief"],
        )

    assert fixture["runner_calls"]() == 1
    assert len(fixture["authorizer"].requests) == 1
    assert fixture["checkpoint_store"].load(invocation) == fixture["checkpoint"]


@pytest.mark.asyncio
async def test_authorized_resume_rejects_prior_receipt_journal_thread_mismatch(
    tmp_path: Path,
) -> None:
    fixture = await _resume_lineage_fixture(
        tmp_path,
        prior_thread_id="codex-thread-123",
    )
    state_root = fixture["state_root"]
    invocation = fixture["invocation"]
    receipt_path = (
        state_root / "sessions" / f"{invocation.idempotency_key}.json"
    )
    journal_path = (
        state_root / "journals" / f"{invocation.idempotency_key}.jsonl"
    )
    journal = (
        b'{"type":"thread.started","thread_id":"different-thread"}\n'
    )
    journal_path.write_bytes(journal)
    receipt = json.loads(receipt_path.read_bytes())
    receipt["jsonl_sha256"] = hashlib.sha256(journal).hexdigest()
    receipt["journal_sha256"] = hashlib.sha256(journal).hexdigest()
    receipt_path.write_bytes(
        json.dumps(
            receipt,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    changed_checkpoint = fixture["checkpoint"].model_copy(
        update={
            "terminal_receipt_sha256": hashlib.sha256(
                receipt_path.read_bytes()
            ).hexdigest()
        }
    )
    checkpoint_path = (
        state_root
        / "checkpoints"
        / f"{invocation.invocation_id.hex}.json"
    )
    checkpoint_path.write_bytes(canonical_factory_codex_model(changed_checkpoint))
    authorized = _authorized_runtime_retry_dispatch(
        fixture["dispatch"],
        invocation,
        changed_checkpoint,
        issued_at=fixture["authorized"].runtime_retry_authorization.issued_at,
        expires_at=fixture["authorized"].runtime_retry_authorization.expires_at,
    )

    with pytest.raises(FactoryDispatchError, match="thread"):
        await fixture["executor"].execute_authorized_resume(
            authorized,
            invocation,
            fixture["brief"],
        )

    assert fixture["runner_calls"]() == 1
    assert len(fixture["authorizer"].requests) == 1


@pytest.mark.asyncio
async def test_authorized_resume_uses_prior_thread_and_persists_parent_lineage(
    tmp_path: Path,
) -> None:
    fixture = await _resume_lineage_fixture(
        tmp_path,
        prior_thread_id="codex-thread-123",
    )

    completed = await fixture["executor"].execute_authorized_resume(
        fixture["authorized"],
        fixture["invocation"],
        fixture["brief"],
    )

    assert fixture["runner_calls"]() == 2
    resume_command = fixture["authorizer"].requests[1].command
    assert resume_command[:5] == (
        "codex",
        "exec",
        "resume",
        "--json",
        "codex-thread-123",
    )
    assert resume_command[-1].startswith(
        fixture["authorizer"].requests[0].command[-1]
    )
    assert "CAPTAIN RESUME REPAIR" in resume_command[-1]
    receipt = json.loads(completed.codex_session_receipt)
    prior_receipt_path = (
        fixture["state_root"]
        / "sessions"
        / f"{fixture['invocation'].idempotency_key}.json"
    )
    prior_journal_path = (
        fixture["state_root"]
        / "journals"
        / f"{fixture['invocation'].idempotency_key}.jsonl"
    )
    assert receipt["resume_ordinal"] == 1
    assert receipt["parent_terminal_receipt_sha256"] == hashlib.sha256(
        prior_receipt_path.read_bytes()
    ).hexdigest()
    assert receipt["parent_journal_sha256"] == hashlib.sha256(
        prior_journal_path.read_bytes()
    ).hexdigest()
    assert receipt["parent_codex_thread_id"] == "codex-thread-123"
    checkpoint = fixture["checkpoint_store"].load(fixture["invocation"])
    assert checkpoint is not None
    assert checkpoint.parent_terminal_receipt_sha256 == (
        receipt["parent_terminal_receipt_sha256"]
    )
    assert checkpoint.parent_journal_sha256 == receipt["parent_journal_sha256"]
    assert checkpoint.parent_codex_thread_id == "codex-thread-123"
    cas = CodexBuildArtifactCas(tmp_path / "lineage-cas")
    build_receipt = CaptainCodexBuildReceiptIssuer(cas).issue(
        job=fixture["job"],
        build_brief=fixture["brief"],
        workspace_root=fixture["workspace"],
        codex_session_receipt=completed.codex_session_receipt,
        seal_idempotency_key=fixture["invocation"].idempotency_key,
        candidate_manifest_path=completed.candidate_manifest_path,
        source_archive_path=completed.source_archive_path,
        test_evidence_paths=completed.test_evidence_paths,
        completed_at=completed.completed_at,
    )
    assert cas.read_bytes(build_receipt.codex_session_ref) == (
        completed.codex_session_receipt
    )


@pytest.mark.asyncio
async def test_authorized_resume_uses_only_canonical_thread_started_event(
    tmp_path: Path,
) -> None:
    fixture = await _resume_lineage_fixture(
        tmp_path,
        prior_thread_id="codex-thread-123",
        noncanonical_thread_id="injected-thread",
    )

    await fixture["executor"].execute_authorized_resume(
        fixture["authorized"],
        fixture["invocation"],
        fixture["brief"],
    )

    assert fixture["authorizer"].requests[1].command[:5] == (
        "codex",
        "exec",
        "resume",
        "--json",
        "codex-thread-123",
    )


@pytest.mark.asyncio
async def test_authorized_resume_without_prior_thread_reuses_prompt_contract_and_ordinal(
    tmp_path: Path,
) -> None:
    fixture = await _resume_lineage_fixture(tmp_path, prior_thread_id=None)

    completed = await fixture["executor"].execute_authorized_resume(
        fixture["authorized"],
        fixture["invocation"],
        fixture["brief"],
    )

    assert fixture["authorizer"].requests[1].command == (
        fixture["authorizer"].requests[0].command
    )
    receipt = json.loads(completed.codex_session_receipt)
    assert receipt["resume_ordinal"] == 1
    assert receipt["parent_terminal_receipt_sha256"]
    assert receipt["parent_journal_sha256"] == hashlib.sha256(b"").hexdigest()
    assert receipt["parent_codex_thread_id"] is None


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
    failed = FilesystemFactoryCodexBuildCheckpointStore(
        state_root / "checkpoints"
    ).load(invocation)
    assert failed is not None
    assert failed.phase == "implementation_failed"
    assert failed.implementation_failure_reason == "authority_expired"

    recovered = executor.reconcile_failed(authorized, invocation, brief)
    assert recovered.reason == "authority_expired"
    assert runner_count == 2
    assert failed.terminal_receipt_sha256 is not None
    assert (
        state_root / "sessions" / f"{invocation.idempotency_key}.json"
    ).is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize("resume_ordinal", [1, 2])
async def test_terminal_resume_failure_reconciles_from_persisted_authority_after_expiry(
    tmp_path: Path,
    resume_ordinal: int,
) -> None:
    """Removing tokenless terminal reconciliation would strand the SEAL replay."""

    job, brief, artifact_reader = _executor_job_and_brief()
    invocation = _seal_invocation(job, brief)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    current = NOW
    outcomes = ["timed_out"] * resume_ordinal + ["failed"]
    runner_calls = 0

    class RecoveringPreparer:
        def prepare_or_recover(self, _request, _invocation, _brief, _checkpoint):
            return PreparedFactoryWorkspace(root=workspace, base_revision="a" * 40)

    class TerminalRunner:
        def __init__(self, journal_path: Path, outcome: str) -> None:
            self.journal_path = journal_path
            self.outcome = outcome

        async def run(self, _authorized: AuthorizedCodexRun) -> CodexRunResult:
            lines = (
                json.dumps(
                    {"type": "thread.started", "thread_id": "codex-thread-123"}
                ),
                json.dumps({"type": "turn.completed"}),
            )
            journal = "".join(f"{line}\n" for line in lines).encode("utf-8")
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            self.journal_path.write_bytes(journal)
            failed = self.outcome == "failed"
            return CodexRunResult(
                exit_code=17 if failed else 124,
                terminal_status="failed" if failed else "timed_out",
                process_cleanup_status=(
                    "not_required" if failed else "verified_cancelled"
                ),
                journal_path=self.journal_path,
                journal_sha256=hashlib.sha256(journal).hexdigest(),
                artifact_references=(),
                jsonl_lines=lines,
            )

    def runner_factory(**kwargs):
        nonlocal runner_calls
        outcome = outcomes[runner_calls]
        runner_calls += 1
        return TerminalRunner(kwargs["journal_path"], outcome)

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

    authorized = dispatch
    for ordinal in range(1, resume_ordinal + 1):
        checkpoint = FilesystemFactoryCodexBuildCheckpointStore(
            state_root / "checkpoints"
        ).load(invocation)
        assert checkpoint is not None
        current = invocation.lease.expires_at + timedelta(seconds=ordinal)
        authorized = _authorized_runtime_retry_dispatch(
            dispatch,
            invocation,
            checkpoint,
            issued_at=current,
            expires_at=current + timedelta(minutes=2),
        )
        if ordinal < resume_ordinal:
            with pytest.raises(FactoryCodexBuildInterrupted):
                await executor.execute_authorized_resume(
                    authorized,
                    invocation,
                    brief,
                )
        else:
            with pytest.raises(FactoryDispatchError, match="process failed"):
                await executor.execute_authorized_resume(
                    authorized,
                    invocation,
                    brief,
                )

    authorization = authorized.runtime_retry_authorization
    assert authorization is not None
    failed_checkpoint = FilesystemFactoryCodexBuildCheckpointStore(
        state_root / "checkpoints"
    ).load(invocation)
    assert failed_checkpoint is not None
    assert failed_checkpoint.phase == "implementation_failed"
    assert failed_checkpoint.resume_ordinal == resume_ordinal
    checkpoint_path = (
        state_root / "checkpoints" / f"{invocation.invocation_id.hex}.json"
    )
    checkpoint_bytes = checkpoint_path.read_bytes()
    persisted_binding_sha256 = hashlib.sha256(
        canonical_factory_codex_model(authorization)
    ).hexdigest()
    tokenless = replace(authorized, runtime_retry_authorization=None)
    current = job.deadline_at + timedelta(seconds=1)

    with pytest.raises(FactoryDispatchError, match="resume ordinal"):
        executor.reconcile_failed(
            tokenless,
            invocation,
            brief,
            persisted_resume_ordinal=resume_ordinal - 1,
            persisted_retry_authorization_ref=authorization.authorization_ref,
            persisted_retry_authorization_binding_sha256=persisted_binding_sha256,
        )
    assert checkpoint_path.read_bytes() == checkpoint_bytes

    with pytest.raises(FactoryDispatchError, match="retry authority"):
        executor.reconcile_failed(
            tokenless,
            invocation,
            brief,
            persisted_resume_ordinal=resume_ordinal,
            persisted_retry_authorization_ref=authorization.authorization_ref,
            persisted_retry_authorization_binding_sha256="0" * 64,
        )
    assert checkpoint_path.read_bytes() == checkpoint_bytes

    recovered = executor.reconcile_failed(
        tokenless,
        invocation,
        brief,
        persisted_resume_ordinal=resume_ordinal,
        persisted_retry_authorization_ref=authorization.authorization_ref,
        persisted_retry_authorization_binding_sha256=persisted_binding_sha256,
    )

    assert recovered.reason == "runtime_failed"
    assert runner_calls == resume_ordinal + 1
    assert checkpoint_path.read_bytes() == checkpoint_bytes


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


@pytest.mark.skipif(
    os.getenv("CI") == "true",
    reason=(
        "git worktree add --detach fails with exit 128 only through this "
        "in-process subprocess.run call on hosted Windows runners (passes "
        "standalone via a plain shell invocation, and passes on self-hosted/"
        "local machines) -- root cause not yet found, see "
        "https://github.com/Flissel/Captain_cook/issues/25"
    ),
)
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


@pytest.mark.skipif(
    os.getenv("CI") == "true",
    reason=(
        "git worktree add --detach fails with exit 128 only through this "
        "in-process subprocess.run call on hosted Windows runners (passes "
        "standalone via a plain shell invocation, and passes on self-hosted/"
        "local machines) -- root cause not yet found, see "
        "https://github.com/Flissel/Captain_cook/issues/25"
    ),
)
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


class _StaticProcessInspector:
    def __init__(self, status: FactoryCodexProcessState) -> None:
        self.status = status
        self.calls: list[tuple[str, Path]] = []

    async def inspect(self, *, session_id: str, state_path: Path) -> FactoryCodexProcessState:
        self.calls.append((session_id, state_path))
        return self.status


async def _running_crash_fixture(
    tmp_path: Path,
    *,
    inspector: _StaticProcessInspector,
):
    job, brief, artifact_reader = _executor_job_and_brief()
    invocation = _seal_invocation(job, brief)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    authorizer = RecordingAuthorizer()
    runner_calls = 0

    class Preparer:
        def prepare_or_recover(self, _request, _invocation, _brief, _checkpoint):
            return PreparedFactoryWorkspace(root=workspace, base_revision="a" * 40)

    class CrashRunner:
        def __init__(self, *, state_path: Path, journal_path: Path) -> None:
            self.state_path = state_path
            self.journal_path = journal_path

        async def run(self, _authorized):
            nonlocal runner_calls
            runner_calls += 1
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text('{"private":"process-identity"}', encoding="utf-8")
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            self.journal_path.write_text(
                json.dumps({"type": "thread.started", "thread_id": "restart-thread"})
                + "\n",
                encoding="utf-8",
            )
            raise RuntimeError("simulated host crash")

    executor = CodexCliFactoryBuildExecutor(
        settings=CodexCliFactoryBuildSettings(
            state_root=state_root,
            maximum_runtime_seconds=120,
        ),
        workspace_preparer=Preparer(),
        artifact_reader=artifact_reader,
        authorizer=authorizer,
        runner_factory=lambda **kwargs: CrashRunner(
            state_path=kwargs["state_path"],
            journal_path=kwargs["journal_path"],
        ),
        process_inspector=inspector,
        clock=lambda: NOW,
    )
    dispatch = _dispatch(job, invocation)
    with pytest.raises(RuntimeError, match="simulated host crash"):
        await executor.execute(dispatch, invocation, brief)
    checkpoint = FilesystemFactoryCodexBuildCheckpointStore(
        state_root / "checkpoints"
    ).load(invocation)
    assert checkpoint is not None
    assert checkpoint.phase == "implementation_running"
    return (
        executor,
        dispatch,
        invocation,
        brief,
        workspace,
        state_root,
        authorizer,
        lambda: runner_calls,
    )


def _persist_crash_receipt(
    *,
    state_root: Path,
    invocation: FactorySkillInvocationV1,
    brief: CodexBuildBriefV1,
    command: tuple[str, ...],
    exit_code: int,
    terminal_status: str,
    process_cleanup_status: str,
) -> bytes:
    journal_path = state_root / "journals" / f"{invocation.idempotency_key}.jsonl"
    journal = journal_path.read_bytes()
    lines = tuple(line.decode("utf-8") for line in journal.splitlines() if line.strip())
    receipt = _session_receipt(
        result=CodexRunResult(
            exit_code=exit_code,
            terminal_status=terminal_status,
            process_cleanup_status=process_cleanup_status,
            journal_path=journal_path,
            journal_sha256=hashlib.sha256(journal).hexdigest(),
            artifact_references=(),
            jsonl_lines=lines,
        ),
        session_id=f"factory-{invocation.invocation_id.hex[:24]}",
        workspace_ref=brief.build_assignment.workspace_ref,
        base_revision="a" * 40,
        command=command,
        completed_at=NOW,
    )
    path = state_root / "sessions" / f"{invocation.idempotency_key}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(receipt)
    return receipt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cleanup_status", "expected_phase"),
    (
        ("verified_cancelled", "implementation_failed"),
        ("unresolved", "implementation_running"),
    ),
)
async def test_cli_executor_persists_output_evidence_failure_before_raising(
    tmp_path: Path,
    cleanup_status: str,
    expected_phase: str,
) -> None:
    job, brief, artifact_reader = _executor_job_and_brief()
    invocation = _seal_invocation(job, brief)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    private_detail = "private stream read detail"
    partial_record = b'{"type":"thread.started","thread_id":"partial-thread"}\n'
    authorizer = RecordingAuthorizer()

    class Preparer:
        def prepare(self, *_args):
            return PreparedFactoryWorkspace(root=workspace, base_revision="a" * 40)

        prepare_or_recover = prepare

    class EvidenceFailureRunner:
        def __init__(self, journal_path: Path) -> None:
            self.journal_path = journal_path

        async def run(self, _authorized: AuthorizedCodexRun):
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            self.journal_path.write_bytes(partial_record)
            error = codex_supervisor.CodexOutputReadError(private_detail)
            error.bind_terminal_evidence(
                process_cleanup_status=cleanup_status,
                journal_path=self.journal_path,
                journal_sha256=hashlib.sha256(partial_record).hexdigest(),
                journal_byte_count=len(partial_record),
                event_count=1,
                event_types=("thread.started",),
            )
            raise error

    executor = CodexCliFactoryBuildExecutor(
        settings=CodexCliFactoryBuildSettings(
            state_root=state_root,
            maximum_runtime_seconds=120,
        ),
        workspace_preparer=Preparer(),
        artifact_reader=artifact_reader,
        authorizer=authorizer,
        runner_factory=lambda **kwargs: EvidenceFailureRunner(kwargs["journal_path"]),
        resume_authorizer=CaptainFactoryCodexResumeAuthorizer(clock=lambda: NOW),
        clock=lambda: NOW,
    )
    failure_type = getattr(codex_build_execution, "FactoryCodexEvidenceFailure")

    with pytest.raises(failure_type) as caught:
        await executor.execute(_dispatch(job, invocation), invocation, brief)

    receipt_path = state_root / "sessions" / f"{invocation.idempotency_key}.json"
    assert receipt_path.is_file()
    receipt_bytes = receipt_path.read_bytes()
    receipt = json.loads(receipt_bytes)
    assert receipt == {
        "base_revision": "a" * 40,
        "command_sha256": hashlib.sha256(
            "\0".join(authorizer.requests[0].command).encode("utf-8")
        ).hexdigest(),
        "completed_at": NOW.isoformat(),
        "event_count": 1,
        "event_types": ["thread.started"],
        "failure_kind": "output_read_failed",
        "journal_byte_count": len(partial_record),
        "journal_sha256": hashlib.sha256(partial_record).hexdigest(),
        "process_cleanup_status": cleanup_status,
        "provider": "codex-cli",
        "resume_ordinal": 0,
        "schema": "captain.codex-session-error-receipt.v1",
        "session_id": f"factory-{invocation.invocation_id.hex[:24]}",
        "status": "evidence_failed",
        "workspace_ref": brief.build_assignment.workspace_ref,
    }
    checkpoint = FilesystemFactoryCodexBuildCheckpointStore(
        state_root / "checkpoints"
    ).load(invocation)
    assert checkpoint is not None
    assert checkpoint.phase == expected_phase
    if cleanup_status == "verified_cancelled":
        assert checkpoint.implementation_failure_reason == "evidence_failure"
        assert checkpoint.terminal_receipt_sha256 == hashlib.sha256(
            receipt_bytes
        ).hexdigest()
    else:
        assert checkpoint.implementation_failure_reason is None
        assert checkpoint.terminal_receipt_sha256 is None
        with pytest.raises(FactoryDispatchError, match="not interrupted"):
            executor.validate_authorized_resume(
                _dispatch(job, invocation), invocation, brief
            )
    assert caught.value.process_cleanup_status == cleanup_status
    assert caught.value.terminal_receipt_ref.sha256 == hashlib.sha256(
        receipt_bytes
    ).hexdigest()
    assert cleanup_status not in str(caught.value)
    assert private_detail not in str(caught.value)
    assert private_detail.encode() not in receipt_bytes
    if cleanup_status == "unresolved":
        with pytest.raises(
            FactoryDispatchError,
            match="cleanup is unresolved|output evidence.*unresolved",
        ):
            await executor.reconcile_pending(
                _dispatch(job, invocation), invocation, brief
            )


@pytest.mark.asyncio
async def test_cli_executor_terminalizes_malformed_injected_runner_output(
    tmp_path: Path,
) -> None:
    job, brief, artifact_reader = _executor_job_and_brief()
    invocation = _seal_invocation(job, brief)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    authorizer = RecordingAuthorizer()

    class Preparer:
        def prepare(self, *_args):
            return PreparedFactoryWorkspace(root=workspace, base_revision="a" * 40)

    class MalformedRunner:
        def __init__(self, journal_path: Path) -> None:
            self.journal_path = journal_path

        async def run(self, _authorized: AuthorizedCodexRun) -> CodexRunResult:
            valid = '{"type":"SENSITIVE_MARKER"}'
            journal = f"{valid}\n[]\n".encode("utf-8")
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            self.journal_path.write_bytes(journal)
            return CodexRunResult(
                exit_code=0,
                terminal_status="succeeded",
                process_cleanup_status="not_required",
                journal_path=self.journal_path,
                journal_sha256=hashlib.sha256(journal).hexdigest(),
                artifact_references=(),
                jsonl_lines=(valid, "[]"),
            )

    executor = CodexCliFactoryBuildExecutor(
        settings=CodexCliFactoryBuildSettings(
            state_root=state_root,
            maximum_runtime_seconds=120,
        ),
        workspace_preparer=Preparer(),
        artifact_reader=artifact_reader,
        authorizer=authorizer,
        runner_factory=lambda **kwargs: MalformedRunner(kwargs["journal_path"]),
        clock=lambda: NOW,
    )

    with pytest.raises(FactoryCodexEvidenceFailure) as caught:
        await executor.execute(_dispatch(job, invocation), invocation, brief)

    receipt_path = state_root / "sessions" / f"{invocation.idempotency_key}.json"
    receipt = json.loads(receipt_path.read_bytes())
    assert receipt["schema"] == "captain.codex-session-error-receipt.v1"
    assert receipt["status"] == "evidence_failed"
    assert receipt["failure_kind"] == "invalid_json_object"
    assert receipt["process_cleanup_status"] == "not_required"
    assert receipt["event_count"] == 1
    assert receipt["event_types"] == ["unknown"]
    assert b"SENSITIVE_MARKER" not in receipt_path.read_bytes()
    checkpoint = FilesystemFactoryCodexBuildCheckpointStore(
        state_root / "checkpoints"
    ).load(invocation)
    assert checkpoint is not None
    assert checkpoint.phase == "implementation_failed"
    assert checkpoint.implementation_failure_reason == "evidence_failure"
    assert checkpoint.terminal_receipt_sha256 == hashlib.sha256(
        receipt_path.read_bytes()
    ).hexdigest()
    assert caught.value.process_cleanup_status == "not_required"


@pytest.mark.asyncio
async def test_restart_reconciliation_never_duplicates_an_active_running_process(
    tmp_path: Path,
) -> None:
    inspector = _StaticProcessInspector("active")
    executor, dispatch, invocation, brief, _, _, _, runner_calls = (
        await _running_crash_fixture(tmp_path, inspector=inspector)
    )

    with pytest.raises(FactoryDispatchError, match="active.*inspection|inspection.*active"):
        await executor.reconcile_pending(dispatch, invocation, brief)

    assert runner_calls() == 1
    assert len(inspector.calls) == 1


@pytest.mark.asyncio
async def test_restart_reconciliation_materializes_cancelled_receipt_for_lost_process(
    tmp_path: Path,
) -> None:
    inspector = _StaticProcessInspector("lost")
    executor, dispatch, invocation, brief, _, state_root, _, runner_calls = (
        await _running_crash_fixture(tmp_path, inspector=inspector)
    )
    journal_path = state_root / "journals" / f"{invocation.idempotency_key}.jsonl"
    os.utime(journal_path, (NOW.timestamp(), NOW.timestamp()))
    receipt_path = state_root / "sessions" / f"{invocation.idempotency_key}.json"
    assert not receipt_path.exists()

    with pytest.raises(FactoryCodexBuildInterrupted) as caught:
        await executor.reconcile_pending(dispatch, invocation, brief)

    assert caught.value.reason == "runtime_cancelled"
    assert caught.value.exit_code == 130
    receipt = json.loads(receipt_path.read_bytes())
    assert receipt["schema"] == "captain.codex-session-receipt.v1"
    assert receipt["status"] == "cancelled"
    assert receipt["exit_code"] == 130
    assert receipt["process_cleanup_status"] == "not_required"
    assert receipt["completed_at"] == NOW.isoformat()
    checkpoint = FilesystemFactoryCodexBuildCheckpointStore(
        state_root / "checkpoints"
    ).load(invocation)
    assert checkpoint is not None
    assert checkpoint.phase == "implementation_interrupted"
    assert checkpoint.terminal_receipt_sha256 == hashlib.sha256(
        receipt_path.read_bytes()
    ).hexdigest()
    assert runner_calls() == 1
    assert len(inspector.calls) == 1


@pytest.mark.asyncio
async def test_failure_only_reconciliation_rejects_running_without_inspection_or_mutation(
    tmp_path: Path,
) -> None:
    inspector = _StaticProcessInspector("lost")
    executor, dispatch, invocation, brief, _, state_root, _, runner_calls = (
        await _running_crash_fixture(tmp_path, inspector=inspector)
    )
    checkpoint_path = (
        state_root / "checkpoints" / f"{invocation.invocation_id.hex}.json"
    )
    checkpoint_bytes = checkpoint_path.read_bytes()

    with pytest.raises(FactoryDispatchError, match="terminal failure"):
        executor.reconcile_failed(dispatch, invocation, brief)

    assert checkpoint_path.read_bytes() == checkpoint_bytes
    assert runner_calls() == 1
    assert inspector.calls == []


@pytest.mark.asyncio
async def test_restart_reconciliation_terminalizes_durable_output_evidence_failure(
    tmp_path: Path,
) -> None:
    inspector = _StaticProcessInspector("lost")
    executor, dispatch, invocation, brief, _, state_root, authorizer, runner_calls = (
        await _running_crash_fixture(tmp_path, inspector=inspector)
    )
    journal_path = state_root / "journals" / f"{invocation.idempotency_key}.jsonl"
    journal = journal_path.read_bytes()
    error = codex_supervisor.CodexOutputReadError("private restart detail")
    error.bind_terminal_evidence(
        process_cleanup_status="verified_cancelled",
        journal_path=journal_path,
        journal_sha256=hashlib.sha256(journal).hexdigest(),
        journal_byte_count=len(journal),
        event_count=1,
        event_types=("thread.started",),
    )
    receipt = _evidence_failure_receipt(
        error=error,
        session_id=f"factory-{invocation.invocation_id.hex[:24]}",
        workspace_ref=brief.build_assignment.workspace_ref,
        base_revision="a" * 40,
        command=authorizer.requests[0].command,
        completed_at=NOW,
        resume_ordinal=0,
    )
    receipt_path = state_root / "sessions" / f"{invocation.idempotency_key}.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_bytes(receipt)

    with pytest.raises(FactoryCodexEvidenceFailure) as caught:
        await executor.reconcile_pending(dispatch, invocation, brief)

    checkpoint = FilesystemFactoryCodexBuildCheckpointStore(
        state_root / "checkpoints"
    ).load(invocation)
    assert checkpoint is not None
    assert checkpoint.phase == "implementation_failed"
    assert checkpoint.implementation_failure_reason == "evidence_failure"
    assert checkpoint.terminal_receipt_sha256 == hashlib.sha256(receipt).hexdigest()
    assert caught.value.process_cleanup_status == "verified_cancelled"
    assert caught.value.terminal_receipt_ref.sha256 == hashlib.sha256(receipt).hexdigest()
    assert runner_calls() == 1

    recovered = executor.reconcile_failed(dispatch, invocation, brief)
    assert recovered.reason == "evidence_failure"
    assert recovered.terminal_receipt_ref.sha256 == hashlib.sha256(
        receipt
    ).hexdigest()
    assert runner_calls() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exit_code", "terminal_status", "cleanup_status", "reason"),
    (
        (124, "timed_out", "verified_cancelled", "codex_timed_out"),
        (130, "cancelled", "verified_cancelled", "runtime_cancelled"),
    ),
)
async def test_restart_reconciliation_terminalizes_running_from_valid_receipt(
    tmp_path: Path,
    exit_code: int,
    terminal_status: str,
    cleanup_status: str,
    reason: str,
) -> None:
    inspector = _StaticProcessInspector("lost")
    executor, dispatch, invocation, brief, _, state_root, authorizer, runner_calls = (
        await _running_crash_fixture(tmp_path, inspector=inspector)
    )
    _persist_crash_receipt(
        state_root=state_root,
        invocation=invocation,
        brief=brief,
        command=authorizer.requests[0].command,
        exit_code=exit_code,
        terminal_status=terminal_status,
        process_cleanup_status=cleanup_status,
    )

    with pytest.raises(FactoryCodexBuildInterrupted) as caught:
        await executor.reconcile_pending(dispatch, invocation, brief)

    assert caught.value.reason == reason
    checkpoint = FilesystemFactoryCodexBuildCheckpointStore(
        state_root / "checkpoints"
    ).load(invocation)
    assert checkpoint is not None
    assert checkpoint.phase == "implementation_interrupted"
    assert checkpoint.terminal_receipt_sha256 == caught.value.terminal_receipt_ref.sha256
    assert runner_calls() == 1
    receipt_path = state_root / "sessions" / f"{invocation.idempotency_key}.json"
    original_receipt = receipt_path.read_bytes()
    receipt_path.unlink()
    with pytest.raises(FactoryDispatchError, match="receipt.*missing|missing.*receipt"):
        await executor.reconcile_pending(dispatch, invocation, brief)
    receipt_path.write_bytes(original_receipt)
    journal_path = state_root / "journals" / f"{invocation.idempotency_key}.jsonl"
    journal_path.write_text('{"type":"tampered"}\n', encoding="utf-8")
    with pytest.raises(FactoryDispatchError, match="journal.*digest|digest.*journal"):
        await executor.reconcile_pending(dispatch, invocation, brief)


@pytest.mark.asyncio
async def test_restart_reconciliation_verifies_lost_unresolved_cleanup_append_only(
    tmp_path: Path,
) -> None:
    inspector = _StaticProcessInspector("lost")
    executor, dispatch, invocation, brief, _, state_root, authorizer, runner_calls = (
        await _running_crash_fixture(tmp_path, inspector=inspector)
    )
    original = _persist_crash_receipt(
        state_root=state_root,
        invocation=invocation,
        brief=brief,
        command=authorizer.requests[0].command,
        exit_code=124,
        terminal_status="timed_out",
        process_cleanup_status="unresolved",
    )

    with pytest.raises(FactoryCodexBuildInterrupted) as caught:
        await executor.reconcile_pending(dispatch, invocation, brief)

    assert caught.value.reason == "codex_timed_out"
    primary = state_root / "sessions" / f"{invocation.idempotency_key}.json"
    verified = (
        state_root
        / "sessions"
        / f"{invocation.idempotency_key}.cleanup-verified.json"
    )
    assert primary.read_bytes() == original
    verified_bytes = verified.read_bytes()
    verified_payload = json.loads(verified_bytes)
    assert verified_payload["status"] == "timed_out"
    assert verified_payload["exit_code"] == 124
    assert verified_payload["process_cleanup_status"] == "verified_cancelled"
    checkpoint = FilesystemFactoryCodexBuildCheckpointStore(
        state_root / "checkpoints"
    ).load(invocation)
    assert checkpoint is not None
    assert checkpoint.phase == "implementation_interrupted"
    assert checkpoint.terminal_receipt_sha256 == hashlib.sha256(
        verified_bytes
    ).hexdigest()
    assert caught.value.terminal_receipt_ref.sha256 == (
        checkpoint.terminal_receipt_sha256
    )
    assert runner_calls() == 1
    with pytest.raises(FactoryCodexBuildInterrupted) as replayed:
        await executor.reconcile_pending(dispatch, invocation, brief)
    assert replayed.value.terminal_receipt_ref.sha256 == (
        checkpoint.terminal_receipt_sha256
    )
    verified.write_text('{"tampered":true}', encoding="utf-8")
    with pytest.raises(FactoryDispatchError, match="receipt.*digest|digest.*receipt"):
        await executor.reconcile_pending(dispatch, invocation, brief)


@pytest.mark.asyncio
async def test_restart_reconciliation_seals_snapshot_after_workspace_outputs_disappear(
    tmp_path: Path,
) -> None:
    inspector = _StaticProcessInspector("lost")
    executor, dispatch, invocation, brief, workspace, state_root, authorizer, runner_calls = (
        await _running_crash_fixture(tmp_path, inspector=inspector)
    )
    successful = SuccessfulRunner(
        workspace,
        state_root / "journals" / f"{invocation.idempotency_key}.jsonl",
    )
    result = await successful.run(
        AuthorizedCodexRun(
            workspace=workspace,
            command=authorizer.requests[0].command,
            environment=FrozenEnvironment({}),
        )
    )
    receipt = _session_receipt(
        result=result,
        session_id=f"factory-{invocation.invocation_id.hex[:24]}",
        workspace_ref=brief.build_assignment.workspace_ref,
        base_revision="a" * 40,
        command=authorizer.requests[0].command,
        completed_at=NOW,
    )
    receipt_path = state_root / "sessions" / f"{invocation.idempotency_key}.json"
    executor._capture_and_persist_output_manifest(
        invocation=invocation,
        prepared=PreparedFactoryWorkspace(root=workspace, base_revision="a" * 40),
        resume_ordinal=0,
        terminal_receipt_sha256=hashlib.sha256(receipt).hexdigest(),
    )
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_bytes(receipt)
    pending = FilesystemFactoryCodexOutputManifestStore(
        state_root / "output-manifests"
    ).load_pending(invocation)
    assert pending is not None
    manifest = pending[0]
    expected_by_path = {
        item.relative_path: item.sha256 for item in manifest.artifacts
    }
    for relative in (
        "candidate.zip",
        "factory-candidate.json",
        "test-evidence.json",
    ):
        (workspace / relative).unlink()

    cas = CodexBuildArtifactCas(tmp_path / "recovery-cas")
    sealer = CaptainCodexBuildSealer(
        executor=executor,
        issuer=CaptainCodexBuildReceiptIssuer(cas),
    )
    evidence = await sealer.reconcile_pending(dispatch, invocation, brief)
    checkpoint = FilesystemFactoryCodexBuildCheckpointStore(
        state_root / "checkpoints"
    ).load(invocation)

    assert checkpoint is not None
    assert checkpoint.phase == "sealed"
    assert evidence.build_receipt.candidate_manifest_ref.sha256 == (
        expected_by_path["factory-candidate.json"]
    )
    assert evidence.build_receipt.source_archive_ref.sha256 == (
        expected_by_path["candidate.zip"]
    )
    assert tuple(
        item.sha256 for item in evidence.build_receipt.test_evidence_refs
    ) == (expected_by_path["test-evidence.json"],)
    assert runner_calls() == 1
    assert await sealer.seal(dispatch, invocation, brief) == evidence
    assert runner_calls() == 1
