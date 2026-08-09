from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
import httpx

from agenten.agent_factory.contracts import FactoryPhase
from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeCommandPayload,
    AgentRuntimeResult,
    ArtifactRef,
    CapabilityGrant,
    CapabilityProfile,
    IntegrationIntent,
    RuntimeLimits,
    RuntimeOperation,
    RuntimeStatus,
)
from agenten.agent_runtime.http_executor import AgentRuntimeHttpExecutor
from gateway.capability_live_pipeline import (
    CapabilityResumeAuthorization,
    CapabilityResumeExecutionRequest,
)
from gateway.capability_resume_contracts import (
    CapabilityResumeAuthorizationRecordV1,
    CapabilityResumeSourceSnapshotV1,
    CapabilityResumeTerminalV1,
)
from gateway.capability_resume_service import (
    CapabilityResumeConflict,
    CapabilityResumeDispatchFailed,
    CapabilityResumeService,
    CapabilityResumeUnavailable,
)
from gateway.capability_resume_repository import (
    GatewayCapabilityResumeAuthoritySource,
)
from gateway.contracts import RuntimeOperationProjection


NOW = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)


def _uuid(label: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"capability-resume-test:{label}")


def _ref(label: str) -> ArtifactRef:
    digest = f"{_uuid(label).int:064x}"[-64:]
    return ArtifactRef(
        uri=f"artifact://sha256/{digest}",
        sha256=digest,
        media_type="application/json",
    )


def _original_command() -> AgentRuntimeCommand:
    return AgentRuntimeCommand(
        schema_name="captain.agent-runtime-command.v1",
        event_id=_uuid("original-command"),
        correlation_id=_uuid("correlation"),
        occurred_at=NOW,
        producer="captain",
        subject_id="capability-task",
        subject_version=3,
        payload=AgentRuntimeCommandPayload(
            operation=RuntimeOperation.CODEX_RUN,
            project_id="factory-project",
            batch_id="factory-batch",
            subtask_id="capability-task",
            workspace_ref="workspace://factory/capability-task",
            prompt_ref=_ref("prompt"),
            integration_intent=IntegrationIntent.NONE,
            capability_profile=CapabilityProfile.CODE_BUILDER,
            limits=RuntimeLimits(wall_seconds=300, max_iterations=2),
        ),
    )


def _result(
    command: AgentRuntimeCommand,
    *,
    status: RuntimeStatus = RuntimeStatus.SUCCEEDED,
    error: str | None = None,
    label: str = "result",
) -> AgentRuntimeResult:
    return AgentRuntimeResult(
        schema_name="captain.agent-runtime-result.v1",
        event_id=_uuid(label),
        command_id=command.event_id,
        correlation_id=command.correlation_id,
        occurred_at=NOW,
        producer="agent-runtime",
        subject_id=command.subject_id,
        subject_version=command.subject_version,
        grant_id=f"grant-{label}",
        operation=command.payload.operation,
        status=status,
        session_id=f"session-{label}",
        evidence_refs=(_ref(f"evidence-{label}"),),
        error=error,
    )


def _operation(
    command: AgentRuntimeCommand,
    result: AgentRuntimeResult,
) -> RuntimeOperationProjection:
    return RuntimeOperationProjection(
        operation_id=command.event_id,
        command=command,
        grant=CapabilityGrant(
            schema_name="captain.capability-grant.v1",
            grant_id=result.grant_id,
            command_id=command.event_id,
            batch_id=command.payload.batch_id or "factory-batch",
            batch_version=command.subject_version,
            subtask_id=command.subject_id,
            workspace_ref=command.payload.workspace_ref or "workspace://missing",
            profile=command.payload.capability_profile,
            capabilities=("filesystem.workspace",),
            issued_at=NOW,
            expires_at=datetime(2026, 8, 9, 13, 0, tzinfo=timezone.utc),
        ),
        result=result,
    )


