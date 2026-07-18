from __future__ import annotations

import copy
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import SecretStr

from blockchain.mariadb_storage import MariaDBStorage
from gateway.app import create_app
from gateway.auth import GatewayRole, require_actor
from gateway.settings import GatewaySettings
from tests.support.mariadb import assert_isolated_test_database


TEST_DSN = os.getenv("TEST_MARIADB_DSN")
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="TEST_MARIADB_DSN is not configured")
FIXTURES = Path(__file__).parents[1] / "fixtures" / "contracts"


class RecordingMirror:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def enqueue_nowait(self, block: dict[str, Any]) -> None:
        self.items.append(block)


async def captain_actor(_: Request) -> GatewayRole:
    return GatewayRole.CAPTAIN


def application(storage: MariaDBStorage, mirror: RecordingMirror) -> FastAPI:
    assert TEST_DSN is not None
    settings = GatewaySettings(
        ledger_dsn=SecretStr(TEST_DSN),
        captain_gateway_token=SecretStr("captain-test-token"),
        worker_gateway_token=SecretStr("worker-test-token"),
    )
    app = create_app(storage=storage, mirror=mirror, settings=settings)
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


@pytest.fixture
def mirror() -> RecordingMirror:
    return RecordingMirror()


@pytest.fixture
def client(storage: MariaDBStorage, mirror: RecordingMirror) -> TestClient:
    return TestClient(application(storage, mirror))


def load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def release_batch(client: TestClient) -> None:
    response = client.post(
        "/blocks",
        json={
            "block_type": "work_batch",
            "status": "pending",
            "data": {
                "batch_id": "batch-1",
                "title": "Runtime integration",
                "goal": "Execute the released runtime command",
                "subtask_ids": ["subtask-1"],
                "target": "python",
                "capability_tags": ["n8n-builder"],
                "acceptance_criteria": [
                    {
                        "assertion_id": "runtime-result",
                        "kind": "status_equals",
                        "expected": "succeeded",
                    }
                ],
            },
        },
    )
    assert response.status_code == 201, response.text


def test_runtime_command_is_idempotent_and_version_fenced(client: TestClient) -> None:
    release_batch(client)
    command = load("agent_runtime_command.v1.json")

    first = client.post("/v1/runtime/commands", json=command)
    replay = client.post("/v1/runtime/commands", json=command)

    assert first.status_code == replay.status_code == 202
    assert first.json()["operation_id"] == replay.json()["operation_id"]
    assert first.json()["replayed"] is False
    assert replay.json()["replayed"] is True

    stale = copy.deepcopy(command)
    stale["event_id"] = str(uuid4())
    stale["subject_version"] -= 1
    assert client.post("/v1/runtime/commands", json=stale).status_code == 409


def test_grant_and_result_require_the_exact_command(client: TestClient) -> None:
    release_batch(client)
    command = load("agent_runtime_command.v1.json")
    issued_at = datetime.now(timezone.utc)
    expires_at = issued_at + timedelta(minutes=15)
    grant = {
        "schema": "captain.capability-grant.v1",
        "grant_id": "grant-gateway-1",
        "command_id": command["event_id"],
        "batch_id": "batch-1",
        "batch_version": 3,
        "subtask_id": "subtask-1",
        "workspace_ref": "workspace://authorized/project-1/subtask-1",
        "profile": "n8n-builder",
        "capabilities": [
            "codex.cancel",
            "codex.heartbeat",
            "codex.resume",
            "codex.run",
            "codex.status",
            "mcp.n8n",
            "tests.run",
            "workspace.write",
        ],
        "mcp_servers": ["n8n-mcp"],
        "issued_at": issued_at.isoformat().replace("+00:00", "Z"),
        "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
    }
    result = load("agent_runtime_result.v1.json")
    result["grant_id"] = grant["grant_id"]

    assert client.post("/v1/runtime/grants", json=grant).status_code == 409
    assert client.post("/v1/runtime/results", json=result).status_code == 409

    assert client.post("/v1/runtime/commands", json=command).status_code == 202
    assert client.post("/v1/runtime/grants", json=grant).status_code == 201
    assert client.post("/v1/runtime/results", json=result).status_code == 201

    operation = client.get(f"/v1/runtime/operations/{command['event_id']}")
    assert operation.status_code == 200
    assert operation.json()["command"] == command
    assert operation.json()["grant"] == grant
    assert operation.json()["result"] == result


def test_result_mismatch_and_conflicting_replay_are_rejected(client: TestClient) -> None:
    release_batch(client)
    command = load("agent_runtime_command.v1.json")
    assert client.post("/v1/runtime/commands", json=command).status_code == 202

    result = load("agent_runtime_result.v1.json")
    result["command_id"] = command["event_id"]
    result["correlation_id"] = str(uuid4())
    assert client.post("/v1/runtime/results", json=result).status_code == 409

    conflict = copy.deepcopy(command)
    conflict["payload"]["limits"]["max_iterations"] = 4
    assert client.post("/v1/runtime/commands", json=conflict).status_code == 409


def test_duplicate_concurrent_submission_and_restart_retrieval(
    storage: MariaDBStorage,
    mirror: RecordingMirror,
) -> None:
    app = application(storage, mirror)
    with TestClient(app) as client:
        release_batch(client)
        command = load("agent_runtime_command.v1.json")

        def submit() -> tuple[int, str]:
            response = client.post("/v1/runtime/commands", json=command)
            return response.status_code, response.json()["operation_id"]

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = tuple(pool.map(lambda _: submit(), range(2)))

    assert {status for status, _ in responses} == {202}
    assert len({operation_id for _, operation_id in responses}) == 1

    with TestClient(application(storage, mirror)) as restarted:
        recovered = restarted.get(f"/v1/runtime/operations/{command['event_id']}")
    assert recovered.status_code == 200
    assert recovered.json()["command"] == command


def test_mirror_receives_only_redacted_projection_after_commit(
    client: TestClient,
    mirror: RecordingMirror,
) -> None:
    release_batch(client)
    command = load("agent_runtime_command.v1.json")

    assert client.post("/v1/runtime/commands", json=command).status_code == 202

    projection = mirror.items[-1]
    rendered = json.dumps(projection, sort_keys=True).lower()
    assert projection == {
        "event_type": "runtime_command_accepted",
        "event_id": command["event_id"],
        "correlation_id": command["correlation_id"],
        "project_id": "project-1",
        "batch_id": "batch-1",
        "subtask_id": "subtask-1",
        "subject_version": 3,
        "operation": "codex.run",
        "status": "accepted",
    }
    assert "prompt_ref" not in rendered
    assert "workspace_ref" not in rendered
    assert "token" not in rendered
