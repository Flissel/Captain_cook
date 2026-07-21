from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from minibook.swarm.contracts import CreationJobV1
from agenten.agent_factory.capability_live_adapters import ContentAddressedArtifactStore
from agenten.agent_factory.outcome_contracts import ForgeCapabilityPackageCandidateV1
from agenten.agent_runtime.contracts import ArtifactRef as CaptainArtifactRef
from minibook.swarm.contracts import ArtifactRef as MinibookArtifactRef
from minibook.swarm.pipeline_adapter import (
    ContentAddressedCreationArtifacts,
    ExportArtifactSnapshotter,
    PIPELINE_STEP_ORDER,
    SwarmPipelineAdapter,
    SwarmSnapshot,
    SwarmStep,
    translate_creation_failure,
)


FIXTURE = Path(__file__).parents[2] / "tests/fixtures/contracts/minibook_creation_job.v1.json"


def job() -> CreationJobV1:
    return CreationJobV1.model_validate_json(FIXTURE.read_text(encoding="utf-8"))


def test_pipeline_step_order_freezes_existing_conditional_flow() -> None:
    assert PIPELINE_STEP_ORDER == (
        SwarmStep.MANAGER,
        SwarmStep.CATALOG,
        SwarmStep.ARCHITECT,
        SwarmStep.CODER,
        SwarmStep.REVIEWER,
        SwarmStep.TESTER,
        SwarmStep.VALIDATOR,
        SwarmStep.BUILDER,
        SwarmStep.EXECUTOR,
        SwarmStep.OUTPUT_EVALUATION,
        SwarmStep.TODO_IMPLEMENTATION,
        SwarmStep.TOOLFORGE,
        SwarmStep.FEEDBACK_LOOP,
        SwarmStep.EVALUATION_REPORT,
        SwarmStep.EXPORT,
    )


class FakePipeline:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.generated_files: dict[str, str] = {}
        self.yaml_files: dict[str, str] = {}

    async def step_catalog(self, session: object) -> str:
        self.calls.append("catalog")
        return "catalog-ok"


@pytest.mark.asyncio
async def test_adapter_dispatches_exactly_one_named_step_and_captures_safe_snapshot() -> None:
    pipeline = FakePipeline()
    adapter = SwarmPipelineAdapter(lambda prior: pipeline, session=object())
    prior = SwarmSnapshot(creation_job_id=job().creation_job_id)
    outcome = await adapter.run_step(job(), SwarmStep.CATALOG.value, prior.model_dump(), "effect", None)
    assert pipeline.calls == ["catalog"]
    assert outcome.snapshot["completed_steps"] == ["catalog"]
    assert "credentials" not in json.dumps(outcome.snapshot).lower()


class DocumentationUnavailable(RuntimeError):
    pass


def test_unknown_boundary_failure_is_redacted_to_type_name() -> None:
    failure = translate_creation_failure(RuntimeError("authorization=top-secret"))
    assert failure.code == "internal_error"
    assert failure.summary == "creation step failed"
    assert failure.exception_type == "RuntimeError"


def test_known_documentation_failure_has_typed_code() -> None:
    failure = translate_creation_failure(DocumentationUnavailable("offline"))
    assert failure.code == "documentation_unavailable"


def _hermes_receipt() -> dict[str, object]:
    creation = job()
    return {
        "schema": "hermes.skill-usage-receipt.v1",
        "receipt_id": "90000000-0000-4000-8000-000000000001",
        "request_id": "90000000-0000-4000-8000-000000000002",
        "job_id": str(creation.factory_job_id),
        "correlation_id": str(creation.correlation_id),
        "lease_id": "factory-lease",
        "occurred_at": "2026-07-21T13:00:00Z",
        "producer": "hermes",
        "released_skill": {
            "schema": "captain.released-hermes-skill.v1",
            "skill_id": creation.released_skill.skill_id,
            "version": creation.released_skill.version,
            "capability": "autogen.agent-factory",
            "content_ref": creation.released_skill.content_ref.model_dump(mode="json"),
            "content_sha256": creation.released_skill.content_sha256,
            "status": "released",
            "released_at": "2026-07-21T12:00:00Z",
            "producer": "captain",
        },
        "used_skill_id": creation.released_skill.skill_id,
        "used_skill_version": creation.released_skill.version,
        "used_skill_sha256": creation.released_skill.content_sha256,
        "commands": [{"command_id": "codex.run", "max_seconds": 60}],
        "evidence_refs": [
            {
                "uri": "artifact://hermes/evidence",
                "sha256": "9" * 64,
                "media_type": "application/json",
            }
        ],
        "assertion_ids": list(creation.public_assertion_ids),
        "outcome": "passed",
    }