def _request(**updates: object) -> CapabilityResumeExecutionRequest:
    values: dict[str, object] = {
        "job_id": _uuid("job"),
        "reservation_id": _uuid("reservation"),
        "run_id": _uuid("run"),
        "input_id": "input-one",
        "maximum_cost_usd": Decimal("2.00"),
        "booked_cost_usd": Decimal("1.25"),
        "correlation_id": _uuid("correlation"),
        "capability_id": "claims-capability",
        "capability_version": 3,
        "original_execution_command_id": _uuid("original-command"),
        "original_execution_result_id": _uuid("original-result"),
    }
    values.update(updates)
    return CapabilityResumeExecutionRequest(**values)


class PersistentRepository:
    def __init__(self) -> None:
        self.authorization_lock = Lock()
        self.records: dict[UUID, CapabilityResumeAuthorizationRecordV1] = {}
        self.by_reservation: dict[UUID, UUID] = {}
        self.started: set[UUID] = set()
        self.terminals: dict[UUID, CapabilityResumeTerminalV1] = {}

    def authorize_once(self, request, source, build):
        with self.authorization_lock:
            existing = self.authorization_for_reservation(request.reservation_id)
            if existing is not None:
                if existing.request != request:
                    raise CapabilityResumeConflict("reservation binding does not match")
                return existing
            return self.record_authorization(build(source.resolve(request)))

    def authorization_for_reservation(
        self, reservation_id: UUID
    ) -> CapabilityResumeAuthorizationRecordV1 | None:
        receipt_id = self.by_reservation.get(reservation_id)
        return self.records.get(receipt_id) if receipt_id is not None else None

    def authorization(
        self, receipt_id: UUID
    ) -> CapabilityResumeAuthorizationRecordV1 | None:
        return self.records.get(receipt_id)

    def record_authorization(
        self, record: CapabilityResumeAuthorizationRecordV1
    ) -> CapabilityResumeAuthorizationRecordV1:
        existing = self.authorization_for_reservation(record.request.reservation_id)
        if existing is not None and existing != record:
            raise CapabilityResumeConflict("reservation binding does not match")
        self.records[record.authorization.authorization_receipt_id] = record
        self.by_reservation[record.request.reservation_id] = (
            record.authorization.authorization_receipt_id
        )
        return existing or record

    def dispatch_started(self, receipt_id: UUID) -> bool:
        return receipt_id in self.started

    def record_dispatch_started(self, receipt_id: UUID) -> bool:
        created = receipt_id not in self.started
        self.started.add(receipt_id)
        return created

    def terminal(self, receipt_id: UUID) -> CapabilityResumeTerminalV1 | None:
        return self.terminals.get(receipt_id)

    def record_terminal(
        self, terminal: CapabilityResumeTerminalV1
    ) -> CapabilityResumeTerminalV1:
        receipt_id = terminal.authorization_receipt_id
        existing = self.terminals.get(receipt_id)
        if existing is not None and existing != terminal:
            raise CapabilityResumeConflict("terminal read-back is immutable")
        self.terminals[receipt_id] = terminal
        return existing or terminal


class RuntimeTruth:
    def __init__(self) -> None:
        original = _original_command()
        original_result = _result(
            original,
            status=RuntimeStatus.INFRASTRUCTURE_FAILED,
            error="codex execution timed out (exit 124)",
            label="original-result",
        )
        self.snapshot = CapabilityResumeSourceSnapshotV1(
            job_id=_uuid("job"),
            correlation_id=_uuid("correlation"),
            capability_id="claims-capability",
            capability_version=3,
            booked_cost_usd=Decimal("1.25"),
            reservation_id=_uuid("reservation"),
            run_id=_uuid("run"),
            input_id="input-one",
            maximum_cost_usd=Decimal("2.00"),
            resume_budget_usd=Decimal("0.75"),
            original_operation=_operation(original, original_result),
        )
        self.operations = {original.event_id: self.snapshot.original_operation}

    def resolve(
        self, _request: CapabilityResumeExecutionRequest
    ) -> CapabilityResumeSourceSnapshotV1:
        return self.snapshot

    def runtime_operation(self, command_id: UUID) -> RuntimeOperationProjection | None:
        return self.operations.get(command_id)


