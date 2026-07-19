from __future__ import annotations

import os
from datetime import datetime, timezone
from uuid import UUID

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import SecretStr

from agenten.agent_factory.contracts import FactoryPhase
from blockchain.mariadb_storage import MariaDBStorage
from gateway.app import create_app
from gateway.auth import GatewayRole, require_actor
from gateway.settings import GatewaySettings
from tests.agent_factory.test_state_machine import block, job
from tests.support.mariadb import assert_isolated_test_database


TEST_DSN = os.getenv("TEST_MARIADB_DSN")
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="TEST_MARIADB_DSN is not configured")


class Mirror:
    def enqueue_nowait(self, _: dict[str, object]) -> None:
        return None


async def captain_actor(_: Request) -> GatewayRole:
    return GatewayRole.CAPTAIN


def application(storage: MariaDBStorage) -> FastAPI:
    assert TEST_DSN is not None
    app = create_app(
        storage=storage,
        mirror=Mirror(),
        settings=GatewaySettings(
            ledger_dsn=SecretStr(TEST_DSN),
            captain_gateway_token=SecretStr("captain-test-token"),
            worker_gateway_token=SecretStr("worker-test-token"),
        ),
    )
    app.dependency_overrides[require_actor] = captain_actor
    return app


@pytest.fixture
def storage() -> MariaDBStorage:
    assert TEST_DSN is not None
    assert_isolated_test_database(TEST_DSN)
    value = MariaDBStorage(TEST_DSN)
    value.clear()
    yield value
    value.clear()


def test_factory_job_and_block_are_idempotent_and_restart_safe(storage: MariaDBStorage) -> None:
    factory_job = job()
    forge = block(FactoryPhase.FORGE_REQUESTED)
    with TestClient(application(storage)) as client:
        first = client.post("/v1/factory/jobs", json=factory_job.model_dump(mode="json", by_alias=True))
        replay = client.post("/v1/factory/jobs", json=factory_job.model_dump(mode="json", by_alias=True))
        assert first.status_code == replay.status_code == 202
        assert first.json()["replayed"] is False
        assert replay.json()["replayed"] is True
        assert client.post("/v1/factory/blocks", json=forge.model_dump(mode="json", by_alias=True)).status_code == 201

    with TestClient(application(storage)) as restarted:
        recovered = restarted.get(f"/v1/factory/jobs/{factory_job.job_id}")

    assert recovered.status_code == 200
    assert recovered.json()["projection"]["phase"] == "forge_requested"
    assert [item["phase"] for item in recovered.json()["blocks"]] == ["forge_requested"]


def test_factory_gateway_rejects_invalid_phase_before_ledger_write(storage: MariaDBStorage) -> None:
    factory_job = job()
    invalid = block(FactoryPhase.BUILD_PASSED)
    with TestClient(application(storage)) as client:
        assert client.post("/v1/factory/jobs", json=factory_job.model_dump(mode="json", by_alias=True)).status_code == 202
        response = client.post("/v1/factory/blocks", json=invalid.model_dump(mode="json", by_alias=True))
        recovered = client.get(f"/v1/factory/jobs/{factory_job.job_id}")

    assert response.status_code == 409
    assert "illegal phase" in response.json()["detail"]
    assert recovered.json()["blocks"] == []


def test_factory_gateway_rejects_conflicting_event_replay(storage: MariaDBStorage) -> None:
    factory_job = job()
    forge = block(FactoryPhase.FORGE_REQUESTED)
    conflict = forge.model_copy(update={"occurred_at": datetime(2026, 7, 19, 11, tzinfo=timezone.utc)})
    with TestClient(application(storage)) as client:
        assert client.post("/v1/factory/jobs", json=factory_job.model_dump(mode="json", by_alias=True)).status_code == 202
        assert client.post("/v1/factory/blocks", json=forge.model_dump(mode="json", by_alias=True)).status_code == 201
        response = client.post("/v1/factory/blocks", json=conflict.model_dump(mode="json", by_alias=True))

    assert response.status_code == 409
    assert "different content" in response.json()["detail"]
