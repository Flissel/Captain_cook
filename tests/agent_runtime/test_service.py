from __future__ import annotations

import json
import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest

from agenten.agent_runtime.capabilities import CapabilityDenied, derive_grant, validate_grant
from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeResult,
    ArtifactRef,
    CapabilityGrant,
    CapabilityGrantRevocation,
    HermesPlanResult,
    RuntimeOperation,
    RuntimeCostEvidenceV1,
    RuntimeResumeCostAuthorityV1,
    RuntimeResumeCostSettlementV1,
    RuntimeStatus,
    RuntimeUsagePricingSnapshotV1,
)
from agenten.agent_runtime.service import AgentRuntimeService, RuntimeContractViolation
from agenten.validation.contracts import AcceptanceAssertion, AssertionKind, WorkBatch


NOW = datetime(2026, 7, 17, 12, tzinfo=timezone.utc)
PRICING = RuntimeUsagePricingSnapshotV1(
    schema_name="captain.runtime-usage-pricing-snapshot.v1",
    snapshot_id="openai-test-2026-08-09",
    provider="openai",
    model="gpt-5.6-terra",
    input_cost_per_million_usd=Decimal("1.25"),
    cached_input_cost_per_million_usd=Decimal("0.125"),
    output_cost_per_million_usd=Decimal("10.00"),
    effective_at=NOW,
    expires_at=NOW + timedelta(days=1),
)
FIXTURE = (
    Path(__file__).parents[1]
    / "fixtures"
    / "contracts"
    / "agent_runtime_command.v1.json"
)


def artifact(name: str, digest: str = "a") -> ArtifactRef:
    return ArtifactRef(
        uri=f"artifact://runtime/{name}",
        sha256=digest * 64,
        media_type="text/markdown",
    )


def command_for(operation: str = "codex.run") -> AgentRuntimeCommand:
    value: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    value["event_id"] = str(uuid4())
    value["occurred_at"] = NOW.isoformat().replace("+00:00", "Z")
    value["payload"]["operation"] = operation
    if operation.startswith("hermes."):
        value["payload"]["capability_profile"] = (
            "planner" if operation == "hermes.plan" else "agent-designer"
        )
        value["payload"]["integration_intent"] = "none"
    return AgentRuntimeCommand.model_validate(value)


def resume_command_with_cost(*, ceiling: str = "0.75") -> AgentRuntimeCommand:
    value: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    value["event_id"] = str(uuid4())
    value["correlation_id"] = str(uuid5(NAMESPACE_URL, "resume-correlation"))
    value["occurred_at"] = NOW.isoformat().replace("+00:00", "Z")
    value["payload"].update(
        {
            "operation": "codex.resume",
            "maximum_cost_usd": ceiling,
            "budget_reservation_id": str(uuid5(NAMESPACE_URL, "resume-reservation")),
            "cost_authority_ref": "gateway://capability-resume-authorizations/authority-one",
            "cost_job_id": str(uuid5(NAMESPACE_URL, "resume-job")),
            "cost_run_id": str(uuid5(NAMESPACE_URL, "resume-run")),
            "cost_input_id": "input-one",
            "cost_capability_id": "claims-capability",
            "cost_capability_version": 3,
        }
    )
    return AgentRuntimeCommand.model_validate(value)


def batch_for(command: AgentRuntimeCommand) -> WorkBatch:
    return WorkBatch(
        batch_id="batch-1",
        title="Released runtime work",
        goal="Perform the bounded runtime operation.",
        subtask_ids=["subtask-1"],
        target="python",
        capability_tags=[command.payload.capability_profile.value],
        acceptance_criteria=[
            AcceptanceAssertion(
                assertion_id="runtime-result-recorded",
                kind=AssertionKind.STATUS_EQUALS,
                path="status",
                expected="succeeded",
            )
        ],
    )


class FakeClock:
    def now(self) -> datetime:
        return NOW


