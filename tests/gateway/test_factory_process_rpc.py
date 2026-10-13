from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4
from types import SimpleNamespace

import pytest

from fastapi.testclient import TestClient

from gateway.factory_process_rpc import (
    FactoryGatewayRpcCommand,
    FactoryGatewayRpcService,
    create_factory_gateway_rpc_router,
    issue_factory_gateway_capability,
)
from gateway.factory_gateway_store_authority import GatewayStoreFactoryRpcAuthority


CAPABILITY_KEY = "gateway-only-capability-key-0001"


class _DurableAuthority:
    def __init__(self) -> None:
        self.effects = 0
        self.revision = 3
        self.receipts: dict[str, dict[str, object]] = {}

    def read_receipt(self, command: FactoryGatewayRpcCommand):
        stored = self.receipts.get(command.idempotency_key)
        if stored is None:
            return None
        if stored["request"] != command.model_dump(mode="json", by_alias=True):
            raise ValueError("replay changed")
        return stored["response"]

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
        self.receipts[command.idempotency_key] = {
            "request": command.model_dump(mode="json", by_alias=True),
            "response": response,
        }
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
    value.setdefault("gateway_capability", issue_factory_gateway_capability(
        capability_key=CAPABILITY_KEY,
        job_id=value["job_id"],
        correlation_id=value["correlation_id"],
        input_id=str(value["input_id"]),
        attempt=int(value["attempt"]),
        allowed_operations=(str(value["operation"]),),
    ))
    return value


NOW = datetime(2026, 8, 17, 12, tzinfo=timezone.utc)


def test_factory_gateway_rpc_rejects_expired_capability_before_authority() -> None:
    authority = _DurableAuthority()
    command = _command()
    command["gateway_capability"] = issue_factory_gateway_capability(
        capability_key=CAPABILITY_KEY,
        job_id=command["job_id"],
        correlation_id=command["correlation_id"],
        input_id=str(command["input_id"]),
        attempt=int(command["attempt"]),
        allowed_operations=(str(command["operation"]),),
        issued_at=NOW,
        not_after=NOW + timedelta(minutes=5),
    )
    client = TestClient(create_factory_gateway_rpc_router(
        service=FactoryGatewayRpcService(
            authority=authority,
            capability_key=CAPABILITY_KEY,
            clock=lambda: NOW + timedelta(minutes=5),
        ),
        factory_runtime_token="factory-runtime-token-0001",
    ))

    response = client.post(
        "/v1/factory-runtime/store", json=command,
        headers={"Authorization": "Bearer factory-runtime-token-0001"},
    )

    assert response.status_code == 409
    assert authority.effects == 0


@pytest.mark.parametrize(
    ("operation", "nested"),
    (
        ("record_factory_block", {"block": {"job_id": str(uuid4())}}),
        ("record_factory_lease", {"lease": {"attempt": 2}}),
        (
            "record_factory_workflow_artifact",
            {"artifact": {"attempt": 2}},
        ),
        ("forge_submit", {"dispatch": {"action": {"attempt": 2}}}),
        (
            "forge_submit",
            {"dispatch": {"action": {"job_id": str(uuid4())}}},
        ),
    ),
)
def test_factory_gateway_rpc_rejects_nested_scope_drift_before_authority(
    operation: str, nested: dict[str, object],
) -> None:
    authority = _DurableAuthority()
    command = _command(operation=operation)
    command["payload"] = {"job_id": command["job_id"], **nested}
    command["gateway_capability"] = issue_factory_gateway_capability(
        capability_key=CAPABILITY_KEY,
        job_id=command["job_id"], correlation_id=command["correlation_id"],
        input_id=str(command["input_id"]), attempt=1,
        allowed_operations=(operation,), issued_at=NOW,
        not_after=NOW + timedelta(minutes=5),
    )
    client = TestClient(create_factory_gateway_rpc_router(
        service=FactoryGatewayRpcService(
            authority=authority, capability_key=CAPABILITY_KEY, clock=lambda: NOW,
        ),
        factory_runtime_token="factory-runtime-token-0001",
    ))

    response = client.post(
        "/v1/factory-runtime/store", json=command,
        headers={"Authorization": "Bearer factory-runtime-token-0001"},
    )

    assert response.status_code == 409
    assert authority.effects == 0


