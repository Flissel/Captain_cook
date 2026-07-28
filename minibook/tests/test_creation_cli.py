from __future__ import annotations

import json
from pathlib import Path

import pytest

from minibook.swarm.contracts import CreationJobV1, CreationResultV1
from minibook.swarm.creation_cli import (
    install_creation_skill_receipt,
    load_creation_job,
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
