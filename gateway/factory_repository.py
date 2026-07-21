"""Gateway-backed adapter for Captain's factory coordinator port."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import HTTPException

from datetime import datetime

from agenten.agent_factory.contracts import (
    AgentFactoryJobV3,
    FactoryEvidenceBlock,
    FactoryJob,
    FactoryLease,
    FactoryRole,
)
from agenten.agent_factory.execution_budget import (
    BudgetExhausted,
    FactoryBudgetPort,
    FactoryBudgetProjection,
    FactoryBudgetReservationV1,
    FactoryBudgetWriteReceipt,
    FactoryUsageReceiptV1,
    ReleaseReason,
)
from agenten.agent_factory.factory_live_runner import (
    FactoryLiveEffectClaim,
    FactoryLiveEffectLedger,
    FactoryLiveEffectOutcomeV1,
    FactoryLiveEffectRecord,
    FactoryLiveEffectRequestV1,
    FactoryLiveEffectWriteReceipt,
)
from agenten.agent_factory.leases import FactoryLeaseDenied, FactoryLeasePort, validate_factory_lease
from agenten.agent_factory.release_gate import FactoryReleaseDecision
from agenten.agent_factory.service import FactoryRepository, FactoryRepositoryError
from agenten.agent_factory.skill_store import StoredSkillEvaluation
from agenten.agent_factory.skill_evaluation import ReleasedHermesSkill
from agenten.agent_factory.skill_workflow_contracts import FactorySkillStep
from gateway.store import GatewayStore
from gateway.contracts import (
    FactoryBudgetReleaseRequest,
    FactorySkillAssignmentV1,
    FactoryUsageSubmissionV2,
)
from gateway.contracts import FactoryWorkflowArtifact


class FactorySkillAssignmentSource(Protocol):
    def released_for(
        self,
        job: FactoryJob,
        step: FactorySkillStep,
    ) -> ReleasedHermesSkill: ...


class GatewayFactoryRepository(FactoryRepository):
    """Use GatewayStore as the sole durable factory lifecycle authority."""

    def __init__(self, store: GatewayStore) -> None:
        self._store = store

    def register(self, job: FactoryJob) -> None:
        self._translate(lambda: self._store.record_factory_job(job))

    def job(self, job_id: UUID) -> FactoryJob:
        return self._translate(lambda: self._store.factory_job(job_id).job)

    def append(self, block: FactoryEvidenceBlock) -> bool:
        receipt = self._translate(lambda: self._store.record_factory_block(block))
        return not receipt.replayed

    def blocks(self, job_id: UUID) -> tuple[FactoryEvidenceBlock, ...]:
        return self._translate(lambda: self._store.factory_job(job_id).blocks)

    def evaluation_for_job(self, job_id: UUID) -> StoredSkillEvaluation | None:
        return self._translate(lambda: self._store.factory_skill_evaluation(job_id))

    def release_decision_for_job(self, job_id: UUID) -> FactoryReleaseDecision | None:
        return self._translate(lambda: self._store.factory_release_decision(job_id))

    def workflow_artifacts(self, job_id: UUID) -> tuple[FactoryWorkflowArtifact, ...]:
        return self._translate(lambda: self._store.factory_workflow_artifacts(job_id))

    def workflow_budget_projection(
        self,
        job_id: UUID,
    ) -> FactoryBudgetProjection:
        return self._translate(lambda: self._store.factory_budget(job_id))

    def workflow_usage_receipts(
        self,
        job_id: UUID,
    ) -> tuple[FactoryUsageReceiptV1, ...]:
        return self._translate(lambda: self._store.factory_usage_receipts(job_id))

    def seed_released_skill_assignments(
        self,
        job: AgentFactoryJobV3,
        source: FactorySkillAssignmentSource,
    ) -> None:
        """Persist all exact job-step envelopes from an explicit Captain source."""

        self._require_exact_job(job)
        resolved = tuple(
            (step, source.released_for(job, step))
            for step in FactorySkillStep
        )
        for step, skill in resolved:
            self._translate(lambda skill=skill: self._store.record_released_factory_skill(skill))
            assignment = FactorySkillAssignmentV1(
                job_id=job.job_id,
                step=step,
                released_skill=skill,
            )
            self._translate(
                lambda assignment=assignment: self._store.record_factory_skill_assignment(
                    assignment
                )
            )

    def released_for(
        self,
        job: FactoryJob,
        step: FactorySkillStep,
    ) -> ReleasedHermesSkill:
        self._require_exact_job(job)
        assignment = self._translate(
            lambda: self._store.factory_skill_assignment(job.job_id, step)
        )
        return assignment.released_skill

    def _require_exact_job(self, job: FactoryJob) -> None:
        stored = self._translate(lambda: self._store.factory_job(job.job_id).job)
        if stored != job:
            raise FactoryRepositoryError(
                "factory skill assignment job envelope does not match Gateway"
            )

    @staticmethod
    def _translate(operation):
        try:
            return operation()
        except HTTPException as exc:
            raise FactoryRepositoryError(str(exc.detail)) from exc


class GatewayFactoryLeases(FactoryLeasePort):
    """Resolve the current valid role lease only from Captain's ledger."""

    def __init__(self, store: GatewayStore) -> None:
        self._store = store

    def active(
        self,
        job: FactoryJob,
        role: FactoryRole,
        attempt: int,
        now: datetime,
    ) -> FactoryLease:
        try:
            leases = self._store.factory_job(job.job_id).leases
        except HTTPException as exc:
            raise FactoryLeaseDenied(str(exc.detail)) from exc
        candidates = [
            lease for lease in leases
            if lease.role is role and lease.attempt == attempt and lease.subject_version == job.subject_version
        ]
        if not candidates:
            raise FactoryLeaseDenied("no active factory lease exists for the requested role")
        candidates.sort(key=lambda lease: lease.issued_at, reverse=True)
        return validate_factory_lease(
            candidates[0], job=job, role=role, attempt=attempt, now=now
        )


