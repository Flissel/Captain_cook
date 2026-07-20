from __future__ import annotations

import hashlib
import json
import signal
import subprocess
import sys
from types import SimpleNamespace
import zipfile
from pathlib import Path

import pytest

from agenten.agent_factory.candidate_evaluation import (
    CandidateEvaluationFactory,
    FactoryCandidateEvaluator,
    FactoryCandidateManifest,
    ResolvedFactoryCandidate,
    StaticFactoryCandidateProvider,
)
from agenten.agent_factory.contracts import FactoryPhase, FactoryRole
from agenten.agent_factory.evidence_store import FilesystemFactoryEvidenceStore
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_factory.orchestration import FactoryDispatch
from agenten.agent_factory.state_machine import FactoryAction, FactoryActionKind
from agenten.agent_runtime.contracts import ArtifactRef
from agenten.agent_factory.n8n_tools import TypedN8nTool
from agenten.agent_factory.skill_evaluation import HermesSkillEvaluationRequest
from tests.agent_factory.test_state_machine import job
from tests.agent_factory.test_skill_evaluation_contracts import request_payload


def _ref(uri: str, content: bytes, media_type: str = "application/json") -> ArtifactRef:
    return ArtifactRef(
        uri=uri,
        sha256=hashlib.sha256(content).hexdigest(),
        media_type=media_type,
    )


def _write_candidate_archive(path: Path) -> tuple[ArtifactRef, ArtifactRef, ArtifactRef, ArtifactRef, ArtifactRef]:
    team_manifest = b'{"schema":"autogen-team.v1","name":"support_triage"}\n'
    workflow = b'{"name":"support-triage","nodes":[]}\n'
    input_schema = b'{"type":"object","required":["ticket"]}\n'
    output_schema = b'{"type":"object","required":["route"]}\n'
    runner = (
        "import json, os\n"
        "print(json.dumps({'trace_id': os.environ['CAPTAIN_TRACE_ID'], "
        "'assertion_ids': ['schema_valid', 'real_case_green']}))\n"
    ).encode()
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("team_manifest.json", team_manifest)
        archive.writestr("workflows/support_triage.json", workflow)
        archive.writestr("schemas/support_triage.input.json", input_schema)
        archive.writestr("schemas/support_triage.output.json", output_schema)
        archive.writestr("run_case.py", runner)
    return (
        _ref("artifact://factory/team/support-triage", team_manifest),
        _ref("artifact://factory/workflow/support-triage", workflow),
        _ref("artifact://factory/schema/support-triage-input", input_schema),
        _ref("artifact://factory/schema/support-triage-output", output_schema),
        _ref("artifact://factory/source/support-triage", path.read_bytes(), "application/zip"),
    )


def test_evaluator_runs_a_sealed_candidate_in_a_temporary_workspace(tmp_path: Path) -> None:
    archive_path = tmp_path / "candidate.zip"
    team_ref, workflow_ref, input_schema_ref, output_schema_ref, source_ref = _write_candidate_archive(archive_path)
    candidate = FactoryCandidateManifest(
        candidate_id="support_triage_v1",
        source_archive_ref=source_ref,
        team_manifest={"reference": team_ref, "relative_path": "team_manifest.json"},
        workflow_artifacts=(
            {"reference": workflow_ref, "relative_path": "workflows/support_triage.json"},
        ),
        tool_schema_artifacts=(
            {"reference": input_schema_ref, "relative_path": "schemas/support_triage.input.json"},
            {"reference": output_schema_ref, "relative_path": "schemas/support_triage.output.json"},
        ),
        n8n_tools=(
            TypedN8nTool(
                name="support_triage",
                description="Route a support request through the approved workflow.",
                input_schema_ref=input_schema_ref.uri,
                output_schema_ref=output_schema_ref.uri,
            ),
        ),
        build_command=("python", "-m", "compileall", "-q", "."),
        real_case_command=("python", "run_case.py"),
        timeout_seconds=10,
    )

    result = FactoryCandidateEvaluator().evaluate(
        job=job(),
        candidate=candidate,
        source_archive=archive_path,
    )

    assert result.status == "succeeded"
    assert result.assertion_ids == ("schema_valid", "real_case_green")
    assert result.trace_id == str(job().correlation_id)
    assert result.workspace_was_temporary is True
    assert result.tool_names == ("support_triage",)
    assert all(check.status == "passed" for check in result.checks)


