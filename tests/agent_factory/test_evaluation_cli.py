from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from agenten.agent_factory.candidate_evaluation import FactoryCandidateManifest
from agenten.agent_factory.evaluation_cli import main
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_factory.contracts import FactoryRole
from agenten.agent_factory.n8n_tools import TypedN8nTool
from agenten.agent_factory.state_machine import FactoryActionKind
from agenten.agent_runtime.contracts import ArtifactRef
from tests.agent_factory.test_state_machine import job


def _ref(uri: str, content: bytes, media_type: str = "application/json") -> ArtifactRef:
    return ArtifactRef(uri=uri, sha256=hashlib.sha256(content).hexdigest(), media_type=media_type)


def test_cli_emits_leased_build_block_and_persists_its_evidence(tmp_path: Path, capsys) -> None:
    team_manifest = b'{"schema":"autogen-team.v1"}\n'
    workflow = b'{"nodes":[]}\n'
    input_schema = b'{"type":"object"}\n'
    output_schema = b'{"type":"object"}\n'
    runner = b"import json, os\nprint(json.dumps({'trace_id': os.environ['CAPTAIN_TRACE_ID'], 'assertion_ids': ['schema_valid', 'real_case_green']}))\n"
    source = tmp_path / "candidate.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("team.json", team_manifest)
        archive.writestr("workflow.json", workflow)
        archive.writestr("input.schema.json", input_schema)
        archive.writestr("output.schema.json", output_schema)
        archive.writestr("run.py", runner)
    candidate = FactoryCandidateManifest(
        candidate_id="support_triage_v1",
        source_archive_ref=_ref("artifact://factory/source/support-triage", source.read_bytes(), "application/zip"),
        team_manifest={"reference": _ref("artifact://factory/team/support-triage", team_manifest), "relative_path": "team.json"},
        workflow_artifacts=({"reference": _ref("artifact://factory/workflow/support-triage", workflow), "relative_path": "workflow.json"},),
        tool_schema_artifacts=(
            {"reference": _ref("artifact://factory/schema/input", input_schema), "relative_path": "input.schema.json"},
            {"reference": _ref("artifact://factory/schema/output", output_schema), "relative_path": "output.schema.json"},
        ),
        n8n_tools=(TypedN8nTool(name="support_triage", description="Route a support request.", input_schema_ref="artifact://factory/schema/input", output_schema_ref="artifact://factory/schema/output"),),
        build_command=("python", "-m", "compileall", "-q", "."),
        real_case_command=("python", "run.py"),
        timeout_seconds=10,
    )
    factory_job = job()
    lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.TOOL_INTEGRATOR,
        attempt=1,
        workspace_ref="workspace://factory/support-triage",
        now=datetime.now(timezone.utc),
    )
    job_path, lease_path, candidate_path = (tmp_path / "job.json", tmp_path / "lease.json", tmp_path / "candidate.json")
    job_path.write_text(factory_job.model_dump_json(by_alias=True), encoding="utf-8")
    lease_path.write_text(lease.model_dump_json(by_alias=True), encoding="utf-8")
    candidate_path.write_text(candidate.model_dump_json(by_alias=True), encoding="utf-8")

    exit_code = main([
        "--job", str(job_path), "--lease", str(lease_path), "--candidate", str(candidate_path),
        "--source-archive", str(source), "--action", FactoryActionKind.DISPATCH_BUILD_VALIDATOR.value,
        "--evidence-root", str(tmp_path / "evidence"),
    ])

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["phase"] == "build_passed"
    assert output["status"] == "succeeded"
    assert (tmp_path / "evidence" / str(factory_job.job_id)).is_dir()
