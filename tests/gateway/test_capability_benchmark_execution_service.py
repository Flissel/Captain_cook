from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
from datetime import datetime, timezone
from types import SimpleNamespace
from threading import Barrier, RLock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from fastapi import HTTPException

from agenten.agent_runtime.captain_production_adapters import (
    ContentAddressedArtifactAdapter,
)
from agenten.agent_runtime.contracts import AgentRuntimeResult, RuntimeStatus
from gateway.capability_assembly_bootstrap import CaptainBenchmarkToolEvidenceAuthority
from gateway.capability_benchmark_execution_service import (
    BenchmarkRuntimeEffectResult,
    GatewayDurableBenchmarkToolBroker,
    GatewayBenchmarkExecutionService,
)
from gateway.capability_live_pipeline import CapabilityLiveAuthorityUnavailable
from gateway.store import GatewayStore
from agenten.provider_proxy.contracts import benchmark_provider_settlement_id
from tests.gateway.test_capability_benchmark_process_executor import _ref, _request
from tests.gateway.test_capability_benchmark_process_app import _payload


class _Store:
    def __init__(self, request) -> None:
        self.request = request
        self.job = SimpleNamespace(
            job_id=request.job_id, correlation_id=uuid4(),
            required_capability=request.capability_id, input_ref=request.input_ref,
            subject_version=1,
            occurred_at=datetime(2026, 8, 17, tzinfo=timezone.utc),
            execution_policy=SimpleNamespace(
                allowed_models=(request.model_id,), max_cost_usd=Decimal("6.00"),
                max_runtime_seconds=600,
            ),
        )
        self.job.execution_policy.model_dump = lambda **_kwargs: {
            "schema": "captain.factory-execution-policy.v1",
            "max_cost_usd": "6.00",
        }
        self.job.model_dump = lambda **_kwargs: {
            "schema": "captain.agent-factory-job.v3",
            "job_id": str(request.job_id),
        }
        self.command = None
        self.result = None
        self.batch = None
        self.registry = {}
        self.authorizations = {}
        self.effects = {}
        self.provider_settlements = []
        self.budget_reservation = None

    def factory_job(self, _job_id):
        return SimpleNamespace(job=self.job)

    def factory_budget(self, _job_id):
        return SimpleNamespace(remaining_usd=Decimal("6.00"))

    def reserve_factory_budget(self, reservation):
        self.budget_reservation = reservation

    def record_capability_benchmark_work_batch(self, **payload):
        self.batch = payload

    def accept_runtime_command(self, command):
        self.command = command

    def record_runtime_result(self, result):
        self.result = result

    def runtime_operation(self, _command_id):
        return SimpleNamespace(command=self.command, result=self.result)

    def record_capability_tool_evidence(self, **payload):
        assert payload["job_id"] == self.request.job_id
        assert payload["batch_id"] == self.batch["batch_id"]
        assert payload["trace_id"] == self.batch["trace_id"]
        assert payload["run_id"] == self.batch["run_id"]
        receipt_ref = payload["receipt_ref"]
        self.registry[receipt_ref.sha256] = {
            "job_id": str(payload["job_id"]),
            "case_digest": payload["case_digest"],
            "tool_key": payload["tool_key"],
            "operation": payload["operation"],
            "trace_id": str(payload["trace_id"]),
            "run_id": str(payload["run_id"]),
            "model_policy_digest": payload["model_policy_digest"],
            "call_ref": payload["call_ref"].model_dump(mode="json"),
            "lease_ref": payload["lease_ref"].model_dump(mode="json"),
            "receipt_ref": receipt_ref.model_dump(mode="json"),
            "batch_id": payload["batch_id"],
            "execution_result_id": str(payload["execution_result_id"]),
            "correlation_id": str(self.job.correlation_id),
            "result_ref_sha256": payload["result_ref_sha256"],
        }

    def record_capability_tool_authorization(self, **payload):
        assert self.result is None
        assert payload["job_id"] == str(self.request.job_id)
        assert payload["batch_id"] == self.batch["batch_id"]
        self.authorizations[payload["tool_key"]] = payload

    def capability_tool_evidence(self, receipt_sha256):
        return self.registry.get(receipt_sha256)

    def capability_tool_authorization(self, call_sha256):
        for value in self.authorizations.values():
            if value["call_ref"].sha256 == call_sha256:
                return {
                    **{key: item for key, item in value.items()
                       if key not in {"call_ref", "lease_ref"}},
                    "call_ref": value["call_ref"].model_dump(mode="json"),
                    "lease_ref": value["lease_ref"].model_dump(mode="json"),
                }
        return None

    def record_capability_tool_effect(self, **payload):
        key = payload["call_ref"].sha256
        stored = {
            "call_ref": payload["call_ref"].model_dump(mode="json"),
            "lease_ref": payload["lease_ref"].model_dump(mode="json"),
            "result_ref": payload["result_ref"].model_dump(mode="json"),
            "receipt_ref": payload["receipt_ref"].model_dump(mode="json"),
            "arguments_sha256": payload["arguments_sha256"],
            "scoped_token_sha256": payload["scoped_token_sha256"],
        }
        if key in self.effects:
            assert self.effects[key] == stored
        self.effects[key] = stored

    def capability_tool_effect(self, call_sha256):
        return self.effects.get(call_sha256)

    def capability_benchmark_provider_settlements(self, command_id):
        return tuple(
            item for item in self.provider_settlements
            if item["command_id"] == str(command_id)
        )

    def record_capability_benchmark_provider_authority(self, **payload):
        self.provider_authority = payload

    def finalize_capability_benchmark_provider_cost(self, command_id):
        return sum(
            (Decimal(item["actual_cost_usd"])
             for item in self.capability_benchmark_provider_settlements(command_id)),
            start=Decimal("0"),
        )


