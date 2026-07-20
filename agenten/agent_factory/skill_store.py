"""Private, append-only persistence for Hermes skill-evaluation records."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Mapping, Protocol
from uuid import UUID

from agenten.agent_factory.evidence_store import SkillEvaluationEvidenceStore
from agenten.agent_factory.skill_evaluation import (
    HermesSkillCandidate,
    HermesSkillEvaluationEvidence,
    HermesSkillUsageReceipt,
    ToolGapMarker,
)
from agenten.agent_runtime.contracts import ArtifactRef


_SECRET_KEY_PATTERN = re.compile(
    r"(?i)(?:^|_)(?:api[_-]?key|authorization|credential|password|private[_-]?key|secret|token)(?:$|_)"
)
_SECRET_VALUE_PATTERN = re.compile(
    r"(?i)(?:\b(?:sk-[a-z0-9_-]{8,}|bearer\s+\S+)|\b(?:api[_-]?key|authorization|credential|password|secret|token)\b\s*[=:])"
)
_ENDPOINT_PATTERN = re.compile(r"(?i)https?://[^\s\"'<>]+")


@dataclass(frozen=True)
class StoredSkillEvaluation:
    """Private append-only view returned by the repository read port."""

    evidence: HermesSkillEvaluationEvidence
    evidence_ref: ArtifactRef
    receipt_ref: ArtifactRef
    tool_gap_refs: tuple[tuple[str, ArtifactRef], ...]
    candidate_ref: ArtifactRef | None


class SkillEvaluationRepository(Protocol):
    """Private aggregate port; a Gateway adapter can implement this later."""

    def record_receipt(self, receipt: HermesSkillUsageReceipt, reference: ArtifactRef) -> bool:
        """Append a receipt, returning false when an identical receipt is replayed."""

    def record_tool_gap(
        self,
        evaluation_id: UUID,
        marker: ToolGapMarker,
        reference: ArtifactRef,
    ) -> bool:
        """Append a private tool-gap marker for one evaluation."""

    def record_evaluation(
        self,
        evidence: HermesSkillEvaluationEvidence,
        reference: ArtifactRef,
    ) -> bool:
        """Append the immutable evaluation envelope."""

    def retain_candidate(
        self,
        evaluation_id: UUID,
        candidate: HermesSkillCandidate,
        reference: ArtifactRef,
    ) -> bool:
        """Retain a private candidate after validated evidence exists."""

    def receipt(self, receipt_id: UUID) -> tuple[HermesSkillUsageReceipt, ArtifactRef] | None:
        """Return one recorded receipt by its immutable identity."""

    def evaluation(self, evaluation_id: UUID) -> StoredSkillEvaluation | None:
        """Return one private evaluation aggregate."""


@dataclass
class InMemorySkillEvaluationRepository:
    """Deterministic append-only repository used by focused tests."""

    _receipts: dict[UUID, tuple[HermesSkillUsageReceipt, ArtifactRef]] = field(
        default_factory=dict
    )
    _tool_gaps: dict[tuple[UUID, str], tuple[ToolGapMarker, ArtifactRef]] = field(
        default_factory=dict
    )
    _evaluations: dict[UUID, tuple[HermesSkillEvaluationEvidence, ArtifactRef]] = field(
        default_factory=dict
    )
    _candidates: dict[UUID, tuple[HermesSkillCandidate, ArtifactRef]] = field(
        default_factory=dict
    )
    _candidate_ids: dict[str, UUID] = field(default_factory=dict)

    def record_receipt(self, receipt: HermesSkillUsageReceipt, reference: ArtifactRef) -> bool:
        return self._append(self._receipts, receipt.receipt_id, (receipt, reference), "receipt_id")

    def record_tool_gap(
        self,
        evaluation_id: UUID,
        marker: ToolGapMarker,
        reference: ArtifactRef,
    ) -> bool:
        return self._append(
            self._tool_gaps,
            (evaluation_id, marker.gap_id),
            (marker, reference),
            "gap_id",
        )

    def record_evaluation(
        self,
        evidence: HermesSkillEvaluationEvidence,
        reference: ArtifactRef,
    ) -> bool:
        return self._append(
            self._evaluations,
            evidence.evidence_id,
            (evidence, reference),
            "evidence_id",
        )

    def retain_candidate(
        self,
        evaluation_id: UUID,
        candidate: HermesSkillCandidate,
        reference: ArtifactRef,
    ) -> bool:
        existing_evaluation = self._candidate_ids.get(candidate.candidate_id)
        if existing_evaluation is not None and existing_evaluation != evaluation_id:
            raise ValueError("candidate_id already exists under a different evaluation")
        appended = self._append(
            self._candidates,
            evaluation_id,
            (candidate, reference),
            "evaluation_id",
        )
        self._candidate_ids[candidate.candidate_id] = evaluation_id
        return appended

    def receipt(self, receipt_id: UUID) -> tuple[HermesSkillUsageReceipt, ArtifactRef] | None:
        return self._receipts.get(receipt_id)

    def evaluation(self, evaluation_id: UUID) -> StoredSkillEvaluation | None:
        evaluation = self._evaluations.get(evaluation_id)
        if evaluation is None:
            return None
        evidence, evidence_ref = evaluation
        receipt = self._receipts.get(evidence.receipt.receipt_id)
        if receipt is None:
            raise ValueError("recorded evaluation is missing its usage receipt")
        tool_gaps = tuple(
            (gap_id, reference)
            for (stored_evaluation_id, gap_id), (_, reference) in self._tool_gaps.items()
            if stored_evaluation_id == evaluation_id
        )
        candidate = self._candidates.get(evaluation_id)
        return StoredSkillEvaluation(
            evidence=evidence,
            evidence_ref=evidence_ref,
            receipt_ref=receipt[1],
            tool_gap_refs=tool_gaps,
            candidate_ref=None if candidate is None else candidate[1],
        )

    @staticmethod
    def _append(
        records: dict[object, object],
        identity: object,
        incoming: object,
        identity_name: str,
    ) -> bool:
        existing = records.get(identity)
        if existing is not None:
            if existing != incoming:
                raise ValueError(f"{identity_name} already exists with different content")
            return False
        records[identity] = incoming
        return True


class SkillEvaluationStore:
    """Persist only redacted, private candidate/evaluation records.

    This boundary neither publishes skills nor creates Captain lifecycle blocks.
    """

    def __init__(
        self,
        *,
        repository: SkillEvaluationRepository,
        evidence_store: SkillEvaluationEvidenceStore,
    ) -> None:
        self._repository = repository
        self._evidence_store = evidence_store

    async def record_receipt(
        self,
        receipt: HermesSkillUsageReceipt,
        *,
        metadata: Mapping[str, object] | None = None,
    ) -> ArtifactRef:
        canonical = _canonical_receipt(receipt)
        existing = self._repository.receipt(canonical.receipt_id)
        content = _record_content("receipt", str(canonical.receipt_id), canonical, metadata)
        reference = await self._evidence_store.persist(
            canonical.request_id,
            "receipts",
            str(canonical.receipt_id),
            content,
        )
        if existing is not None:
            if existing != (canonical, reference):
                raise ValueError("receipt_id already exists with different content")
            return reference
        self._repository.record_receipt(canonical, reference)
        return reference

    async def record_tool_gap(
        self,
        evaluation_id: UUID,
        marker: ToolGapMarker,
        *,
        metadata: Mapping[str, object] | None = None,
    ) -> ArtifactRef:
        canonical = _canonical_tool_gap(marker)
        content = _record_content("tool_gap", canonical.gap_id, canonical, metadata)
        reference = await self._evidence_store.persist(
            evaluation_id,
            "tool-gaps",
            canonical.gap_id,
            content,
        )
        self._repository.record_tool_gap(evaluation_id, canonical, reference)
        return reference

    async def record_evaluation(
        self,
        evidence: HermesSkillEvaluationEvidence,
        *,
        metadata: Mapping[str, object] | None = None,
    ) -> ArtifactRef:
        canonical = _canonical_evidence(evidence)
        recorded_receipt = self._repository.receipt(canonical.receipt.receipt_id)
        if recorded_receipt is None or recorded_receipt[0] != canonical.receipt:
            raise ValueError("evaluation evidence requires its recorded usage receipt")
        content = _record_content("evaluation", str(canonical.evidence_id), canonical, metadata)
        reference = await self._evidence_store.persist(
            canonical.evidence_id,
            "evaluations",
            str(canonical.evidence_id),
            content,
        )
        self._repository.record_evaluation(canonical, reference)
        return reference

    async def retain_candidate(
        self,
        evaluation_id: UUID,
        candidate: HermesSkillCandidate | None,
        *,
        metadata: Mapping[str, object] | None = None,
    ) -> ArtifactRef:
        if candidate is None:
            raise ValueError("private candidate retention requires a candidate")
        canonical = _canonical_candidate(candidate)
        if canonical.status != "private_candidate":
            raise ValueError("private candidate store only accepts private_candidate status")
        stored = self._repository.evaluation(evaluation_id)
        if not _has_successful_evidence(stored, canonical):
            raise ValueError("candidate retention requires successful evaluation evidence")
        content = _record_content("candidate", canonical.candidate_id, canonical, metadata)
        reference = await self._evidence_store.persist(
            evaluation_id,
            "candidates",
            canonical.candidate_id,
            content,
        )
        self._repository.retain_candidate(evaluation_id, canonical, reference)
        return reference

    def get_evaluation(self, evaluation_id: UUID) -> StoredSkillEvaluation | None:
        return self._repository.evaluation(evaluation_id)

    def fetch_evaluation(self, evaluation_id: UUID) -> StoredSkillEvaluation | None:
        """Compatibility-oriented explicit read name for Gateway adapters."""

        return self.get_evaluation(evaluation_id)

    async def require(self, reference: ArtifactRef) -> None:
        await self._evidence_store.require(reference)


def _has_successful_evidence(
    stored: StoredSkillEvaluation | None,
    candidate: HermesSkillCandidate,
) -> bool:
    if stored is None or stored.evidence.candidate != candidate:
        return False
    evidence = stored.evidence
    return (
        evidence.outcome == "passed"
        and evidence.receipt.outcome == "passed"
        and {check.kind for check in evidence.checks} == {"build", "test"}
        and all(check.status == "passed" for check in evidence.checks)
    )


def _record_content(
    record_kind: str,
    record_id: str,
    model: HermesSkillUsageReceipt | ToolGapMarker | HermesSkillEvaluationEvidence | HermesSkillCandidate,
    metadata: Mapping[str, object] | None,
) -> bytes:
    payload = model.model_dump(mode="json", by_alias=True)
    _reject_sensitive_data(payload, "record")
    safe_metadata = _validated_metadata(metadata)
    return json.dumps(
        {
            "schema": "captain.private-skill-evaluation-record.v1",
            "record_kind": record_kind,
            "record_id": record_id,
            "payload": payload,
            "metadata": safe_metadata,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _validated_metadata(metadata: Mapping[str, object] | None) -> dict[str, object]:
    safe_metadata = {} if metadata is None else dict(metadata)
    try:
        json.dumps(safe_metadata, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("metadata must contain JSON-compatible structured values") from exc
    _reject_sensitive_data(safe_metadata, "metadata")
    return safe_metadata


def _reject_sensitive_data(value: object, location: str) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            key_text = str(key)
            if _SECRET_KEY_PATTERN.search(key_text):
                raise ValueError(f"{location} contains a secret-like field")
            _reject_sensitive_data(nested, f"{location}.{key_text}")
        return
    if isinstance(value, (tuple, list)):
        for index, nested in enumerate(value):
            _reject_sensitive_data(nested, f"{location}[{index}]")
        return
    if isinstance(value, str):
        if _ENDPOINT_PATTERN.search(value):
            raise ValueError(f"{location} contains a raw endpoint")
        if _SECRET_VALUE_PATTERN.search(value):
            raise ValueError(f"{location} contains a secret-like value")


def _canonical_receipt(value: HermesSkillUsageReceipt) -> HermesSkillUsageReceipt:
    return HermesSkillUsageReceipt.model_validate(value.model_dump(mode="json", by_alias=True))


def _canonical_tool_gap(value: ToolGapMarker) -> ToolGapMarker:
    return ToolGapMarker.model_validate(value.model_dump(mode="json", by_alias=True))


def _canonical_evidence(value: HermesSkillEvaluationEvidence) -> HermesSkillEvaluationEvidence:
    return HermesSkillEvaluationEvidence.model_validate(value.model_dump(mode="json", by_alias=True))


def _canonical_candidate(value: HermesSkillCandidate) -> HermesSkillCandidate:
    return HermesSkillCandidate.model_validate(value.model_dump(mode="json", by_alias=True))