class FakeCapabilityPolicy:
    def derive(
        self,
        command: AgentRuntimeCommand,
        batch: WorkBatch,
        now: datetime,
    ) -> CapabilityGrant:
        return derive_grant(command, batch, now)

    def validate(
        self,
        grant: CapabilityGrant,
        command: AgentRuntimeCommand,
        now: datetime,
        revocation: CapabilityGrantRevocation | None = None,
    ) -> CapabilityGrant:
        return validate_grant(grant, command, now, revocation)


class FakeState:
    def __init__(self, events: list[str], batch: WorkBatch) -> None:
        self.events = events
        self.batch = batch
        self.commands: dict[UUID, AgentRuntimeCommand] = {}
        self.grants: dict[UUID, CapabilityGrant] = {}
        self.revocations: dict[UUID, CapabilityGrantRevocation] = {}
        self.results: dict[UUID, AgentRuntimeResult] = {}

    async def get_result(self, command_id: UUID) -> AgentRuntimeResult | None:
        return self.results.get(command_id)

    async def accept_command(self, command: AgentRuntimeCommand) -> None:
        existing = self.commands.get(command.event_id)
        if existing is not None and existing != command:
            raise RuntimeError("conflicting command replay")
        if existing is None:
            self.events.append("command_accepted")
        self.commands[command.event_id] = command

    async def get_released_batch(self, command: AgentRuntimeCommand) -> WorkBatch:
        assert command.event_id in self.commands
        return self.batch

    async def get_grant(self, command_id: UUID) -> CapabilityGrant | None:
        return self.grants.get(command_id)

    async def get_grant_revocation(
        self, command_id: UUID
    ) -> CapabilityGrantRevocation | None:
        return self.revocations.get(command_id)

    async def record_grant(self, grant: CapabilityGrant) -> CapabilityGrant:
        self.grants[grant.command_id] = grant
        self.events.append("grant_recorded")
        return grant

    async def record_result(self, result: AgentRuntimeResult) -> AgentRuntimeResult:
        assert result.command_id in self.commands
        self.results[result.command_id] = result
        self.events.append("result_recorded")
        return result


class FakeArtifacts:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.contents: dict[str, bytes] = {}

    async def require(self, reference: ArtifactRef) -> None:
        assert reference.uri.startswith("artifact://")
        self.events.append("prompt_resolved")

    async def write(self, content: bytes, media_type: str) -> ArtifactRef:
        digest = hashlib.sha256(content).hexdigest()
        self.contents[digest] = content
        self.events.append("evidence_written")
        return ArtifactRef(
            uri=f"artifact://sha256/{digest}",
            sha256=digest,
            media_type=media_type,
        )


