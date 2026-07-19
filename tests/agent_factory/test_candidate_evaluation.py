from __future__ import annotations

import hashlib
import json
import sys
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
from tests.agent_factory.test_state_machine import job


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
