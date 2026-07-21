"""Evidence-verifying facade over the existing Codex runtime port."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from agenten.agent_runtime.capabilities import validate_grant
from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeResult,
    CapabilityGrant,
    RuntimeOperation,
    RuntimeStatus,
)
from agenten.agent_runtime.ports import CodexExecutionPort


class BoundCodexBuildAdapter:
    """Run or resume Codex and require its real session and content evidence."""

    def __init__(
        self,
        *,
        runtime: CodexExecutionPort,
        clock: Callable[[], datetime],
    ) -> None:
        if runtime is None or clock is None:
            raise ValueError("Codex runtime and clock are required")
        self._runtime = runtime
        self._clock = clock

    async def execute(
        self,
        command: AgentRuntimeCommand,
        grant: CapabilityGrant,
    ) -> AgentRuntimeResult:
        validate_grant(grant, command, self._clock())
        if command.payload.operation is RuntimeOperation.CODEX_RUN:
            result = await self._runtime.start(command, grant)
        elif command.payload.operation is RuntimeOperation.CODEX_RESUME:
            result = await self._runtime.resume(command, grant)
        else:
            raise ValueError("Codex build adapter accepts only run or resume commands")
        if (
            result.command_id != command.event_id
            or result.correlation_id != command.correlation_id
            or result.subject_id != command.subject_id
            or result.subject_version != command.subject_version
            or result.grant_id != grant.grant_id
            or result.operation is not command.payload.operation
        ):
            raise ValueError("Codex result is not bound to the released command and grant")
        if result.status is not RuntimeStatus.SUCCEEDED:
            raise ValueError("Codex build did not succeed")
        if not result.session_id:
            raise ValueError("Codex result is missing a real session ID")
        if not result.artifact_refs or not result.evidence_refs:
            raise ValueError("Codex result is missing content-addressed build evidence")
        return result
