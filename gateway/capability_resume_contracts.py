"""Persistent records for Gateway-owned capability resume authority."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agenten.agent_runtime.contracts import AgentRuntimeCommand, ArtifactRef
from agenten.agent_factory.capability_live_runner import CapabilityLiveReservation
from gateway.capability_live_pipeline import (
    CapabilityExecutionAuthority,
    CapabilityResumeAuthorization,
    CapabilityResumeExecutionRequest,
)
from gateway.contracts import RuntimeOperationProjection


class _FrozenResumeContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _HttpResumeContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CapabilityResumeAuthorizeHttpRequest(_HttpResumeContract):
    """JSON boundary for the strict domain authorization request."""

    job_id: UUID
    reservation_id: UUID
    run_id: UUID
    input_id: str = Field(min_length=1)
    maximum_cost_usd: Decimal
    correlation_id: UUID
    capability_id: str = Field(min_length=1)
    capability_version: int = Field(ge=1, strict=True)
    original_execution_command_id: UUID
    original_execution_result_id: UUID

    def to_domain(self) -> CapabilityResumeExecutionRequest:
        return CapabilityResumeExecutionRequest.model_validate(
            self.model_dump(), strict=True
        )


class CapabilityResumeAuthorizationHttpRequest(_HttpResumeContract):
    """JSON boundary for the strict persisted authorization receipt."""

    job_id: UUID
    reservation_id: UUID
    correlation_id: UUID
    capability_id: str = Field(min_length=1)
    capability_version: int = Field(ge=1, strict=True)
    authorization_receipt_id: UUID
    evidence_refs: tuple[ArtifactRef, ...] = Field(min_length=1)

    def to_domain(self) -> CapabilityResumeAuthorization:
        return CapabilityResumeAuthorization.model_validate(
            self.model_dump(), strict=True
        )


class CapabilityResumeSourceSnapshotV1(_FrozenResumeContract):
    """Gateway projections needed to authorize one exact continuation."""

    schema_name: Literal["captain.capability-resume-source.v1"] = Field(
        default="captain.capability-resume-source.v1",
        alias="schema",
        serialization_alias="schema",
    )
    job_id: UUID
    correlation_id: UUID
    capability_id: str = Field(min_length=1)
    capability_version: int = Field(ge=1, strict=True)
    booked_cost_usd: Decimal
    reservation: CapabilityLiveReservation
    original_operation: RuntimeOperationProjection

    @field_validator("booked_cost_usd", mode="before")
    @classmethod
    def require_known_cost(cls, value: object) -> Decimal:
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise ValueError("booked cost must be a known decimal") from None
        if not amount.is_finite() or amount < 0:
            raise ValueError("booked cost must be finite and non-negative")
        return amount


class CapabilityResumeAuthorizationRecordV1(_FrozenResumeContract):
    """Immutable authorization plus its exact derived runtime command."""

    schema_name: Literal["captain.capability-resume-authorization-record.v1"] = Field(
        default="captain.capability-resume-authorization-record.v1",
        alias="schema",
        serialization_alias="schema",
    )
    request: CapabilityResumeExecutionRequest
    authorization: CapabilityResumeAuthorization
    resume_command: AgentRuntimeCommand
    booked_cost_usd: Decimal
    resume_budget_usd: Decimal

    @field_validator("booked_cost_usd", "resume_budget_usd", mode="before")
    @classmethod
    def require_nonnegative_booking(cls, value: object) -> Decimal:
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise ValueError("resume booking must be a known decimal") from None
        if not amount.is_finite() or amount < 0:
            raise ValueError("resume booking must be finite and non-negative")
        return amount


class CapabilityResumeDispatchStartedV1(_FrozenResumeContract):
    """Append-only fence written before the external runtime effect."""

    schema_name: Literal["captain.capability-resume-dispatch-started.v1"] = Field(
        default="captain.capability-resume-dispatch-started.v1",
        alias="schema",
        serialization_alias="schema",
    )
    authorization_receipt_id: UUID
    execution_command_id: UUID


class CapabilityResumeDispatchFailureV1(_FrozenResumeContract):
    """Gateway-owned terminal receipt when no Runtime result was persisted."""

    schema_name: Literal["captain.capability-resume-dispatch-failure.v1"] = Field(
        default="captain.capability-resume-dispatch-failure.v1",
        alias="schema",
        serialization_alias="schema",
    )
    failure_id: UUID
    authorization_receipt_id: UUID
    execution_command_id: UUID
    failure_kind: Literal["runtime_effect_failed_without_persisted_result"]


class CapabilityResumeTerminalV1(_FrozenResumeContract):
    """Immutable terminal authority reconstructed from runtime read-back."""

    schema_name: Literal["captain.capability-resume-terminal.v1"] = Field(
        default="captain.capability-resume-terminal.v1",
        alias="schema",
        serialization_alias="schema",
    )
    authorization_receipt_id: UUID
    execution: CapabilityExecutionAuthority


__all__ = [
    "CapabilityResumeAuthorizationHttpRequest",
    "CapabilityResumeAuthorizationRecordV1",
    "CapabilityResumeAuthorizeHttpRequest",
    "CapabilityResumeDispatchStartedV1",
    "CapabilityResumeDispatchFailureV1",
    "CapabilityResumeSourceSnapshotV1",
    "CapabilityResumeTerminalV1",
]
