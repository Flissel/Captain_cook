from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from agenten.agent_factory.integration_setup import IntegrationSetupPlanner
from blockchain.mariadb_storage import MariaDBStorage
from gateway.app import create_app
from gateway.settings import GatewaySettings
from tests.agent_factory.test_integration_setup import (
    credential,
    integration,
    receipt,
    requirement,
)
from tests.agent_factory.test_state_machine import job
from tests.support.mariadb import assert_isolated_test_database


TEST_DSN = os.getenv("TEST_MARIADB_DSN")
pytestmark = pytest.mark.skipif(
    not TEST_DSN,
    reason="TEST_MARIADB_DSN is not configured",
)
CAPTAIN_TOKEN = "captain-test-token"
WORKER_TOKEN = "worker-test-token"
NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


class RecordingMirror:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def enqueue_nowait(self, block: dict[str, Any]) -> None:
        self.items.append(block)


@pytest.fixture
def storage() -> Iterator[MariaDBStorage]:
    assert TEST_DSN is not None
    assert_isolated_test_database(TEST_DSN)
    result = MariaDBStorage(TEST_DSN)
    result.clear()
    yield result
    result.clear()


def application(storage: MariaDBStorage, mirror: RecordingMirror | None = None):
    assert TEST_DSN is not None
    return create_app(
        storage=storage,
        mirror=mirror or RecordingMirror(),
        settings=GatewaySettings(
            ledger_dsn=SecretStr(TEST_DSN),
            captain_gateway_token=SecretStr(CAPTAIN_TOKEN),
            worker_gateway_token=SecretStr(WORKER_TOKEN),
        ),
    )


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def setup_payload(
    factory_job,
    *,
    event_id: str = "80000000-0000-0000-0000-000000000001",
    revision: int = 1,
    previous_sha256: str | None = None,
) -> dict[str, Any]:
    return {
        "schema": "captain.integration-setup-submission.v1",
        "event_id": event_id,
        "job_id": str(factory_job.job_id),
        "correlation_id": str(factory_job.correlation_id),
        "subject_version": factory_job.subject_version,
        "revision": revision,
        "previous_content_sha256": previous_sha256,
        "occurred_at": (NOW + timedelta(seconds=revision - 1)).isoformat().replace("+00:00", "Z"),
        "change_kind": "observed",
        "plan": {
            "schema": "captain.integration-setup-plan.v1",
            "connections": [
                {
                    "schema": "captain.integration-connection.v1",
                    "requirement": {
                        "schema": "captain.integration-credential-requirement.v1",
                        "integration_key": "crm",
                        "credential_alias": "CRM_PRIMARY",
                        "credential_type": "hubspotApi",
                        "required": True,
                        "setup_method": "n8n_ui",
                        "setup_label": "Connect HubSpot in n8n",
                        "project_id": None,
                        "verification_workflow_sha256": "d" * 64,
                    },
                    "status": "missing",
                    "candidate_credentials": [],
                    "selected_credential": None,
                    "verification_receipt": None,
                }
            ],
        },
    }


def ready_setup_payload(factory_job) -> dict[str, Any]:
    payload = setup_payload(factory_job)
    payload["plan"] = IntegrationSetupPlanner().plan(
        integrations=(integration(),),
        requirements=(requirement(),),
        credentials=(credential(),),
        verification_receipts=(receipt(),),
    ).model_dump(mode="json", by_alias=True)
    return payload


def test_setup_api_is_captain_only_restart_safe_and_idempotent(
    storage: MariaDBStorage,
) -> None:
    factory_job = job()
    payload = setup_payload(factory_job)

    with TestClient(application(storage)) as client:
        assert client.post("/v1/factory/integration-setups", json=payload).status_code == 401
        assert client.post(
            "/v1/factory/integration-setups",
            json=payload,
            headers=auth(WORKER_TOKEN),
        ).status_code == 403
        assert client.post(
            "/v1/factory/jobs",
            json=factory_job.model_dump(mode="json", by_alias=True),
            headers=auth(CAPTAIN_TOKEN),
        ).status_code == 202

        created = client.post(
            "/v1/factory/integration-setups",
            json=payload,
            headers=auth(CAPTAIN_TOKEN),
        )
        replay = client.post(
            "/v1/factory/integration-setups",
            json=payload,
            headers=auth(CAPTAIN_TOKEN),
        )
        surface = client.get(
            f"/v1/factory/jobs/{factory_job.job_id}/integration-setup/surface",
            headers=auth(CAPTAIN_TOKEN),
        )

    assert created.status_code == 201
    assert replay.status_code == 200
    assert created.json()["replayed"] is False
    assert replay.json()["replayed"] is True
    assert created.json()["content_sha256"] == replay.json()["content_sha256"]
    assert surface.status_code == 200
    assert surface.json()["n8n_credentials_url"] == "http://localhost:5679/home/credentials"
    assert surface.json()["actions"][0]["credential_type"] == "hubspotApi"

    with TestClient(application(storage)) as restarted:
        recovered = restarted.get(
            f"/v1/factory/jobs/{factory_job.job_id}/integration-setup",
            headers=auth(CAPTAIN_TOKEN),
        )

    assert recovered.status_code == 200
    assert recovered.json()["submission"] == payload
    assert recovered.json()["content_sha256"] == created.json()["content_sha256"]


