import os
import inspect
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from blockchain.mariadb_storage import MariaDBStorage
from gateway.app import create_app
from tests.support.mariadb import assert_isolated_test_database


TEST_DSN = os.getenv("TEST_MARIADB_DSN")
if os.getenv("REQUIRE_MARIADB_TESTS") == "1":
    assert_isolated_test_database(TEST_DSN)
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="TEST_MARIADB_DSN is not configured")


class RecordingMirror:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.items: list[dict[str, Any]] = []

    def enqueue_nowait(self, block: dict[str, Any]) -> None:
        if self.fail:
            raise RuntimeError("minibook unavailable")
        self.items.append(block)


@pytest.fixture
def storage() -> MariaDBStorage:
    assert TEST_DSN is not None
    assert_isolated_test_database(TEST_DSN)
    result = MariaDBStorage(TEST_DSN)
    assert_isolated_test_database(TEST_DSN)
    result.clear()
    yield result
    assert_isolated_test_database(TEST_DSN)
    result.clear()


@pytest.fixture
def client(storage: MariaDBStorage) -> TestClient:
    return TestClient(create_app(storage=storage, mirror=RecordingMirror(), approval_enabled=True))


def batch_payload(batch_id: str = "batch-1", title: str = "Build the notification workflow") -> dict[str, Any]:
    return {
        "batch_id": batch_id,
        "title": title,
        "goal": "Deliver a tested workflow",
        "subtask_ids": [f"subtask-{batch_id}"],
        "target": "n8n",
        "capability_tags": ["notifications", "email"],
        "depends_on": [],
        "constraints": [],
        "acceptance_criteria": [
            {"assertion_id": "status-ok", "kind": "status_equals", "expected": "succeeded"}
        ],
        "golden_cases": [],
    }


def create_batch(client: TestClient, batch_id: str = "batch-1", status: str = "pending") -> int:
    response = client.post(
        "/blocks",
        json={"block_type": "work_batch", "status": status, "data": batch_payload(batch_id)},
    )
    assert response.status_code == 201, response.text
    return response.json()["index"]


def claim(client: TestClient, batch_id: str = "batch-1") -> str:
    response = client.post(f"/batches/{batch_id}/claim")
    assert response.status_code == 200, response.text
    return response.json()["claim_token"]


def worker_block(
    client: TestClient,
    token: str | None,
    *,
    block_type: str = "codex_session",
    status: str = "in_progress",
    data: dict[str, Any] | None = None,
) -> Any:
    headers = {"X-Claim-Token": token} if token else {}
    return client.post(
        "/blocks",
        headers=headers,
        json={
            "block_type": block_type,
            "status": status,
            "data": data or {"batch_id": "batch-1", "iteration": 1},
        },
    )


def test_two_workers_cannot_claim_the_same_batch(storage: MariaDBStorage) -> None:
    client = TestClient(create_app(storage=storage, mirror=RecordingMirror()))
    create_batch(client)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.post("/batches/batch-1/claim"), range(2)))

    assert sorted(response.status_code for response in responses) == [200, 409]


def test_duplicate_batch_ids_are_rejected(client: TestClient) -> None:
    create_batch(client)

    response = client.post(
        "/blocks",
        json={"block_type": "work_batch", "data": batch_payload(title="Duplicate")},
    )

    assert response.status_code == 409


def test_work_batch_must_satisfy_the_captain_contract(client: TestClient) -> None:
    response = client.post(
        "/blocks",
        json={"block_type": "work_batch", "data": {"batch_id": "batch-1", "title": "Incomplete"}},
    )

    assert response.status_code == 422


def test_holdout_must_satisfy_the_captain_contract(client: TestClient) -> None:
    parent = create_batch(client)

    response = client.post(
        "/blocks",
        json={
            "block_type": "holdout",
            "parent_index": parent,
            "data": {"batch_id": "batch-1", "cases": [{"case_id": "missing-input"}]},
        },
    )

    assert response.status_code == 422


def test_worker_blocks_require_the_current_fencing_token(client: TestClient) -> None:
    create_batch(client)
    token = claim(client)

    assert worker_block(client, None).status_code == 409
    assert worker_block(client, "stale-token").status_code == 409
    assert worker_block(client, token).status_code == 201


def test_fenced_block_cannot_attach_to_another_batch(client: TestClient) -> None:
    create_batch(client, "batch-1")
    other_parent = create_batch(client, "batch-2")
    token = claim(client, "batch-1")

    response = client.post(
        "/blocks",
        headers={"X-Claim-Token": token},
        json={
            "block_type": "codex_session",
            "parent_index": other_parent,
            "data": {"batch_id": "batch-1", "iteration": 1},
        },
    )

    assert response.status_code == 409


def test_claim_token_is_not_persisted_or_exposed(client: TestClient, storage: MariaDBStorage) -> None:
    create_batch(client)
    token = claim(client)

    persisted = storage.load()[0]
    response = client.get("/batches/batch-1/blocks")

    assert token not in str(persisted)
    assert token not in response.text
    assert "claim_token" not in response.text


def test_holdout_is_hidden_until_a_codex_session_exists(client: TestClient) -> None:
    parent_index = create_batch(client)
    holdout = client.post(
        "/blocks",
        json={
            "block_type": "holdout",
            "parent_index": parent_index,
            "data": {"batch_id": "batch-1", "cases": [{"case_id": "secret-1", "input": {"lead": 1}}]},
        },
    )
    assert holdout.status_code == 201
    token = claim(client)
    headers = {"X-Claim-Token": token}

    assert client.get("/batches/batch-1/holdout", headers=headers).status_code == 404
    assert worker_block(client, token).status_code == 201

    response = client.get("/batches/batch-1/holdout", headers=headers)
    assert response.status_code == 200
    assert response.json()["cases"] == [
        {"case_id": "secret-1", "input": {"lead": 1}, "expected_observations": {}}
    ]


