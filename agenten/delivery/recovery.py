"""Captain-owned recovery decisions for expired gateway claims."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from agenten.delivery.gateway_client import GatewayBatchProjection


class RecoveryDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    batch_id: str = Field(min_length=1, max_length=32)
    iteration: int = Field(ge=1, strict=True)
    reason: Literal["claim_expired"]
    decision: Literal["requeue", "aborted_infra"]


class RecoveryGateway(Protocol):
    async def list_batches(self, status: str) -> tuple[str, ...]: ...

    async def get_batch(self, batch_id: str) -> GatewayBatchProjection: ...

    async def record_recovery(self, decision: RecoveryDecision) -> RecoveryDecision: ...


class GatewayRecoveryService:
    """Find expired claims through the gateway and persist Captain decisions."""

    def __init__(self, gateway: RecoveryGateway) -> None:
        self._gateway = gateway

    async def recover_expired(self, now: datetime) -> tuple[RecoveryDecision, ...]:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        recovered: list[RecoveryDecision] = []
        for batch_id in await self._gateway.list_batches("pending"):
            projection = await self._gateway.get_batch(batch_id)
            expires_at = projection.claim_expires_at
            if (
                projection.status != "pending"
                or projection.claim_iteration < 1
                or expires_at is None
                or expires_at > now
                or (
                    projection.recovery_recorded
                    and projection.recovered_iteration == projection.claim_iteration
                )
            ):
                continue
            decision = RecoveryDecision(
                batch_id=batch_id,
                iteration=projection.claim_iteration,
                reason="claim_expired",
                decision="requeue",
            )
            recovered.append(await self._gateway.record_recovery(decision))
        return tuple(recovered)
