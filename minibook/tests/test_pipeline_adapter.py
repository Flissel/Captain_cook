from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from minibook.swarm.contracts import ArtifactRef, CreationJobV1
from minibook.swarm.pipeline_adapter import (
    ContentAddressedCreationArtifactPublisher,
    CreationExportBundle,
    PIPELINE_STEP_ORDER,
    SwarmPipelineAdapter,
    SwarmSnapshot,
    SwarmStep,
    translate_creation_failure,
)
from agenten.agent_factory.business_benchmark_production_ports import (
    BusinessBenchmarkContentAddressedArtifactStore,
)
from agenten.agent_runtime.contracts import ArtifactRef as RuntimeArtifactRef
from minibook.swarm.contracts import CreationPackageManifestV1


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


class ExportPipeline:
    def __init__(self, bundle: CreationExportBundle | None) -> None:
        self.bundle = bundle
        self.calls = 0

    async def step_export(self, session: object):
        self.calls += 1
        return self.bundle


def _export_bundle(tmp_path: Path) -> CreationExportBundle:
    source = tmp_path / "candidate.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("run_team.py", "print('ok')\n")
    candidate_manifest = {
        "schema_name": "captain.factory-candidate.v1",
        "candidate_id": "demo_team",
        "team_manifest": {
            "reference": {
                "uri": "artifact://forge/team",
                "sha256": "1" * 64,
                "media_type": "application/json",
            },
            "relative_path": "team.json",
        },
        "workflow_artifacts": [
            {
                "reference": {
                    "uri": "artifact://forge/workflow",
                    "sha256": "2" * 64,
                    "media_type": "application/json",
                },
                "relative_path": "workflow.json",
            }
        ],
        "tool_schema_artifacts": [],
        "n8n_tools": [],
        "n8n_tool_references": [],
        "build_command": ["python", "-m", "compileall", "-q", "."],
        "real_case_command": ["python", "run_team.py"],
        "timeout_seconds": 10,
    }
    return CreationExportBundle(
        source_archive=source.read_bytes(),
        candidate_manifest=candidate_manifest,
        skill_usage_receipt=json.dumps(
            {"schema": "hermes.skill-usage-receipt.v1", "status": "used"}
        ).encode("utf-8"),
    )


@pytest.mark.asyncio
async def test_adapter_dispatches_exactly_one_named_step_and_captures_safe_snapshot() -> None:
    pipeline = FakePipeline()
    adapter = SwarmPipelineAdapter(lambda prior: pipeline, session=object())
    prior = SwarmSnapshot(creation_job_id=job().creation_job_id)
    outcome = await adapter.run_step(job(), SwarmStep.CATALOG.value, prior.model_dump(), "effect", None)
    assert pipeline.calls == ["catalog"]
    assert outcome.snapshot["completed_steps"] == ["catalog"]
    assert "credentials" not in json.dumps(outcome.snapshot).lower()


@pytest.mark.asyncio
async def test_export_publishes_real_bytes_and_rehydrates_accepted_receipt(
    tmp_path: Path,
) -> None:
    pipeline = ExportPipeline(_export_bundle(tmp_path))
    store = BusinessBenchmarkContentAddressedArtifactStore(
        tmp_path / ".captain-cook" / "minibook-export"
    )
    publisher = ContentAddressedCreationArtifactPublisher(store)
    adapter = SwarmPipelineAdapter(
        lambda prior: pipeline,
        session=object(),
        artifact_publisher=publisher,
    )
    prior = SwarmSnapshot(creation_job_id=job().creation_job_id)

    outcome = await adapter.run_step(
        job(), SwarmStep.EXPORT.value, prior.model_dump(), "effect", None
    )
    result = adapter.assemble_result(job(), outcome.snapshot)

    assert result.status == "succeeded"
    assert result.package_manifest_ref is not None
    package = CreationPackageManifestV1.model_validate_json(
        store.read_bytes(
            RuntimeArtifactRef.model_validate(
                result.package_manifest_ref.model_dump(mode="json")
            )
        )
    )
    assert package.creation_job_id == job().creation_job_id
    assert package.factory_job_id == job().factory_job_id
    assert result.artifact_refs == (
        package.candidate_manifest_ref,
        package.source_archive_ref,
    )

    replay_pipeline = ExportPipeline(None)
    replay_adapter = SwarmPipelineAdapter(
        lambda prior: replay_pipeline,
        session=object(),
        artifact_publisher=ContentAddressedCreationArtifactPublisher(store),
    )
    replay = await replay_adapter.run_step(
        job(),
        SwarmStep.EXPORT.value,
        prior.model_dump(),
        "effect",
        outcome.effect_receipt,
    )
    assert replay.snapshot == outcome.snapshot
    assert replay_pipeline.calls == 0

    tampered_receipt = dict(outcome.effect_receipt)
    tampered_receipt["receipt_id"] = "f" * 64
    with pytest.raises(ValueError, match="receipt"):
        await replay_adapter.run_step(
            job(),
            SwarmStep.EXPORT.value,
            prior.model_dump(),
            "effect",
            tampered_receipt,
        )

    changed_job = job().model_copy(update={"creation_job_id": uuid4()})
    with pytest.raises(ValueError, match="package.*job"):
        await replay_adapter.run_step(
            changed_job,
            SwarmStep.EXPORT.value,
            prior.model_dump(),
            "effect",
            outcome.effect_receipt,
        )