class ExportPipeline:
    def __init__(self, export_path: Path) -> None:
        self.export_result: dict[str, object] | None = None
        self.export_path = export_path
        self.generated_files = {"src/main.py": "print('ready')\n"}
        self.yaml_files = {"project.yml": "name: ready\n"}

    async def step_export(self, session: object) -> dict[str, object]:
        del session
        self.export_result = {"status": "SUCCESS", "path": str(self.export_path)}
        return dict(self.export_result)


def _export_tree(path: Path, *, with_receipt: bool) -> None:
    (path / "src").mkdir(parents=True)
    (path / "src/main.py").write_text("print('ready')\n", encoding="utf-8")
    (path / "project.yml").write_text("name: ready\n", encoding="utf-8")
    (path / "autogen").mkdir()
    (path / "autogen/team.py").write_text("TEAM_READY = True\n", encoding="utf-8")
    (path / "skills/factory").mkdir(parents=True)
    (path / "skills/factory/SKILL.md").write_text("# Factory skill\n", encoding="utf-8")
    (path / "tests").mkdir()
    (path / "tests/test_team.py").write_text(
        "def test_team_ready():\n    assert True\n", encoding="utf-8"
    )
    (path / "evidence").mkdir()
    (path / "evidence/assertions.json").write_text(
        json.dumps(
            {
                "schema": "minibook.creation-assertions.v1",
                "assertion_ids": list(job().public_assertion_ids),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (path / "evidence/tool-gaps.json").write_text(
        json.dumps(
            {"schema": "minibook.creation-tool-gaps.v1", "tool_gaps": []},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (path / "RUNBOOK.md").write_text("# Runbook\n", encoding="utf-8")
    (path / "team-manifest.json").write_text(
        json.dumps(
            {
                "schema": "autogen-team.v1",
                "capability_id": "requested-team",
                "capability_version": 1,
                "autogen_modules": ["autogen/team.py"],
                "test_paths": ["tests/test_team.py"],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    if with_receipt:
        (path / "evidence/hermes-skill-usage-receipt.json").write_text(
            json.dumps(_hermes_receipt(), sort_keys=True), encoding="utf-8"
        )


@pytest.mark.asyncio
async def test_export_snapshot_uses_real_content_and_hermes_receipt(tmp_path: Path) -> None:
    export = tmp_path / "export"
    _export_tree(export, with_receipt=True)
    pipeline = ExportPipeline(export)
    artifacts = ContentAddressedCreationArtifacts(tmp_path / "artifacts")
    adapter = SwarmPipelineAdapter(
        lambda prior: pipeline,
        session=object(),
        snapshotter=ExportArtifactSnapshotter(artifacts),
    )
    prior = SwarmSnapshot(creation_job_id=job().creation_job_id)

    outcome = await adapter.run_step(
        job(), SwarmStep.EXPORT.value, prior.model_dump(), "effect", None
    )
    result = adapter.assemble_result(job(), outcome.snapshot)

    assert result.status == "succeeded"
    assert result.package_manifest_ref is not None
    assert result.skill_usage_receipt_ref is not None
    candidate = ForgeCapabilityPackageCandidateV1.model_validate_json(
        artifacts.read(result.package_manifest_ref)
    )
    assert candidate.factory_job_id == job().factory_job_id
    assert candidate.creation_job_id == job().creation_job_id
    assert candidate.capability_id == "requested-team"
    assert candidate.skill_usage_receipt_ref == CaptainArtifactRef.model_validate(
        result.skill_usage_receipt_ref.model_dump(mode="json")
    )
    assert json.loads(artifacts.read(result.skill_usage_receipt_ref))["producer"] == "hermes"
    assert len(result.artifact_refs) == 1
    assert artifacts.read(result.artifact_refs[0]).startswith(b"PK")
    captain = ContentAddressedArtifactStore(tmp_path / "artifacts")
    references = (
        candidate.source_ref,
        candidate.team_manifest_ref,
        candidate.skill_usage_receipt_ref,
        candidate.runbook_ref,
        *(item.reference for item in candidate.artifacts),
    )
    assert all(captain.read_bytes(reference) for reference in references)


@pytest.mark.asyncio
async def test_export_without_hermes_receipt_is_required_todo_tool(tmp_path: Path) -> None:
    export = tmp_path / "export"
    _export_tree(export, with_receipt=False)
    pipeline = ExportPipeline(export)
    artifacts = ContentAddressedCreationArtifacts(tmp_path / "artifacts")
    adapter = SwarmPipelineAdapter(
        lambda prior: pipeline,
        session=object(),
        snapshotter=ExportArtifactSnapshotter(artifacts),
    )
    prior = SwarmSnapshot(creation_job_id=job().creation_job_id)

    outcome = await adapter.run_step(
        job(), SwarmStep.EXPORT.value, prior.model_dump(), "effect", None
    )
    result = adapter.assemble_result(job(), outcome.snapshot)

    assert result.status == "blocked"
    assert result.failure is not None and result.failure.code == "tool_unresolved"
    assert len(result.tool_gaps) == 1
    assert result.tool_gaps[0].severity == "required"
    assert json.loads(artifacts.read(result.tool_gaps[0].evidence_ref)) == {
        "schema": "TODO_TOOL.v1",
        "gap_id": "hermes-skill-usage-receipt",
        "severity": "required",
        "status": "unresolved",
        "required_output": "evidence/hermes-skill-usage-receipt.json",
    }


@pytest.mark.asyncio
async def test_export_blocks_receipt_for_another_factory_job(tmp_path: Path) -> None:
    export = tmp_path / "export"
    _export_tree(export, with_receipt=True)
    receipt_path = export / "evidence/hermes-skill-usage-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["job_id"] = "90000000-0000-4000-8000-000000000099"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    pipeline = ExportPipeline(export)
    adapter = SwarmPipelineAdapter(
        lambda prior: pipeline,
        session=object(),
        snapshotter=ExportArtifactSnapshotter(
            ContentAddressedCreationArtifacts(tmp_path / "artifacts")
        ),
    )
    prior = SwarmSnapshot(creation_job_id=job().creation_job_id)

    outcome = await adapter.run_step(
        job(), SwarmStep.EXPORT.value, prior.model_dump(), "effect", None
    )
    result = adapter.assemble_result(job(), outcome.snapshot)

    assert result.status == "blocked"
    assert result.tool_gaps[0].gap_id == "forge-capability-candidate-contract"


@pytest.mark.asyncio
async def test_export_effect_receipt_replays_structured_snapshot_without_dispatch(
    tmp_path: Path,
) -> None:
    export = tmp_path / "export"
    _export_tree(export, with_receipt=False)
    artifacts = ContentAddressedCreationArtifacts(tmp_path / "artifacts")
    first_pipeline = ExportPipeline(export)
    first = SwarmPipelineAdapter(
        lambda prior: first_pipeline,
        session=object(),
        snapshotter=ExportArtifactSnapshotter(artifacts),
    )
    prior = SwarmSnapshot(creation_job_id=job().creation_job_id)
    first_outcome = await first.run_step(
        job(), SwarmStep.EXPORT.value, prior.model_dump(), "effect", None
    )
    assert first_outcome.effect_receipt is not None

    class MustNotDispatch:
        async def step_export(self, session: object) -> None:
            raise AssertionError("accepted export effect was dispatched twice")

    replay = SwarmPipelineAdapter(
        lambda snapshot: MustNotDispatch(),
        session=object(),
        snapshotter=ExportArtifactSnapshotter(artifacts),
    )
    replayed = await replay.run_step(
        job(),
        SwarmStep.EXPORT.value,
        prior.model_dump(),
        "effect",
        first_outcome.effect_receipt,
    )
    result = replay.assemble_result(job(), replayed.snapshot)

    assert result.status == "blocked"
    assert result.tool_gaps[0].gap_id == "hermes-skill-usage-receipt"


def test_creation_artifacts_read_captain_seeded_input_ref(tmp_path: Path) -> None:
    root = tmp_path / "shared-artifacts"
    captain = ContentAddressedArtifactStore(root)
    captain_ref = captain.put(
        b"Build the requested team",
        "text/markdown",
        namespace="creation-input",
    )
    minibook = ContentAddressedCreationArtifacts(root)

    assert minibook.read(
        MinibookArtifactRef.model_validate(captain_ref.model_dump(mode="json"))
    ) == b"Build the requested team"


def test_captain_reads_minibook_exported_artifact_refs(tmp_path: Path) -> None:
    root = tmp_path / "shared-artifacts"
    minibook = ContentAddressedCreationArtifacts(root)
    minibook_ref = minibook.put(
        b'{"schema":"minibook.creation-export-manifest.v1"}',
        "application/json",
        namespace="creation-export-manifest",
    )
    captain = ContentAddressedArtifactStore(root)

    assert captain.read_bytes(
        CaptainArtifactRef.model_validate(minibook_ref.model_dump(mode="json"))
    ) == b'{"schema":"minibook.creation-export-manifest.v1"}'


def test_creation_artifacts_reject_uri_outside_shared_capability_store(
    tmp_path: Path,
) -> None:
    store = ContentAddressedCreationArtifacts(tmp_path / "artifacts")
    reference = store.put(b"content", "application/octet-stream")
    changed = reference.model_copy(
        update={"uri": f"artifact://other-store/{reference.sha256}"}
    )

    with pytest.raises(ValueError, match="outside the capability store"):
        store.read(changed)
