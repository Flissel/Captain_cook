from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from gateway.factory_process_rpc import (
    FactoryGatewayRpcCommand,
    FactoryGatewayRpcService,
    create_factory_gateway_rpc_router,
)


class _DurableAuthority:
    def __init__(self) -> None:
        self.effects = 0
        self.revision = 3
        self.receipts: dict[str, dict[str, object]] = {}

    def read_receipt(self, idempotency_key: str):
        return self.receipts.get(idempotency_key)

    def execute(self, command: FactoryGatewayRpcCommand) -> dict[str, object]:
        if command.expected_revision != self.revision:
            raise ValueError("revision")
        self.effects += 1
        self.revision += 1
        response = {
            "schema": "captain.factory-gateway-rpc-result.v1",
            "request_id": str(command.request_id),
            "job_id": str(command.job_id),
            "revision": self.revision,
            "result": {"accepted": True},
        }
        self.receipts[command.idempotency_key] = response
        return response


def _command(**updates: object) -> dict[str, object]:
    job_id = uuid4()
    value: dict[str, object] = {
        "schema": "captain.factory-gateway-rpc-command.v1",
        "operation": "record_factory_block",
        "request_id": str(uuid4()),
        "idempotency_key": "f" * 64,
        "job_id": str(job_id),
        "correlation_id": str(uuid4()),
        "input_id": "sales-assistant",
        "attempt": 1,
        "expected_revision": 3,
        "payload": {"job_id": str(job_id)},
    }
    value.update(updates)
    return value


def test_factory_gateway_rpc_is_role_authenticated_and_job_bound() -> None:
    authority = _DurableAuthority()
    app = create_factory_gateway_rpc_router(
        service=FactoryGatewayRpcService(authority=authority),
        factory_runtime_token="factory-runtime-token-0001",
    )
    client = TestClient(app)

    assert client.post("/v1/factory-runtime/store", json=_command()).status_code == 401
    foreign = _command(payload={"job_id": str(uuid4())})
    response = client.post(
        "/v1/factory-runtime/store",
        json=foreign,
        headers={"Authorization": "Bearer factory-runtime-token-0001"},
    )
    assert response.status_code == 409
    assert authority.effects == 0


def test_factory_gateway_rpc_restart_replays_durable_receipt_once() -> None:
    authority = _DurableAuthority()
    command = _command()
    headers = {"Authorization": "Bearer factory-runtime-token-0001"}
    first = TestClient(create_factory_gateway_rpc_router(
        service=FactoryGatewayRpcService(authority=authority),
        factory_runtime_token="factory-runtime-token-0001",
    )).post("/v1/factory-runtime/store", json=command, headers=headers)
    restarted = TestClient(create_factory_gateway_rpc_router(
        service=FactoryGatewayRpcService(authority=authority),
        factory_runtime_token="factory-runtime-token-0001",
    )).post("/v1/factory-runtime/store", json=command, headers=headers)

    assert first.status_code == restarted.status_code == 200
    assert first.content == restarted.content
    assert authority.effects == 1


def test_factory_gateway_rpc_rejects_cross_job_replay_and_stale_revision() -> None:
    authority = _DurableAuthority()
    command = _command()
    headers = {"Authorization": "Bearer factory-runtime-token-0001"}
    client = TestClient(create_factory_gateway_rpc_router(
        service=FactoryGatewayRpcService(authority=authority),
        factory_runtime_token="factory-runtime-token-0001",
    ))
    assert client.post(
        "/v1/factory-runtime/store", json=command, headers=headers
    ).status_code == 200

    cross_job = _command(
        idempotency_key=command["idempotency_key"],
        expected_revision=4,
    )
    assert client.post(
        "/v1/factory-runtime/store", json=cross_job, headers=headers
    ).status_code == 409
    stale = _command(expected_revision=3, idempotency_key="e" * 64)
    assert client.post(
        "/v1/factory-runtime/store", json=stale, headers=headers
    ).status_code == 409
    assert authority.effects == 1


def test_factory_gateway_rpc_rejects_oversized_payload_before_authority() -> None:
    authority = _DurableAuthority()
    client = TestClient(create_factory_gateway_rpc_router(
        service=FactoryGatewayRpcService(authority=authority),
        factory_runtime_token="factory-runtime-token-0001",
    ))
    response = client.post(
        "/v1/factory-runtime/store",
        json=_command(payload={"job_id": str(uuid4()), "body": "x" * 1_048_576}),
        headers={"Authorization": "Bearer factory-runtime-token-0001"},
    )
    assert response.status_code == 422
    assert authority.effects == 0