def test_evaluator_accepts_the_typed_skill_evaluation_request_context(tmp_path: Path) -> None:
    archive_path = tmp_path / "candidate.zip"
    team_ref, workflow_ref, input_schema_ref, output_schema_ref, source_ref = _write_candidate_archive(archive_path)
    candidate = FactoryCandidateManifest(
        candidate_id="support_triage_v1",
        source_archive_ref=source_ref,
        team_manifest={"reference": team_ref, "relative_path": "team_manifest.json"},
        workflow_artifacts=(({"reference": workflow_ref, "relative_path": "workflows/support_triage.json"}),),
        tool_schema_artifacts=(
            {"reference": input_schema_ref, "relative_path": "schemas/support_triage.input.json"},
            {"reference": output_schema_ref, "relative_path": "schemas/support_triage.output.json"},
        ),
        n8n_tools=(
            TypedN8nTool(
                name="support_triage",
                description="Route a support request.",
                input_schema_ref=input_schema_ref.uri,
                output_schema_ref=output_schema_ref.uri,
            ),
        ),
        build_command=("python", "-m", "compileall", "-q", "."),
        real_case_command=("python", "run_case.py"),
        timeout_seconds=10,
    )
    request = HermesSkillEvaluationRequest.model_validate(
        request_payload(candidate_source_ref=source_ref.model_dump(mode="json"))
    )

    result = FactoryCandidateEvaluator().evaluate_skill(
        request=request,
        candidate=candidate,
        source_archive=archive_path,
    )

    assert result.status == "succeeded"
    assert result.assertion_ids == request.acceptance_assertion_ids
    assert result.trace_id == str(request.correlation_id)


def test_skill_evaluator_bounds_candidate_commands_by_remaining_lease_time(tmp_path: Path) -> None:
    archive_path = tmp_path / "candidate.zip"
    team_ref, workflow_ref, input_schema_ref, output_schema_ref, source_ref = _write_candidate_archive(archive_path)
    candidate = FactoryCandidateManifest(
        candidate_id="support_triage_v1",
        source_archive_ref=source_ref,
        team_manifest={"reference": team_ref, "relative_path": "team_manifest.json"},
        workflow_artifacts=({"reference": workflow_ref, "relative_path": "workflows/support_triage.json"},),
        tool_schema_artifacts=(
            {"reference": input_schema_ref, "relative_path": "schemas/support_triage.input.json"},
            {"reference": output_schema_ref, "relative_path": "schemas/support_triage.output.json"},
        ),
        n8n_tools=(
            TypedN8nTool(
                name="support_triage",
                description="Route a support request.",
                input_schema_ref=input_schema_ref.uri,
                output_schema_ref=output_schema_ref.uri,
            ),
        ),
        build_command=("python", "-c", "import time; time.sleep(5)"),
        real_case_command=("python", "run_case.py"),
        timeout_seconds=10,
    )
    request = HermesSkillEvaluationRequest.model_validate(
        request_payload(candidate_source_ref=source_ref.model_dump(mode="json"))
    )

    result = FactoryCandidateEvaluator().evaluate_skill(
        request=request,
        candidate=candidate,
        source_archive=archive_path,
        max_seconds=0.05,
    )

    assert result.status == "failed"
    assert result.checks[-1].name == "validation"
    assert "timed out" in result.checks[-1].detail


