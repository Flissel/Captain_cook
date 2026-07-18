"""Command-before-effect orchestration for external agent runtimes."""

from __future__ import annotations

from uuid import uuid5

from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeResult,
    CapabilityGrant,
    HermesPlanResult,
    RuntimeOperation,
    RuntimeStatus,
)
from agenten.agent_runtime.ports import (
    ArtifactPort,
    CapabilityPolicyPort,
    Clock,
    CodexExecutionPort,
    HermesPlannerPort,
    RuntimeStatePort,
)


class RuntimeContractViolation(RuntimeError):
    """An external adapter returned data that does not match its command."""


class AgentRuntimeService:
    """Persist authority state around exactly one injected runtime effect."""

    def __init__(
        self,
        *,
        state: RuntimeStatePort,
        hermes: HermesPlannerPort,
        codex: CodexExecutionPort,
        artifacts: ArtifactPort,
        capabilities: CapabilityPolicyPort,
        clock: Clock,
    ) -> None:
        self._state = state
        self._hermes = hermes
        self._codex = codex
        self._artifacts = artifacts
        self._capabilities = capabilities
        self._clock = clock

    async def execute(self, command: AgentRuntimeCommand) -> AgentRuntimeResult:
        """Execute idempotently, never calling an adapter before durable acceptance."""

        await self._state.accept_command(command)
        existing_result = await self._state.get_result(command.event_id)
        if existing_result is not None:
            self._validate_result(existing_result, command)
            return existing_result

        now = self._clock.now()
        batch = await self._state.get_released_batch(command)
        grant = await self._state.get_grant(command.event_id)
        if grant is None:
            derived = self._capabilities.derive(command, batch, now)
            grant = await self._state.record_grant(derived)
        grant = self._capabilities.validate(grant, command, now)
        await self._artifacts.require(command.payload.prompt_ref)

        try:
            adapter_result = await self._dispatch(command, grant)
        except Exception:
            result = self._infrastructure_failure(command, grant)
        else:
            if isinstance(adapter_result, HermesPlanResult):
                result = self._from_hermes_plan(command, grant, adapter_result)
            else:
                result = adapter_result
            self._validate_result(result, command, grant)
        persisted = await self._state.record_result(result)
        self._validate_result(persisted, command, grant)
        return persisted

    async def _dispatch(
        self,
        command: AgentRuntimeCommand,
        grant: CapabilityGrant,
    ) -> AgentRuntimeResult | HermesPlanResult:
        match command.payload.operation:
            case RuntimeOperation.HERMES_PLAN:
                return await self._hermes.plan(command, grant)
            case RuntimeOperation.HERMES_DESIGN_AGENT:
                return await self._hermes.design_agent(command, grant)
            case RuntimeOperation.CODEX_RUN:
                return await self._codex.start(command, grant)
            case RuntimeOperation.CODEX_RESUME:
                return await self._codex.resume(command, grant)
            case RuntimeOperation.CODEX_STATUS:
                return await self._codex.status(command, grant)
            case RuntimeOperation.CODEX_CANCEL:
                return await self._codex.cancel(command, grant)
            case RuntimeOperation.CODEX_HEARTBEAT:
                return await self._codex.heartbeat(command, grant)
        raise RuntimeContractViolation("unsupported runtime operation")

    @staticmethod
    def _from_hermes_plan(
        command: AgentRuntimeCommand,
        grant: CapabilityGrant,
        plan: HermesPlanResult,
    ) -> AgentRuntimeResult:
        if plan.project_id != command.payload.project_id:
            raise RuntimeContractViolation("Hermes plan project does not match command")
        if plan.correlation_id != command.correlation_id:
            raise RuntimeContractViolation("Hermes plan correlation does not match command")
        if plan.subject_version != command.subject_version:
            raise RuntimeContractViolation("Hermes plan version does not match command")
        return AgentRuntimeResult(
            schema_name="captain.agent-runtime-result.v1",
            event_id=uuid5(command.event_id, "hermes-runtime-result"),
            command_id=command.event_id,
            correlation_id=command.correlation_id,
            occurred_at=plan.ended_at,
            producer="hermes-runtime",
            subject_id=command.subject_id,
            subject_version=command.subject_version,
            grant_id=grant.grant_id,
            operation=command.payload.operation,
            status=RuntimeStatus.SUCCEEDED,
            session_id=plan.planner_id,
            artifact_refs=(plan.plan_ref, *plan.blueprint_refs),
            evidence_refs=(plan.decision_log_ref,),
        )

    def _infrastructure_failure(
        self,
        command: AgentRuntimeCommand,
        grant: CapabilityGrant,
    ) -> AgentRuntimeResult:
        return AgentRuntimeResult(
            schema_name="captain.agent-runtime-result.v1",
            event_id=uuid5(command.event_id, "infrastructure-failure"),
            command_id=command.event_id,
            correlation_id=command.correlation_id,
            occurred_at=self._clock.now(),
            producer="agent-runtime",
            subject_id=command.subject_id,
            subject_version=command.subject_version,
            grant_id=grant.grant_id,
            operation=command.payload.operation,
            status=RuntimeStatus.INFRASTRUCTURE_FAILED,
            error=f"{command.payload.operation.value} adapter failed",
        )

    @staticmethod
    def _validate_result(
        result: AgentRuntimeResult,
        command: AgentRuntimeCommand,
        grant: CapabilityGrant | None = None,
    ) -> None:
        if result.command_id != command.event_id:
            raise RuntimeContractViolation("result command does not match request")
        if result.correlation_id != command.correlation_id:
            raise RuntimeContractViolation("result correlation does not match command")
        if result.subject_id != command.subject_id:
            raise RuntimeContractViolation("result subject does not match command")
        if result.subject_version != command.subject_version:
            raise RuntimeContractViolation("result version does not match command")
        if result.operation is not command.payload.operation:
            raise RuntimeContractViolation("result operation does not match command")
        if grant is not None and result.grant_id != grant.grant_id:
            raise RuntimeContractViolation("result grant does not match command grant")
