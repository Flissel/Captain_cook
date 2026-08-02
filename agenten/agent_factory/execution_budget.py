"""Captain-owned USD reservations and provider usage accounting."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from threading import Lock
from typing import Literal, Protocol
from uuid import UUID, uuid5

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)

from agenten.agent_factory.contracts import AgentFactoryJobV3, FactoryLease
from agenten.agent_factory.execution_policy import FactoryExecutionPolicyV1
from agenten.agent_runtime.contracts import ArtifactRef, SHA256_PATTERN


_BUDGET_NAMESPACE = UUID("337539e1-8858-4d99-b253-665803c59a48")
ReleaseReason = Literal["provider_failed", "cancelled", "unused"]


class BudgetExhausted(RuntimeError):
    """The requested paid effect exceeds the released job budget."""


class _FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class FactoryUsageReceiptV1(_FrozenContract):
    schema_name: Literal["captain.factory-usage-receipt.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    receipt_id: UUID
    reservation_id: UUID
    job_id: UUID
    correlation_id: UUID
    attempt: int = Field(ge=1, le=5, strict=True)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    input_units: int = Field(ge=0, strict=True)
    output_units: int = Field(ge=0, strict=True)
    cost_usd: Decimal
    started_at: datetime
    ended_at: datetime
    evidence_ref: ArtifactRef

    @field_validator("provider", "model")
    @classmethod
    def require_named_provider_field(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("provider and model must not be blank")
        return normalized

    @field_validator("cost_usd", mode="before")
    @classmethod
    def require_known_cost(cls, value: object) -> Decimal:
        if value is None:
            raise ValueError("a known USD cost is required")
        return _parse_usage_usd(value, "cost_usd")

    @field_validator("started_at", "ended_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def require_ordered_times(self) -> "FactoryUsageReceiptV1":
        if self.ended_at < self.started_at:
            raise ValueError("ended_at must not be before started_at")
        return self


class FactoryBudgetReservationV1(_FrozenContract):
    schema_name: Literal["captain.factory-budget-reservation.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    reservation_id: UUID
    job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    execution_policy_sha256: str = Field(pattern=SHA256_PATTERN)
    attempt: int = Field(ge=1, le=5, strict=True)
    requested_usd: Decimal
    reserved_at: datetime
    expires_at: datetime
    status: Literal["active"] = "active"

    @field_validator("requested_usd", mode="before")
    @classmethod
    def require_positive_request(cls, value: object) -> Decimal:
        requested = _parse_usd(value, "requested_usd")
        if requested <= 0:
            raise ValueError("requested_usd must be positive")
        return requested

    @field_validator("reserved_at", "expires_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def require_active_window(self) -> "FactoryBudgetReservationV1":
        if self.expires_at <= self.reserved_at:
            raise ValueError("budget reservation must expire after it is reserved")
        return self


class FactoryBudgetWriteReceipt(_FrozenContract):
    event_id: UUID
    job_id: UUID
    replayed: StrictBool


class FactoryBudgetProjection(_FrozenContract):
    job_id: UUID
    limit_usd: Decimal
    consumed_usd: Decimal
    reserved_usd: Decimal
    remaining_usd: Decimal
    active_reservation_ids: tuple[UUID, ...] = ()

    @field_validator("limit_usd", "reserved_usd", mode="before")
    @classmethod
    def require_canonical_reserved_amount(cls, value: object) -> Decimal:
        return _parse_usd(value, "projection amount")

    @field_validator("consumed_usd", "remaining_usd", mode="before")
    @classmethod
    def require_canonical_usage_amount(cls, value: object) -> Decimal:
        return _parse_usage_usd(value, "projection amount")

    @model_validator(mode="after")
    def require_consistent_totals(self) -> "FactoryBudgetProjection":
        expected = self.limit_usd - self.consumed_usd - self.reserved_usd
        if self.remaining_usd != expected or expected < 0:
            raise ValueError("factory budget projection totals are inconsistent")
        if len(self.active_reservation_ids) != len(set(self.active_reservation_ids)):
            raise ValueError("active reservation IDs must not contain duplicates")
        return self


class FactoryBudgetPort(Protocol):
    def reserve(
        self,
        job: AgentFactoryJobV3,
        *,
        attempt: int,
        requested_usd: Decimal,
        now: datetime,
    ) -> FactoryBudgetReservationV1: ...

    def record_usage(
        self,
        job: AgentFactoryJobV3,
        reservation: FactoryBudgetReservationV1,
        receipt: FactoryUsageReceiptV1,
        *,
        lease: FactoryLease | None = None,
    ) -> FactoryBudgetWriteReceipt: ...

    def release(
        self,
        job: AgentFactoryJobV3,
        reservation: FactoryBudgetReservationV1,
        *,
        now: datetime,
        reason: ReleaseReason,
    ) -> FactoryBudgetWriteReceipt: ...

    def projection(self, job_id: UUID) -> FactoryBudgetProjection: ...


@dataclass(frozen=True)
class _ReservationEvent:
    reservation: FactoryBudgetReservationV1
    limit_usd: Decimal


@dataclass(frozen=True)
class _UsageEvent:
    receipt: FactoryUsageReceiptV1
    event_id: UUID


@dataclass(frozen=True)
class _ReleaseEvent:
    reservation_id: UUID
    reason: ReleaseReason
    released_at: datetime
    event_id: UUID


_BudgetEvent = _ReservationEvent | _UsageEvent | _ReleaseEvent


class InMemoryFactoryBudgetLedger:
    """Unit-test ledger; production persistence is the Task 9 Gateway adapter."""

    def __init__(self) -> None:
        self._events: tuple[_BudgetEvent, ...] = ()
        self._lock = Lock()

    @property
    def events(self) -> tuple[_BudgetEvent, ...]:
        return self._events

    def reserve(
        self,
        job: AgentFactoryJobV3,
        *,
        attempt: int,
        requested_usd: Decimal,
        now: datetime,
    ) -> FactoryBudgetReservationV1:
        checked_at = _require_utc(now)
        requested = _require_positive_usd(requested_usd, "requested_usd")
        _require_attempt(attempt)
        if not job.execution_policy.live_execution:
            raise BudgetExhausted("offline factory execution has no USD budget")
        if checked_at < job.occurred_at or checked_at >= job.deadline_at:
            raise BudgetExhausted("factory job deadline does not permit a reservation")

        with self._lock:
            projection = self._projection_for_job(job)
            if requested > projection.remaining_usd:
                raise BudgetExhausted("factory USD budget is exhausted")
            sequence = len(self._events) + 1
            policy_digest = _execution_policy_digest(job.execution_policy)
            reservation_id = uuid5(
                _BUDGET_NAMESPACE,
                "|".join(
                    (
                        "reserve",
                        str(job.job_id),
                        str(job.subject_version),
                        policy_digest,
                        str(attempt),
                        _canonical_usd_text(requested),
                        checked_at.isoformat(),
                        str(sequence),
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
                requested_usd=requested,
                reserved_at=checked_at,
                expires_at=job.deadline_at,
            )
            self._events += (
                _ReservationEvent(
                    reservation=reservation,
                    limit_usd=_canonical_usd(job.execution_policy.max_cost_usd),
                ),
            )
            return reservation

    def record_usage(
        self,
        job: AgentFactoryJobV3,
        reservation: FactoryBudgetReservationV1,
        receipt: FactoryUsageReceiptV1,
        *,
        lease: FactoryLease | None = None,
    ) -> FactoryBudgetWriteReceipt:
        del lease
        with self._lock:
            stored = self._require_reservation(job, reservation)
            self._require_receipt_bindings(job, stored, receipt)
            existing = self._usage_by_receipt_id(receipt.receipt_id)
            if existing is not None:
                if existing.receipt != receipt:
                    raise ValueError("receipt_id already exists with different content")
                return FactoryBudgetWriteReceipt(
                    event_id=existing.event_id,
                    job_id=job.job_id,
                    replayed=True,
                )
            if not self._reservation_is_active(reservation.reservation_id):
                raise ValueError("budget reservation is not active")
            if receipt.model not in job.execution_policy.allowed_models:
                raise BudgetExhausted("usage receipt does not name an allowed model")
            if (
                receipt.started_at < reservation.reserved_at
                or receipt.ended_at > reservation.expires_at
            ):
                raise ValueError("usage receipt is outside the reservation window")
            if receipt.cost_usd > reservation.requested_usd:
                raise BudgetExhausted("usage exceeds its USD reservation")
            projection = self._projection_for_job(job)
            if receipt.cost_usd + projection.consumed_usd > projection.limit_usd:
                raise BudgetExhausted("usage exceeds the job USD budget")
            event_id = uuid5(_BUDGET_NAMESPACE, f"usage|{receipt.receipt_id}")
            self._events += (_UsageEvent(receipt=receipt, event_id=event_id),)
            return FactoryBudgetWriteReceipt(
                event_id=event_id,
                job_id=job.job_id,
                replayed=False,
            )

    def release(
        self,
        job: AgentFactoryJobV3,
        reservation: FactoryBudgetReservationV1,
        *,
        now: datetime,
        reason: ReleaseReason,
    ) -> FactoryBudgetWriteReceipt:
        checked_at = _require_utc(now)
        if reason not in {"provider_failed", "cancelled", "unused"}:
            raise ValueError("unsupported budget release reason")
        with self._lock:
            stored = self._require_reservation(job, reservation)
            existing = self._release_by_reservation_id(reservation.reservation_id)
            if existing is not None:
                if existing.reason != reason:
                    raise ValueError("reservation was released with a different reason")
                return FactoryBudgetWriteReceipt(
                    event_id=existing.event_id,
                    job_id=job.job_id,
                    replayed=True,
                )
            if checked_at < stored.reserved_at:
                raise ValueError("reservation cannot be released before it was created")
            if not self._reservation_is_active(reservation.reservation_id):
                raise ValueError("budget reservation is not active")
            event_id = uuid5(
                _BUDGET_NAMESPACE,
                f"release|{reservation.reservation_id}|{reason}",
            )
            self._events += (
                _ReleaseEvent(
                    reservation_id=reservation.reservation_id,
                    reason=reason,
                    released_at=checked_at,
                    event_id=event_id,
                ),
            )
            return FactoryBudgetWriteReceipt(
                event_id=event_id,
                job_id=job.job_id,
                replayed=False,
            )

    def projection(self, job_id: UUID) -> FactoryBudgetProjection:
        return self._derive_projection(job_id)

    def _projection_for_job(self, job: AgentFactoryJobV3) -> FactoryBudgetProjection:
        limit = _canonical_usd(job.execution_policy.max_cost_usd)
        policy_digest = _execution_policy_digest(job.execution_policy)
        reservations = tuple(
            event
            for event in self._events
            if isinstance(event, _ReservationEvent)
            and event.reservation.job_id == job.job_id
        )
        if not reservations:
            return FactoryBudgetProjection(
                job_id=job.job_id,
                limit_usd=limit,
                consumed_usd=Decimal("0"),
                reserved_usd=Decimal("0"),
                remaining_usd=limit,
            )
        first = reservations[0]
        if (
            first.reservation.correlation_id != job.correlation_id
            or first.reservation.subject_version != job.subject_version
            or first.reservation.execution_policy_sha256 != policy_digest
            or first.limit_usd != limit
        ):
            raise ValueError("factory budget job identity or execution policy changed")
        return self._derive_projection(job.job_id)

    def _derive_projection(self, job_id: UUID) -> FactoryBudgetProjection:
        reservations: dict[UUID, Decimal] = {}
        consumed = Decimal("0")
        limit: Decimal | None = None
        for event in self._events:
            if isinstance(event, _ReservationEvent):
                if event.reservation.job_id != job_id:
                    continue
                if limit is not None and limit != event.limit_usd:
                    raise ValueError("factory budget limit changed")
                limit = event.limit_usd
                reservations[event.reservation.reservation_id] = (
                    event.reservation.requested_usd
                )
            elif isinstance(event, _UsageEvent) and event.receipt.job_id == job_id:
                consumed += event.receipt.cost_usd
                reservations.pop(event.receipt.reservation_id, None)
            elif isinstance(event, _ReleaseEvent):
                reservations.pop(event.reservation_id, None)
        if limit is None:
            raise KeyError(f"unknown factory budget job: {job_id}")
        reserved = sum(reservations.values(), start=Decimal("0"))
        remaining = limit - consumed - reserved
        return FactoryBudgetProjection(
            job_id=job_id,
            limit_usd=limit,
            consumed_usd=consumed,
            reserved_usd=reserved,
            remaining_usd=remaining,
            active_reservation_ids=tuple(reservations),
        )

    def _require_reservation(
        self,
        job: AgentFactoryJobV3,
        reservation: FactoryBudgetReservationV1,
    ) -> FactoryBudgetReservationV1:
        event = next(
            (
                candidate
                for candidate in self._events
                if isinstance(candidate, _ReservationEvent)
                and candidate.reservation.reservation_id == reservation.reservation_id
            ),
            None,
        )
        if event is None:
            raise ValueError("budget reservation was not issued by this ledger")
        if event.reservation != reservation:
            raise ValueError("reservation_id already exists with different content")
        policy_digest = _execution_policy_digest(job.execution_policy)
        if (
            reservation.job_id != job.job_id
            or reservation.correlation_id != job.correlation_id
            or reservation.subject_version != job.subject_version
            or reservation.execution_policy_sha256 != policy_digest
            or event.limit_usd
            != _canonical_usd(job.execution_policy.max_cost_usd)
        ):
            raise ValueError(
                "budget reservation does not match the factory job identity or execution policy"
            )
        return event.reservation

    @staticmethod
    def _require_receipt_bindings(
        job: AgentFactoryJobV3,
        reservation: FactoryBudgetReservationV1,
        receipt: FactoryUsageReceiptV1,
    ) -> None:
        if (
            receipt.reservation_id != reservation.reservation_id
            or receipt.job_id != job.job_id
            or receipt.correlation_id != job.correlation_id
            or receipt.attempt != reservation.attempt
        ):
            raise ValueError("usage receipt does not match its reservation")

    def _reservation_is_active(self, reservation_id: UUID) -> bool:
        for event in self._events:
            if isinstance(event, _UsageEvent) and event.receipt.reservation_id == reservation_id:
                return False
            if isinstance(event, _ReleaseEvent) and event.reservation_id == reservation_id:
                return False
        return True

    def _usage_by_receipt_id(self, receipt_id: UUID) -> _UsageEvent | None:
        return next(
            (
                event
                for event in self._events
                if isinstance(event, _UsageEvent)
                and event.receipt.receipt_id == receipt_id
            ),
            None,
        )

    def _release_by_reservation_id(self, reservation_id: UUID) -> _ReleaseEvent | None:
        return next(
            (
                event
                for event in self._events
                if isinstance(event, _ReleaseEvent)
                and event.reservation_id == reservation_id
            ),
            None,
        )


def _require_attempt(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
        raise ValueError("attempt must be an integer from 1 through 5")


def _require_positive_usd(value: object, field_name: str) -> Decimal:
    amount = _parse_usd(value, field_name)
    if amount <= 0:
        raise ValueError(f"{field_name} must be positive")
    return amount


def _parse_usd(value: object, field_name: str) -> Decimal:
    if isinstance(value, (bool, float)) or not isinstance(value, (str, Decimal)):
        raise ValueError(f"{field_name} must be a decimal string")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{field_name} must be finite") from exc
    if not amount.is_finite() or amount < 0 or amount.as_tuple().exponent < -2:
        raise ValueError(
            f"{field_name} must be finite, non-negative, and use cents"
        )
    return _canonical_usd(amount)


def _parse_usage_usd(value: object, field_name: str) -> Decimal:
    """Parse finalized provider usage at exact micro-USD precision."""

    if isinstance(value, (bool, float)) or not isinstance(value, (str, Decimal)):
        raise ValueError(f"{field_name} must be a decimal string")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{field_name} must be finite") from exc
    if not amount.is_finite() or amount < 0 or amount.as_tuple().exponent < -6:
        raise ValueError(
            f"{field_name} must be finite, non-negative, and use micro-USD"
        )
    return _canonical_usd(amount)


def _canonical_usd(value: Decimal) -> Decimal:
    return Decimal(_canonical_usd_text(value))


def _canonical_usd_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    rendered = format(value, "f")
    if "." in rendered:
        return rendered.rstrip("0").rstrip(".")
    return rendered


def _execution_policy_digest(policy: FactoryExecutionPolicyV1) -> str:
    payload = policy.model_dump(mode="json", by_alias=True)
    payload["max_cost_usd"] = _canonical_usd_text(policy.max_cost_usd)
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("budget timestamps must include a UTC offset")
    if value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError("budget timestamps must be UTC")
    return value.astimezone(timezone.utc)
