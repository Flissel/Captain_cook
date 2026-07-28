"""Fail-closed file boundary for one-shot Captain creation runs."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .artifact_store import FilesystemCreationArtifactStore
from .contracts import (
    CreationJobV1,
    CreationResultV1,
    ForgeBuildSkillUsageReceiptV1,
)
from .creation_export import build_creation_export
from .pipeline_adapter import (
    ContentAddressedCreationArtifactPublisher,
    CreationExportBundle,
)


def load_creation_job(path: Path) -> CreationJobV1:
    if not path.is_file():
        raise FileNotFoundError("creation job file is unavailable")
    return CreationJobV1.model_validate_json(path.read_text(encoding="utf-8"))


def publish_creation_output(
    job: CreationJobV1,
    *,
    output_path: Path,
    artifact_root: Path,
) -> CreationResultV1:
    """Seal one generated team into the persistent Minibook creation CAS."""

    source_archive, candidate_manifest, skill_usage_receipt = build_creation_export(
        output_path
    )
    publisher = ContentAddressedCreationArtifactPublisher(
        FilesystemCreationArtifactStore(artifact_root)
    )
    receipt = publisher.publish(
        job,
        CreationExportBundle(
            source_archive=source_archive,
            candidate_manifest=candidate_manifest,
            skill_usage_receipt=skill_usage_receipt,
        ),
    )
    return CreationResultV1(
        creation_job_id=job.creation_job_id,
        correlation_id=job.correlation_id,
        subject_version=job.subject_version,
        attempt=job.attempt,
        status="succeeded",
        package_manifest_ref=receipt.package_manifest_ref,
        artifact_refs=(
            receipt.candidate_manifest_ref,
            receipt.source_archive_ref,
        ),
        skill_usage_receipt_ref=receipt.skill_usage_receipt_ref,
    )


def install_creation_skill_receipt(
    job: CreationJobV1,
    *,
    source_path: Path,
    output_path: Path,
) -> Path:
    """Install the exact Captain-supplied Hermes receipt without mutation."""

    if source_path.is_symlink() or not source_path.is_file():
        raise FileNotFoundError("Forge skill usage receipt file is unavailable")
    content = source_path.read_bytes()
    try:
        receipt = ForgeBuildSkillUsageReceiptV1.model_validate_json(content)
    except ValueError as exc:
        raise ValueError("Forge skill usage receipt is invalid") from exc
    if (
        receipt.creation_job_id != job.creation_job_id
        or receipt.factory_job_id != job.factory_job_id
        or receipt.correlation_id != job.correlation_id
        or receipt.subject_version != job.subject_version
        or receipt.attempt != job.attempt
        or receipt.idempotency_key != job.idempotency_key
        or receipt.released_skill != job.released_skill
        or receipt.public_assertion_ids != job.public_assertion_ids
    ):
        raise ValueError("Forge skill usage receipt does not match creation job")
    target = output_path / "evidence" / "hermes-factory-skill-usage-receipt.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.is_symlink() or target.read_bytes() != content:
            raise ValueError("generated Forge skill usage receipt changed Captain evidence")
        return target
    with target.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    return target


def write_creation_result_atomic(path: Path, result: CreationResultV1) -> None:
    """Create one result exactly once; stale success files are never reused."""

    if path.exists():
        raise FileExistsError("creation result file already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    if temporary.exists():
        raise FileExistsError("creation result temporary file already exists")
    content = json.dumps(
        result.model_dump(mode="json", by_alias=True, exclude_none=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