@pytest.mark.asyncio
async def test_export_replay_without_cas_read_authority_fails_closed(
    tmp_path: Path,
) -> None:
    store = BusinessBenchmarkContentAddressedArtifactStore(
        tmp_path / ".captain-cook" / "minibook-export"
    )
    producer = SwarmPipelineAdapter(
        lambda prior: ExportPipeline(_export_bundle(tmp_path)),
        session=object(),
        artifact_publisher=ContentAddressedCreationArtifactPublisher(store),
    )
    prior = SwarmSnapshot(creation_job_id=job().creation_job_id)
    published = await producer.run_step(
        job(), SwarmStep.EXPORT.value, prior.model_dump(), "effect", None
    )
    replay = SwarmPipelineAdapter(
        lambda prior: ExportPipeline(None),
        session=object(),
    )

    with pytest.raises(ValueError, match="read authority"):
        await replay.run_step(
            job(),
            SwarmStep.EXPORT.value,
            prior.model_dump(),
            "effect",
            published.effect_receipt,
        )


def test_assemble_result_rejects_missing_or_unknown_artifact_bindings() -> None:
    adapter = SwarmPipelineAdapter(lambda prior: ExportPipeline(None), session=object())
    reference = ArtifactRef.model_validate(
        {
            "uri": "artifact://forge/test/" + "a" * 64,
            "sha256": "a" * 64,
            "media_type": "application/json",
        }
    )
    source_reference = ArtifactRef.model_validate(
        reference.model_dump(mode="json") | {"media_type": "application/zip"}
    )
    base = SwarmSnapshot(
        creation_job_id=job().creation_job_id,
        package_manifest_ref=reference,
        skill_usage_receipt_ref=reference,
        artifact_bindings={"candidate_manifest": reference},
    )

    missing = adapter.assemble_result(job(), base.model_dump(mode="json"))
    extra = adapter.assemble_result(
        job(),
        base.model_copy(
            update={
                "artifact_bindings": {
                    "candidate_manifest": reference,
                    "source_archive": source_reference,
                    "unexpected": reference,
                }
            }
        ).model_dump(mode="json"),
    )

    assert missing.status == "blocked"
    assert extra.status == "blocked"
    assert missing.failure is not None
    assert extra.failure is not None
    assert missing.failure.code == extra.failure.code == "validation_failed"


@pytest.mark.asyncio
async def test_legacy_export_without_typed_bytes_stays_blocked(tmp_path: Path) -> None:
    pipeline = ExportPipeline(None)
    store = BusinessBenchmarkContentAddressedArtifactStore(
        tmp_path / ".captain-cook" / "legacy-export"
    )
    adapter = SwarmPipelineAdapter(
        lambda prior: pipeline,
        session=object(),
        artifact_publisher=ContentAddressedCreationArtifactPublisher(store),
    )
    outcome = await adapter.run_step(
        job(),
        SwarmStep.EXPORT.value,
        SwarmSnapshot(creation_job_id=job().creation_job_id).model_dump(),
        "effect",
        None,
    )

    result = adapter.assemble_result(job(), outcome.snapshot)

    assert result.status == "blocked"
    assert result.failure is not None
    assert result.failure.code == "validation_failed"


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