def test_factory_gateway_rpc_stale_attempt_grant_cannot_mutate_current_attempt() -> None:
    job_id, correlation_id = uuid4(), uuid4()

    class Store:
        effects = 0

        def factory_job(self, _job_id):
            return SimpleNamespace(
                job=SimpleNamespace(
                    job_id=job_id, correlation_id=correlation_id,
                    required_capability="sales-assistant",
                ),
                projection=SimpleNamespace(attempt=2), blocks=(), leases=(),
            )

        def factory_workflow_artifacts(self, _job_id):
            return ()

        def factory_runtime_rpc_receipt(self, *_args, **_kwargs):
            return None

        def record_factory_runtime_rpc_receipt(self, **_kwargs):
            self.effects += 1
            raise AssertionError("receipt must not be written")

    command = _command(
        operation="factory_job", job_id=str(job_id),
        correlation_id=str(correlation_id), input_id="sales-assistant",
        attempt=1, expected_revision=1, payload={"job_id": str(job_id)},
    )
    command["gateway_capability"] = issue_factory_gateway_capability(
        capability_key=CAPABILITY_KEY, job_id=job_id,
        correlation_id=correlation_id, input_id="sales-assistant", attempt=1,
        allowed_operations=("factory_job",), issued_at=NOW,
        not_after=NOW + timedelta(minutes=5),
    )
    store = Store()
    client = TestClient(create_factory_gateway_rpc_router(
        service=FactoryGatewayRpcService(
            authority=GatewayStoreFactoryRpcAuthority(store=store),
            capability_key=CAPABILITY_KEY, clock=lambda: NOW,
        ),
        factory_runtime_token="factory-runtime-token-0001",
    ))

    response = client.post(
        "/v1/factory-runtime/store", json=command,
        headers={"Authorization": "Bearer factory-runtime-token-0001"},
    )

    assert response.status_code == 409
    assert store.effects == 0


