"""Bounded opt-in resume loop for Captain-authorized Factory side effects."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agenten.agent_factory.contracts import FactoryJob, FactoryLease, FactoryRole
from agenten.agent_factory.orchestration import (
    FactoryDispatcher,
    FactoryRuntimeRetryAuthorizationPort,
)
from agenten.agent_factory.skill_sequence import FactoryRuntimeRetryAuthorizationV1
from agenten.agent_factory.state_machine import (
    FactoryAction,
    FactoryActionKind,
    FactoryProjection,
    FactoryLifecycleStatus,
)


_ACTION_ROLES: dict[FactoryActionKind, FactoryRole] = {
    FactoryActionKind.DISPATCH_AGENT_ARCHITECT: FactoryRole.AGENT_ARCHITECT,
    FactoryActionKind.DISPATCH_TOOL_INTEGRATOR: FactoryRole.TOOL_INTEGRATOR,
    FactoryActionKind.SUBMIT_FORGE_JOB: FactoryRole.TOOL_INTEGRATOR,
    FactoryActionKind.DISPATCH_BUILD_VALIDATOR: FactoryRole.TOOL_INTEGRATOR,
    FactoryActionKind.DISPATCH_REAL_CASE_TESTER: FactoryRole.REAL_CASE_TESTER,
    FactoryActionKind.DISPATCH_TECHNICAL_REVALIDATION: FactoryRole.REAL_CASE_TESTER,
    FactoryActionKind.DISPATCH_QUALITY_WARDEN: FactoryRole.QUALITY_WARDEN,
}

_CAPTAIN_ONLY_ACTIONS = frozenset(
    {
        FactoryActionKind.APPEND_FORGE_REQUESTED,
        FactoryActionKind.APPEND_IMPROVEMENT_REQUESTED,
        FactoryActionKind.APPEND_TECHNICAL_REVALIDATION_REQUESTED,
        FactoryActionKind.VALIDATE_FOR_PROMOTION,
        FactoryActionKind.APPEND_ESCALATED,
    }
)


class FactoryDispatchCoordinatorPort(Protocol):
    def next_action(self, job_id: UUID) -> FactoryAction: ...

    def projection(self, job_id: UUID) -> FactoryProjection: ...


class FactoryDispatcherPort(Protocol):
    async def dispatch_next(self, job_id: UUID) -> FactoryAction: ...


class CaptainNextActionLeaseIssuerPort(Protocol):
    """Ask Captain authority to persist or recover the exact next role lease."""

    def ensure_for(
        self,
        job: FactoryJob,
        action: FactoryAction,
        role: FactoryRole,
        now: datetime,
    ) -> FactoryLease: ...

    def ensure_recovery_for(
        self,
        job: FactoryJob,
        action: FactoryAction,
        role: FactoryRole,
        now: datetime,
        authorization: FactoryRuntimeRetryAuthorizationV1,
    ) -> FactoryLease: ...


class ProductionFactoryDispatchResult(BaseModel):
    """Redacted checkpoint returned without taking Captain-only decisions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_name: Literal["captain.factory-dispatch-run-result.v1"] = Field(
        default="captain.factory-dispatch-run-result.v1",
        alias="schema",
        serialization_alias="schema",
    )
    job_id: UUID
    status: Literal[
        "complete",
        "captain_action_required",
        "infrastructure_blocked",
        "dispatch_limit_reached",
        "stop_point_reached",
    ]
    lifecycle_status: FactoryLifecycleStatus
    next_action: FactoryAction
    dispatched_actions: tuple[FactoryActionKind, ...] = ()