@pytest.mark.parametrize(
    "real_case_code",
    [
        "print('not-json')",
        "import json; print(json.dumps({'trace_id': 'wrong', 'assertion_ids': ['schema_valid', 'real_case_green']}))",
        "import json, os; print(json.dumps({'trace_id': os.environ['CAPTAIN_TRACE_ID']}))",
    ],
)
def test_exit_zero_invalid_real_case_output_is_an_actual_real_case_failure(
    tmp_path: Path,
    real_case_code: str,
) -> None:
    archive_path = tmp_path / "candidate.zip"
    team_ref, workflow_ref, input_schema_ref, output_schema_ref, source_ref = _write_candidate_archive(archive_path)
    candidate = FactoryCandidateManifest(
        candidate_id="support_triage_v1",
        source_archive_ref=source_ref,
        team_manifest={"reference": team_ref, "relative_path": "team_manifest.json"},
        workflow_artifacts=({"reference": workflow_ref, "relative_path": "workflows/support_triage.json"},),
        tool_schema_artifacts=(
            {"reference": input_schema_ref, "relative_path": "schemas/support_triage.input.json"},
            {"reference": output_schema_ref, "relative_path": "schemas/support_triage.output.json"},
        ),
        n8n_tools=(
            TypedN8nTool(
                name="support_triage",
                description="Route a support request.",
                input_schema_ref=input_schema_ref.uri,
                output_schema_ref=output_schema_ref.uri,
            ),
        ),
        build_command=("python", "-m", "compileall", "-q", "."),
        real_case_command=("python", "-c", real_case_code),
        timeout_seconds=10,
    )
    request = HermesSkillEvaluationRequest.model_validate(
        request_payload(candidate_source_ref=source_ref.model_dump(mode="json"))
    )

    result = FactoryCandidateEvaluator().evaluate_skill(
        request=request,
        candidate=candidate,
        source_archive=archive_path,
    )

    assert result.status == "failed"
    assert result.checks[-1].name == "real_case"
    assert result.checks[-1].status == "failed"


def test_evaluator_timeout_terminates_the_verified_candidate_process_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agenten.agent_factory.candidate_evaluation as candidate_evaluation

    terminated: list[int] = []

    class Process:
        pid = 4343
        returncode = None
        args = (sys.executable, "-c", "pass")

        def __enter__(self) -> "Process":
            return self

        def __exit__(self, *_: object) -> None:
            pass

        def communicate(self, *_: object, timeout: float | None = None) -> tuple[str, str]:
            if self.returncode is None:
                raise subprocess.TimeoutExpired(self.args, timeout)
            return "", ""

        def kill(self) -> None:
            self.returncode = -9

        def poll(self) -> int | None:
            return self.returncode

    def terminate_tree(process: Process, *, executable: str) -> None:
        assert executable == sys.executable
        terminated.append(process.pid)
        process.returncode = -9

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(
        candidate_evaluation,
        "_terminate_sync_process_tree",
        terminate_tree,
        raising=False,
    )

    with pytest.raises(ValueError, match="timed out"):
        FactoryCandidateEvaluator._run(
            ("python", "-c", "pass"),
            tmp_path,
            "trace-1",
            0.01,
        )

    assert terminated == [4343]


def test_posix_evaluator_tree_cleanup_escalates_after_the_leader_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agenten.agent_factory.candidate_evaluation as candidate_evaluation

    signals: list[int] = []

    class Process:
        pid = 4444
        returncode = 0
        args = (sys.executable, "-c", "pass")

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            self.returncode = -signal.SIGTERM
            return self.returncode

    monkeypatch.setattr(
        candidate_evaluation,
        "os",
        SimpleNamespace(
            name="posix",
            path=candidate_evaluation.os.path,
            killpg=lambda _pid, sent: signals.append(sent),
        ),
    )

    candidate_evaluation._terminate_sync_process_tree(
        Process(),
        executable=sys.executable,
    )

    assert signals == [signal.SIGTERM, 9]