class _PrivateCases:
    def __init__(self, request) -> None:
        self.request = request

    def resolve(self, case_ref, **_kwargs):
        if case_ref != self.request.case_ref:
            raise RuntimeError("foreign case")
        return self.request.execution_case

    def resolve_execution_transport(self, case_ref, **_kwargs):
        if (
            case_ref.case_id != self.request.case_ref.case_id
            or case_ref.category != self.request.case_ref.category
            or case_ref.case_digest != self.request.case_ref.case_digest
        ):
            raise RuntimeError("foreign case")
        return self.request.case_ref, self.request.execution_case


class _RuntimeEffect:
    def __init__(self, *, artifacts, tools) -> None:
        self.artifacts = artifacts
        self.tools = tools
        self.calls = 0

    async def execute(
        self, command, *, execution_plan, team_kind, authorized_tools,
        tool_broker, execution_case,
    ):
        self.calls += 1
        assert execution_plan == self.artifacts_plan
        assert execution_case.setup == self.request.execution_case.setup
        assert execution_case.action == self.request.execution_case.action
        assert execution_case.declared_tool_keys == self.request.execution_case.declared_tool_keys
        self.authorized_tools = authorized_tools
        authorized = {item.tool_key: item for item in authorized_tools}
        refs = {}
        for tool in self.tools:
            lease = authorized.get(tool)
            refs[tool] = tool_broker.execute(
                tool_key=tool,
                call_id=(lease.call_id if lease else "foreign-call"),
                lease_id=(lease.lease_id if lease else "foreign-lease"),
                operation="read", arguments={"tool_key": tool},
            )
        result = AgentRuntimeResult(
            schema="captain.agent-runtime-result.v1", event_id=uuid4(),
            command_id=command.event_id,
            correlation_id=command.correlation_id,
            occurred_at=command.occurred_at, producer="agent-runtime",
            subject_id=command.subject_id,
            subject_version=command.subject_version,
            grant_id="benchmark-runtime-grant", operation=command.payload.operation,
            status=RuntimeStatus.SUCCEEDED,
            artifact_refs=tuple(refs.values()), evidence_refs=(),
        )
        return BenchmarkRuntimeEffectResult(
            result=result, observations={"business_outcome": "bounded"},
            rationale="bounded", messages=(("answer", "bounded"),),
            handoffs=(), tool_result_refs=refs,
            cost_usd=Decimal("0.01"), latency_ms=1,
            candidate_ref=execution_plan.candidate_ref,
            execution_ref=(execution_plan.team_manifest_ref if team_kind == "team"
                           else execution_plan.baseline_policy_ref),
            agent_names=(execution_plan.team_agent_names if team_kind == "team"
                         else (execution_plan.baseline_agent_name,)),
        )


