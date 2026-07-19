from __future__ import annotations

import hashlib

import pytest

from agenten.agent_factory.evidence_store import FilesystemFactoryEvidenceStore
from tests.agent_factory.test_state_machine import job


@pytest.mark.asyncio
async def test_filesystem_store_persists_and_verifies_hermes_transcript(tmp_path) -> None:
    store = FilesystemFactoryEvidenceStore(tmp_path)
    transcript = b'{"schema":"captain.agent-factory-block.v1"}'

    reference = await store.persist(job(), transcript)

    assert reference.uri.startswith(f"artifact://factory-evidence/{job().job_id}/")
    assert reference.sha256 == hashlib.sha256(transcript).hexdigest()
    assert await store.read(reference) == transcript
    await store.require(reference)