class PersistingExecutor:
    def __init__(
        self,
        truth: RuntimeTruth,
        *,
        status: RuntimeStatus = RuntimeStatus.SUCCEEDED,
        error: str | None = None,
        persist: bool = True,
    ) -> None:
        self.truth = truth
        self.status = status
        self.error = error
        self.persist = persist
        self.calls = 0

    async def execute(self, command: AgentRuntimeCommand) -> AgentRuntimeResult:
        self.calls += 1
        result = _result(
            command,
            status=self.status,
            error=self.error,
            label=f"resume-result-{self.status.value}",
        )
        if self.persist:
            self.truth.operations[command.event_id] = _operation(command, result)
        return result


def _service(
    repository: PersistentRepository,
    truth: RuntimeTruth,
    executor: PersistingExecutor | None,
) -> CapabilityResumeService:
    return CapabilityResumeService(
        repository=repository,
        authority_source=truth,
        runtime_reader=truth,
        executor=executor,
    )


def test_authorize_is_idempotent_and_binds_original_runtime_truth() -> None:
    repository = PersistentRepository()
    truth = RuntimeTruth()
    service = _service(repository, truth, None)

    first = service.authorize(_request())
    second = service.authorize(_request())

    assert second == first
    record = repository.authorization(first.authorization_receipt_id)
    assert record is not None
    assert record.resume_command.payload.operation is RuntimeOperation.CODEX_RESUME
    assert record.resume_command.causation_id == _uuid("original-command")
    assert record.resume_command.event_id != _uuid("original-command")
    assert record.resume_budget_usd == Decimal("0.75")
    assert record.booked_cost_usd == Decimal("1.25")
    assert record.resume_command.payload.maximum_cost_usd == Decimal("0.75")
    assert record.resume_command.payload.cost_authority_ref is not None


def test_concurrent_authorization_has_exactly_one_atomic_winner() -> None:
    repository = PersistentRepository()
    truth = RuntimeTruth()
    service = _service(repository, truth, None)

    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = tuple(pool.map(lambda _: service.authorize(_request()), range(8)))

    assert len(set(receipts)) == 1
    assert len(repository.records) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("job_id", _uuid("wrong-job")),
        ("reservation_id", _uuid("wrong-reservation")),
        ("run_id", _uuid("wrong-run")),
        ("input_id", "wrong-input"),
        ("maximum_cost_usd", Decimal("3.00")),
        ("booked_cost_usd", Decimal("1.00")),
        ("correlation_id", _uuid("wrong-correlation")),
        ("capability_id", "wrong-capability"),
        ("capability_version", 4),
        ("original_execution_command_id", _uuid("wrong-command")),
        ("original_execution_result_id", _uuid("wrong-result")),
    ],
)
def test_authorize_rejects_every_binding_mismatch(field: str, value: object) -> None:
    with pytest.raises(CapabilityResumeConflict, match="binding"):
        _service(PersistentRepository(), RuntimeTruth(), None).authorize(
            _request(**{field: value})
        )


def test_authorize_rejects_a_succeeded_original_as_not_resumable() -> None:
    truth = RuntimeTruth()
    original = _original_command()
    truth.snapshot = truth.snapshot.model_copy(
        update={
            "original_operation": _operation(
                original,
                _result(original, label="original-result"),
            )
        }
    )

    with pytest.raises(CapabilityResumeUnavailable, match="not resumable"):
        _service(PersistentRepository(), truth, None).authorize(_request())


@pytest.mark.asyncio
async def test_dispatch_calls_effect_once_and_replays_immutable_terminal_readback() -> None:
    repository = PersistentRepository()
    truth = RuntimeTruth()
    executor = PersistingExecutor(truth)
    service = _service(repository, truth, executor)
    authorization = service.authorize(_request())

    first = await service.dispatch(authorization)
    second = await service.dispatch(authorization)
    readback = service.readback(authorization)

    assert executor.calls == 1
    assert first == second == readback
    assert first.execution_authority_id == authorization.authorization_receipt_id
    assert first.terminal_status == "succeeded"
    assert first.replayed is False


