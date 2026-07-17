from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from blockchain.mariadb_storage import MariaDBStorage
from gateway.app import create_app
from gateway.contracts import DeliveryEventEnvelope
from gateway.settings import GatewaySettings
from tests.support.mariadb import assert_isolated_test_database


TEST_DSN = os.getenv("TEST_MARIADB_DSN")
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
def client(storage: MariaDBStorage) -> TestClient:
    assert TEST_DSN is not None
    settings = GatewaySettings(
        ledger_dsn=SecretStr(TEST_DSN),
        captain_gateway_token=SecretStr(CAPTAIN_TOKEN),
        worker_gateway_token=SecretStr(WORKER_TOKEN),
        approval_enabled=True,
    )
    return TestClient(create_app(storage=storage, mirror=RecordingMirror(), settings=settings))


def authorization(token: str, *, claim_token: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if claim_token is not None:
        headers["X-Claim-Token"] = claim_token
    return headers


def publish_holdout(client: TestClient) -> None:
    batch = client.post(
        "/blocks",
        headers=authorization(CAPTAIN_TOKEN),
        json={
            "block_type": "work_batch",
            "data": {
                "batch_id": "batch-1",
                "title": "Build workflow",
                "goal": "Deliver it",
                "subtask_ids": ["subtask-1"],
                "target": "n8n",
                "acceptance_criteria": [
                    {"assertion_id": "status-ok", "kind": "status_equals", "expected": "ok"}
                ],
            },
        },
    )
    assert batch.status_code == 201, batch.text
    holdout = client.post(
        "/blocks",
        headers=authorization(CAPTAIN_TOKEN),
        json={
            "block_type": "holdout",
            "parent_index": batch.json()["index"],
            "data": {
                "batch_id": "batch-1",
                "cases": [
                    {
                        "case_id": "case-1",
                        "input": {"secret": 42},
                        "expected_observations": {"status": "ok"},
                    }
                ],
            },
        },
    )
    assert holdout.status_code == 201, holdout.text


def sealed_artifact_event() -> DeliveryEventEnvelope:
    return DeliveryEventEnvelope.model_validate(
        {
            "event_id": UUID(int=1),
            "event_type": "artifact_built",
            "occurred_at": datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc),
            "actor": "builder",
            "trace": {
                "project_id": "project-1",
                "run_id": "run-1",
                "trace_id": "trace-1",
                "batch_id": "batch-1",
                "worker_id": "builder-1",
                "claim_id": "claim-1",
                "fencing_token": 1,
                "artifact_id": "artifact-1",
            },
            "payload": {
                "event_type": "artifact_built",
                "artifact_id": "artifact-1",
                "artifact_version": "v1",
                "sha256": "a" * 64,
                "artifact_type": "n8n-workflow",
                "sealed_ref": "artifact://sealed/artifact-1",
            },
        }
    )


def claimed_artifact_write(
    client: TestClient,
) -> tuple[dict[str, str], DeliveryEventEnvelope]:
    claimed = client.post(
        "/batches/batch-1/claim",
        headers=authorization(WORKER_TOKEN),
    )
    assert claimed.status_code == 200, claimed.text
    claim = claimed.json()
    event = sealed_artifact_event()
    return (
        authorization(WORKER_TOKEN, claim_token=claim["claim_token"]),
        event.model_copy(
            update={
                "trace": event.trace.model_copy(
                    update={
                        "claim_id": claim["claim_id"],
                        "fencing_token": claim["fencing_token"],
                    }
                )
            }
        ),
    )


@pytest.mark.parametrize(
    "gateway_token",
    [WORKER_TOKEN, CAPTAIN_TOKEN],
    ids=["worker", "captain"],
)
def test_legacy_holdout_route_never_discloses_after_valid_claim_and_session(
    client: TestClient,
    gateway_token: str,
) -> None:
    publish_holdout(client)
    claimed = client.post(
        "/batches/batch-1/claim",
        headers=authorization(WORKER_TOKEN),
    )
    assert claimed.status_code == 200, claimed.text
    claim_token = claimed.json()["claim_token"]
    worker_claim = authorization(WORKER_TOKEN, claim_token=claim_token)
    session = client.post(
        "/blocks",
        headers=worker_claim,
        json={
            "block_type": "codex_session",
            "data": {"batch_id": "batch-1", "iteration": 1},
        },
    )
    assert session.status_code == 201, session.text

    response = client.get(
        "/batches/batch-1/holdout",
        headers=authorization(gateway_token, claim_token=claim_token),
    )

    assert response.status_code == 410
    assert response.json() == {"detail": "legacy holdout route is gone"}
    assert "secret" not in response.text


def test_builder_cannot_read_holdout_before_or_after_artifact_is_sealed(
    client: TestClient,
) -> None:
    publish_holdout(client)
    route = "/v1/projects/project-1/runs/run-1/holdouts/case-1"
    worker = authorization(WORKER_TOKEN)

    assert client.get(route, headers=worker).status_code == 403
    claim_headers, event = claimed_artifact_write(client)
    appended = client.post(
        "/v1/delivery/events",
        headers=claim_headers,
        json=event.model_dump(mode="json"),
    )
    assert appended.status_code == 201, appended.text
    assert client.get(route, headers=worker).status_code == 403


def test_validator_reads_only_matching_holdout_after_artifact_is_sealed(
    client: TestClient,
) -> None:
    publish_holdout(client)
    route = "/v1/projects/project-1/runs/run-1/holdouts/case-1"
    captain = authorization(CAPTAIN_TOKEN)

    assert client.get(route, headers=captain).status_code == 404
    claim_headers, event = claimed_artifact_write(client)
    appended = client.post(
        "/v1/delivery/events",
        headers=claim_headers,
        json=event.model_dump(mode="json"),
    )
    assert appended.status_code == 201, appended.text

    released = client.get(route, headers=captain)
    assert released.status_code == 200
    assert released.json() == {
        "case_id": "case-1",
        "input": {"secret": 42},
        "expected_observations": {"status": "ok"},
    }
    assert client.get(
        "/v1/projects/project-1/runs/run-1/holdouts/missing",
        headers=captain,
    ).status_code == 404
