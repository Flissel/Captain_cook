from __future__ import annotations

import hashlib
import json
import signal
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4
import zipfile
from pathlib import Path

import pytest

from agenten.agent_factory.candidate_evaluation import (
    FactoryAutoGenTeamManifestV1,
    CandidateEvaluationFactory,
    FactoryCandidateEvaluator,
    FactoryCandidateManifest,
    ResolvedFactoryCandidate,
    StaticFactoryCandidateProvider,
)
from agenten.agent_factory.contracts import FactoryPhase, FactoryRole
from agenten.agent_factory.evidence_store import FilesystemFactoryEvidenceStore
from agenten.agent_factory.forge_contracts import CreationResultV1
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_factory.orchestration import FactoryDispatch, FactoryDispatchError
from agenten.agent_factory.state_machine import FactoryAction, FactoryActionKind
from agenten.agent_runtime.contracts import ArtifactRef
from agenten.agent_factory.n8n_tools import (
    TypedN8nCatalog,
    TypedN8nTool,
    resolve_tool_gap_n8n_option,
)
from agenten.agent_factory.skill_evaluation import (
    HermesSkillEvaluationRequest,
    ToolGapMarker,
    ToolImplementationOption,
)
from agenten.agent_runtime.contracts import IntegrationIntent
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


def test_autogen_manifest_defaults_to_swarm_for_specialist_handoffs() -> None:
    manifest = FactoryAutoGenTeamManifestV1.model_validate(
        {
            "schema": "autogen-team.v1",
            "name": "support_triage",
            "agents": [
                {
                    "name": "triage",
                    "tools": ["support_triage"],
                    "system_prompt_ref": _ref("artifact://factory/prompts/triage", b"triage"),
                    "handoffs": ["resolver"],
                },
                {
                    "name": "resolver",
                    "tools": [],
                    "system_prompt_ref": _ref("artifact://factory/prompts/resolver", b"resolver"),
                    "handoffs": [],
                },
            ],
            "memory_policy": "buffered",
            "max_messages": 20,
            "max_handoffs": 4,
            "max_tool_calls": 6,
            "termination_conditions": ["task_completed", "max_messages"],
            "entrypoint_command": ["python", "run_team.py"],
        },
        context={"allowed_tools": {"support_triage"}},
    )

    assert manifest.conversation_pattern == "swarm"