class FakeCodex:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls: list[RuntimeOperation] = []
        self.fail = False
        self.mismatch = False
        self.actual_cost_usd = Decimal("0.25")

    async def start(
        self, command: AgentRuntimeCommand, grant: CapabilityGrant
    ) -> AgentRuntimeResult:
        return self._result(command, grant)

    async def resume(
        self, command: AgentRuntimeCommand, grant: CapabilityGrant
    ) -> AgentRuntimeResult:
        return self._result(command, grant)

    async def status(
        self, command: AgentRuntimeCommand, grant: CapabilityGrant
    ) -> AgentRuntimeResult:
        return self._result(command, grant)

    async def cancel(
        self, command: AgentRuntimeCommand, grant: CapabilityGrant
    ) -> AgentRuntimeResult:
        return self._result(command, grant)

    async def heartbeat(
        self, command: AgentRuntimeCommand, grant: CapabilityGrant
    ) -> AgentRuntimeResult:
        return self._result(command, grant)

    def _result(
        self, command: AgentRuntimeCommand, grant: CapabilityGrant
    ) -> AgentRuntimeResult:
        self.events.append("codex_adapter")
        self.calls.append(command.payload.operation)
        assert "command_accepted" in self.events
        assert "grant_recorded" in self.events
        if self.fail:
            raise OSError("sensitive adapter detail")
        result = AgentRuntimeResult(
            schema_name="captain.agent-runtime-result.v1",
            event_id=uuid4(),
            command_id=uuid4() if self.mismatch else command.event_id,
            correlation_id=command.correlation_id,
            occurred_at=NOW,
            producer="agent-runtime",
            subject_id=command.subject_id,
            subject_version=command.subject_version,
            grant_id=grant.grant_id,
            operation=command.payload.operation,
            status=RuntimeStatus.SUCCEEDED,
            session_id="codex-session-1",
            artifact_refs=(artifact("build-output"),),
            evidence_refs=(artifact("test-evidence"),),
        )
        if command.payload.operation is RuntimeOperation.CODEX_RESUME:
            return result.model_copy(
                update={
                    "cost_evidence": RuntimeCostEvidenceV1(
                        schema_name="captain.runtime-cost-evidence.v1",
                        receipt_id=uuid5(command.event_id, "provider-usage"),
                        command_id=command.event_id,
                        result_id=result.event_id,
                        original_command_id=command.causation_id or command.event_id,
                        reservation_id=command.payload.budget_reservation_id,
                        job_id=command.payload.cost_job_id,
                        run_id=command.payload.cost_run_id,
                        input_id=command.payload.cost_input_id,
                        correlation_id=command.correlation_id,
                        capability_id=command.payload.cost_capability_id,
                        capability_version=command.payload.cost_capability_version,
                        provider="openai",
                        model="gpt-5.6-terra",
                        input_units=100_000,
                        cached_input_units=0,
                        output_units=12_500,
                        actual_cost_usd=self.actual_cost_usd,
                        pricing_snapshot_id=PRICING.snapshot_id,
                        pricing_snapshot_sha256=PRICING.snapshot_sha256,
                        started_at=NOW,
                        ended_at=NOW,
                        evidence_ref=artifact("provider-usage", "f"),
                    )
                }
            )
        return result


class FakeCostAuthority:
    def __init__(
        self,
        *,
        ceiling_usd: Decimal = Decimal("0.75"),
        disposition: str = "accounted",
        expired: bool = False,
        hard_ceiling_enforced: bool = True,
        metering_mode: str = "provider_usage_receipt",
    ) -> None:
        self.ceiling_usd = ceiling_usd
        self.disposition = disposition
        self.expired = expired
        self.hard_ceiling_enforced = hard_ceiling_enforced
        self.metering_mode = metering_mode
        self.authorize_calls = 0
        self.settle_calls = 0

    async def authorize(self, command: AgentRuntimeCommand):
        self.authorize_calls += 1
        return RuntimeResumeCostAuthorityV1(
            schema_name="captain.runtime-resume-cost-authority.v1",
            authorization_receipt_id=uuid5(NAMESPACE_URL, "authority-one"),
            cost_authority_ref=command.payload.cost_authority_ref,
            reservation_id=command.payload.budget_reservation_id,
            job_id=command.payload.cost_job_id,
            run_id=command.payload.cost_run_id,
            input_id=command.payload.cost_input_id,
            correlation_id=command.correlation_id,
            capability_id=command.payload.cost_capability_id,
            capability_version=command.payload.cost_capability_version,
            command_id=command.event_id,
            ceiling_usd=self.ceiling_usd,
            expires_at=(NOW - timedelta(seconds=1) if self.expired else NOW + timedelta(hours=1)),
            hard_ceiling_enforced=self.hard_ceiling_enforced,
            metering_mode=self.metering_mode,
        )

    async def settle(self, command, result, authority):
        self.settle_calls += 1
        actual = getattr(getattr(result, "cost_evidence", None), "actual_cost_usd", None)
        return RuntimeResumeCostSettlementV1(
            schema_name="captain.runtime-resume-cost-settlement.v1",
            settlement_id=uuid5(command.event_id, "cost-settlement"),
            command_id=command.event_id,
            reservation_id=authority.reservation_id,
            disposition=self.disposition,
            actual_cost_usd=actual,
            accounted_cost_usd=(
                actual
                if self.disposition == "overrun"
                else authority.ceiling_usd
                if self.disposition == "unmetered"
                else actual
            ),
            evidence_refs=(artifact("cost-settlement", "e"),),
        )


