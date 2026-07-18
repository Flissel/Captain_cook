"""Injected boundaries for the agent runtime control plane."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeResult,
    ArtifactRef,
    CapabilityGrant,
    HermesPlanResult,
)
from agenten.validation.contracts import WorkBatch


class Clock(Protocol):
    def now(self) -> datetime: ...


class RuntimeStatePort(Protocol):
    async def accept_command(self, command: AgentRuntimeCommand) -> None: ...

    async def get_released_batch(self, command: AgentRuntimeCommand) -> WorkBatch: ...

    async def get_grant(self, command_id: UUID) -> CapabilityGrant | None: ...

    async def record_grant(self, grant: CapabilityGrant) -> CapabilityGrant: ...

    async def get_result(self, command_id: UUID) -> AgentRuntimeResult | None: ...

    async def record_result(self, result: AgentRuntimeResult) -> AgentRuntimeResult: ...


class ArtifactPort(Protocol):
    async def require(self, reference: ArtifactRef) -> None: ...


class CapabilityPolicyPort(Protocol):
    def derive(
        self,
        command: AgentRuntimeCommand,
        batch: WorkBatch,
        now: datetime,
    ) -> CapabilityGrant: ...

    def validate(
        self,
        grant: CapabilityGrant,
        command: AgentRuntimeCommand,
        now: datetime,
    ) -> CapabilityGrant: ...


class HermesPlannerPort(Protocol):
    async def plan(
        self,
        command: AgentRuntimeCommand,
        grant: CapabilityGrant,
    ) -> HermesPlanResult: ...

    async def design_agent(
        self,
        command: AgentRuntimeCommand,
        grant: CapabilityGrant,
    ) -> HermesPlanResult: ...


class CodexExecutionPort(Protocol):
    async def start(
        self,
        command: AgentRuntimeCommand,
        grant: CapabilityGrant,
    ) -> AgentRuntimeResult: ...

    async def resume(
        self,
        command: AgentRuntimeCommand,
        grant: CapabilityGrant,
    ) -> AgentRuntimeResult: ...

    async def status(
        self,
        command: AgentRuntimeCommand,
        grant: CapabilityGrant,
    ) -> AgentRuntimeResult: ...

    async def cancel(
        self,
        command: AgentRuntimeCommand,
        grant: CapabilityGrant,
    ) -> AgentRuntimeResult: ...

    async def heartbeat(
        self,
        command: AgentRuntimeCommand,
        grant: CapabilityGrant,
    ) -> AgentRuntimeResult: ...