def test_bundle_and_blocks_never_leak_holdout(client: TestClient) -> None:
    parent_index = create_batch(client)
    client.post(
        "/blocks",
        json={
            "block_type": "holdout",
            "parent_index": parent_index,
            "data": {"batch_id": "batch-1", "cases": [{"case_id": "secret-1", "input": {"lead": 1}}]},
        },
    )

    bundle = client.get("/batches/batch-1/bundle")
    blocks = client.get("/batches/batch-1/blocks")

    assert bundle.status_code == 200
    assert "holdout" not in bundle.json()
    assert blocks.status_code == 200
    assert all(block["block_type"] != "holdout" for block in blocks.json())
    assert "secret-1" not in blocks.text


def test_terminal_batch_rejects_further_worker_blocks(client: TestClient) -> None:
    create_batch(client)
    token = claim(client)
    done = worker_block(
        client,
        token,
        block_type="batch_done",
        status="succeeded",
        data={
            "batch_id": "batch-1",
            "outcome": "succeeded",
            "artifact_ref": "workflow-42",
            "capabilities": ["notifications", "email"],
            "eval_score": 9,
        },
    )
    assert done.status_code == 201

    assert worker_block(client, token, block_type="validation_run").status_code == 409


def test_batch_done_status_must_match_its_outcome(client: TestClient) -> None:
    create_batch(client)
    token = claim(client)

    response = worker_block(
        client,
        token,
        block_type="batch_done",
        status="succeeded",
        data={"batch_id": "batch-1", "outcome": "failed"},
    )

    assert response.status_code == 422


def test_mirror_failure_never_fails_a_ledger_write(storage: MariaDBStorage) -> None:
    client = TestClient(create_app(storage=storage, mirror=RecordingMirror(fail=True)))

    response = client.post(
        "/blocks",
        json={"block_type": "work_batch", "data": batch_payload(title="T")},
    )

    assert response.status_code == 201
    assert storage.load()[0]["data"]["batch_id"] == "batch-1"


def test_heartbeat_extends_a_live_claim(client: TestClient) -> None:
    create_batch(client)
    token = claim(client)

    response = client.post(
        "/batches/batch-1/claim/heartbeat",
        headers={"X-Claim-Token": token},
    )

    assert response.status_code == 200
    expiry = datetime.fromisoformat(response.json()["claim_expires_at"])
    assert timedelta(minutes=29) < expiry - datetime.now(timezone.utc) <= timedelta(minutes=30)


def test_expired_claim_is_lazily_reclaimable(client: TestClient, storage: MariaDBStorage) -> None:
    create_batch(client)
    old_token = claim(client)
    with storage.transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT metadata FROM blocks WHERE block_type='work_batch' LIMIT 1")
            metadata = json.loads(cursor.fetchone()["metadata"])
            metadata["claim_expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            cursor.execute(
                "UPDATE blocks SET metadata=%s WHERE block_type='work_batch'",
                (json.dumps(metadata),),
            )

    new_token = claim(client)

    assert new_token != old_token
    assert worker_block(client, old_token).status_code == 409
    assert worker_block(client, new_token).status_code == 201


def test_approve_is_flag_gated_and_transitions_pending_review(storage: MariaDBStorage) -> None:
    disabled = TestClient(create_app(storage=storage, mirror=RecordingMirror(), approval_enabled=False))
    create_batch(disabled, status="pending_review")
    assert disabled.post("/batches/batch-1/approve").status_code == 404

    enabled = TestClient(create_app(storage=storage, mirror=RecordingMirror(), approval_enabled=True))
    assert enabled.post("/batches/batch-1/approve").status_code == 200
    assert enabled.get("/batches", params={"status": "pending"}).json() == [
        {"batch_id": "batch-1", "title": "Build the notification workflow"}
    ]


def test_mock_crm_sink_round_trip(client: TestClient) -> None:
    payload = {"case_id": "case-1", "tag": "qualified", "account": "acme"}

    assert client.post("/sink/crm", json=payload).status_code == 201
    assert client.get("/sink/crm", params={"case_id": "case-1"}).json() == [payload]


def test_successful_batch_is_searchable_as_validated_capability(client: TestClient) -> None:
    create_batch(client)
    token = claim(client)
    assert worker_block(
        client,
        token,
        block_type="batch_done",
        status="succeeded",
        data={
            "batch_id": "batch-1",
            "outcome": "succeeded",
            "artifact_ref": "workflow-42",
            "capabilities": ["notifications", "email"],
            "eval_score": 9,
        },
    ).status_code == 201

    response = client.get("/capabilities", params={"need": "email"})

    assert response.status_code == 200
    assert response.json()[0]["batch_id"] == "batch-1"
    assert response.json()[0]["artifact_ref"] == "workflow-42"


def test_all_write_routes_are_async_and_uvicorn_is_single_worker(storage: MariaDBStorage) -> None:
    app = create_app(storage=storage, mirror=RecordingMirror())
    write_routes = [
        route for route in app.routes
        if getattr(route, "methods", set()) & {"POST", "PUT", "PATCH", "DELETE"}
    ]

    assert write_routes
    assert all(inspect.iscoroutinefunction(route.endpoint) for route in write_routes)
    source = open("gateway/app.py", encoding="utf-8").read()
    assert 'workers=1' in source
