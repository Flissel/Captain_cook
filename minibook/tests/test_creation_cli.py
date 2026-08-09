from __future__ import annotations

import json
import hashlib
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
import importlib
import sys
import types

from agenten.agent_factory.forge_contracts import (
    CodexBuildReceiptV1 as CaptainCodexBuildReceiptV1,
)
from minibook.swarm.artifact_store import FilesystemCreationArtifactStore
from minibook.swarm.contracts import (
    CodexBuildReceiptV1,
    CreationJobV1,
    CreationJobV2,
    CreationJobV3,
    CreationPackageManifestV2,
    CreationResultV1,
    CreationResultV2,
    CreationFailure,
    FactoryBuildAssignmentV2,
    FactoryBuildEffectV2,
)
from minibook.swarm.v3_pipeline import V3CreationPipeline
from minibook.swarm.runner import CreationRunner, StepOutcome
from minibook.swarm.job_store import CreationJobStore
from uuid import uuid4
from minibook.swarm.creation_cli import (
    install_creation_skill_receipt,
    load_creation_job,
    publish_captain_sealed_creation_output,
    publish_creation_output,
    write_creation_result_atomic,
)


FIXTURE_ROOT = Path(__file__).parents[2] / "tests" / "fixtures" / "contracts"


def test_creation_cli_loads_exact_typed_job(tmp_path: Path) -> None:
    source = FIXTURE_ROOT / "minibook_creation_job.v1.json"
    job_path = tmp_path / "creation-job.json"
    job_path.write_bytes(source.read_bytes())

    loaded = load_creation_job(job_path)

    assert loaded == CreationJobV1.model_validate_json(source.read_text(encoding="utf-8"))


def test_creation_cli_writes_result_atomically_without_overwriting(tmp_path: Path) -> None:
    result = CreationResultV1.model_validate_json(
        (FIXTURE_ROOT / "minibook_creation_result.v1.json").read_text(
            encoding="utf-8"
        )
    )
    result_path = tmp_path / "creation-result.json"

    write_creation_result_atomic(result_path, result)

    assert CreationResultV1.model_validate_json(
        result_path.read_text(encoding="utf-8")
    ) == result
    assert not (tmp_path / "creation-result.json.tmp").exists()
    with pytest.raises(FileExistsError):
        write_creation_result_atomic(result_path, result)


def test_creation_cli_rejects_invalid_or_non_file_job(tmp_path: Path) -> None:
    invalid = tmp_path / "creation-job.json"
    invalid.write_text(json.dumps({"schema": "minibook.creation-job.v1"}), encoding="utf-8")

    with pytest.raises(ValueError):
        load_creation_job(invalid)
    with pytest.raises(FileNotFoundError):
        load_creation_job(tmp_path / "missing.json")