@pytest.mark.parametrize(
    ("agents", "message"),
    [
        (
            [
                {
                    "name": "triage",
                    "tools": ["unreleased_tool"],
                    "system_prompt_ref": _ref("artifact://factory/prompts/triage", b"triage"),
                    "handoffs": [],
                }
            ],
            "unknown tool",
        ),
        (
            [
                {
                    "name": "triage",
                    "tools": [],
                    "system_prompt_ref": _ref("artifact://factory/prompts/triage", b"triage"),
                    "handoffs": ["missing_agent"],
                }
            ],
            "unknown handoff",
        ),
    ],
)
def test_autogen_manifest_rejects_unknown_tools_and_handoffs(
    agents: list[dict[str, object]],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        FactoryAutoGenTeamManifestV1.model_validate(
            {
                "schema": "autogen-team.v1",
                "name": "support_triage",
                "conversation_pattern": "single_agent",
                "agents": agents,
                "memory_policy": "none",
                "max_messages": 10,
                "max_handoffs": 2,
                "max_tool_calls": 3,
                "termination_conditions": ["task_completed"],
                "entrypoint_command": ["python", "run_team.py"],
            },
            context={"allowed_tools": {"support_triage"}},
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


def test_tool_gap_option_resolves_only_through_an_approved_n8n_tool_lease(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "candidate.zip"
    _, _, input_schema_ref, output_schema_ref, source_ref = _write_candidate_archive(archive_path)
    tool = TypedN8nTool(
        name="support_triage",
        description="Route a support request through the approved workflow.",
        input_schema_ref=input_schema_ref.uri,
        output_schema_ref=output_schema_ref.uri,
    )
    marker = ToolGapMarker(
        schema_name="TODO_TOOL.v1",
        gap_id="support-triage-gap",
        severity="required",
        input_contract_ref=input_schema_ref,
        output_contract_ref=output_schema_ref,
        least_privilege_capability="mcp.n8n",
        implementation_options=(
            ToolImplementationOption(
                option_id="support_triage",
                description="Use the released support triage MCP tool.",
                acceptance_assertion_id="schema_valid",
            ),
        ),
        acceptance_assertion_ids=("schema_valid",),
        evidence_ref=source_ref,
        status="unresolved",
    )
    lease = issue_factory_lease(
        job=job(),
        role=FactoryRole.TOOL_INTEGRATOR,
        attempt=1,
        workspace_ref="workspace://factory/support-triage",
        now=job().occurred_at,
        integration_intent=IntegrationIntent.N8N,
    )

    reference = resolve_tool_gap_n8n_option(
        lease=lease,
        marker=marker,
        option=marker.implementation_options[0],
        catalog=TypedN8nCatalog((tool,)),
    )
    candidate = FactoryCandidateManifest(
        candidate_id="support_triage_v1",
        source_archive_ref=source_ref,
        team_manifest={
            "reference": _ref("artifact://factory/team/support-triage", b"{}"),
            "relative_path": "team_manifest.json",
        },
        workflow_artifacts=(
            {
                "reference": _ref("artifact://factory/workflow/support-triage", b"{}"),
                "relative_path": "workflows/support_triage.json",
            },
        ),
        tool_schema_artifacts=(
            {"reference": input_schema_ref, "relative_path": "schemas/support_triage.input.json"},
            {"reference": output_schema_ref, "relative_path": "schemas/support_triage.output.json"},
        ),
        n8n_tools=(tool,),
        n8n_tool_references=(reference,),
        build_command=("python", "-m", "compileall", "-q", "."),
        real_case_command=("python", "run_case.py"),
        timeout_seconds=10,
    )

    assert reference.model_dump() == {
        "schema_name": "captain.n8n-mcp-tool-reference.v1",
        "tool_name": "support_triage",
        "input_schema_ref": input_schema_ref.uri,
        "output_schema_ref": output_schema_ref.uri,
        "server_name": "n8n-mcp",
    }
    serialized = candidate.model_dump_json()
    assert "hidden-workflow-id" not in serialized
    assert "token" not in serialized.lower()
    assert "http://" not in serialized


@pytest.mark.parametrize(
    "lease",
    (
        issue_factory_lease(
            job=job(),
            role=FactoryRole.TOOL_INTEGRATOR,
            attempt=1,
            workspace_ref="workspace://factory/support-triage",
            now=job().occurred_at,
        ),
        issue_factory_lease(
            job=job(),
            role=FactoryRole.AGENT_ARCHITECT,
            attempt=1,
            workspace_ref="workspace://factory/support-triage",
            now=job().occurred_at,
        ),
        issue_factory_lease(
            job=job(),
            role=FactoryRole.TOOL_INTEGRATOR,
            attempt=1,
            workspace_ref="workspace://factory/support-triage",
            now=job().occurred_at,
            integration_intent=IntegrationIntent.N8N,
        ).model_copy(update={"capabilities": ("codex.run",)}),
    ),
)
def test_tool_gap_option_rejects_unapproved_or_nonintegrator_leases(lease: object) -> None:
    input_schema_ref = _ref("artifact://factory/schema/support-triage-input", b"{}")
    output_schema_ref = _ref("artifact://factory/schema/support-triage-output", b"{}")
    marker = ToolGapMarker(
        schema_name="TODO_TOOL.v1",
        gap_id="support-triage-gap",
        severity="required",
        input_contract_ref=input_schema_ref,
        output_contract_ref=output_schema_ref,
        least_privilege_capability="mcp.n8n",
        implementation_options=(ToolImplementationOption(option_id="support_triage", description="Use tool.", acceptance_assertion_id="schema_valid"),),
        acceptance_assertion_ids=("schema_valid",),
        evidence_ref=_ref("artifact://factory/evidence/support-triage", b"{}"),
        status="unresolved",
    )
    catalog = TypedN8nCatalog((TypedN8nTool(name="support_triage", description="Route support.", input_schema_ref=input_schema_ref.uri, output_schema_ref=output_schema_ref.uri),))

    with pytest.raises(PermissionError, match="Captain-issued n8n lease"):
        resolve_tool_gap_n8n_option(
            lease=lease,
            marker=marker,
            option=marker.implementation_options[0],
            catalog=catalog,
        )


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
async def test_real_case_dispatch_never_accepts_candidate_owned_offline_evaluator(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "candidate.zip"
    team_ref, workflow_ref, input_ref, output_ref, source_ref = _write_candidate_archive(
        archive_path
    )
    candidate = FactoryCandidateManifest(
        candidate_id="support_triage_v1",
        source_archive_ref=source_ref,
        team_manifest={"reference": team_ref, "relative_path": "team_manifest.json"},
        workflow_artifacts=(
            {
                "reference": workflow_ref,
                "relative_path": "workflows/support_triage.json",
            },
        ),
        tool_schema_artifacts=(
            {
                "reference": input_ref,
                "relative_path": "schemas/support_triage.input.json",
            },
            {
                "reference": output_ref,
                "relative_path": "schemas/support_triage.output.json",
            },
        ),
        n8n_tools=(
            TypedN8nTool(
                name="support_triage",
                description="Route support.",
                input_schema_ref=input_ref.uri,
                output_schema_ref=output_ref.uri,
            ),
        ),
        build_command=("python", "-m", "compileall", "-q", "."),
        real_case_command=("python", "run_case.py"),
        timeout_seconds=10,
    )
    factory_job = job()
    lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.REAL_CASE_TESTER,
        attempt=1,
        workspace_ref="workspace://factory/support-triage",
        now=factory_job.occurred_at,
    )
    validator = CandidateEvaluationFactory(
        provider=StaticFactoryCandidateProvider(
            {
                factory_job.job_id: ResolvedFactoryCandidate(
                    candidate=candidate,
                    source_archive=archive_path,
                )
            }
        ),
        evidence_store=FilesystemFactoryEvidenceStore(tmp_path / "evidence"),
    )

    with pytest.raises(FactoryDispatchError, match="TeamExecutionService"):
        await validator.dispatch(
            FactoryDispatch(
                job=factory_job,
                action=FactoryAction(
                    kind=FactoryActionKind.DISPATCH_REAL_CASE_TESTER,
                    attempt=1,
                ),
                role=FactoryRole.REAL_CASE_TESTER,
                lease=lease,
            )
        )

    class RoutedThroughTeamExecution(RuntimeError):
        pass

    class TeamExecution:
        def invocation_for(self, request: FactoryDispatch) -> object:
            assert request.lease == lease
            return object()

        async def execute(
            self,
            request: FactoryDispatch,
            resolved: ResolvedFactoryCandidate,
        ) -> object:
            assert request.action.kind is FactoryActionKind.DISPATCH_REAL_CASE_TESTER
            assert resolved.candidate == candidate
            raise RoutedThroughTeamExecution

    routed = CandidateEvaluationFactory(
        provider=StaticFactoryCandidateProvider(
            {
                factory_job.job_id: ResolvedFactoryCandidate(
                    candidate=candidate,
                    source_archive=archive_path,
                )
            }
        ),
        evidence_store=FilesystemFactoryEvidenceStore(tmp_path / "routed-evidence"),
        team_execution=TeamExecution(),  # type: ignore[arg-type]
    )

    with pytest.raises(RoutedThroughTeamExecution):
        await routed.dispatch(
            FactoryDispatch(
                job=factory_job,
                action=FactoryAction(
                    kind=FactoryActionKind.DISPATCH_REAL_CASE_TESTER,
                    attempt=1,
                ),
                role=FactoryRole.REAL_CASE_TESTER,
                lease=lease,
            )
        )


@pytest.mark.asyncio
async def test_validator_rejects_agent_code_evidence_without_exact_forge_result(tmp_path: Path) -> None:
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

    with pytest.raises(FactoryDispatchError, match="CreationResultV1"):
        await validator.dispatch(
            FactoryDispatch(
                job=factory_job,
                action=FactoryAction(kind=FactoryActionKind.EMIT_AGENT_CODE_EVIDENCE, attempt=1),
                role=FactoryRole.TOOL_INTEGRATOR,
                lease=lease,
            )
        )


@pytest.mark.asyncio
async def test_validator_records_digest_bound_agent_code_from_exact_forge_result(tmp_path: Path) -> None:
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
    evidence_store = FilesystemFactoryEvidenceStore(tmp_path / "evidence")
    validator = CandidateEvaluationFactory(
        provider=StaticFactoryCandidateProvider({factory_job.job_id: ResolvedFactoryCandidate(candidate=candidate, source_archive=archive_path)}),
        evidence_store=evidence_store,
    )
    package_manifest_ref = _ref("artifact://factory/package-manifest", b"package manifest")
    skill_receipt_ref = _ref("artifact://factory/skill-usage", b"skill usage")
    forge_evidence_ref = _ref("artifact://factory/forge-evidence", b"forge evidence")
    result = CreationResultV1(
        creation_job_id=uuid4(),
        correlation_id=factory_job.correlation_id,
        subject_version=factory_job.subject_version,
        attempt=1,
        status="succeeded",
        package_manifest_ref=package_manifest_ref.model_dump(mode="json"),
        artifact_refs=(source_ref.model_dump(mode="json"),),
        evidence_refs=(forge_evidence_ref.model_dump(mode="json"),),
        skill_usage_receipt_ref=skill_receipt_ref.model_dump(mode="json"),
    )
    request = FactoryDispatch(
        job=factory_job,
        action=FactoryAction(kind=FactoryActionKind.EMIT_AGENT_CODE_EVIDENCE, attempt=1),
        role=FactoryRole.TOOL_INTEGRATOR,
        lease=lease,
    )

    code_block = await validator.record_creation_result(request, result)
    replayed_block = await validator.record_creation_result(request, result)

    assert code_block.phase is FactoryPhase.AGENT_CODE_CREATED
    assert code_block.status.value == "succeeded"
    assert code_block.artifact_refs == (package_manifest_ref, source_ref)
    assert code_block.evidence_refs[1:] == (forge_evidence_ref, skill_receipt_ref)
    expected_content = json.dumps(
        result.model_dump(mode="json", by_alias=True, exclude_none=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    assert await evidence_store.read(code_block.evidence_refs[0]) == expected_content
    assert replayed_block.event_id == code_block.event_id
    assert replayed_block.evidence_refs == code_block.evidence_refs


@pytest.mark.asyncio
async def test_validator_rejects_unbound_direct_forge_result(tmp_path: Path) -> None:
    factory_job = job()
    lease = issue_factory_lease(job=factory_job, role=FactoryRole.TOOL_INTEGRATOR, attempt=1, workspace_ref="workspace://factory/support-triage", now=factory_job.occurred_at)
    validator = CandidateEvaluationFactory(
        provider=StaticFactoryCandidateProvider({}),
        evidence_store=FilesystemFactoryEvidenceStore(tmp_path / "evidence"),
    )
    result = CreationResultV1(
        creation_job_id=uuid4(),
        correlation_id=uuid4(),
        subject_version=factory_job.subject_version,
        attempt=1,
        status="succeeded",
        package_manifest_ref=_ref("artifact://factory/package-manifest", b"package manifest").model_dump(mode="json"),
        artifact_refs=(
            _ref("artifact://factory/generated-code", b"generated code", "application/zip").model_dump(mode="json"),
        ),
        skill_usage_receipt_ref=_ref("artifact://factory/skill-usage", b"skill usage").model_dump(mode="json"),
    )

    with pytest.raises(FactoryDispatchError, match="does not match"):
        await validator.record_creation_result(
            FactoryDispatch(
                job=factory_job,
                action=FactoryAction(kind=FactoryActionKind.EMIT_AGENT_CODE_EVIDENCE, attempt=1),
                role=FactoryRole.TOOL_INTEGRATOR,
                lease=lease,
            ),
            result,
        )


@pytest.mark.asyncio
async def test_validator_rejects_artifactless_successful_forge_result(tmp_path: Path) -> None:
    factory_job = job()
    lease = issue_factory_lease(job=factory_job, role=FactoryRole.TOOL_INTEGRATOR, attempt=1, workspace_ref="workspace://factory/support-triage", now=factory_job.occurred_at)
    validator = CandidateEvaluationFactory(
        provider=StaticFactoryCandidateProvider({}),
        evidence_store=FilesystemFactoryEvidenceStore(tmp_path / "evidence"),
    )
    result = CreationResultV1(
        creation_job_id=uuid4(),
        correlation_id=factory_job.correlation_id,
        subject_version=factory_job.subject_version,
        attempt=1,
        status="succeeded",
        package_manifest_ref=_ref("artifact://factory/package-manifest", b"package manifest").model_dump(mode="json"),
        artifact_refs=(),
        skill_usage_receipt_ref=_ref("artifact://factory/skill-usage", b"skill usage").model_dump(mode="json"),
    )

    with pytest.raises(FactoryDispatchError, match="code artifacts"):
        await validator.record_creation_result(
            FactoryDispatch(
                job=factory_job,
                action=FactoryAction(kind=FactoryActionKind.EMIT_AGENT_CODE_EVIDENCE, attempt=1),
                role=FactoryRole.TOOL_INTEGRATOR,
                lease=lease,
            ),
            result,
        )
