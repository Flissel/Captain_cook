from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from pymysql.cursors import DictCursor

from blockchain.mariadb_storage import MariaDBStorage
from gateway.app import create_app
from gateway.settings import GatewayConfigurationError, GatewaySettings
from tests.support.mariadb import assert_isolated_test_database


TEST_DSN = os.getenv("TEST_MARIADB_DSN")
if os.getenv("REQUIRE_MARIADB_TESTS") == "1":
    assert_isolated_test_database(TEST_DSN)
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="TEST_MARIADB_DSN is not configured")

CAPTAIN_TOKEN = "captain-test-token"
WORKER_TOKEN = "worker-test-token"


class RecordingMirror:
    def enqueue_nowait(self, block: dict[str, Any]) -> None:
        del block


@pytest.fixture
def storage() -> Iterator[MariaDBStorage]:
    assert TEST_DSN is not None
    assert_isolated_test_database(TEST_DSN)
    result = MariaDBStorage(TEST_DSN)
    result.clear()
    yield result
    assert_isolated_test_database(TEST_DSN)
    result.clear()


@pytest.fixture
def settings() -> GatewaySettings:
    assert TEST_DSN is not None
    return GatewaySettings(
        ledger_dsn=SecretStr(TEST_DSN),
        captain_gateway_token=SecretStr(CAPTAIN_TOKEN),
        worker_gateway_token=SecretStr(WORKER_TOKEN),
        approval_enabled=True,
    )


@pytest.fixture
def client(storage: MariaDBStorage, settings: GatewaySettings) -> TestClient:
    return TestClient(
        create_app(storage=storage, mirror=RecordingMirror(), settings=settings)
    )