class _ToolRegistry:
    digest = "9" * 64

    def __init__(self, tools) -> None:
        self.tools = tuple(tools)
        self.calls = []
        self.host_receipts = {}

    def require_declared(self, tools):
        if any(tool not in {"captain_tool", "second_tool"} for tool in tools):
            raise RuntimeError("unknown tool")

    def operation_for(self, tool):
        self.require_declared((tool,))
        return "read"

    def execute(self, *, tool_key, operation, arguments):
        assert operation == "read"
        self.calls.append((tool_key, dict(arguments)))
        return {
            "schema": "captain.test-benchmark-tool-result.v1",
            "tool_key": tool_key,
        }

    def effect_request_sha256(self, *, tool_key, operation, arguments, effect_id):
        return __import__("hashlib").sha256(__import__("json").dumps({
            "tool_key": tool_key, "operation": operation,
            "arguments": dict(arguments), "effect_id": effect_id,
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def read_effect(self, *, tool_key, effect_id, request_sha256):
        value = self.host_receipts.get(effect_id)
        if value is not None:
            assert value[0] == request_sha256
            return value[1]
        return None

    def execute_effect(self, *, tool_key, operation, arguments, effect_id):
        request_sha256 = self.effect_request_sha256(
            tool_key=tool_key, operation=operation,
            arguments=arguments, effect_id=effect_id,
        )
        existing = self.host_receipts.get(effect_id)
        if existing is not None:
            assert existing[0] == request_sha256
            return existing[1]
        value = self.execute(
            tool_key=tool_key, operation=operation, arguments=arguments,
        )
        self.host_receipts[effect_id] = (request_sha256, value)
        return value
def _service(tmp_path, request, tools, *, artifacts=None):
    artifacts = artifacts or ContentAddressedArtifactAdapter(tmp_path / "cas")
    store = _Store(request)
    effect = _RuntimeEffect(artifacts=artifacts, tools=tools)
    effect.request = request
    effect.artifacts_plan = request.execution_plan
    registry = _ToolRegistry(tools)
    issuer = CaptainBenchmarkToolEvidenceAuthority(
        gateway_store=store, artifacts=artifacts,
        scoped_token_key=SecretStr("benchmark-tool-test-key"),
    )
    return GatewayBenchmarkExecutionService(
        store=store, artifacts=artifacts,
        private_cases=_PrivateCases(request),
        executor_authority_id=uuid4(), runtime_effect=effect,
        tool_issuer=issuer, tool_registry=registry,
        provider_binding={
            "provider_proxy_url": "http://127.0.0.1:8091/v1",
            "provider_policy_sha256": "1" * 64,
            "provider_price_card_sha256": "2" * 64,
            "provider_context_sha256": "3" * 64,
            "provider_proxy_executable_sha256": "4" * 64,
            "provider_proxy_code_sha256": "5" * 64,
            "provider_paid_command_sha256": "6" * 64,
            "provider_hard_ceiling_enforced": True,
        },
        clock=lambda: __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ),
    ), store, effect


def test_gateway_executes_and_issues_every_exact_declared_tool(tmp_path) -> None:
    original = _request()
    view = replace(
        original.execution_case,
        declared_tool_keys=("captain_tool", "second_tool"),
        tool_authority_sha256=__import__("hashlib").sha256(
            b'["captain_tool","second_tool"]'
        ).hexdigest(),
    )
    from gateway.capability_generic_benchmark import generic_execution_request_digest
    request = replace(original, execution_case=view)
    request = replace(request, request_digest=generic_execution_request_digest(
        job_id=request.job_id, capability_id=request.capability_id,
        input_ref=request.input_ref, case_ref=request.case_ref,
        team_kind=request.team_kind, model_id=request.model_id,
        model_policy_digest=request.model_policy_digest,
        execution_case=request.execution_case, request_nonce=request.request_nonce,
        execution_plan=request.execution_plan,
    ))
    service, store, effect = _service(
        tmp_path, request, ("captain_tool", "second_tool"),
    )

    evidence = asyncio.run(service.execute(request))

    assert effect.calls == 1
    assert set(store.authorizations) == {"captain_tool", "second_tool"}
    assert tuple(item.tool_key for item in evidence.tool_calls) == (
        "captain_tool", "second_tool",
    )
    assert len(store.registry) == 2
    assert store.command.payload.batch_id == store.batch["batch_id"]


def test_durable_tool_broker_reconciles_crash_after_persist_without_second_effect(
    tmp_path,
) -> None:
    request = _request()
    service, store, _effect = _service(tmp_path, request, ("captain_tool",))
    del service
    store.record_capability_benchmark_work_batch(
        job_id=request.job_id, correlation_id=store.job.correlation_id,
        batch_id="benchmark-batch", case_digest=request.case_ref.case_digest,
        trace_id=uuid4(), run_id=uuid4(), command_id=uuid4(),
        declared_tool_keys=("captain_tool",),
        tool_authority_sha256=__import__("hashlib").sha256(
            b'["captain_tool"]'
        ).hexdigest(),
    )
    artifacts = ContentAddressedArtifactAdapter(tmp_path / "durable-cas")
    issuer = CaptainBenchmarkToolEvidenceAuthority(
        gateway_store=store, artifacts=artifacts,
        scoped_token_key=SecretStr("benchmark-tool-test-key"),
    )
    authorization = issuer.authorize(
        job_id=request.job_id, case_ref=request.case_ref,
        tool_key="captain_tool", operation="read",
        call_id="call-durable", lease_id="lease-durable",
        trace_id=store.batch["trace_id"], run_id=store.batch["run_id"],
        model_policy_digest=request.model_policy_digest,
        batch_id="benchmark-batch", command_id=store.batch["command_id"],
        candidate_ref=request.execution_plan.candidate_ref,
        execution_ref=request.execution_plan.team_manifest_ref,
    )
    registry = _ToolRegistry(("captain_tool",))
    crashed = GatewayDurableBenchmarkToolBroker(
        store=store, artifacts=artifacts, registry=registry,
        after_persist=lambda: (_ for _ in ()).throw(RuntimeError("crash")),
    )
    with pytest.raises(RuntimeError, match="crash"):
        crashed.execute(
            authorization=authorization,
            scoped_token=authorization.scoped_token,
            operation="read", arguments={"account_id": "demo-acme"},
        )

    restarted = GatewayDurableBenchmarkToolBroker(
        store=store, artifacts=artifacts, registry=registry,
    )
    result = restarted.execute(
        authorization=authorization,
        scoped_token=authorization.scoped_token,
        operation="read", arguments={"account_id": "demo-acme"},
    )

    assert len(registry.calls) == 1
    assert result.value["tool_key"] == "captain_tool"
    assert result.receipt_ref == __import__(
        "agenten.agent_runtime.contracts", fromlist=["ArtifactRef"]
    ).ArtifactRef.model_validate(
        store.effects[authorization.call_ref.sha256]["receipt_ref"]
    )


def test_durable_tool_broker_rejects_wrong_scoped_token_before_host_effect(tmp_path) -> None:
    request = _request()
    _service_value, store, _effect = _service(
        tmp_path, request, ("captain_tool",)
    )
    store.record_capability_benchmark_work_batch(
        job_id=request.job_id, correlation_id=store.job.correlation_id,
        batch_id="benchmark-batch", case_digest=request.case_ref.case_digest,
        trace_id=uuid4(), run_id=uuid4(), command_id=uuid4(),
        declared_tool_keys=("captain_tool",),
        tool_authority_sha256=__import__("hashlib").sha256(
            b'["captain_tool"]'
        ).hexdigest(),
    )
    artifacts = ContentAddressedArtifactAdapter(tmp_path / "durable-cas")
    issuer = CaptainBenchmarkToolEvidenceAuthority(
        gateway_store=store, artifacts=artifacts,
        scoped_token_key=SecretStr("benchmark-tool-test-key"),
    )
    authorization = issuer.authorize(
        job_id=request.job_id, case_ref=request.case_ref,
        tool_key="captain_tool", operation="read",
        call_id="call-durable", lease_id="lease-durable",
        trace_id=store.batch["trace_id"], run_id=store.batch["run_id"],
        model_policy_digest=request.model_policy_digest,
        batch_id="benchmark-batch", command_id=store.batch["command_id"],
        candidate_ref=request.execution_plan.candidate_ref,
        execution_ref=request.execution_plan.team_manifest_ref,
    )
    registry = _ToolRegistry(("captain_tool",))
    broker = GatewayDurableBenchmarkToolBroker(
        store=store, artifacts=artifacts, registry=registry,
    )
    with pytest.raises(CapabilityLiveAuthorityUnavailable):
        broker.execute(
            authorization=authorization,
            scoped_token=SecretStr("wrong-scoped-token-value"),
            operation="read", arguments={"account_id": "demo-acme"},
        )
    assert registry.calls == []


def test_prepare_broker_complete_crosses_process_contract_without_gateway_host_effect(
    tmp_path,
) -> None:
    from gateway.capability_generic_benchmark import generic_execution_request_digest

    artifacts = ContentAddressedArtifactAdapter(tmp_path / "process-cas")
    def put(value: bytes, media_type: str = "application/json"):
        return artifacts.put(value, media_type)

    original = _request()
    plan = replace(
        original.execution_plan,
        candidate_ref=put(b"candidate-archive", "application/zip"),
        candidate_manifest_ref=put(b'{"schema":"candidate"}'),
        baseline_policy_ref=put(b'{"schema":"baseline"}'),
        model_ref=put(b'{"schema":"model"}'),
        execution_policy_ref=put(b'{"schema":"policy"}'),
    )
    request = replace(original, execution_plan=plan)
    request = replace(request, request_digest=generic_execution_request_digest(
        job_id=request.job_id, capability_id=request.capability_id,
        input_ref=request.input_ref, case_ref=request.case_ref,
        team_kind=request.team_kind, model_id=request.model_id,
        model_policy_digest=request.model_policy_digest,
        execution_case=request.execution_case,
        execution_plan=request.execution_plan,
        request_nonce=request.request_nonce,
    ))
    service, store, gateway_effect = _service(
        tmp_path, request, ("captain_tool",), artifacts=artifacts,
    )

    prepared = service.prepare(request)
    assert gateway_effect.calls == 0
    assert store.budget_reservation.reservation_id == (
        prepared.command.payload.budget_reservation_id
    )
    assert store.provider_authority["context"].sha256 == (
        prepared.command.payload.provider_context_sha256
    )
    broker = GatewayDurableBenchmarkToolBroker(
        store=store, artifacts=artifacts, registry=service._tool_registry,
    )
    authorization = prepared.authorized_tools[0]
    tool = broker.execute(
        authorization=authorization,
        scoped_token=authorization.scoped_token,
        operation="read", arguments={"account_id": "demo-acme"},
    )
    result_payload = {
        "schema": "captain.capability-benchmark-process-result.v1",
        "observations": {"business_outcome": "bounded"},
        "rationale": "bounded process result",
        "messages": [{"message_key": "answer", "content": "bounded"}],
        "handoffs": [],
        "tool_result_refs": {
            "captain_tool": tool.result_ref.model_dump(mode="json")
        },
        "latency_ms": 2,
        "candidate_ref": plan.candidate_ref.model_dump(mode="json"),
        "execution_ref": plan.team_manifest_ref.model_dump(mode="json"),
        "agent_names": list(plan.team_agent_names),
    }
    with pytest.raises(
        CapabilityLiveAuthorityUnavailable,
        match="provider settlement is unavailable",
    ):
        service.complete_process_execution(request, result_payload)

    command_id = prepared.command.event_id
    settlement = {
        "schema": "captain.capability-benchmark-provider-settlement.v1",
        "settlement_id": str(benchmark_provider_settlement_id(
            command_id, "benchmark-provider-call-1"
        )),
        "provider_call_id": "benchmark-provider-call-1",
        "job_id": str(request.job_id),
        "correlation_id": str(store.job.correlation_id),
        "case_digest": request.case_ref.case_digest,
        "trace_id": str(prepared.trace_id),
        "run_id": str(prepared.run_id),
        "command_id": str(command_id),
        "provider_result_id": str(prepared.command.payload.provider_result_id),
        "provider_session_id": prepared.command.payload.provider_session_id,
        "provider": "openai", "model": request.model_id,
        "input_units": 10, "cached_input_units": 0, "output_units": 5,
        "actual_cost_usd": "0.012345",
        "provider_proxy_url": "http://127.0.0.1:8091/v1",
        "provider_policy_sha256": "1" * 64,
        "provider_price_card_sha256": "2" * 64,
        "provider_context_sha256": prepared.command.payload.provider_context_sha256,
        "provider_proxy_executable_sha256": "4" * 64,
        "provider_proxy_code_sha256": "5" * 64,
        "provider_paid_command_sha256": "6" * 64,
        "provider_hard_ceiling_enforced": True,
    }
    for field in (
        "provider_proxy_url", "provider_policy_sha256",
        "provider_price_card_sha256", "provider_context_sha256",
        "provider_proxy_executable_sha256", "provider_proxy_code_sha256",
        "provider_paid_command_sha256", "provider_hard_ceiling_enforced",
    ):
        changed = dict(settlement)
        changed[field] = False if field == "provider_hard_ceiling_enforced" else (
            "f" * 64 if field != "provider_proxy_url"
            else "http://127.0.0.1:9999/v1"
        )
        store.provider_settlements[:] = [changed]
        with pytest.raises(
            CapabilityLiveAuthorityUnavailable,
            match="settlement authority changed",
        ):
            service.complete_process_execution(request, result_payload)
        assert store.result is None
    forged_identity = dict(settlement)
    forged_identity["settlement_id"] = str(uuid4())
    store.provider_settlements[:] = [forged_identity]
    with pytest.raises(
        CapabilityLiveAuthorityUnavailable,
        match="settlement authority changed",
    ):
        service.complete_process_execution(request, result_payload)
    assert store.result is None
    store.provider_settlements[:] = [settlement]
    evidence = service.complete_process_execution(request, result_payload)

    assert gateway_effect.calls == 0
    assert store.result.status is RuntimeStatus.SUCCEEDED
    assert evidence.tool_calls[0].receipt_ref == tool.receipt_ref
    assert evidence.request_digest == request.request_digest
    assert evidence.cost_usd == Decimal("0.012345")


@pytest.mark.parametrize("tools", [(), ("captain_tool", "extra"), ("wrong",)])
def test_gateway_rejects_runtime_tool_set_drift_without_receipts(tmp_path, tools) -> None:
    request = _request()
    service, store, effect = _service(tmp_path, request, tools)

    with pytest.raises(CapabilityLiveAuthorityUnavailable):
        asyncio.run(service.execute(request))

    assert effect.calls == 1
    assert store.registry == {}


def test_provider_settlement_and_benchmark_finalize_serialize_and_replay(
    tmp_path,
) -> None:
    original = _request()
    artifacts = ContentAddressedArtifactAdapter(tmp_path / "race-cas")
    put = lambda value, media_type="application/json": artifacts.put(
        value, media_type
    )
    plan = replace(
        original.execution_plan,
        candidate_ref=put(b"candidate-archive", "application/zip"),
        candidate_manifest_ref=put(b'{"schema":"candidate"}'),
        baseline_policy_ref=put(b'{"schema":"baseline"}'),
        model_ref=put(b'{"schema":"model"}'),
        execution_policy_ref=put(b'{"schema":"policy"}'),
    )
    from gateway.capability_generic_benchmark import generic_execution_request_digest
    request = replace(original, execution_plan=plan)
    request = replace(request, request_digest=generic_execution_request_digest(
        job_id=request.job_id, capability_id=request.capability_id,
        input_ref=request.input_ref, case_ref=request.case_ref,
        team_kind=request.team_kind, model_id=request.model_id,
        model_policy_digest=request.model_policy_digest,
        execution_case=request.execution_case, execution_plan=plan,
        request_nonce=request.request_nonce,
    ))
    service, memory, _effect = _service(
        tmp_path, request, ("captain_tool",), artifacts=artifacts,
    )
    prepared = service.prepare(request)
    command = prepared.command
    reservation = memory.budget_reservation

    class Cursor:
        def __init__(self, storage):
            self.storage = storage
            self.rows = []

        def execute(self, sql, params=()):
            normalized = " ".join(sql.split())
            if "FROM capability_benchmark_work_batches" in normalized:
                self.rows = [{
                    "job_id": str(request.job_id),
                    "correlation_id": str(memory.job.correlation_id),
                    "case_digest": request.case_ref.case_digest,
                    "trace_id": str(prepared.trace_id),
                    "run_id": str(prepared.run_id),
                    "command_id": str(command.event_id),
                }]
            elif "WHERE settlement_id = %s OR provider_call_id = %s" in normalized:
                self.rows = [item for item in self.storage.settlements if (
                    item["settlement_id"] == str(params[0])
                    or item["provider_call_id"] == str(params[1])
                )]
            elif "WHERE command_id = %s" in normalized and (
                "capability_benchmark_provider_settlements" in normalized
            ):
                self.rows = [item for item in self.storage.settlements if (
                    item["command_id"] == str(params[0])
                )]
            elif normalized.startswith(
                "INSERT INTO capability_benchmark_provider_settlements"
            ):
                self.storage.settlements.append(json.loads(str(params[4])))
                self.rows = []
            else:
                self.rows = []

        def fetchone(self):
            return None if not self.rows else {"payload": json.dumps(self.rows[0]), **self.rows[0]}

        def fetchall(self):
            return tuple({"payload": json.dumps(item)} for item in self.rows)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Connection:
        def __init__(self, storage):
            self.storage = storage

        def cursor(self):
            return Cursor(self.storage)

    class Storage:
        def __init__(self):
            self.lock = RLock()
            self.settlements = []
            self.closed_payload = None

        @contextmanager
        def transaction(self):
            with self.lock:
                yield Connection(self)

    binding = (
        "http://127.0.0.1:8091", "1" * 64, "2" * 64, "ignored-context",
        "4" * 64, "5" * 64, "6" * 64, True,
    )
    storage = Storage()
    store = object.__new__(GatewayStore)
    store.storage = storage
    store._runtime_provider_bundle_bindings = (binding,)
    store._runtime_block_by_json_value = lambda _cursor, *, block_type, **_kw: (
        {"data": command.model_dump(mode="json", by_alias=True)}
        if block_type == "agent_runtime_command"
        else {"data": {"execution_policy": {"allowed_models": [request.model_id]}}}
    )
    store._factory_reservation = lambda _cursor, _reservation_id, **_kw: reservation
    store._reservation_is_closed = lambda _cursor, _reservation_id: storage.closed_payload is not None
    store._factory_budget_event = lambda _cursor, _event_id, **_kw: (
        None if storage.closed_payload is None
        else {"payload": json.dumps(storage.closed_payload)}
    )
    store._factory_budget_job = lambda _cursor, _job_id: (memory.job, {"index": 1})

    def insert_budget_event(_cursor, **kwargs):
        storage.closed_payload = kwargs["payload"]

    store._insert_factory_budget_event = insert_budget_event
    store._factory_budget_projection = lambda *_args, **_kw: SimpleNamespace(
        remaining_usd=Decimal("6.00") - Decimal(
            "0" if storage.closed_payload is None
            else storage.closed_payload["actual_cost_usd"]
        )
    )

    def settlement(call_id: str, cost: str) -> dict[str, object]:
        return {
            "schema": "captain.capability-benchmark-provider-settlement.v1",
            "settlement_id": str(benchmark_provider_settlement_id(
                command.event_id, call_id
            )),
            "provider_call_id": call_id,
            "job_id": str(request.job_id),
            "correlation_id": str(memory.job.correlation_id),
            "case_digest": request.case_ref.case_digest,
            "trace_id": str(prepared.trace_id), "run_id": str(prepared.run_id),
            "command_id": str(command.event_id),
            "provider_result_id": str(command.payload.provider_result_id),
            "provider_session_id": command.payload.provider_session_id,
            "provider": "openai", "model": request.model_id,
            "input_units": 1, "cached_input_units": 0, "output_units": 1,
            "actual_cost_usd": cost,
            "provider_proxy_url": "http://127.0.0.1:8091/v1",
            "provider_policy_sha256": "1" * 64,
            "provider_price_card_sha256": "2" * 64,
            "provider_context_sha256": command.payload.provider_context_sha256,
            "provider_proxy_executable_sha256": "4" * 64,
            "provider_proxy_code_sha256": "5" * 64,
            "provider_paid_command_sha256": "6" * 64,
            "provider_hard_ceiling_enforced": True,
        }

    first, racing = settlement("call-one", "0.10"), settlement("call-two", "0.05")
    assert reservation.requested_usd == Decimal("0.20")
    storage.settlements.append(first)
    barrier = Barrier(2)

    def admit():
        barrier.wait()
        return store.record_capability_benchmark_provider_settlement(racing)

    def finalize():
        barrier.wait()
        return store.finalize_capability_benchmark_provider_cost(command.event_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(future.result() if not future.exception() else future.exception()
                         for future in (pool.submit(admit), pool.submit(finalize)))

    assert sum(isinstance(item, HTTPException) for item in outcomes) in {0, 1}
    assert storage.closed_payload is not None, repr(outcomes)
    actual = Decimal(storage.closed_payload["actual_cost_usd"])
    assert actual in {Decimal("0.10"), Decimal("0.15")}
    assert Decimal("6.00") - actual in {Decimal("5.90"), Decimal("5.85")}
    assert store.finalize_capability_benchmark_provider_cost(command.event_id) == actual
    assert store.record_capability_benchmark_provider_settlement(first) is None
    with pytest.raises(HTTPException):
        store.record_capability_benchmark_provider_settlement(settlement("late", "0.01"))


def test_gateway_rejects_foreign_case_before_runtime_effect(tmp_path) -> None:
    request = _request()
    service, store, effect = _service(tmp_path, request, ("captain_tool",))
    foreign = replace(
        request, case_ref=replace(request.case_ref, case_digest="f" * 64)
    )

    with pytest.raises(Exception):
        asyncio.run(service.execute(foreign))

    assert effect.calls == 0
    assert store.command is None
    assert store.registry == {}


def test_gateway_rejects_execution_plan_drift_before_authorization_or_effect(tmp_path) -> None:
    request = _request()
    service, store, effect = _service(tmp_path, request, ("captain_tool",))
    drifted = replace(
        request,
        execution_plan=replace(
            request.execution_plan, candidate_ref=_ref("foreign-candidate"),
        ),
    )

    with pytest.raises(
        CapabilityLiveAuthorityUnavailable, match="request authority changed"
    ):
        asyncio.run(service.execute(drifted))

    assert effect.calls == 0
    assert store.authorizations == {}
    assert store.registry == {}


def test_gateway_execution_route_requires_own_bearer_and_runs_real_service(
    tmp_path, monkeypatch,
) -> None:
    import gateway.app as gateway_app

    request = _request()
    service, _store, effect = _service(tmp_path, request, ("captain_tool",))
    monkeypatch.setattr(
        gateway_app, "load_gateway_settings", lambda _app: SimpleNamespace()
    )
    app = gateway_app.create_app(
        capability_benchmark_execution_service=service,
        capability_benchmark_gateway_token="benchmark-gateway-token",
    )
    with TestClient(app) as client:
        assert client.post(
            "/v1/capability-benchmark/executions", json=_payload(),
        ).status_code == 401
        response = client.post(
            "/v1/capability-benchmark/executions",
            headers={"Authorization": "Bearer benchmark-gateway-token"},
            json=_payload(),
        )

    assert response.status_code == 200
    assert response.json()["request_digest"] == str(_payload()["request_digest"])
    assert effect.calls == 1