def test_factory_gateway_rpc_is_role_authenticated_and_job_bound() -> None:
    authority = _DurableAuthority()
    app = create_factory_gateway_rpc_router(
        service=FactoryGatewayRpcService(authority=authority, capability_key=CAPABILITY_KEY),
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
        service=FactoryGatewayRpcService(authority=authority, capability_key=CAPABILITY_KEY),
        factory_runtime_token="factory-runtime-token-0001",
    )).post("/v1/factory-runtime/store", json=command, headers=headers)
    restarted = TestClient(create_factory_gateway_rpc_router(
        service=FactoryGatewayRpcService(authority=authority, capability_key=CAPABILITY_KEY),
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
        service=FactoryGatewayRpcService(authority=authority, capability_key=CAPABILITY_KEY),
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


def test_factory_gateway_rpc_rejects_missing_or_forged_scoped_capability() -> None:
    authority = _DurableAuthority()
    client = TestClient(create_factory_gateway_rpc_router(
        service=FactoryGatewayRpcService(
            authority=authority, capability_key=CAPABILITY_KEY,
        ),
        factory_runtime_token="factory-runtime-token-0001",
    ))
    headers = {"Authorization": "Bearer factory-runtime-token-0001"}
    missing = _command()
    missing.pop("gateway_capability")
    forged = _command(gateway_capability="x" * 64)

    assert client.post(
        "/v1/factory-runtime/store", json=missing, headers=headers,
    ).status_code == 422
    assert client.post(
        "/v1/factory-runtime/store", json=forged, headers=headers,
    ).status_code == 409
    assert authority.effects == 0


def test_factory_gateway_rpc_replay_compares_every_command_field() -> None:
    authority = _DurableAuthority()
    client = TestClient(create_factory_gateway_rpc_router(
        service=FactoryGatewayRpcService(
            authority=authority, capability_key=CAPABILITY_KEY,
        ),
        factory_runtime_token="factory-runtime-token-0001",
    ))
    headers = {"Authorization": "Bearer factory-runtime-token-0001"}
    command = _command()
    assert client.post(
        "/v1/factory-runtime/store", json=command, headers=headers,
    ).status_code == 200

    for field, changed in (
        ("payload", {"job_id": command["job_id"], "changed": True}),
        ("expected_revision", 99),
        ("operation", "factory_job"),
        ("input_id", "foreign-input"),
    ):
        replay = dict(command)
        replay[field] = changed
        response = client.post(
            "/v1/factory-runtime/store", json=replay, headers=headers,
        )
        assert response.status_code == 409, field
    assert authority.effects == 1


def test_factory_gateway_rpc_rejects_oversized_payload_before_authority() -> None:
    authority = _DurableAuthority()
    client = TestClient(create_factory_gateway_rpc_router(
        service=FactoryGatewayRpcService(authority=authority, capability_key=CAPABILITY_KEY),
        factory_runtime_token="factory-runtime-token-0001",
    ))
    response = client.post(
        "/v1/factory-runtime/store",
        json=_command(payload={"job_id": str(uuid4()), "body": "x" * 1_048_576}),
        headers={"Authorization": "Bearer factory-runtime-token-0001"},
    )
    assert response.status_code == 422
    assert authority.effects == 0


@pytest.mark.parametrize(
    ("operation", "payload"),
    (
        ("runtime_operation", {"command_id": str(uuid4())}),
        (
            "business_benchmark_summary_by_artifact",
            {"artifact_ref": {
                "uri": "artifact://foreign/value",
                "sha256": "1" * 64,
                "media_type": "application/json",
            }},
        ),
        ("bundle", {"batch_id": "foreign-batch"}),
        ("forge_status", {"creation_job_id": str(uuid4())}),
        ("forge_result", {"creation_job_id": str(uuid4())}),
    ),
)
def test_factory_gateway_authority_rejects_foreign_resource_ids_before_effect(
    operation: str, payload: dict[str, object],
) -> None:
    job_id, correlation_id = uuid4(), uuid4()

    class Store:
        external_effects = 0

        def factory_job(self, requested_job_id):
            assert requested_job_id == job_id
            return SimpleNamespace(
                    job=SimpleNamespace(
                        job_id=job_id,
                        correlation_id=correlation_id,
                        required_capability="sales-assistant",
                    ),
                    projection=SimpleNamespace(attempt=1),
                    blocks=(),
                leases=(),
            )

        def factory_workflow_artifacts(self, requested_job_id):
            assert requested_job_id == job_id
            return ()

        def runtime_operation(self, _command_id):
            return SimpleNamespace(
                command=SimpleNamespace(
                    correlation_id=uuid4(), subject_id="foreign-capability",
                ),
                result=None,
            )

        def business_benchmark_summary_by_artifact(self, _ref):
            self.external_effects += 1
            return object()

        def capability_n8n_work_batch(self, requested_job_id):
            assert requested_job_id == job_id
            return "bound-batch"

        def bundle(self, _batch_id):
            self.external_effects += 1
            return {}

    class Forge:
        effects = 0

        async def status(self, _creation_job_id):
            self.effects += 1
            return object()

        async def result(self, _creation_job_id):
            self.effects += 1
            return object()

    store, forge = Store(), Forge()
    command = FactoryGatewayRpcCommand(
        schema="captain.factory-gateway-rpc-command.v1",
        operation=operation,
        request_id=uuid4(),
        idempotency_key="a" * 64,
        job_id=job_id,
        correlation_id=correlation_id,
        input_id="sales-assistant",
        attempt=1,
        expected_revision=1,
        gateway_capability="x" * 64,
        payload=payload,
    )

    with pytest.raises(ValueError, match="authority binding"):
        GatewayStoreFactoryRpcAuthority(
            store=store, forge=forge,
        ).execute(command)

    assert store.external_effects == 0
    assert forge.effects == 0
