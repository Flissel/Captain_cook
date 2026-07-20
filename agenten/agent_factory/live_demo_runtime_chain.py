"""Opt-in Captain -> Hermes -> AutoGen -> Codex/n8n one-shot adapter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable
from uuid import UUID

from agenten.agent_factory.contracts import FactoryEvidenceBlock, FactoryRole
from agenten.agent_factory.leases import validate_factory_lease
from agenten.agent_factory.orchestration import FactoryDispatch, HermesFactoryPort
from agenten.agent_factory.state_machine import FactoryActionKind
from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeResult,
    CapabilityProfile,
    IntegrationIntent,
)
from agenten.runtime.autogen_one_shot import (
    AutoGenOneShotRuntimeRelay,
    LiveDemoRuntimeMessage,
    RuntimeCommandExecutor,
)


class LiveDemoRuntimeChainError(RuntimeError):
    """The released job, lease, Hermes evidence, or runtime result diverged."""


@dataclass(frozen=True)
class LiveDemoRuntimeRelease:
    """Exact hand-off accepted by the final demo runner."""

    dispatch: FactoryDispatch
    command: AgentRuntimeCommand


@dataclass(frozen=True)
class LiveDemoRuntimeChainResult:
    correlation_id: UUID
    command: AgentRuntimeCommand
    hermes_evidence: FactoryEvidenceBlock
    runtime_result: AgentRuntimeResult
    autogen_delivery_count: int


class LiveDemoRuntimeChain:
    """Run exactly one Captain-authorized n8n tool-integrator lease."""

    def __init__(
        self,
        *,
        hermes: HermesFactoryPort,
        runtime_service: RuntimeCommandExecutor,
        clock: Callable[[], datetime],
    ) -> None:
        self._hermes = hermes
        self._relay = AutoGenOneShotRuntimeRelay(runtime_service)
        self._clock = clock

    async def run(self, release: LiveDemoRuntimeRelease) -> LiveDemoRuntimeChainResult:
        dispatch = release.dispatch
        lease = dispatch.lease
        if dispatch.role is not FactoryRole.TOOL_INTEGRATOR or lease is None:
            raise LiveDemoRuntimeChainError("live demo requires a tool-integrator lease")
        if dispatch.action.kind is not FactoryActionKind.DISPATCH_TOOL_INTEGRATOR:
            raise LiveDemoRuntimeChainError("live demo requires a released tool-integrator action")
        validate_factory_lease(
            lease, job=dispatch.job, role=FactoryRole.TOOL_INTEGRATOR,
            attempt=dispatch.action.attempt, now=self._clock(),
        )
        command = release.command
        if (
            lease.integration_intent is not IntegrationIntent.N8N
            or lease.capability_profile is not CapabilityProfile.N8N_BUILDER
            or command.payload.integration_intent is not IntegrationIntent.N8N
            or command.payload.capability_profile is not CapabilityProfile.N8N_BUILDER
        ):
            raise LiveDemoRuntimeChainError("live demo requires Captain-approved n8n scope")
        if (
            command.correlation_id != dispatch.job.correlation_id
            or command.causation_id != dispatch.job.event_id
            or command.subject_version != dispatch.job.subject_version
            or command.payload.workspace_ref != lease.workspace_ref
        ):
            raise LiveDemoRuntimeChainError("runtime command is not bound to the released job and lease")

        evidence = await self._hermes.dispatch(dispatch)
        if (
            evidence.job_id != dispatch.job.job_id
            or evidence.correlation_id != dispatch.job.correlation_id
            or evidence.lease_id != lease.lease_id
            or evidence.role is not FactoryRole.TOOL_INTEGRATOR
        ):
            raise LiveDemoRuntimeChainError("Hermes evidence is not bound to the released job and lease")
        result, delivery_count = await self._relay.deliver(
            LiveDemoRuntimeMessage(
                correlation_id=command.correlation_id,
                hermes_evidence_id=evidence.event_id,
                command=command,
            )
        )
        if result.correlation_id != command.correlation_id or result.command_id != command.event_id:
            raise LiveDemoRuntimeChainError("Gateway runtime result does not finalize the released command")
        return LiveDemoRuntimeChainResult(
            correlation_id=command.correlation_id,
            command=command,
            hermes_evidence=evidence,
            runtime_result=result,
            autogen_delivery_count=delivery_count,
        )
