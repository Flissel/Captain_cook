from __future__ import annotations

import asyncio
import hashlib

import pytest

from agenten.agent_factory.evidence_store import FilesystemFactoryEvidenceStore
from agenten.agent_runtime.contracts import ArtifactRef
from tests.agent_factory.test_state_machine import job


REFERENCE_JOB_ID = "abcdefab-cdef-4abc-8def-abcdefabcdef"


@pytest.mark.asyncio
async def test_filesystem_store_persists_and_verifies_hermes_transcript(tmp_path) -> None:
    store = FilesystemFactoryEvidenceStore(tmp_path)
    transcript = b'{"schema":"captain.agent-factory-block.v1"}'

    reference = await store.persist(job(), transcript)

    assert reference.uri.startswith(f"artifact://factory-evidence/{job().job_id}/")
    assert reference.sha256 == hashlib.sha256(transcript).hexdigest()
    assert await store.read(reference) == transcript
    await store.require(reference)


@pytest.mark.asyncio
async def test_filesystem_store_handles_concurrent_identical_replays(tmp_path) -> None:
    store = FilesystemFactoryEvidenceStore(tmp_path)
    transcript = b'{"schema":"captain.agent-factory-block.v1"}'

    references = await asyncio.gather(
        *(store.persist(job(), transcript) for _ in range(12))
    )

    assert len(set(references)) == 1
    assert await store.read(references[0]) == transcript


def test_write_once_rejects_changed_content_for_an_existing_path(tmp_path) -> None:
    path = tmp_path / "immutable-evidence.json"

    FilesystemFactoryEvidenceStore._write_once(path, b"first")

    with pytest.raises(ValueError, match="collision"):
        FilesystemFactoryEvidenceStore._write_once(path, b"changed")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "job_segment",
    (
        REFERENCE_JOB_ID.upper(),
        REFERENCE_JOB_ID.replace("-", ""),
        "{" + REFERENCE_JOB_ID + "}",
    ),
)
async def test_filesystem_store_rejects_noncanonical_job_uuid_segments(
    tmp_path,
    job_segment: str,
) -> None:
    store = FilesystemFactoryEvidenceStore(tmp_path)
    factory_job = job().model_copy(update={"job_id": REFERENCE_JOB_ID})
    reference = await store.persist(factory_job, b"canonical")
    noncanonical = ArtifactRef(
        uri=f"artifact://factory-evidence/{job_segment}/{reference.sha256}",
        sha256=reference.sha256,
        media_type=reference.media_type,
    )

    with pytest.raises(ValueError, match="canonical"):
        await store.read(noncanonical)
