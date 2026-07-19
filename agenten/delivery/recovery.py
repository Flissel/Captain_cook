"""Captain-owned recovery decisions for expired gateway claims."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from collections.abc import Awaitable, Callable
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from agenten.delivery.gateway_client import (
    GatewayBatchProjection,
    GatewayDeliveryConflictError,
)


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


@dataclass(frozen=True)
class RecoveryPass:
    """One safe recovery scan, including claims intentionally left fenced."""

    recovered: tuple[RecoveryDecision, ...]
    deferred_batch_ids: tuple[str, ...]


class GatewayRecoveryService:
    """Find expired claims through the gateway and persist Captain decisions."""

    def __init__(
        self,
        gateway: RecoveryGateway,
        *,
        prepare_for_requeue: Callable[[str, int], Awaitable[bool]] | None = None,
    ) -> None:
        self._gateway = gateway
        self._prepare_for_requeue = prepare_for_requeue

    async def recover_expired(self, now: datetime) -> tuple[RecoveryDecision, ...]:
        """Compatibility view exposing only decisions persisted by this pass."""

        return (await self.recover_expired_pass(now)).recovered

    async def recover_expired_pass(self, now: datetime) -> RecoveryPass:
        """Requeue only expired claims whose terminal evidence is authoritative.

        A Gateway conflict means that a persisted Codex session still needs its
        owning worker's process-aware recovery.  The scan continues for other
        batches but never turns that ambiguity into a duplicate provider run.
        """

        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        recovered: list[RecoveryDecision] = []
        deferred: list[str] = []
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
            if self._prepare_for_requeue is not None:
                try:
                    if not await self._prepare_for_requeue(batch_id, projection.claim_iteration):
                        deferred.append(batch_id)
                        continue
                except Exception:
                    deferred.append(batch_id)
                    continue
            try:
                recovered.append(await self._gateway.record_recovery(decision))
            except GatewayDeliveryConflictError:
                deferred.append(batch_id)
        return RecoveryPass(
            recovered=tuple(recovered),
            deferred_batch_ids=tuple(deferred),
        )
