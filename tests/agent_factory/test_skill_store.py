from __future__ import annotations

from pathlib import Path

import pytest

from agenten.agent_factory.evidence_store import FilesystemSkillEvaluationEvidenceStore
from agenten.agent_factory.skill_evaluation import (
    HermesSkillCandidate,
    HermesSkillEvaluationEvidence,
    HermesSkillUsageReceipt,
    ToolGapMarker,
)
from agenten.agent_factory.skill_store import (
    InMemorySkillEvaluationRepository,
    SkillEvaluationStore,
)
from tests.agent_factory.test_skill_evaluation_contracts import (
    candidate_payload,
    evidence_payload,
    gap_payload,
    receipt_payload,
)


def _store(tmp_path: Path) -> SkillEvaluationStore:
    return SkillEvaluationStore(
        repository=InMemorySkillEvaluationRepository(),
        evidence_store=FilesystemSkillEvaluationEvidenceStore(tmp_path),
    )


@pytest.mark.asyncio
async def test_store_records_private_receipt_gap_evaluation_and_candidate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    receipt = HermesSkillUsageReceipt.model_validate(receipt_payload())
    gap = ToolGapMarker.model_validate(gap_payload(severity="optional"))
    evidence = HermesSkillEvaluationEvidence.model_validate(evidence_payload())

    receipt_ref = await store.record_receipt(receipt)
    gap_ref = await store.record_tool_gap(evidence.evidence_id, gap)
    evidence_ref = await store.record_evaluation(evidence)
    candidate_ref = await store.retain_candidate(evidence.evidence_id, evidence.candidate)

    stored = store.get_evaluation(evidence.evidence_id)
    assert stored is not None
    assert stored.evidence == evidence
    assert stored.receipt_ref == receipt_ref
    assert stored.evidence_ref == evidence_ref
    assert stored.candidate_ref == candidate_ref
    assert stored.tool_gap_refs == ((gap.gap_id, gap_ref),)
    await store.require(evidence_ref)


@pytest.mark.asyncio
async def test_store_identical_replays_are_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    receipt = HermesSkillUsageReceipt.model_validate(receipt_payload())
    gap = ToolGapMarker.model_validate(gap_payload(severity="optional"))
    evidence = HermesSkillEvaluationEvidence.model_validate(evidence_payload())

    assert await store.record_receipt(receipt) == await store.record_receipt(receipt)
    assert await store.record_tool_gap(evidence.evidence_id, gap) == await store.record_tool_gap(
        evidence.evidence_id, gap
    )
    assert await store.record_evaluation(evidence) == await store.record_evaluation(evidence)
    assert await store.retain_candidate(evidence.evidence_id, evidence.candidate) == await store.retain_candidate(
        evidence.evidence_id, evidence.candidate
    )


@pytest.mark.asyncio
async def test_store_rejects_changed_replay_for_the_same_record_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    receipt = HermesSkillUsageReceipt.model_validate(receipt_payload())

    await store.record_receipt(receipt)

    changed = receipt.model_copy(update={"outcome": "redo"})
    with pytest.raises(ValueError, match="different content"):
        await store.record_receipt(changed)


@pytest.mark.asyncio
async def test_store_rejects_candidate_until_successful_evidence_is_recorded(tmp_path: Path) -> None:
    store = _store(tmp_path)
    evidence = HermesSkillEvaluationEvidence.model_validate(evidence_payload())

    with pytest.raises(ValueError, match="successful evaluation evidence"):
        await store.retain_candidate(evidence.evidence_id, evidence.candidate)

    await store.record_receipt(evidence.receipt)
    failed = evidence.model_copy(update={"outcome": "failed"})
    await store.record_evaluation(failed)
    with pytest.raises(ValueError, match="successful evaluation evidence"):
        await store.retain_candidate(evidence.evidence_id, evidence.candidate)


@pytest.mark.asyncio
async def test_store_rejects_a_candidate_after_any_failed_build_or_test(tmp_path: Path) -> None:
    store = _store(tmp_path)
    evidence = HermesSkillEvaluationEvidence.model_validate(evidence_payload())
    await store.record_receipt(evidence.receipt)
    failed_check = evidence.checks[1].model_copy(update={"status": "failed"})
    failed = evidence.model_copy(
        update={"checks": (evidence.checks[0], failed_check), "outcome": "failed"}
    )
    await store.record_evaluation(failed)

    with pytest.raises(ValueError, match="successful evaluation evidence"):
        await store.retain_candidate(evidence.evidence_id, evidence.candidate)


@pytest.mark.asyncio
async def test_store_never_accepts_released_or_shared_candidate_status(tmp_path: Path) -> None:
    store = _store(tmp_path)
    evidence = HermesSkillEvaluationEvidence.model_validate(evidence_payload())
    await store.record_receipt(evidence.receipt)
    await store.record_evaluation(evidence)
    released = HermesSkillCandidate.model_construct(
        candidate_id=evidence.candidate.candidate_id,
        request_id=evidence.candidate.request_id,
        created_at=evidence.candidate.created_at,
        producer=evidence.candidate.producer,
        content_ref=evidence.candidate.content_ref,
        content_sha256=evidence.candidate.content_sha256,
        parent_released_skill=evidence.candidate.parent_released_skill,
        creation_reason=evidence.candidate.creation_reason,
        status="released",
    )

    with pytest.raises(ValueError, match="private_candidate"):
        await store.retain_candidate(evidence.evidence_id, released)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [
        {"api_key": "sk-test-secret"},
        {"nested": {"authorization": "Bearer test-secret"}},
        {"n8n_endpoint": "http://localhost:5678/api/v1"},
        {"diagnostic": "captured output: Bearer real-token"},
        {"diagnostic": "captured output: n8n at http://localhost:5678/api/v1"},
    ],
)
async def test_store_rejects_secret_like_or_raw_endpoint_metadata(
    tmp_path: Path, metadata: dict[str, object]
) -> None:
    store = _store(tmp_path)
    receipt = HermesSkillUsageReceipt.model_validate(receipt_payload())

    with pytest.raises(ValueError, match="metadata"):
        await store.record_receipt(receipt, metadata=metadata)