class FakeHermes:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls: list[RuntimeOperation] = []

    async def plan(
        self, command: AgentRuntimeCommand, grant: CapabilityGrant
    ) -> HermesPlanResult:
        return self._plan(command)

    async def design_agent(
        self, command: AgentRuntimeCommand, grant: CapabilityGrant
    ) -> HermesPlanResult:
        return self._plan(command)

    def _plan(self, command: AgentRuntimeCommand) -> HermesPlanResult:
        self.events.append("hermes_adapter")
        self.calls.append(command.payload.operation)
        return HermesPlanResult(
            schema_name="captain.hermes-plan-result.v1",
            project_id="project-1",
            correlation_id=command.correlation_id,
            subject_version=command.subject_version,
            plan_ref=artifact("hermes-plan", "b"),
            decision_log_ref=artifact("decision-log", "c"),
            blueprint_refs=(artifact("blueprint", "d"),),
            minibook={"project_id": "project-1", "post_id": "post-1"},
            planner_id="hermes-planner-1",
            runtime_provenance="hermes-fixture",
            started_at=NOW,
            ended_at=NOW,
        )


def service_with(
    state: FakeState,
    events: list[str],
    codex: FakeCodex,
    hermes: FakeHermes,
    cost_authority: FakeCostAuthority | None = None,
) -> AgentRuntimeService:
    return AgentRuntimeService(
        state=state,
        hermes=hermes,
        codex=codex,
        artifacts=FakeArtifacts(events),
        capabilities=FakeCapabilityPolicy(),
        clock=FakeClock(),
        cost_authority=cost_authority,
    )


@pytest.mark.asyncio
async def test_command_and_grant_are_persisted_before_external_effect() -> None:
    events: list[str] = []
    command = command_for()
    state = FakeState(events, batch_for(command))
    codex = FakeCodex(events)
    result = await service_with(state, events, codex, FakeHermes(events)).execute(command)

    assert result.status is RuntimeStatus.SUCCEEDED
    assert events == [
        "command_accepted",
        "grant_recorded",
        "prompt_resolved",
        "codex_adapter",
        "result_recorded",
    ]


@pytest.mark.asyncio
async def test_replay_returns_stored_result_without_second_adapter_call() -> None:
    events: list[str] = []
    command = command_for()
    state = FakeState(events, batch_for(command))
    codex = FakeCodex(events)
    first_service = service_with(state, events, codex, FakeHermes(events))
    first = await first_service.execute(command)

    restarted_service = service_with(state, events, codex, FakeHermes(events))
    replay = await restarted_service.execute(command)

    assert replay == first
    assert codex.calls == [RuntimeOperation.CODEX_RUN]
    assert events.count("command_accepted") == 1


