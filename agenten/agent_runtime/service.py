"""Command-before-effect orchestration for external agent runtimes."""

from __future__ import annotations

import logging

import json
import hashlib
from uuid import uuid5

from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeResult,
    CapabilityGrant,
    HermesPlanResult,
    RuntimeOperation,
    RuntimeInfrastructureFailureEvidenceV1,
    RuntimeResumeCostAuthorityV1,
    RuntimeResumeCostSettlementV1,
    RuntimeStatus,
)
from agenten.agent_runtime.ports import (
    ArtifactPort,
    CapabilityPolicyPort,
    Clock,
    CodexExecutionPort,
    HermesPlannerPort,
    RuntimeStatePort,
    RuntimeCostAuthorityPort,
)


logger = logging.getLogger(__name__)


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
        cost_authority: RuntimeCostAuthorityPort | None = None,
    ) -> None:
        self._state = state
        self._hermes = hermes
        self._codex = codex
        self._artifacts = artifacts
        self._capabilities = capabilities
        self._clock = clock
        self._cost_authority = cost_authority

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
        revocation = await self._state.get_grant_revocation(command.event_id)
        grant = self._capabilities.validate(grant, command, now, revocation)
        resume_cost_authority: RuntimeResumeCostAuthorityV1 | None = None
        # The pre-existing in-process swarm REDO lane has no Gateway reservation;
        # externally dispatched resumes must always present the new authority.
        if (
            command.payload.operation is RuntimeOperation.CODEX_RESUME
            and (
                command.producer != "captain-swarm"
                or command.payload.cost_authority_ref is not None
            )
        ):
            if self._cost_authority is None:
                raise RuntimeContractViolation(
                    "codex.resume requires an authenticated cost authority"
                )
            resume_cost_authority = await self._cost_authority.authorize(command)
            self._require_resume_cost_authority(
                command, resume_cost_authority, now=now
            )
        await self._artifacts.require(command.payload.prompt_ref)

        try:
            adapter_result = await self._dispatch(command, grant)
        except Exception:
            # The evidence artifact records only reason_code, by contract: it
            # must not reproduce local paths. The cause still has to be
            # recoverable, so it goes to the log instead of the artifact.
            logger.exception(
                "Adapter dispatch failed for %s", command.payload.operation.value
            )
            result = await self._infrastructure_failure(command, grant)
        else:
            if isinstance(adapter_result, HermesPlanResult):
                result = self._from_hermes_plan(command, grant, adapter_result)
            else:
                result = adapter_result
            self._validate_result(result, command, grant)
        if resume_cost_authority is not None:
            assert self._cost_authority is not None
            settlement = await self._cost_authority.settle(
                command, result, resume_cost_authority
            )
            self._require_resume_cost_settlement(
                command, result, resume_cost_authority, settlement
            )
            result = self._apply_resume_cost_settlement(
                command, grant, result, settlement
            )
        persisted = await self._state.record_result(result)
        self._validate_result(persisted, command, grant)
        return persisted

    @staticmethod
    def _require_resume_cost_authority(
        command: AgentRuntimeCommand,
        authority: RuntimeResumeCostAuthorityV1,
        *,
        now,
    ) -> None:
        payload = command.payload
        if (
            authority.cost_authority_ref != payload.cost_authority_ref
            or authority.reservation_id != payload.budget_reservation_id
            or authority.job_id != payload.cost_job_id
            or authority.run_id != payload.cost_run_id
            or authority.input_id != payload.cost_input_id
            or authority.correlation_id != command.correlation_id
            or authority.capability_id != payload.cost_capability_id
            or authority.capability_version != payload.cost_capability_version
            or authority.command_id != command.event_id
            or authority.ceiling_usd != payload.maximum_cost_usd
            or authority.expires_at <= now
            or not authority.hard_ceiling_enforced
            or authority.metering_mode != "provider_usage_receipt"
            or authority.provider_proxy_url != payload.provider_proxy_url
            or authority.provider_policy_sha256 != payload.provider_policy_sha256
            or authority.provider_price_card_sha256 != payload.provider_price_card_sha256
            or authority.provider_context_sha256 != payload.provider_context_sha256
            or authority.provider_session_id != payload.provider_session_id
            or authority.provider_result_id != payload.provider_result_id
        ):
            raise RuntimeContractViolation(
                "codex.resume cost authority binding is invalid"
            )

    @staticmethod
    def _require_resume_cost_settlement(
        command: AgentRuntimeCommand,
        result: AgentRuntimeResult,
        authority: RuntimeResumeCostAuthorityV1,
        settlement: RuntimeResumeCostSettlementV1,
    ) -> None:
        evidence = result.cost_evidence
        if (
            settlement.command_id != command.event_id
            or settlement.reservation_id != authority.reservation_id
        ):
            raise RuntimeContractViolation(
                "codex.resume cost settlement binding is invalid"
            )
        if settlement.disposition == "accounted":
            if (
                evidence is None
                or evidence.command_id != command.event_id
                or evidence.result_id != result.event_id
                or evidence.original_command_id
                != (command.causation_id or command.event_id)
                or evidence.reservation_id != authority.reservation_id
                or evidence.job_id != authority.job_id
                or evidence.run_id != authority.run_id
                or evidence.input_id != authority.input_id
                or evidence.correlation_id != authority.correlation_id
                or evidence.capability_id != authority.capability_id
                or evidence.capability_version != authority.capability_version
                or evidence.actual_cost_usd > authority.ceiling_usd
                or settlement.actual_cost_usd != evidence.actual_cost_usd
                or settlement.accounted_cost_usd != evidence.actual_cost_usd
            ):
                raise RuntimeContractViolation(
                    "codex.resume actual usage binding is invalid"
                )
        elif settlement.disposition == "overrun":
            if (
                evidence is None
                or evidence.command_id != command.event_id
                or evidence.result_id != result.event_id
                or evidence.original_command_id
                != (command.causation_id or command.event_id)
                or evidence.reservation_id != authority.reservation_id
                or evidence.job_id != authority.job_id
                or evidence.run_id != authority.run_id
                or evidence.input_id != authority.input_id
                or evidence.correlation_id != authority.correlation_id
                or evidence.capability_id != authority.capability_id
                or evidence.capability_version != authority.capability_version
                or settlement.actual_cost_usd != evidence.actual_cost_usd
                or settlement.actual_cost_usd <= authority.ceiling_usd
                or settlement.accounted_cost_usd != settlement.actual_cost_usd
            ):
                raise RuntimeContractViolation(
                    "codex.resume overrun accounting is invalid"
                )
        elif settlement.accounted_cost_usd != authority.ceiling_usd:
            raise RuntimeContractViolation(
                "codex.resume failed accounting must consume its ceiling"
            )

    @staticmethod
    def _apply_resume_cost_settlement(
        command: AgentRuntimeCommand,
        grant: CapabilityGrant,
        result: AgentRuntimeResult,
        settlement: RuntimeResumeCostSettlementV1,
    ) -> AgentRuntimeResult:
        references = tuple(
            dict.fromkeys(
                (
                    *result.evidence_refs,
                    *settlement.evidence_refs,
                    *((result.cost_evidence.evidence_ref,) if result.cost_evidence else ()),
                )
            )
        )
        if settlement.disposition == "accounted" or result.status in {
            RuntimeStatus.FAILED,
            RuntimeStatus.INFRASTRUCTURE_FAILED,
            RuntimeStatus.CANCELLED,
        }:
            return result.model_copy(update={"evidence_refs": references})
        return AgentRuntimeResult(
            schema_name="captain.agent-runtime-result.v1",
            event_id=uuid5(command.event_id, "resume-cost-accounting-failure"),
            command_id=command.event_id,
            correlation_id=command.correlation_id,
            occurred_at=result.occurred_at,
            producer="agent-runtime",
            subject_id=command.subject_id,
            subject_version=command.subject_version,
            grant_id=grant.grant_id,
            operation=command.payload.operation,
            status=RuntimeStatus.POLICY_FAILED,
            session_id=result.session_id,
            evidence_refs=references,
            cost_evidence=result.cost_evidence,
            error="codex.resume cost accounting failed",
        )

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

    async def _infrastructure_failure(
        self,
        command: AgentRuntimeCommand,
        grant: CapabilityGrant,
    ) -> AgentRuntimeResult:
        occurred_at = command.occurred_at
        evidence = RuntimeInfrastructureFailureEvidenceV1(
            schema_name="captain.runtime-infrastructure-failure-evidence.v1",
            failure_id=uuid5(command.event_id, "infrastructure-failure-evidence"),
            command_id=command.event_id,
            correlation_id=command.correlation_id,
            operation=command.payload.operation,
            occurred_at=occurred_at,
        )
        evidence_bytes = json.dumps(
            evidence.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        evidence_ref = await self._artifacts.write(
            evidence_bytes,
            "application/json",
        )
        digest = hashlib.sha256(evidence_bytes).hexdigest()
        if (
            evidence_ref.sha256 != digest
            or evidence_ref.uri != f"artifact://sha256/{digest}"
            or evidence_ref.media_type != "application/json"
        ):
            raise RuntimeContractViolation(
                "infrastructure failure evidence is not content-addressed"
            )
        return AgentRuntimeResult(
            schema_name="captain.agent-runtime-result.v1",
            event_id=uuid5(command.event_id, "infrastructure-failure"),
            command_id=command.event_id,
            correlation_id=command.correlation_id,
            occurred_at=occurred_at,
            producer="agent-runtime",
            subject_id=command.subject_id,
            subject_version=command.subject_version,
            grant_id=grant.grant_id,
            operation=command.payload.operation,
            status=RuntimeStatus.INFRASTRUCTURE_FAILED,
            evidence_refs=(evidence_ref,),
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