class ProductionFactoryDispatchRunner:
    """Resume only externally dispatched work under Gateway-approved leases.

    Promotion, escalation, improvement authorization, and the initial Forge
    request remain explicit Captain transitions.  Encountering one returns a
    typed checkpoint and performs no write for that action.
    """

    def __init__(
        self,
        *,
        coordinator: FactoryDispatchCoordinatorPort,
        dispatcher: FactoryDispatcher | FactoryDispatcherPort,
        lease_issuer: CaptainNextActionLeaseIssuerPort,
        runtime_retries: FactoryRuntimeRetryAuthorizationPort | None = None,
        clock: Callable[[], datetime],
    ) -> None:
        dispatcher_lease_authority = getattr(dispatcher, "lease_authority", None)
        if (
            dispatcher_lease_authority is not None
            and dispatcher_lease_authority is not lease_issuer
        ):
            raise ValueError(
                "Factory runner and dispatcher must use the same lease authority"
            )
        self._coordinator = coordinator
        self._dispatcher = dispatcher
        self._lease_issuer = lease_issuer
        self._runtime_retries = runtime_retries
        self._clock = clock

    async def run(
        self,
        job_id: UUID,
        *,
        maximum_dispatches: int = 12,
        stop_before_action: FactoryActionKind | None = None,
    ) -> ProductionFactoryDispatchResult:
        if maximum_dispatches < 1:
            raise ValueError("maximum_dispatches must be positive")
        if (
            stop_before_action is not None
            and type(stop_before_action) is not FactoryActionKind
        ):
            raise ValueError("Factory stop point must be a FactoryActionKind")
        if stop_before_action is not None and stop_before_action not in _ACTION_ROLES:
            raise ValueError("Factory stop point must be an externally dispatched action")
        dispatched: list[FactoryActionKind] = []
        while len(dispatched) < maximum_dispatches:
            action = self._coordinator.next_action(job_id)
            projection = self._coordinator.projection(job_id)
            self._require_job_identity(job_id, action, projection)
            if action.kind is stop_before_action:
                return self._result(
                    job_id,
                    status="stop_point_reached",
                    action=action,
                    projection_status=projection.status,
                    dispatched=dispatched,
                )
            if action.kind is FactoryActionKind.COMPLETE:
                return self._result(
                    job_id,
                    status="complete",
                    action=action,
                    projection_status=projection.status,
                    dispatched=dispatched,
                )
            if action.kind is FactoryActionKind.WAIT_INFRASTRUCTURE:
                return self._result(
                    job_id,
                    status="infrastructure_blocked",
                    action=action,
                    projection_status=projection.status,
                    dispatched=dispatched,
                )
            if action.kind in _CAPTAIN_ONLY_ACTIONS:
                return self._result(
                    job_id,
                    status="captain_action_required",
                    action=action,
                    projection_status=projection.status,
                    dispatched=dispatched,
                )
            try:
                role = _ACTION_ROLES[action.kind]
            except KeyError as exc:
                raise ValueError(
                    f"unsupported Factory dispatch action: {action.kind.value}"
                ) from exc
            now = self._utc_now()
            runtime_retry = (
                self._runtime_retries.active(
                    projection.job,
                    action,
                    projection,
                    now,
                )
                if self._runtime_retries is not None
                else None
            )
            if runtime_retry is None:
                self._lease_issuer.ensure_for(projection.job, action, role, now)
            else:
                self._lease_issuer.ensure_recovery_for(
                    projection.job,
                    action,
                    role,
                    now,
                    runtime_retry,
                )
            completed = await self._dispatcher.dispatch_next(job_id)
            if completed != action:
                raise ValueError("Factory dispatcher returned a different action")
            dispatched.append(action.kind)

        action = self._coordinator.next_action(job_id)
        projection = self._coordinator.projection(job_id)
        self._require_job_identity(job_id, action, projection)
        if action.kind is stop_before_action:
            return self._result(
                job_id,
                status="stop_point_reached",
                action=action,
                projection_status=projection.status,
                dispatched=dispatched,
            )
        return self._result(
            job_id,
            status="dispatch_limit_reached",
            action=action,
            projection_status=projection.status,
            dispatched=dispatched,
        )

    def _utc_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("Factory dispatch runner clock must be UTC")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _result(
        job_id: UUID,
        *,
        status: Literal[
            "complete",
            "captain_action_required",
            "infrastructure_blocked",
            "dispatch_limit_reached",
            "stop_point_reached",
        ],
        action: FactoryAction,
        projection_status: FactoryLifecycleStatus,
        dispatched: list[FactoryActionKind],
    ) -> ProductionFactoryDispatchResult:
        return ProductionFactoryDispatchResult(
            schema="captain.factory-dispatch-run-result.v1",
            job_id=job_id,
            status=status,
            lifecycle_status=projection_status,
            next_action=action,
            dispatched_actions=tuple(dispatched),
        )

    @staticmethod
    def _require_job_identity(
        job_id: UUID,
        action: FactoryAction,
        projection: FactoryProjection,
    ) -> None:
        if action.job_id != job_id or projection.job.job_id != job_id:
            raise ValueError(
                "Factory action or projection job identity does not match request"
            )


__all__ = [
    "CaptainNextActionLeaseIssuerPort",
    "ProductionFactoryDispatchResult",
    "ProductionFactoryDispatchRunner",
]