@pytest.mark.asyncio
async def test_captain_revocation_blocks_an_existing_grant_before_external_effect() -> None:
    events: list[str] = []
    command = command_for()
    state = FakeState(events, batch_for(command))
    grant = derive_grant(command, state.batch, NOW)
    state.grants[command.event_id] = grant
    state.revocations[command.event_id] = CapabilityGrantRevocation(
        schema_name="captain.capability-grant-revocation.v1",
        revocation_id=uuid4(),
        grant_id=grant.grant_id,
        command_id=command.event_id,
        revoked_at=NOW,
        reason="captain_cancelled",
    )
    codex = FakeCodex(events)

    with pytest.raises(CapabilityDenied, match="revoked"):
        await service_with(state, events, codex, FakeHermes(events)).execute(command)

    assert codex.calls == []
    assert events == ["command_accepted"]


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        ("codex.run", "codex.run"),
        ("codex.resume", "codex.resume"),
        ("codex.status", "codex.status"),
        ("codex.cancel", "codex.cancel"),
        ("codex.heartbeat", "codex.heartbeat"),
    ],
)
@pytest.mark.asyncio
async def test_codex_operations_dispatch_explicitly(operation: str, expected: str) -> None:
    events: list[str] = []
    command = (
        resume_command_with_cost()
        if operation == "codex.resume"
        else command_for(operation)
    )
    state = FakeState(events, batch_for(command))
    codex = FakeCodex(events)
    costs = FakeCostAuthority() if operation == "codex.resume" else None

    await service_with(
        state, events, codex, FakeHermes(events), costs
    ).execute(command)

    assert [call.value for call in codex.calls] == [expected]


@pytest.mark.asyncio
async def test_resume_without_cost_authority_stops_before_codex_effect() -> None:
    events: list[str] = []
    command = command_for("codex.resume").model_copy(update={"producer": "captain"})
    state = FakeState(events, batch_for(command))
    codex = FakeCodex(events)

    with pytest.raises(RuntimeContractViolation, match="cost authority"):
        await service_with(state, events, codex, FakeHermes(events)).execute(command)

    assert codex.calls == []


def test_resume_command_carries_complete_cost_authority_binding() -> None:
    try:
        command = resume_command_with_cost()
    except Exception as exc:
        pytest.fail(f"resume cost binding contract is unavailable: {type(exc).__name__}")

    assert str(command.payload.cost_job_id) == str(uuid5(NAMESPACE_URL, "resume-job"))
    assert str(command.payload.cost_run_id) == str(uuid5(NAMESPACE_URL, "resume-run"))
    assert command.payload.cost_input_id == "input-one"
    assert command.payload.cost_capability_id == "claims-capability"
    assert command.payload.cost_capability_version == 3


@pytest.mark.asyncio
async def test_arbitrary_resume_ceiling_is_rejected_before_codex_effect() -> None:
    events: list[str] = []
    command = resume_command_with_cost(ceiling="0.01")
    state = FakeState(events, batch_for(command))
    codex = FakeCodex(events)
    costs = FakeCostAuthority(ceiling_usd=Decimal("0.75"))

    with pytest.raises(RuntimeContractViolation, match="cost authority binding"):
        await service_with(
            state, events, codex, FakeHermes(events), costs
        ).execute(command)

    assert costs.authorize_calls == 1
    assert codex.calls == []


@pytest.mark.parametrize(
    "costs",
    [
        FakeCostAuthority(expired=True),
        FakeCostAuthority(hard_ceiling_enforced=False),
        FakeCostAuthority(metering_mode="unavailable"),
    ],
)
@pytest.mark.asyncio
async def test_unusable_resume_cost_authority_stops_before_codex_effect(
    costs: FakeCostAuthority,
) -> None:
    events: list[str] = []
    command = resume_command_with_cost()
    state = FakeState(events, batch_for(command))
    codex = FakeCodex(events)

    with pytest.raises(RuntimeContractViolation, match="cost authority binding"):
        await service_with(
            state, events, codex, FakeHermes(events), costs
        ).execute(command)

    assert codex.calls == []


@pytest.mark.asyncio
async def test_valid_resume_authority_executes_once_and_accounts_actual_spend() -> None:
    events: list[str] = []
    command = resume_command_with_cost()
    state = FakeState(events, batch_for(command))
    codex = FakeCodex(events)
    costs = FakeCostAuthority()

    result = await service_with(
        state, events, codex, FakeHermes(events), costs
    ).execute(command)

    assert codex.calls == [RuntimeOperation.CODEX_RESUME]
    assert costs.authorize_calls == costs.settle_calls == 1
    assert result.status is RuntimeStatus.SUCCEEDED
    assert result.cost_evidence.actual_cost_usd == Decimal("0.25")