class GatewayFactoryBudgetLedger(FactoryBudgetPort):
    """Route budget authority through GatewayStore without database credentials."""

    def __init__(self, store: GatewayStore) -> None:
        self._store = store

    def reserve(
        self,
        job: AgentFactoryJobV3,
        *,
        attempt: int,
        requested_usd: Decimal,
        now: datetime,
    ) -> FactoryBudgetReservationV1:
        policy_digest = _execution_policy_digest(job.execution_policy)
        reservation_id = uuid5(
            NAMESPACE_URL,
            "|".join(
                (
                    "factory-budget-reservation",
                    str(job.job_id),
                    str(job.subject_version),
                    str(attempt),
                    _canonical_decimal(requested_usd),
                    now.isoformat(),
                )
            ),
        )
        reservation = FactoryBudgetReservationV1(
            schema_name="captain.factory-budget-reservation.v1",
            reservation_id=reservation_id,
            job_id=job.job_id,
            correlation_id=job.correlation_id,
            subject_version=job.subject_version,
            execution_policy_sha256=policy_digest,
            attempt=attempt,
            requested_usd=requested_usd,
            reserved_at=now,
            expires_at=job.deadline_at,
        )
        return self._translate_budget(
            lambda: self._store.reserve_factory_budget(reservation).reservation
        )

    def record_usage(
        self,
        job: AgentFactoryJobV3,
        reservation: FactoryBudgetReservationV1,
        receipt: FactoryUsageReceiptV1,
    ) -> FactoryBudgetWriteReceipt:
        if (
            receipt.reservation_id != reservation.reservation_id
            or receipt.job_id != job.job_id
            or receipt.correlation_id != job.correlation_id
            or receipt.attempt != reservation.attempt
        ):
            raise ValueError("factory usage receipt binding mismatch")
        leases = self._translate_budget(
            lambda: self._store.factory_job(job.job_id).leases
        )
        candidates = tuple(
            lease
            for lease in leases
            if lease.job_id == job.job_id
            and lease.correlation_id == job.correlation_id
            and lease.subject_version == job.subject_version
            and lease.attempt == receipt.attempt
            and lease.role is FactoryRole.REAL_CASE_TESTER
            and "model.invoke" in lease.capabilities
            and lease.issued_at <= receipt.started_at
            and receipt.ended_at < lease.expires_at
        )
        if len(candidates) != 1:
            raise ValueError("usage requires one exact active factory lease")
        submission = FactoryUsageSubmissionV2(
            subject_version=job.subject_version,
            lease_id=candidates[0].lease_id,
            receipt=receipt,
        )
        return self._translate_budget(
            lambda: self._store.record_factory_usage(submission)
        )

    def release(
        self,
        job: AgentFactoryJobV3,
        reservation: FactoryBudgetReservationV1,
        *,
        now: datetime,
        reason: ReleaseReason,
    ) -> FactoryBudgetWriteReceipt:
        release_id = uuid5(
            NAMESPACE_URL,
            f"factory-budget-release|{reservation.reservation_id}|{reason}",
        )
        return self._translate_budget(
            lambda: self._store.release_factory_budget(
                FactoryBudgetReleaseRequest(
                    release_id=release_id,
                    reservation_id=reservation.reservation_id,
                    job_id=job.job_id,
                    correlation_id=job.correlation_id,
                    subject_version=job.subject_version,
                    attempt=reservation.attempt,
                    released_at=now,
                    reason=reason,
                )
            )
        )

    def projection(self, job_id: UUID) -> FactoryBudgetProjection:
        return self._translate_budget(lambda: self._store.factory_budget(job_id))

    @staticmethod
    def _translate_budget(operation):
        try:
            return operation()
        except HTTPException as exc:
            detail = str(exc.detail)
            if exc.status_code == 409 and any(
                marker in detail.lower()
                for marker in (
                    "budget is exhausted",
                    "budget exhausted",
                    "exceeds its reservation",
                    "exceeds the job usd budget",
                    "exceeds the job budget",
                )
            ):
                raise BudgetExhausted(detail) from exc
            raise ValueError(detail) from exc


class GatewayFactoryLiveEffectLedger(FactoryLiveEffectLedger):
    """Persist pre-effect claims and completions through the sole-writer Gateway."""

    def __init__(self, store: GatewayStore) -> None:
        self._store = store

    def claim(
        self,
        request: FactoryLiveEffectRequestV1,
    ) -> FactoryLiveEffectClaim:
        return self._translate(
            lambda: self._store.claim_factory_live_effect(request)
        )

    def complete(
        self,
        request: FactoryLiveEffectRequestV1,
        outcome: FactoryLiveEffectOutcomeV1,
    ) -> FactoryLiveEffectWriteReceipt:
        return self._translate(
            lambda: self._store.complete_factory_live_effect(request, outcome)
        )

    def history(self, job_id: UUID) -> tuple[FactoryLiveEffectRecord, ...]:
        return self._translate(
            lambda: self._store.factory_live_effect_history(job_id)
        )

    @staticmethod
    def _translate(operation):
        try:
            return operation()
        except HTTPException as exc:
            raise ValueError(str(exc.detail)) from exc


def _execution_policy_digest(policy) -> str:
    payload = policy.model_dump(mode="json", by_alias=True)
    payload["max_cost_usd"] = _canonical_decimal(policy.max_cost_usd)
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _canonical_decimal(value: Decimal) -> str:
    rendered = format(value, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered
