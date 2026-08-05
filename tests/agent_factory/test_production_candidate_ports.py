from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from agenten.agent_factory.candidate_evaluation import FactoryCandidateManifest
from agenten.agent_factory.capability_live_adapters import ContentAddressedArtifactStore
from agenten.agent_factory.capability_v3_evidence_bridge import build_v3_job_from_package_c
from agenten.agent_factory.execution_policy import FactoryExecutionPolicyV1
from agenten.agent_factory.n8n_tools import TypedN8nTool
from agenten.agent_factory.outcome_contracts import ForgeCapabilityPackageCandidateV1
from agenten.agent_factory.outcome_validation import (
    CapabilitySandboxRequest,
    CapabilitySandboxResult,
)
from agenten.agent_factory.production_candidate_ports import (
    FactoryCandidateExecutionDescriptorV1,
    ProductionCandidatePortError,
    build_production_candidate_ports,
    candidate_ref_for_job,
)
from agenten.agent_runtime.contracts import ArtifactRef
from minibook.swarm.package_assembler import PackageAssembler


NOW = datetime(2026, 7, 21, 19, 0, tzinfo=timezone.utc)
IMAGE = "captain-capability-sandbox@sha256:" + "a" * 64


def _v2_job():
    from agenten.agent_factory.contracts import AgentFactoryJobV2

    return AgentFactoryJobV2.model_validate_json(
        Path("tests/fixtures/agent_factory/agent_factory_job.v2.json").read_text(
            encoding="utf-8"
        )
    )


def _v3_job():
    return build_v3_job_from_package_c(
        _v2_job(),
        FactoryExecutionPolicyV1.model_validate(
            {
                "schema": "captain.factory-execution-policy.v1",
                "mode": "release",
                "live_execution": True,
                "max_cost_usd": "10.00",
                "max_runtime_seconds": 900,
                "required_live_runs": 3,
                "allowed_models": ["gpt-5.2"],
                "live_capabilities": ["model.invoke", "docker.run"],
                "sandbox_mode": "isolated_danger_full_access",
            }
        ),
    )


def _zip(files: dict[str, bytes], *, unsafe_name: str | None = None) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(files.items()):
            archive.writestr(name, content)
        if unsafe_name is not None:
            archive.writestr(unsafe_name, b"escape")
    return stream.getvalue()


def _case(
    tmp_path: Path,
    *,
    unsafe_name: str | None = None,
    include_descriptor: bool = True,
    marker: str = "ready",
) -> tuple[
    ContentAddressedArtifactStore,
    ForgeCapabilityPackageCandidateV1,
    FactoryCandidateManifest,
]:
    store = ContentAddressedArtifactStore(tmp_path / "cas")
    raw: dict[str, tuple[str, bytes]] = {
        "team-manifest.json": ("team_manifest", b'{"schema":"package-team.v1"}'),
        "autogen/team.py": (
            "autogen_source",
            f"TEAM = {marker!r}\n".encode("utf-8"),
        ),
        "skills/demo/SKILL.md": ("skill", b"# Demo skill\n"),
        "tests/test_team.py": ("test", b"def test_team(): assert True\n"),
        "evidence/summary.json": ("evidence", b'{"status":"candidate"}'),
        "RUNBOOK.md": ("runbook", b"Run the candidate.\n"),
        "adapters/system-prompt.md": ("local_adapter", b"Complete the task safely.\n"),
        "adapters/input.json": ("local_adapter", b'{"type":"object","title":"in"}'),
        "adapters/output.json": ("local_adapter", b'{"type":"object","title":"out"}'),
        "n8n/workflow.json": ("n8n_workflow", b'{"name":"demo-flow","nodes":[]}'),
    }
    refs: dict[str, ArtifactRef] = {
        path: store.put(content, _media_type(path), namespace="package-file")
        for path, (_, content) in raw.items()
    }
    execution_manifest = json.dumps(
        {
            "schema": "autogen-team.v1",
            "name": "demo_team",
            "conversation_pattern": "single_agent",
            "agents": [
                {
                    "name": "worker",
                    "tools": ["demo_tool"],
                    "system_prompt_ref": refs["adapters/system-prompt.md"].model_dump(
                        mode="json"
                    ),
                    "handoffs": [],
                }
            ],
            "memory_policy": "bounded",
            "max_messages": 8,
            "max_handoffs": 0,
            "max_tool_calls": 4,
            "termination_conditions": ["task_completed", "max_messages"],
            "entrypoint_command": ["python", "autogen/team.py"],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    raw["adapters/execution-team.json"] = ("local_adapter", execution_manifest)
    refs["adapters/execution-team.json"] = store.put(
        execution_manifest,
        "application/json",
        namespace="package-file",
    )
    descriptor = FactoryCandidateExecutionDescriptorV1(
        candidate_id="demo_candidate",
        team_manifest={
            "reference": refs["adapters/execution-team.json"],
            "relative_path": "adapters/execution-team.json",
        },
        workflow_artifacts=(
            {
                "reference": refs["n8n/workflow.json"],
                "relative_path": "n8n/workflow.json",
            },
        ),
        tool_schema_artifacts=(
            {
                "reference": refs["adapters/input.json"],
                "relative_path": "adapters/input.json",
            },
            {
                "reference": refs["adapters/output.json"],
                "relative_path": "adapters/output.json",
            },
        ),
        n8n_tools=(
            TypedN8nTool(
                name="demo_tool",
                description="Run the sealed demo workflow",
                input_schema_ref=refs["adapters/input.json"].uri,
                output_schema_ref=refs["adapters/output.json"].uri,
            ),
        ),
        build_command=("python", "-m", "compileall", "autogen"),
        real_case_command=("python", "autogen/team.py"),
        timeout_seconds=30,
    )
    if include_descriptor:
        descriptor_bytes = descriptor.model_dump_json(by_alias=True).encode("utf-8")
        raw["adapters/factory-candidate.json"] = (
            "local_adapter",
            descriptor_bytes,
        )
        refs["adapters/factory-candidate.json"] = store.put(
            descriptor_bytes,
            "application/json",
            namespace="package-file",
        )
    files = {path: content for path, (_, content) in raw.items()}
    archive = _zip(files, unsafe_name=unsafe_name)
    source_ref = store.put(archive, "application/zip", namespace="package-archive")
    job = _v2_job()
    candidate = ForgeCapabilityPackageCandidateV1.model_validate(
        {
            "schema": "forge.capability-package-candidate.v1",
            "capability_id": job.required_capability,
            "capability_version": 1,
            "factory_job_id": str(job.job_id),
            "creation_job_id": "11111111-1111-4111-8111-111111111111",
            "correlation_id": str(job.correlation_id),
            "subject_version": job.subject_version,
            "attempt": 1,
            "source_ref": source_ref,
            "team_manifest_ref": refs["team-manifest.json"],
            "artifacts": [
                {"path": path, "kind": kind, "reference": refs[path]}
                for path, (kind, _) in raw.items()
            ],
            "skill_usage_receipt_ref": store.put(
                b'{"usage":"paid"}', "application/json", namespace="skill-usage"
            ),
            "tool_gaps": [],
            "runbook_ref": refs["RUNBOOK.md"],
        }
    )
    manifest = FactoryCandidateManifest(
        source_archive_ref=source_ref,
        **descriptor.model_dump(mode="python", exclude={"schema_name"}),
    )
    return store, candidate, manifest


def _other_v3_job():
    from agenten.agent_factory.contracts import AgentFactoryJobV3

    job = _v3_job()
    input_digest = hashlib.sha256(b"second canonical input").hexdigest()
    payload = job.model_dump(mode="python", by_alias=True)
    payload.update(
        {
            "event_id": uuid5(NAMESPACE_URL, "captain-demo-event-two"),
            "correlation_id": uuid5(NAMESPACE_URL, "captain-demo-correlation-two"),
            "causation_id": uuid5(NAMESPACE_URL, "captain-demo-causation-two"),
            "job_id": uuid5(NAMESPACE_URL, "captain-demo-job-two"),
            "input_ref": ArtifactRef(
                uri=(
                    "artifact://capability-factory/canonical-input/"
                    f"{input_digest}"
                ),
                sha256=input_digest,
                media_type="text/markdown",
            ),
        }
    )
    return AgentFactoryJobV3.model_validate(payload)


def _media_type(path: str) -> str:
    if path.endswith(".json"):
        return "application/json"
    if path.endswith(".py"):
        return "text/x-python"
    return "text/markdown"


@dataclass
class RecordingSandbox:
    requests: list[CapabilitySandboxRequest] = field(default_factory=list)
    status: str = "passed"

    async def validate(self, request: CapabilitySandboxRequest) -> CapabilitySandboxResult:
        assert request.workspace.is_dir()
        assert request.workspace_access == "read_only"
        assert request.network_access == "disabled"
        self.requests.append(request)
        return CapabilitySandboxResult(
            execution_id=request.execution_id,
            request_digest=request.request_digest,
            status=self.status,
            failure_stage=None if self.status == "passed" else "test",
            imported_modules=request.module_names,
            executed_test_paths=request.test_paths,
            sandbox_identity="sandbox://docker/" + "b" * 64,
            process_identity=request.process_identity,
            process_identity_verified=True,
            extracted_tree_sha256=request.extracted_tree_sha256,
            workspace_was_read_only=True,
            network_was_disabled=True,
            resource_limits_were_enforced=True,
            process_tree_termination_capable=True,
        )

    async def cancel(self, _execution_id):
        raise AssertionError("sandbox must not time out")

    async def await_termination(self, _execution_id):
        raise AssertionError("sandbox must not time out")


@pytest.mark.asyncio
async def test_ports_resolve_shared_cas_candidate_and_cache_real_sandbox_attestation(
    tmp_path: Path,
) -> None:
    store, candidate, manifest = _case(tmp_path)
    sandbox = RecordingSandbox()
    ports = build_production_candidate_ports(
        artifacts=store,
        sandbox_image=IMAGE,
        sandbox_runner=sandbox,
        clock=lambda: NOW,
    )
    job = _v3_job()
    assert store.binding("factory-candidate-manifest", candidate.source_ref.sha256) is None

    resolved = ports.candidate_provider.candidate_for(job, candidate)
    first = await ports.candidate_attestation.attest(job, resolved, candidate)
    replay = await ports.candidate_attestation.attest(job, resolved, candidate)

    assert resolved.candidate == manifest
    assert store.binding(
        "factory-candidate-manifest", candidate.source_ref.sha256
    ) is not None
    assert resolved.source_archive.read_bytes() == store.read_bytes(candidate.source_ref)
    assert first == replay
    assert first.candidate_ref == candidate.source_ref
    assert len(sandbox.requests) == 1
    request = sandbox.requests[0]
    assert request.module_names == ("autogen.team",)
    assert request.test_paths == ("tests/test_team.py",)
    assert request.timeout_seconds == manifest.timeout_seconds
    evidence = json.loads(store.read_bytes(first.sandbox_evidence_ref))
    assert evidence["sandbox_image"] == IMAGE
    assert evidence["network_was_disabled"] is True
    assert evidence["workspace_was_read_only"] is True


def test_provider_write_once_binds_two_jobs_to_their_distinct_inputs(
    tmp_path: Path,
) -> None:
    store, first_candidate, _ = _case(tmp_path, marker="first-input")
    _, second_candidate, _ = _case(tmp_path, marker="second-input")
    first_job = _v3_job()
    second_job = _other_v3_job()
    second_candidate = second_candidate.model_copy(
        update={"correlation_id": second_job.correlation_id}
    )
    ports = build_production_candidate_ports(
        artifacts=store,
        sandbox_image=IMAGE,
        sandbox_runner=RecordingSandbox(),
        clock=lambda: NOW,
    )

    first = ports.candidate_provider.candidate_for(first_job, first_candidate)
    second = ports.candidate_provider.candidate_for(second_job, second_candidate)
    replay = ports.candidate_provider.candidate_for(first_job, first_candidate)

    assert first.candidate.source_archive_ref != second.candidate.source_archive_ref
    assert replay == first
    assert candidate_ref_for_job(store, first_job) == first_candidate.source_ref
    assert candidate_ref_for_job(store, second_job) == second_candidate.source_ref


def test_provider_rejects_a_different_input_for_an_already_bound_job(
    tmp_path: Path,
) -> None:
    store, first_candidate, _ = _case(tmp_path, marker="first-input")
    _, replacement_candidate, _ = _case(tmp_path, marker="replacement-input")
    job = _v3_job()
    ports = build_production_candidate_ports(
        artifacts=store,
        sandbox_image=IMAGE,
        sandbox_runner=RecordingSandbox(),
        clock=lambda: NOW,
    )
    ports.candidate_provider.candidate_for(job, first_candidate)

    with pytest.raises(ProductionCandidatePortError, match="different input"):
        ports.candidate_provider.candidate_for(job, replacement_candidate)

    assert candidate_ref_for_job(store, job) == first_candidate.source_ref


def test_provider_rejects_archive_member_outside_sealed_package(tmp_path: Path) -> None:
    store, candidate, _ = _case(tmp_path, unsafe_name="../escape.py")
    ports = build_production_candidate_ports(
        artifacts=store,
        sandbox_image=IMAGE,
        sandbox_runner=RecordingSandbox(),
        clock=lambda: NOW,
    )

    with pytest.raises(ProductionCandidatePortError, match="unsafe path"):
        ports.candidate_provider.candidate_for(_v3_job(), candidate)


def test_provider_requires_content_addressed_candidate_manifest_binding(
    tmp_path: Path,
) -> None:
    store, candidate, _ = _case(tmp_path, include_descriptor=False)
    ports = build_production_candidate_ports(
        artifacts=store,
        sandbox_image=IMAGE,
        sandbox_runner=RecordingSandbox(),
        clock=lambda: NOW,
    )

    with pytest.raises(ProductionCandidatePortError, match="TODO_TOOL.v1"):
        ports.candidate_provider.candidate_for(_v3_job(), candidate)


@pytest.mark.asyncio
async def test_failed_sandbox_result_never_becomes_attestation(tmp_path: Path) -> None:
    store, candidate, _ = _case(tmp_path)
    ports = build_production_candidate_ports(
        artifacts=store,
        sandbox_image=IMAGE,
        sandbox_runner=RecordingSandbox(status="failed"),
        clock=lambda: NOW,
    )
    job = _v3_job()
    resolved = ports.candidate_provider.candidate_for(job, candidate)

    with pytest.raises(ProductionCandidatePortError, match="did not pass"):
        await ports.candidate_attestation.attest(job, resolved, candidate)

    assert store.binding("candidate-sandbox-attestation", str(job.job_id)) is None


def test_builder_rejects_unpinned_or_non_captain_image(tmp_path: Path) -> None:
    store, _, _ = _case(tmp_path)

    with pytest.raises(ProductionCandidatePortError, match="digest-pinned"):
        build_production_candidate_ports(
            artifacts=store,
            sandbox_image="python:3.11",
            sandbox_runner=RecordingSandbox(),
            clock=lambda: NOW,
        )


@pytest.mark.asyncio
async def test_minibook_export_to_zip_to_provider_to_attestation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "live-export"
    for directory in (
        "autogen",
        "skills/demo",
        "tests",
        "evidence",
        "agents/worker",
        "n8n",
        "adapters/schemas",
    ):
        (source / directory).mkdir(parents=True)
    exported_files = {
        "autogen/main.py": "TEAM = 'ready'\n",
        "skills/demo/SKILL.md": "# Demo\n",
        "tests/test_team.py": "def test_team(): assert True\n",
        "evidence/summary.json": '{"status":"candidate"}',
        "RUNBOOK.md": "Run the team.\n",
        "agents/worker/agent.yml": (
            "name: worker\nrole: worker\nmodel: gpt-5.2\n"
            "system_message: Execute the approved workflow.\n"
            "tools:\n  - customer_sync\nhandoffs: []\n"
        ),
        "project.yml": "name: Live Demo\npattern: single_agent\n",
        "n8n/customer_sync.json": '{"name":"customer-sync","nodes":[]}',
        "adapters/schemas/customer_sync.input.json": (
            '{"type":"object","title":"input"}'
        ),
        "adapters/schemas/customer_sync.output.json": (
            '{"type":"object","title":"output"}'
        ),
    }
    for relative, content in exported_files.items():
        (source / relative).write_text(content, encoding="utf-8")
    contract = {
        "workflow": "n8n/customer_sync.json",
        "input_schema": "adapters/schemas/customer_sync.input.json",
        "output_schema": "adapters/schemas/customer_sync.output.json",
        "idempotency": "correlation_id",
        "timeout": 30,
        "retry": "bounded",
        "duplicate": "reject",
        "failure": "fail_closed",
        "compensation": "none",
    }
    assembled = PackageAssembler().assemble(
        source,
        tmp_path / "candidate.zip",
        startup_command=("python", "autogen/main.py"),
        integration_contracts=(contract,),
        capability_id=_v2_job().required_capability,
        capability_version=1,
    )
    store = ContentAddressedArtifactStore(tmp_path / "shared-cas")
    references: dict[str, ArtifactRef] = {}
    with zipfile.ZipFile(assembled.archive_path) as archive:
        for item in assembled.artifacts:
            reference = store.put(
                archive.read(item.path), item.media_type, namespace="candidate-file"
            )
            assert reference.uri == item.uri
            assert reference.sha256 == item.sha256
            references[item.path] = reference
    source_ref = store.put(
        assembled.archive_path.read_bytes(),
        "application/zip",
        namespace="package-archive",
    )
    job = _v2_job()
    candidate = ForgeCapabilityPackageCandidateV1.model_validate(
        {
            "schema": "forge.capability-package-candidate.v1",
            "capability_id": job.required_capability,
            "capability_version": 1,
            "factory_job_id": str(job.job_id),
            "creation_job_id": "11111111-1111-4111-8111-111111111111",
            "correlation_id": str(job.correlation_id),
            "subject_version": job.subject_version,
            "attempt": 1,
            "source_ref": source_ref,
            "team_manifest_ref": references["team-manifest.json"],
            "artifacts": [
                {
                    "path": item.path,
                    "kind": item.kind,
                    "reference": references[item.path],
                }
                for item in assembled.artifacts
            ],
            "skill_usage_receipt_ref": store.put(
                b'{"usage":"paid"}', "application/json", namespace="skill-usage"
            ),
            "tool_gaps": [],
            "runbook_ref": references["RUNBOOK.md"],
        }
    )
    sandbox = RecordingSandbox()
    ports = build_production_candidate_ports(
        artifacts=store,
        sandbox_image=IMAGE,
        sandbox_runner=sandbox,
        clock=lambda: NOW,
    )
    v3 = _v3_job()

    resolved = ports.candidate_provider.candidate_for(v3, candidate)
    attestation = await ports.candidate_attestation.attest(v3, resolved, candidate)

    assert resolved.candidate.source_archive_ref == source_ref
    assert resolved.candidate.n8n_tools[0].name == "customer_sync"
    assert attestation.candidate_ref == source_ref
    assert len(sandbox.requests) == 1