@pytest.mark.asyncio
async def test_resume_cost_overrun_never_persists_success() -> None:
    events: list[str] = []
    command = resume_command_with_cost()
    state = FakeState(events, batch_for(command))
    codex = FakeCodex(events)
    codex.actual_cost_usd = Decimal("0.80")
    costs = FakeCostAuthority(disposition="overrun")

    result = await service_with(
        state, events, codex, FakeHermes(events), costs
    ).execute(command)

    assert codex.calls == [RuntimeOperation.CODEX_RESUME]
    assert costs.settle_calls == 1
    assert result.status is not RuntimeStatus.SUCCEEDED
    assert state.results[command.event_id] == result


@pytest.mark.parametrize("operation", ["hermes.plan", "hermes.design_agent"])
@pytest.mark.asyncio
async def test_hermes_results_are_converted_to_runtime_results(operation: str) -> None:
    events: list[str] = []
    command = command_for(operation)
    state = FakeState(events, batch_for(command))
    hermes = FakeHermes(events)

    result = await service_with(state, events, FakeCodex(events), hermes).execute(command)

    assert result.producer == "hermes-runtime"
    assert result.artifact_refs[0].uri.endswith("hermes-plan")
    assert result.evidence_refs[0].uri.endswith("decision-log")
    assert hermes.calls == [command.payload.operation]


@pytest.mark.asyncio
async def test_mismatched_adapter_result_is_not_persisted() -> None:
    events: list[str] = []
    command = command_for()
    state = FakeState(events, batch_for(command))
    codex = FakeCodex(events)
    codex.mismatch = True

    with pytest.raises(RuntimeContractViolation, match="command"):
        await service_with(state, events, codex, FakeHermes(events)).execute(command)

    assert command.event_id not in state.results


@pytest.mark.asyncio
async def test_adapter_exception_becomes_redacted_durable_infrastructure_result() -> None:
    events: list[str] = []
    command = command_for()
    state = FakeState(events, batch_for(command))
    codex = FakeCodex(events)
    codex.fail = True

    result = await service_with(state, events, codex, FakeHermes(events)).execute(command)

    assert result.status is RuntimeStatus.INFRASTRUCTURE_FAILED
    assert result.error == "codex.run adapter failed"
    assert "sensitive" not in result.model_dump_json()
    assert len(result.evidence_refs) == 1
    reference = result.evidence_refs[0]
    assert reference.uri == f"artifact://sha256/{reference.sha256}"
    assert events[-1] == "result_recorded"


@pytest.mark.asyncio
async def test_resume_adapter_exception_remains_durable_infrastructure_failure_after_accounting() -> None:
    events: list[str] = []
    command = resume_command_with_cost()
    state = FakeState(events, batch_for(command))
    codex = FakeCodex(events)
    codex.fail = True
    costs = FakeCostAuthority(disposition="unmetered")

    result = await service_with(
        state, events, codex, FakeHermes(events), costs
    ).execute(command)

    assert costs.settle_calls == 1
    assert result.status is RuntimeStatus.INFRASTRUCTURE_FAILED
    assert result.error == "codex.resume adapter failed"
    assert state.results[command.event_id] == result
    assert result.evidence_refs[0].uri.startswith("artifact://sha256/")


@pytest.mark.asyncio
async def test_unreleased_capability_stops_before_artifact_or_adapter() -> None:
    events: list[str] = []
    command = command_for()
    wrong_batch = batch_for(command).model_copy(update={"capability_tags": ["code-builder"]})
    state = FakeState(events, wrong_batch)
    codex = FakeCodex(events)

    with pytest.raises(CapabilityDenied, match="not released"):
        await service_with(state, events, codex, FakeHermes(events)).execute(command)

    assert events == ["command_accepted"]
    assert codex.calls == []