def test_setup_revision_is_digest_fenced_and_rejects_secret_fields(
    storage: MariaDBStorage,
) -> None:
    factory_job = job()
    with TestClient(application(storage)) as client:
        client.post(
            "/v1/factory/jobs",
            json=factory_job.model_dump(mode="json", by_alias=True),
            headers=auth(CAPTAIN_TOKEN),
        )
        first = client.post(
            "/v1/factory/integration-setups",
            json=setup_payload(factory_job),
            headers=auth(CAPTAIN_TOKEN),
        )
        assert first.status_code == 201

        unfenced = client.post(
            "/v1/factory/integration-setups",
            json=setup_payload(
                factory_job,
                event_id="80000000-0000-0000-0000-000000000002",
                revision=2,
                previous_sha256="0" * 64,
            ),
            headers=auth(CAPTAIN_TOKEN),
        )
        assert unfenced.status_code == 409

        accepted = client.post(
            "/v1/factory/integration-setups",
            json=setup_payload(
                factory_job,
                event_id="80000000-0000-0000-0000-000000000002",
                revision=2,
                previous_sha256=first.json()["content_sha256"],
            ),
            headers=auth(CAPTAIN_TOKEN),
        )
        assert accepted.status_code == 201

        leaked = setup_payload(
            factory_job,
            event_id="80000000-0000-0000-0000-000000000003",
            revision=3,
            previous_sha256=accepted.json()["content_sha256"],
        )
        leaked["plan"]["connections"][0]["selected_credential"] = {
            "schema": "captain.n8n-credential-metadata.v1",
            "credential_id": "cred-1",
            "credential_name": "HubSpot",
            "credential_type": "hubspotApi",
            "project_id": None,
            "project_name": None,
            "api_key": "must-never-enter-the-gateway",
        }
        leaked["plan"]["connections"][0]["candidate_credentials"] = [
            leaked["plan"]["connections"][0]["selected_credential"]
        ]
        leaked["plan"]["connections"][0]["status"] = "verification_required"

        rejected = client.post(
            "/v1/factory/integration-setups",
            json=leaked,
            headers=auth(CAPTAIN_TOKEN),
        )

    assert rejected.status_code == 422
    assert "must-never-enter-the-gateway" not in rejected.text


def test_setup_submission_must_match_the_factory_job(storage: MariaDBStorage) -> None:
    factory_job = job()
    payload = setup_payload(factory_job)
    payload["correlation_id"] = str(UUID("90000000-0000-0000-0000-000000000001"))

    with TestClient(application(storage)) as client:
        client.post(
            "/v1/factory/jobs",
            json=factory_job.model_dump(mode="json", by_alias=True),
            headers=auth(CAPTAIN_TOKEN),
        )
        response = client.post(
            "/v1/factory/integration-setups",
            json=payload,
            headers=auth(CAPTAIN_TOKEN),
        )

    assert response.status_code == 409
    assert response.json() == {"detail": "integration setup does not match its factory job"}


def test_setup_rotation_and_revoke_persist_across_restart(
    storage: MariaDBStorage,
) -> None:
    factory_job = job()
    with TestClient(application(storage)) as client:
        assert client.post(
            "/v1/factory/jobs",
            json=factory_job.model_dump(mode="json", by_alias=True),
            headers=auth(CAPTAIN_TOKEN),
        ).status_code == 202
        created = client.post(
            "/v1/factory/integration-setups",
            json=ready_setup_payload(factory_job),
            headers=auth(CAPTAIN_TOKEN),
        )
        assert created.status_code == 201

        rotation = {
            "schema": "captain.integration-setup-mutation.v1",
            "event_id": "80000000-0000-0000-0000-000000000004",
            "credential_alias": "CRM_API_KEY",
            "expected_content_sha256": created.json()["content_sha256"],
            "occurred_at": "2026-08-04T12:01:00Z",
            "action": "rotation_requested",
        }
        rotated = client.post(
            f"/v1/factory/jobs/{factory_job.job_id}/integration-setup/mutations",
            json=rotation,
            headers=auth(CAPTAIN_TOKEN),
        )
        assert rotated.status_code == 201
        assert client.post(
            f"/v1/factory/jobs/{factory_job.job_id}/integration-setup/mutations",
            json=rotation,
            headers=auth(CAPTAIN_TOKEN),
        ).status_code == 200
        assert client.post(
            f"/v1/factory/jobs/{factory_job.job_id}/integration-setup/mutations",
            json={**rotation, "credential_alias": "OTHER_CREDENTIAL"},
            headers=auth(CAPTAIN_TOKEN),
        ).status_code == 409

        revocation = {
            **rotation,
            "event_id": "80000000-0000-0000-0000-000000000005",
            "expected_content_sha256": rotated.json()["content_sha256"],
            "occurred_at": "2026-08-04T12:02:00Z",
            "action": "revoked",
        }
        revoked = client.post(
            f"/v1/factory/jobs/{factory_job.job_id}/integration-setup/mutations",
            json=revocation,
            headers=auth(CAPTAIN_TOKEN),
        )
        assert revoked.status_code == 201

    with TestClient(application(storage)) as restarted:
        recovered = restarted.get(
            f"/v1/factory/jobs/{factory_job.job_id}/integration-setup",
            headers=auth(CAPTAIN_TOKEN),
        )

    assert recovered.status_code == 200
    assert recovered.json()["submission"]["revision"] == 3
    assert recovered.json()["submission"]["change_kind"] == "revoked"
    assert recovered.json()["submission"]["plan"]["connections"][0]["status"] == "revoked"