def test_creation_cli_publishes_sealed_output_to_persistent_cas(tmp_path: Path) -> None:
    job = CreationJobV1.model_validate_json(
        (FIXTURE_ROOT / "minibook_creation_job.v1.json").read_text(encoding="utf-8")
    )
    output = tmp_path / "candidate"
    (output / "evidence").mkdir(parents=True)
    (output / "factory-candidate.json").write_text(
        json.dumps(
            {
                "schema": "captain.factory-candidate.v1",
                "candidate_id": "demo-team",
            }
        ),
        encoding="utf-8",
    )
    (output / "evidence/hermes-factory-skill-usage-receipt.json").write_text(
        json.dumps(
            {
                "schema": "hermes.forge-build-skill-usage-receipt.v1",
                "producer": "hermes",
                "outcome": "fulfilled",
                "creation_job_id": str(job.creation_job_id),
                "factory_job_id": str(job.factory_job_id),
                "correlation_id": str(job.correlation_id),
                "subject_version": job.subject_version,
                "attempt": job.attempt,
                "idempotency_key": job.idempotency_key,
                "released_skill": job.released_skill.model_dump(mode="json"),
                "public_assertion_ids": list(job.public_assertion_ids),
                "evidence_refs": [
                    {
                        "uri": "artifact://forge/evidence/" + "9" * 64,
                        "sha256": "9" * 64,
                        "media_type": "application/json",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (output / "run_team.py").write_text("print('ready')\n", encoding="utf-8")

    result = publish_creation_output(
        job,
        output_path=output,
        artifact_root=tmp_path / ".captain-cook" / "creation-cas",
    )

    assert result.status == "succeeded"
    assert result.package_manifest_ref is not None
    assert result.skill_usage_receipt_ref is not None
    assert len(result.artifact_refs) == 2
    assert all(ref.uri.startswith("artifact://minibook-creation/") for ref in result.artifact_refs)


def test_creation_cli_installs_exact_captain_supplied_skill_receipt(
    tmp_path: Path,
) -> None:
    job = CreationJobV1.model_validate_json(
        (FIXTURE_ROOT / "minibook_creation_job.v1.json").read_text(encoding="utf-8")
    )
    payload = {
        "schema": "hermes.forge-build-skill-usage-receipt.v1",
        "producer": "hermes",
        "outcome": "fulfilled",
        "creation_job_id": str(job.creation_job_id),
        "factory_job_id": str(job.factory_job_id),
        "correlation_id": str(job.correlation_id),
        "subject_version": job.subject_version,
        "attempt": job.attempt,
        "idempotency_key": job.idempotency_key,
        "released_skill": job.released_skill.model_dump(mode="json"),
        "public_assertion_ids": list(job.public_assertion_ids),
        "evidence_refs": [
            {
                "uri": "artifact://forge/hermes-brief/" + "8" * 64,
                "sha256": "8" * 64,
                "media_type": "application/json",
            }
        ],
    }
    source = tmp_path / "receipt.json"
    source_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    source.write_bytes(source_bytes)
    output = tmp_path / "candidate"
    output.mkdir()

    installed = install_creation_skill_receipt(
        job,
        source_path=source,
        output_path=output,
    )

    assert installed.read_bytes() == source_bytes
    assert installed == output / "evidence/hermes-factory-skill-usage-receipt.json"

    changed = source_bytes.replace(str(job.factory_job_id).encode(), str(job.creation_job_id).encode())
    source.write_bytes(changed)
    with pytest.raises(ValueError, match="does not match"):
        install_creation_skill_receipt(job, source_path=source, output_path=output)


def _forge_receipt_bytes(job: CreationJobV1 | CreationJobV2) -> bytes:
    payload = {
        "schema": "hermes.forge-build-skill-usage-receipt.v1",
        "producer": "hermes",
        "outcome": "fulfilled",
        "creation_job_id": str(job.creation_job_id),
        "factory_job_id": str(job.factory_job_id),
        "correlation_id": str(job.correlation_id),
        "subject_version": job.subject_version,
        "attempt": job.attempt,
        "idempotency_key": job.idempotency_key,
        "released_skill": job.released_skill.model_dump(mode="json"),
        "public_assertion_ids": list(job.public_assertion_ids),
        "evidence_refs": [
            {
                "uri": "artifact://captain/codex-build/" + "8" * 64,
                "sha256": "8" * 64,
                "media_type": "application/json",
            }
        ],
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _captain_source_zip(*, extra_name: str | None = None) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "factory-candidate.json",
            json.dumps(
                {
                    "schema": "captain.factory-candidate.v1",
                    "candidate_id": "captain-sealed-team",
                },
                separators=(",", ":"),
            ),
        )
        archive.writestr("run_team.py", "print('captain sealed')\n")
        if extra_name is not None:
            archive.writestr(extra_name, "forbidden")
    return buffer.getvalue()


def _v2_job(source_bytes: bytes) -> CreationJobV2:
    payload = json.loads(
        (FIXTURE_ROOT / "minibook_creation_job.v1.json").read_text(encoding="utf-8")
    )
    payload["schema"] = "minibook.creation-job.v2"
    source_ref = {
        "uri": "artifact://captain/codex-source/" + hashlib.sha256(source_bytes).hexdigest(),
        "sha256": hashlib.sha256(source_bytes).hexdigest(),
        "media_type": "application/zip",
    }
    with zipfile.ZipFile(BytesIO(source_bytes)) as archive:
        candidate_bytes = archive.read("factory-candidate.json")
    candidate_ref = {
        "uri": "artifact://captain/candidate-manifest/"
        + hashlib.sha256(candidate_bytes).hexdigest(),
        "sha256": hashlib.sha256(candidate_bytes).hexdigest(),
        "media_type": "application/json",
    }
    payload["source_archive_ref"] = source_ref
    payload["codex_build_receipt"] = {
        "schema": "captain.codex-build-receipt.v1",
        "receipt_id": "508c776e-a351-43d8-9539-90a3b8ee6f15",
        "producer": "captain",
        "outcome": "sealed",
        "assignment_id": "cd22cf0c-f76e-46ca-84c6-e12bf0354daf",
        "creation_job_id": payload["creation_job_id"],
        "factory_job_id": payload["factory_job_id"],
        "correlation_id": payload["correlation_id"],
        "subject_version": payload["subject_version"],
        "attempt": payload["attempt"],
        "idempotency_key": payload["idempotency_key"],
        "seal_idempotency_key": "5" * 64,
        "build_brief_ref": {
            "uri": "artifact://captain/codex-brief/" + "1" * 64,
            "sha256": "1" * 64,
            "media_type": "application/json",
        },
        "codex_session_ref": {
            "uri": "artifact://captain/codex-session/" + "2" * 64,
            "sha256": "2" * 64,
            "media_type": "application/json",
        },
        "workspace_ref": "workspace://factory/codex-build",
        "workspace_snapshot_ref": {
            "uri": "artifact://captain/workspace-snapshot/" + "4" * 64,
            "sha256": "4" * 64,
            "media_type": "application/zip",
        },
        "source_archive_ref": source_ref,
        "candidate_manifest_ref": candidate_ref,
        "test_evidence_refs": [
            {
                "uri": "artifact://captain/codex-tests/" + "3" * 64,
                "sha256": "3" * 64,
                "media_type": "application/json",
            }
        ],
        "acceptance_assertion_ids": payload["public_assertion_ids"],
        "completed_at": "2026-07-28T12:00:00Z",
    }
    canonical_receipt = json.dumps(
        CodexBuildReceiptV1.model_validate(
            payload["codex_build_receipt"]
        ).model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    receipt_digest = hashlib.sha256(canonical_receipt).hexdigest()
    payload["codex_build_receipt_ref"] = {
        "uri": "artifact://captain/codex-build-receipt/" + receipt_digest,
        "sha256": receipt_digest,
        "media_type": "application/json",
    }
    return CreationJobV2.model_validate(payload)


def test_v2_job_binds_complete_captain_codex_build_receipt() -> None:
    job = _v2_job(_captain_source_zip())

    assert isinstance(job.codex_build_receipt, CodexBuildReceiptV1)
    assert job.codex_build_receipt.source_archive_ref == job.source_archive_ref
    assert job.codex_build_receipt_ref.sha256 == hashlib.sha256(
        json.dumps(
            job.codex_build_receipt.model_dump(mode="json", by_alias=True),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    changed = job.model_dump(mode="json", by_alias=True)
    changed["codex_build_receipt"]["attempt"] = job.attempt + 1
    with pytest.raises(ValueError, match="Codex build receipt.*creation job"):
        CreationJobV2.model_validate(changed)

    duplicate = job.codex_build_receipt.model_dump(mode="json", by_alias=True)
    duplicate["workspace_snapshot_ref"] = duplicate["source_archive_ref"]
    with pytest.raises(ValueError, match="sealed Codex build refs"):
        CodexBuildReceiptV1.model_validate(duplicate)

    changed_ref = job.model_dump(mode="json", by_alias=True)
    changed_ref["codex_build_receipt_ref"]["sha256"] = "e" * 64
    with pytest.raises(ValueError, match="canonical.*digest"):
        CreationJobV2.model_validate(changed_ref)


def test_codex_build_receipt_is_schema_compatible_with_captain() -> None:
    minibook_receipt = _v2_job(_captain_source_zip()).codex_build_receipt

    captain_receipt = CaptainCodexBuildReceiptV1.model_validate(
        minibook_receipt.model_dump(mode="json", by_alias=True)
    )

    assert captain_receipt.model_dump(mode="json", by_alias=True) == (
        minibook_receipt.model_dump(mode="json", by_alias=True)
    )


def test_v2_import_publishes_exact_captain_zip_and_keeps_receipt_external(
    tmp_path: Path,
) -> None:
    source_bytes = _captain_source_zip()
    job = _v2_job(source_bytes)
    source = tmp_path / "captain-source.zip"
    source.write_bytes(source_bytes)
    receipt = tmp_path / "forge-skill-receipt.json"
    receipt_bytes = _forge_receipt_bytes(job)
    receipt.write_bytes(receipt_bytes)
    artifact_root = tmp_path / ".captain-cook" / "creation-cas"

    result = publish_captain_sealed_creation_output(
        job,
        source_archive_path=source,
        skill_usage_receipt_path=receipt,
        artifact_root=artifact_root,
    )

    assert result.status == "succeeded"
    store = FilesystemCreationArtifactStore(artifact_root)
    source_ref = next(ref for ref in result.artifact_refs if ref.media_type == "application/zip")
    assert source_ref.sha256 == job.source_archive_ref.sha256
    assert store.read_bytes(source_ref) == source_bytes
    assert result.skill_usage_receipt_ref is not None
    assert store.read_bytes(result.skill_usage_receipt_ref) == receipt_bytes
    codex_receipt_refs = [
        ref
        for ref in result.artifact_refs
        if ref.media_type == "application/json"
        and ref.sha256 == job.codex_build_receipt_ref.sha256
    ]
    assert len(codex_receipt_refs) == 1
    assert CodexBuildReceiptV1.model_validate_json(
        store.read_bytes(codex_receipt_refs[0])
    ) == job.codex_build_receipt
    assert result.package_manifest_ref is not None
    package = CreationPackageManifestV2.model_validate_json(
        store.read_bytes(result.package_manifest_ref)
    )
    assert package.codex_build_receipt_ref == codex_receipt_refs[0]
    with zipfile.ZipFile(BytesIO(store.read_bytes(source_ref))) as archive:
        assert "factory-candidate.json" in archive.namelist()
        assert "evidence/hermes-factory-skill-usage-receipt.json" not in archive.namelist()


def test_v2_import_fails_closed_for_changed_digest(tmp_path: Path) -> None:
    approved = _captain_source_zip()
    job = _v2_job(approved)
    source = tmp_path / "captain-source.zip"
    source.write_bytes(approved + b"changed")
    receipt = tmp_path / "receipt.json"
    receipt.write_bytes(_forge_receipt_bytes(job))

    with pytest.raises(ValueError, match="digest"):
        publish_captain_sealed_creation_output(
            job,
            source_archive_path=source,
            skill_usage_receipt_path=receipt,
            artifact_root=tmp_path / "cas",
        )


def test_v2_import_fails_closed_for_changed_candidate_manifest_binding(
    tmp_path: Path,
) -> None:
    source_bytes = _captain_source_zip()
    job = _v2_job(source_bytes)
    changed = job.model_dump(mode="json", by_alias=True)
    changed["codex_build_receipt"]["candidate_manifest_ref"]["sha256"] = "f" * 64
    changed["codex_build_receipt"]["candidate_manifest_ref"]["uri"] = (
        "artifact://captain/candidate-manifest/" + "f" * 64
    )
    changed_receipt = CodexBuildReceiptV1.model_validate(
        changed["codex_build_receipt"]
    )
    changed_receipt_digest = hashlib.sha256(
        json.dumps(
            changed_receipt.model_dump(mode="json", by_alias=True),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    changed["codex_build_receipt_ref"]["sha256"] = changed_receipt_digest
    changed["codex_build_receipt_ref"]["uri"] = (
        "artifact://captain/codex-build-receipt/" + changed_receipt_digest
    )
    job = CreationJobV2.model_validate(changed)
    source = tmp_path / "captain-source.zip"
    source.write_bytes(source_bytes)
    receipt = tmp_path / "receipt.json"
    receipt.write_bytes(_forge_receipt_bytes(job))

    with pytest.raises(ValueError, match="candidate manifest digest"):
        publish_captain_sealed_creation_output(
            job,
            source_archive_path=source,
            skill_usage_receipt_path=receipt,
            artifact_root=tmp_path / "cas",
        )


@pytest.mark.parametrize(
    "unsafe_name",
    (
        "../escape.py",
        "/absolute.py",
        "C:/drive.py",
        "..\\escape.py",
        "evidence/hermes-factory-skill-usage-receipt.json",
    ),
)
def test_v2_import_rejects_unsafe_or_embedded_external_evidence(
    tmp_path: Path,
    unsafe_name: str,
) -> None:
    source_bytes = _captain_source_zip(extra_name=unsafe_name)
    job = _v2_job(source_bytes)
    source = tmp_path / "captain-source.zip"
    source.write_bytes(source_bytes)
    receipt = tmp_path / "receipt.json"
    receipt.write_bytes(_forge_receipt_bytes(job))

    with pytest.raises(ValueError, match="unsafe|external skill usage receipt"):
        publish_captain_sealed_creation_output(
            job,
            source_archive_path=source,
            skill_usage_receipt_path=receipt,
            artifact_root=tmp_path / "cas",
        )


def test_creation_cli_reads_legacy_v1_and_new_v2_jobs(tmp_path: Path) -> None:
    source_bytes = _captain_source_zip()
    v2 = _v2_job(source_bytes)
    v2_path = tmp_path / "creation-job-v2.json"
    v2_path.write_text(v2.model_dump_json(by_alias=True), encoding="utf-8")

    assert isinstance(load_creation_job(v2_path), CreationJobV2)
    assert isinstance(
        load_creation_job(FIXTURE_ROOT / "minibook_creation_job.v1.json"),
        CreationJobV1,
    )


def _reload_main() -> object:
    sys.modules.pop("minibook.src.main", None)
    return importlib.import_module("minibook.src.main")


def test_v3_startup_fails_closed_when_required_composition_is_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINIBOOK_CREATION_DB", str(tmp_path / "creation.sqlite3"))
    monkeypatch.setenv("MINIBOOK_CREATION_V3_ENABLED", "1")
    monkeypatch.delenv("MINIBOOK_CREATION_CAS_ROOT", raising=False)
    monkeypatch.delenv("MINIBOOK_CREATION_V3_VALIDATOR", raising=False)
    monkeypatch.delenv("MINIBOOK_CREATION_WORKER_TOKEN", raising=False)
    monkeypatch.delenv("MINIBOOK_CREATION_WORKER_ID", raising=False)
    monkeypatch.delenv("MINIBOOK_CREATION_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="V3 creation configuration is incomplete"):
        _reload_main()


def test_v3_startup_constructs_pipeline_only_with_all_explicit_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Validator:
        def build_assignment(self, *_args: object) -> object:
            raise NotImplementedError

        async def validate_build_effect(self, *_args: object) -> object:
            raise NotImplementedError

        async def validate(self, *_args: object) -> object:
            raise NotImplementedError

        def assemble_result(self, *_args: object) -> object:
            raise NotImplementedError

    validator_module = types.ModuleType("test_v3_validator")
    validator_module.validator = Validator()
    monkeypatch.setitem(sys.modules, "test_v3_validator", validator_module)
    monkeypatch.setenv("MINIBOOK_CREATION_DB", str(tmp_path / "creation.sqlite3"))
    monkeypatch.setenv("MINIBOOK_CREATION_V3_ENABLED", "1")
    monkeypatch.setenv("MINIBOOK_CREATION_CAS_ROOT", str(tmp_path / "cas"))
    monkeypatch.setenv("MINIBOOK_CREATION_V3_VALIDATOR", "test_v3_validator:validator")
    monkeypatch.setenv("MINIBOOK_CREATION_WORKER_TOKEN", "worker-secret")
    monkeypatch.setenv("MINIBOOK_CREATION_WORKER_ID", "hermes-factory")
    monkeypatch.setenv("MINIBOOK_CREATION_API_KEY", "captain-secret")
    module = _reload_main()
    job_payload = json.loads(
        (FIXTURE_ROOT / "minibook_creation_job.v3.json").read_text(encoding="utf-8")
    )
    job_payload["deadline_at"] = "2099-08-09T12:00:00Z"
    job = CreationJobV3.model_validate(job_payload)
    module._creation_store.submit(job)
    runner = module._creation_runner_for(job.creation_job_id)
    assert isinstance(runner.pipeline, V3CreationPipeline)


def test_v3_startup_fails_closed_when_creation_caller_credential_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Validator:
        def build_assignment(self, *_args: object) -> object:
            raise NotImplementedError

        async def validate_build_effect(self, *_args: object) -> object:
            raise NotImplementedError

        async def validate(self, *_args: object) -> object:
            raise NotImplementedError

        def assemble_result(self, *_args: object) -> object:
            raise NotImplementedError

    validator_module = types.ModuleType("test_v3_missing_caller_validator")
    validator_module.validator = Validator()
    monkeypatch.setitem(sys.modules, "test_v3_missing_caller_validator", validator_module)
    monkeypatch.setenv("MINIBOOK_CREATION_DB", str(tmp_path / "creation.sqlite3"))
    monkeypatch.setenv("MINIBOOK_CREATION_V3_ENABLED", "1")
    monkeypatch.setenv("MINIBOOK_CREATION_CAS_ROOT", str(tmp_path / "cas"))
    monkeypatch.setenv(
        "MINIBOOK_CREATION_V3_VALIDATOR",
        "test_v3_missing_caller_validator:validator",
    )
    monkeypatch.setenv("MINIBOOK_CREATION_WORKER_TOKEN", "worker-secret")
    monkeypatch.setenv("MINIBOOK_CREATION_WORKER_ID", "hermes-factory")
    monkeypatch.delenv("MINIBOOK_CREATION_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="V3 creation configuration is incomplete"):
        _reload_main()


def test_disabled_creation_does_not_import_forge_docker_or_llm_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MINIBOOK_CREATION_DB", raising=False)
    monkeypatch.delenv("MINIBOOK_CREATION_V3_ENABLED", raising=False)
    before = set(sys.modules)
    _reload_main()
    imported = set(sys.modules) - before
    assert not any(name in imported for name in {
        "minibook.swarm.pipeline", "minibook.swarm.docker_ops", "anthropic", "openai",
    })


class RecordingV3Validator:
    def __init__(self) -> None:
        self.accepted_effects: list[FactoryBuildEffectV2] = []
        self.validation_calls = 0

    def build_assignment(
        self, job: CreationJobV3, checkpoint_version: int, effect_key: str
    ) -> FactoryBuildAssignmentV2:
        return FactoryBuildAssignmentV2.model_validate({
            "assignment_id": str(uuid4()), "creation_job_id": str(job.creation_job_id),
            "factory_job_id": str(job.factory_job_id), "correlation_id": str(job.correlation_id),
            "subject_version": job.subject_version, "checkpoint_version": checkpoint_version,
            "attempt": job.attempt, "effect_key": effect_key,
            "workspace_ref": "workspace://captain/codex",
            "workspace_policy_sha256": job.workspace_policy_ref.sha256,
            "required_agent_ids": list(job.required_agent_ids),
            "ordered_handoffs": [value.model_dump(mode="json") for value in job.ordered_handoffs],
            "tool_requirements": [value.model_dump(mode="json") for value in job.tool_requirements],
            "allowed_output_roots": ["out"], "allowed_commands": [["python", "-m", "pytest"]],
            "limits": {"max_wall_clock_seconds": 30, "max_provider_cost_usd": 1.0, "max_retries": 0, "max_files": 10},
            "lease_ref": job.lease_ref.model_dump(mode="json"), "deadline_at": job.deadline_at.isoformat(),
        })

    async def validate_build_effect(
        self,
        _job: CreationJobV3,
        _assignment: FactoryBuildAssignmentV2,
        effect: FactoryBuildEffectV2,
        _artifact_store: FilesystemCreationArtifactStore,
        snapshot: dict[str, object],
    ) -> StepOutcome:
        self.accepted_effects.append(effect)
        return StepOutcome(snapshot=snapshot | {"accepted_effect_key": effect.effect_key})

    async def validate(
        self,
        _job: CreationJobV3,
        snapshot: dict[str, object],
        _artifact_store: FilesystemCreationArtifactStore,
    ) -> StepOutcome:
        self.validation_calls += 1
        return StepOutcome(snapshot=snapshot | {"validated": True})

    def assemble_result(
        self, job: CreationJobV3, _snapshot: dict[str, object]
    ) -> CreationResultV2:
        return CreationResultV2(
            creation_job_id=job.creation_job_id, factory_job_id=job.factory_job_id,
            correlation_id=job.correlation_id, subject_version=job.subject_version,
            attempt=job.attempt, status="failed",
            failure=CreationFailure(code="validation_failed", summary="Task 4 not configured"),
        )


def _v3_effect(assignment: FactoryBuildAssignmentV2) -> FactoryBuildEffectV2:
    def ref(digit: str, media_type: str = "application/json") -> dict[str, str]:
        return {"uri": f"artifact://worker/{digit * 64}", "sha256": digit * 64, "media_type": media_type}

    return FactoryBuildEffectV2.model_validate({
        "assignment_id": str(assignment.assignment_id), "creation_job_id": str(assignment.creation_job_id),
        "factory_job_id": str(assignment.factory_job_id), "correlation_id": str(assignment.correlation_id),
        "subject_version": assignment.subject_version, "checkpoint_version": assignment.checkpoint_version,
        "attempt": assignment.attempt, "effect_key": assignment.effect_key,
        "hermes_usage_receipt_ref": ref("1"), "codex_build_receipt_ref": ref("2"),
        "codex_session_ref": ref("3"), "workspace_snapshot_ref": ref("4", "application/zip"),
        "source_archive_ref": ref("5", "application/zip"), "preliminary_team_manifest_ref": ref("6"),
        "build_evidence_refs": [ref("7")], "test_evidence_refs": [ref("8")],
    })


@pytest.mark.asyncio
async def test_production_v3_pipeline_yields_assignment_then_consumes_exact_durable_effect(
    tmp_path: Path,
) -> None:
    store = CreationJobStore(tmp_path / "creation.sqlite3")
    artifact_store = FilesystemCreationArtifactStore(tmp_path / "cas")
    validator = RecordingV3Validator()
    job_payload = json.loads(
        (FIXTURE_ROOT / "minibook_creation_job.v3.json").read_text(encoding="utf-8")
    )
    job_payload["deadline_at"] = "2099-08-09T12:00:00Z"
    job = CreationJobV3.model_validate(job_payload)
    store.submit(job)
    pipeline = V3CreationPipeline(
        store=store, artifact_store=artifact_store, validator=validator
    )
    runner = CreationRunner(store, pipeline)

    assert await runner.run_slice(job.creation_job_id) is None
    assert store.progress(job.creation_job_id).status == "awaiting_build"
    assignment = store.build_assignment_for_job(job.creation_job_id)
    assert assignment is not None
    assert validator.accepted_effects == []
    assert validator.validation_calls == 0

    store.claim_build_assignment(assignment.assignment_id, "hermes-factory")
    accepted = store.accept_build_effect(_v3_effect(assignment))
    changed_receipt = accepted.model_copy(
        update={"build_evidence_refs": accepted.test_evidence_refs}
    )
    with pytest.raises(ValueError, match="differs from durable effect"):
        await pipeline.run_step(
            job,
            "build_handoff",
            store.snapshot(job.creation_job_id),
            assignment.effect_key,
            changed_receipt.model_dump(mode="json", by_alias=True),
        )
    await runner.run_slice(job.creation_job_id)

    assert validator.accepted_effects == [accepted]
    assert validator.validation_calls == 1
    assert store.snapshot(job.creation_job_id)["accepted_effect_key"] == accepted.effect_key
