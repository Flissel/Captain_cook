from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from agenten.agent_factory.production_dispatch_runner import ProductionFactoryDispatchResult
from agenten.agent_factory.state_machine import (
    FactoryAction, FactoryActionKind, FactoryLifecycleStatus,
)
from gateway.capability_factory_process_app import (
    FactoryProcessExecutor, create_capability_factory_process_app,
)
from gateway.factory_gateway_store_client import GatewayFactoryStoreRpcClient
from gateway.factory_process_rpc import (
    FactoryGatewayRpcCommand, FactoryGatewayRpcService,
    create_factory_gateway_rpc_router,
)


class _DurableGatewayAuthority:
    def __init__(self) -> None:
        self.effects = 0
        self.receipts: dict[str, dict[str, object]] = {}

    def read_receipt(self, key: str):
        return self.receipts.get(key)

    def execute(self, command: FactoryGatewayRpcCommand):
        self.effects += 1
        response = {
            "schema": "captain.factory-gateway-rpc-result.v1",
            "request_id": str(command.request_id), "job_id": str(command.job_id),
            "revision": command.expected_revision + 1,
            "result": {"value": {"persisted": True}},
        }
        self.receipts[command.idempotency_key] = response
        return response


class _Composition:
    def __init__(self, store: GatewayFactoryStoreRpcClient) -> None:
        self._store = store

    async def run(self, job_id, **_kwargs):
        self._store._call("record_factory_block", {"job_id": str(job_id)})
        return ProductionFactoryDispatchResult(
            job_id=job_id, status="stop_point_reached",
            lifecycle_status=FactoryLifecycleStatus.RUNNING,
            next_action=FactoryAction(
                kind=FactoryActionKind.DISPATCH_TOOL_INTEGRATOR, attempt=1,
            ),
        )


def test_factory_process_restart_replays_gateway_receipt_without_second_effect() -> None:
    authority = _DurableGatewayAuthority()
    gateway_token = "factory-runtime-token-0001"
    gateway_app = create_factory_gateway_rpc_router(
        service=FactoryGatewayRpcService(authority=authority),
        factory_runtime_token=gateway_token,
    )

    def build_process_client() -> TestClient:
        gateway_http = TestClient(gateway_app)
        remote = GatewayFactoryStoreRpcClient(
            gateway_url="http://127.0.0.1:8090",
            factory_runtime_token=gateway_token,
            send=lambda payload: gateway_http.post(
                "/v1/factory-runtime/store", json=payload,
                headers={"Authorization": "Bearer " + gateway_token},
            ).json(),
        )
        return TestClient(create_capability_factory_process_app(
            executor=FactoryProcessExecutor(
                composition=_Composition(remote), gateway_store=remote,
            ),
            factory_runtime_token=gateway_token,
        ))

    job_id, correlation_id, request_id = uuid4(), uuid4(), uuid4()
    payload = {
        "schema": "captain.factory-process-execution.v1",
        "job_id": str(job_id), "correlation_id": str(correlation_id),
        "input_id": "incident-command", "attempt": 1, "operation": "run",
        "request_id": str(request_id), "idempotency_key": "a" * 64,
        "expected_revision": 3, "maximum_dispatches": 1,
        "stop_before_action": "submit_forge_job",
    }
    headers = {"Authorization": "Bearer " + gateway_token}
    first = build_process_client().post("/v1/execute", json=payload, headers=headers)
    restarted = build_process_client().post("/v1/execute", json=payload, headers=headers)

    assert first.status_code == restarted.status_code == 200
    assert first.content == restarted.content
    assert authority.effects == 1


def test_factory_process_requires_its_own_role_credential() -> None:
    authority = _DurableGatewayAuthority()
    remote = GatewayFactoryStoreRpcClient(
        gateway_url="http://127.0.0.1:8090",
        factory_runtime_token="factory-runtime-token-0001",
        send=lambda _payload: (_ for _ in ()).throw(AssertionError("Gateway called")),
    )
    client = TestClient(create_capability_factory_process_app(
        executor=FactoryProcessExecutor(
            composition=_Composition(remote), gateway_store=remote,
        ), factory_runtime_token="factory-runtime-token-0001",
    ))
    assert client.get("/healthz").status_code == 401
    assert client.get(
        "/healthz", headers={"Authorization": "Bearer factory-runtime-token-0001"}
    ).json() == {"status": "ready", "authority": "factory-runtime"}