def authorization(token: str, *, claim_token: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if claim_token is not None:
        headers["X-Claim-Token"] = claim_token
    return headers


def test_legacy_delivery_import_is_captain_only(client: TestClient) -> None:
    request = {
        "legacy_record_id": "todo:legacy-1",
        "batch_id": "legacy-1",
        "record_type": "todo",
        "data": {"batch_id": "legacy-1", "title": "Archived delivery item"},
    }

    assert client.post("/imports/legacy-delivery", json=request).status_code == 401
    assert client.post(
        "/imports/legacy-delivery",
        json=request,
        headers=authorization(WORKER_TOKEN),
    ).status_code == 403
    imported = client.post(
        "/imports/legacy-delivery",
        json=request,
        headers=authorization(CAPTAIN_TOKEN),
    )
    assert imported.status_code == 201
    assert imported.json()["created"] is True


def batch_payload() -> dict[str, Any]:
    return {
        "batch_id": "batch-1",
        "title": "Build the notification workflow",
        "goal": "Deliver a tested workflow",
        "subtask_ids": ["subtask-1"],
        "target": "n8n",
        "capability_tags": ["notifications", "email"],
        "depends_on": [],
        "constraints": [],
        "acceptance_criteria": [
            {"assertion_id": "status-ok", "kind": "status_equals", "expected": "succeeded"}
        ],
        "golden_cases": [],
    }


def test_healthz_is_the_only_public_route_and_executes_database_readiness(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements: list[str] = []
    original_execute = DictCursor.execute

    def recording_execute(self: DictCursor, query: str, args: Any = None) -> int:
        statements.append(" ".join(query.lower().split()))
        return original_execute(self, query, args)

    monkeypatch.setattr(DictCursor, "execute", recording_execute)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ready"}
    assert statements == ["select 1"]
    assert CAPTAIN_TOKEN not in response.text
    assert WORKER_TOKEN not in response.text
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/batches", params={"status": "pending"}).status_code == 401


def test_unauthenticated_slash_variant_does_not_redirect(client: TestClient) -> None:
    canonical = client.get(
        "/batches",
        params={"status": "pending"},
        follow_redirects=False,
    )
    slash_variant = client.get(
        "/batches/",
        params={"status": "pending"},
        follow_redirects=False,
    )

    assert canonical.status_code == 401
    assert not 300 <= slash_variant.status_code < 400
    assert "location" not in slash_variant.headers


def test_healthz_returns_generic_503_when_database_is_unavailable(
    client: TestClient,
    storage: MariaDBStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @contextmanager
    def unavailable_transaction() -> Iterator[None]:
        raise RuntimeError("mariadb://user:database-secret@protected-host/database")
        yield

    monkeypatch.setattr(storage, "transaction", unavailable_transaction)

    response = client.get("/healthz")

    assert response.status_code == 503
    assert response.json() == {"detail": "gateway unavailable"}
    assert "database-secret" not in response.text
    assert "protected-host" not in response.text


def test_missing_invalid_and_wrong_role_bearers_fail_closed(client: TestClient) -> None:
    request = {"block_type": "work_batch", "data": batch_payload()}

    missing = client.post("/blocks", json=request)
    invalid = client.post(
        "/blocks",
        json=request,
        headers=authorization("invalid-secret-token"),
    )
    wrong_scheme = client.post(
        "/blocks",
        json=request,
        headers={"Authorization": f"Basic {CAPTAIN_TOKEN}"},
    )
    wrong_role = client.post(
        "/blocks",
        json=request,
        headers=authorization(WORKER_TOKEN),
    )

    assert [missing.status_code, invalid.status_code, wrong_scheme.status_code] == [401, 401, 401]
    assert wrong_role.status_code == 403
    assert invalid.headers["www-authenticate"] == "Bearer"


def test_gateway_owned_block_requires_auth_before_store_validation(client: TestClient) -> None:
    request = {
        "block_type": "batch_claimed",
        "status": "recorded",
        "data": {
            "batch_id": "batch-1",
            "claim_token_sha256": "0" * 64,
            "claim_expires_at": "2026-07-17T00:00:00Z",
        },
    }

    assert client.post("/blocks", json=request).status_code == 401
    authenticated = client.post(
        "/blocks",
        json=request,
        headers=authorization(WORKER_TOKEN),
    )
    assert authenticated.status_code == 422


def test_authenticated_malformed_json_remains_a_validation_error(client: TestClient) -> None:
    response = client.post(
        "/blocks",
        content="{",
        headers={
            **authorization(CAPTAIN_TOKEN),
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 422


def test_captain_and_worker_route_matrix(client: TestClient) -> None:
    captain = authorization(CAPTAIN_TOKEN)
    worker = authorization(WORKER_TOKEN)

    problem = client.post(
        "/blocks",
        headers=captain,
        json={"block_type": "problem", "data": {"batch_id": "project-1"}},
    )
    batch = client.post(
        "/blocks",
        headers=captain,
        json={"block_type": "work_batch", "status": "pending_review", "data": batch_payload()},
    )
    assert problem.status_code == 201
    assert batch.status_code == 201
    parent_index = batch.json()["index"]
    holdout = client.post(
        "/blocks",
        headers=captain,
        json={
            "block_type": "holdout",
            "parent_index": parent_index,
            "data": {"batch_id": "batch-1", "cases": [{"case_id": "secret-1", "input": {}}]},
        },
    )
    assert holdout.status_code == 201
    assert client.post("/batches/batch-1/approve", headers=worker).status_code == 403
    assert client.post("/batches/batch-1/approve", headers=captain).status_code == 200
    assert client.post("/batches/batch-1/claim", headers=captain).status_code == 403
    claimed = client.post("/batches/batch-1/claim", headers=worker)
    assert claimed.status_code == 200
    claim_token = claimed.json()["claim_token"]
    worker_claim = authorization(WORKER_TOKEN, claim_token=claim_token)

    assert client.post(
        "/batches/batch-1/claim/heartbeat",
        headers=authorization(CAPTAIN_TOKEN, claim_token=claim_token),
    ).status_code == 403
    assert client.post(
        "/batches/batch-1/claim/heartbeat",
        headers=worker_claim,
    ).status_code == 200
    assert client.post(
        "/blocks",
        headers=authorization(CAPTAIN_TOKEN, claim_token=claim_token),
        json={"block_type": "codex_session", "data": {"batch_id": "batch-1", "iteration": 1}},
    ).status_code == 403
    assert client.post(
        "/blocks",
        headers=worker_claim,
        json={"block_type": "codex_session", "data": {"batch_id": "batch-1", "iteration": 1}},
    ).status_code == 201

    for token in (CAPTAIN_TOKEN, WORKER_TOKEN):
        reader = authorization(token, claim_token=claim_token)
        assert client.get("/batches", params={"status": "claimed"}, headers=reader).status_code == 200
        assert client.get("/batches/batch-1", headers=reader).status_code == 200
        assert client.get("/batches/batch-1/bundle", headers=reader).status_code == 200
        assert client.get("/batches/batch-1/blocks", headers=reader).status_code == 200
        assert client.get("/batches/batch-1/holdout", headers=reader).status_code == 200
        assert client.get("/capabilities", params={"need": "email"}, headers=reader).status_code == 200

    assert client.post("/sink/crm", headers=captain, json={"case_id": "case-1", "tag": "lead"}).status_code == 403
    assert client.post("/sink/crm", headers=worker, json={"case_id": "case-1", "tag": "lead"}).status_code == 201
    assert client.get("/sink/crm", params={"case_id": "case-1"}, headers=captain).status_code == 403
    assert client.get("/sink/crm", params={"case_id": "case-1"}, headers=worker).status_code == 200


def test_runtime_lifespan_fails_closed_without_complete_settings(
    storage: MariaDBStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("LEDGER_DSN", "CAPTAIN_GATEWAY_TOKEN", "WORKER_GATEWAY_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    application = create_app(storage=storage, mirror=RecordingMirror())

    with pytest.raises(GatewayConfigurationError):
        with TestClient(application):
            pass
