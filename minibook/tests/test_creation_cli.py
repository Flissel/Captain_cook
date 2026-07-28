from __future__ import annotations

import json
import hashlib
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from agenten.agent_factory.forge_contracts import (
    CodexBuildReceiptV1 as CaptainCodexBuildReceiptV1,
)
from minibook.swarm.artifact_store import FilesystemCreationArtifactStore
from minibook.swarm.contracts import (
    CodexBuildReceiptV1,
    CreationJobV1,
    CreationJobV2,
    CreationResultV1,
)
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
    return CreationJobV2.model_validate(payload)


def test_v2_job_binds_complete_captain_codex_build_receipt() -> None:
    job = _v2_job(_captain_source_zip())

    assert isinstance(job.codex_build_receipt, CodexBuildReceiptV1)
    assert job.codex_build_receipt.source_archive_ref == job.source_archive_ref
    changed = job.model_dump(mode="json", by_alias=True)
    changed["codex_build_receipt"]["attempt"] = job.attempt + 1
    with pytest.raises(ValueError, match="Codex build receipt.*creation job"):
        CreationJobV2.model_validate(changed)

    duplicate = job.codex_build_receipt.model_dump(mode="json", by_alias=True)
    duplicate["workspace_snapshot_ref"] = duplicate["source_archive_ref"]
    with pytest.raises(ValueError, match="sealed Codex build refs"):
        CodexBuildReceiptV1.model_validate(duplicate)


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