def test_evaluator_timeout_never_uses_an_unbounded_post_termination_wait(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agenten.agent_factory.candidate_evaluation as candidate_evaluation

    wait_timeouts: list[float | None] = []

    class Process:
        pid = 4848
        returncode = None
        args = (sys.executable, "-c", "pass")

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            wait_timeouts.append(timeout)
            if len(wait_timeouts) <= 2:
                raise subprocess.TimeoutExpired(self.args, timeout)
            return "", ""

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(
        candidate_evaluation,
        "_terminate_sync_process_tree",
        lambda process, *, executable: None,
    )

    with pytest.raises(ValueError, match="timed out"):
        FactoryCandidateEvaluator._run(
            ("python", "-c", "pass"),
            tmp_path,
            "trace-1",
            0.01,
        )

    assert wait_timeouts == [0.01, 5, 5]


@pytest.mark.asyncio
async def test_validator_persists_build_evidence_for_a_leased_candidate(tmp_path: Path) -> None:
    archive_path = tmp_path / "candidate.zip"
    team_ref, workflow_ref, input_schema_ref, output_schema_ref, source_ref = _write_candidate_archive(archive_path)
    candidate = FactoryCandidateManifest(
        candidate_id="support_triage_v1",
        source_archive_ref=source_ref,
        team_manifest={"reference": team_ref, "relative_path": "team_manifest.json"},
        workflow_artifacts=(({"reference": workflow_ref, "relative_path": "workflows/support_triage.json"}),),
        tool_schema_artifacts=(
            {"reference": input_schema_ref, "relative_path": "schemas/support_triage.input.json"},
            {"reference": output_schema_ref, "relative_path": "schemas/support_triage.output.json"},
        ),
        n8n_tools=(
            TypedN8nTool(
                name="support_triage",
                description="Route a support request through the approved workflow.",
                input_schema_ref=input_schema_ref.uri,
                output_schema_ref=output_schema_ref.uri,
            ),
        ),
        build_command=("python", "-m", "compileall", "-q", "."),
        real_case_command=("python", "run_case.py"),
        timeout_seconds=10,
    )
    factory_job = job()
    lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.TOOL_INTEGRATOR,
        attempt=1,
        workspace_ref="workspace://factory/support-triage",
        now=factory_job.occurred_at,
    )
    validator = CandidateEvaluationFactory(
        provider=StaticFactoryCandidateProvider(
            {factory_job.job_id: ResolvedFactoryCandidate(candidate=candidate, source_archive=archive_path)}
        ),
        evidence_store=FilesystemFactoryEvidenceStore(tmp_path / "evidence"),
    )

    block = await validator.dispatch(
        FactoryDispatch(
            job=factory_job,
            action=FactoryAction(kind=FactoryActionKind.DISPATCH_BUILD_VALIDATOR, attempt=1),
            role=FactoryRole.TOOL_INTEGRATOR,
            lease=lease,
        )
    )

    assert block.phase is FactoryPhase.BUILD_PASSED
    assert block.status.value == "succeeded"
    assert block.assertion_ids == ()
    assert block.evidence_refs[0].uri.startswith("artifact://factory-evidence/")


@pytest.mark.asyncio
async def test_validator_emits_agent_code_evidence_for_a_leased_forge_result(tmp_path: Path) -> None:
    archive_path = tmp_path / "candidate.zip"
    team_ref, workflow_ref, input_schema_ref, output_schema_ref, source_ref = _write_candidate_archive(archive_path)
    candidate = FactoryCandidateManifest(
        candidate_id="support_triage_v1",
        source_archive_ref=source_ref,
        team_manifest={"reference": team_ref, "relative_path": "team_manifest.json"},
        workflow_artifacts=({"reference": workflow_ref, "relative_path": "workflows/support_triage.json"},),
        tool_schema_artifacts=(
            {"reference": input_schema_ref, "relative_path": "schemas/support_triage.input.json"},
            {"reference": output_schema_ref, "relative_path": "schemas/support_triage.output.json"},
        ),
        n8n_tools=(TypedN8nTool(name="support_triage", description="Route a support request.", input_schema_ref=input_schema_ref.uri, output_schema_ref=output_schema_ref.uri),),
        build_command=("python", "-m", "compileall", "-q", "."),
        real_case_command=("python", "run_case.py"),
        timeout_seconds=10,
    )
    factory_job = job()
    lease = issue_factory_lease(job=factory_job, role=FactoryRole.TOOL_INTEGRATOR, attempt=1, workspace_ref="workspace://factory/support-triage", now=factory_job.occurred_at)
    validator = CandidateEvaluationFactory(
        provider=StaticFactoryCandidateProvider({factory_job.job_id: ResolvedFactoryCandidate(candidate=candidate, source_archive=archive_path)}),
        evidence_store=FilesystemFactoryEvidenceStore(tmp_path / "evidence"),
    )

    block = await validator.dispatch(
        FactoryDispatch(
            job=factory_job,
            action=FactoryAction(kind=FactoryActionKind.EMIT_AGENT_CODE_EVIDENCE, attempt=1),
            role=FactoryRole.TOOL_INTEGRATOR,
            lease=lease,
        )
    )

    assert block.phase is FactoryPhase.AGENT_CODE_CREATED
    assert block.status.value == "succeeded"
    assert block.assertion_ids == ()