@pytest.mark.asyncio
async def test_dispatch_uses_authenticated_runtime_http_boundary_then_gateway_readback() -> None:
    repository = PersistentRepository()
    truth = RuntimeTruth()
    calls = 0

    def runtime(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.path == "/v1/runtime/execute"
        assert request.headers["Authorization"] == "Bearer runtime-test-token"
        command = AgentRuntimeCommand.model_validate(json.loads(request.content))
        result = _result(command, label="http-runtime-result")
        truth.operations[command.event_id] = _operation(command, result)
        return httpx.Response(
            200,
            json=result.model_dump(mode="json", by_alias=True),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(runtime)) as http:
        service = CapabilityResumeService(
            repository=repository,
            authority_source=truth,
            runtime_reader=truth,
            executor=AgentRuntimeHttpExecutor(
                "https://runtime.test",
                "runtime-test-token",
                http,
            ),
        )
        authorization = service.authorize(_request())
        first = await service.dispatch(authorization)
        second = await service.dispatch(authorization)

    assert calls == 1
    assert first == second == service.readback(authorization)


@pytest.mark.asyncio
async def test_dispatch_never_accepts_executor_only_success_without_persisted_runtime_result() -> None:
    repository = PersistentRepository()
    truth = RuntimeTruth()
    executor = PersistingExecutor(truth, persist=False)
    service = _service(repository, truth, executor)
    authorization = service.authorize(_request())

    with pytest.raises(CapabilityResumeUnavailable, match="persisted runtime result"):
        await service.dispatch(authorization)

    assert executor.calls == 1
    assert repository.terminal(authorization.authorization_receipt_id) is None


@pytest.mark.asyncio
async def test_existing_dispatch_fence_never_repeats_an_ambiguous_effect() -> None:
    repository = PersistentRepository()
    truth = RuntimeTruth()
    executor = PersistingExecutor(truth)
    service = _service(repository, truth, executor)
    authorization = service.authorize(_request())
    repository.record_dispatch_started(authorization.authorization_receipt_id)

    with pytest.raises(CapabilityResumeUnavailable, match="persisted runtime result"):
        await service.dispatch(authorization)

    assert executor.calls == 0


@pytest.mark.asyncio
async def test_transport_failure_remains_ambiguous_until_late_runtime_truth() -> None:
    repository = PersistentRepository()
    truth = RuntimeTruth()

    class BrokenExecutor:
        calls = 0

        async def execute(self, command: AgentRuntimeCommand) -> AgentRuntimeResult:
            self.calls += 1
            raise RuntimeError("transport contained secret-token")

    executor = BrokenExecutor()
    first = CapabilityResumeService(
        repository=repository,
        authority_source=truth,
        runtime_reader=truth,
        executor=executor,
    )
    authorization = first.authorize(_request())

    with pytest.raises(CapabilityResumeUnavailable, match="ambiguous"):
        await first.dispatch(authorization)
    record = repository.authorization(authorization.authorization_receipt_id)
    assert record is not None
    late_result = _result(record.resume_command, label="late-runtime-result")
    truth.operations[record.resume_command.event_id] = _operation(
        record.resume_command,
        late_result,
    )
    restarted = CapabilityResumeService(
        repository=repository,
        authority_source=truth,
        runtime_reader=truth,
        executor=executor,
    )
    recovered = restarted.readback(authorization)

    assert executor.calls == 1
    assert recovered.execution_result_id == late_result.event_id
    assert repository.terminal(authorization.authorization_receipt_id) is not None


def test_restart_recovers_terminal_runtime_truth_without_a_second_dispatch() -> None:
    repository = PersistentRepository()
    truth = RuntimeTruth()
    first = _service(repository, truth, None)
    authorization = first.authorize(_request())
    record = repository.authorization(authorization.authorization_receipt_id)
    assert record is not None
    repository.record_dispatch_started(authorization.authorization_receipt_id)
    persisted_result = _result(record.resume_command, label="restart-result")
    truth.operations[record.resume_command.event_id] = _operation(
        record.resume_command, persisted_result
    )

    restarted = _service(repository, truth, None)
    recovered = restarted.readback(authorization)

    assert recovered.execution_result_id == persisted_result.event_id
    assert repository.terminal(authorization.authorization_receipt_id) is not None


@pytest.mark.parametrize(
    ("runtime_status", "error", "terminal_status"),
    [
        (RuntimeStatus.CANCELLED, None, "cancelled"),
        (
            RuntimeStatus.INFRASTRUCTURE_FAILED,
            "codex execution timed out (exit 124)",
            "timed_out",
        ),
        (RuntimeStatus.FAILED, "codex execution failed", "failed"),
    ],
)
@pytest.mark.asyncio
async def test_terminal_readback_preserves_non_success_outcome(
    runtime_status: RuntimeStatus,
    error: str | None,
    terminal_status: str,
) -> None:
    repository = PersistentRepository()
    truth = RuntimeTruth()
    executor = PersistingExecutor(truth, status=runtime_status, error=error)
    service = _service(repository, truth, executor)
    authorization = service.authorize(_request())

    if terminal_status == "failed":
        with pytest.raises(CapabilityResumeDispatchFailed) as failure:
            await service.dispatch(authorization)
        assert failure.value.failure.execution.terminal_status == "failed"
        with pytest.raises(CapabilityResumeDispatchFailed):
            service.readback(authorization)
    else:
        authority = await service.dispatch(authorization)
        assert authority.terminal_status == terminal_status
        assert service.readback(authorization) == authority


class GatewayStoreProjectionDouble:
    def __init__(self, truth: RuntimeTruth, *, matching_outcome: bool = True) -> None:
        self.truth = truth
        request = _request()
        self.job = SimpleNamespace(
            phase=FactoryPhase.CAPABILITY_PROMOTED,
            job=SimpleNamespace(
                job_id=request.job_id,
                correlation_id=request.correlation_id,
                required_capability=request.capability_id,
                subject_version=request.capability_version,
            )
        )
        outcome = SimpleNamespace(
            command_id=request.original_execution_command_id,
            result_id=(
                request.original_execution_result_id
                if matching_outcome
                else _uuid("different-factory-result")
            ),
            correlation_id=request.correlation_id,
            capability_id=request.capability_id,
            capability_version=request.capability_version,
        )
        self.artifacts = (SimpleNamespace(execution_outcome=outcome),)

    def factory_job(self, _job_id: UUID):
        return self.job

    def runtime_operation(self, command_id: UUID):
        operation = self.truth.runtime_operation(command_id)
        if operation is None:
            raise AssertionError("test runtime operation missing")
        return operation

    def factory_budget(self, _job_id: UUID):
        return SimpleNamespace(
            limit_usd=Decimal("2.00"),
            consumed_usd=Decimal("1.25"),
            reserved_usd=Decimal("0.00"),
            remaining_usd=Decimal("0.75"),
        )

    def factory_workflow_artifacts(self, _job_id: UUID):
        return self.artifacts


def test_gateway_authority_source_requires_exact_factory_and_runtime_projections() -> None:
    truth = RuntimeTruth()
    source = GatewayCapabilityResumeAuthoritySource(
        GatewayStoreProjectionDouble(truth),  # type: ignore[arg-type]
    )

    snapshot = source.resolve(_request())

    assert snapshot == truth.snapshot


def test_gateway_authority_source_rejects_unbound_runtime_outcome() -> None:
    truth = RuntimeTruth()
    source = GatewayCapabilityResumeAuthoritySource(
        GatewayStoreProjectionDouble(  # type: ignore[arg-type]
            truth, matching_outcome=False
        ),
    )

    with pytest.raises(CapabilityResumeConflict, match="Factory outcome"):
        source.resolve(_request())
