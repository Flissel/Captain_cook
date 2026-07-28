"""Fail-closed file boundary for one-shot Captain creation runs."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .artifact_store import FilesystemCreationArtifactStore
from .contracts import CreationJobV1, CreationResultV1
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
