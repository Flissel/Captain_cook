import asyncio
from datetime import datetime, timezone
from uuid import UUID

from fastapi.testclient import TestClient
from pydantic import SecretStr

from gateway import registry_feed
from gateway.app import create_app
from gateway.settings import GatewaySettings


def test_successful_batch_is_mirrored_as_validated_without_forum_credentials(monkeypatch) -> None:
    calls = []

    async def record(payload):
        calls.append(payload)

    monkeypatch.setattr(registry_feed, "_post_registry", record)

    asyncio.run(
        registry_feed.mirror_validated_batch(
            {
                "block_type": "batch_done",
                "status": "succeeded",
                "data": {
                    "batch_id": "batch-1",
                    "artifact_ref": "workflow-42",
                    "capabilities": ["email"],
                    "eval_score": 9,
                },
            }
        )
    )

    payload = calls[0]
    assert payload["status"] == "validated"
    assert "registry_agent_api_key" not in payload
    assert payload["tools_py_path"] == "workflow-42"


def test_non_successful_blocks_are_not_mirrored(monkeypatch) -> None:
    calls = []

    async def record(payload):
        calls.append(payload)

    monkeypatch.setattr(registry_feed, "_post_registry", record)

    asyncio.run(
        registry_feed.mirror_validated_batch(
            {"block_type": "batch_done", "status": "failed", "data": {"batch_id": "batch-1"}}
        )
    )

    assert calls == []


def test_factory_promotion_projects_only_ready_capability_metadata(monkeypatch) -> None:
    calls = []

    async def record(payload):
        calls.append(payload)

    monkeypatch.setattr(registry_feed, "_post_registry", record)

    asyncio.run(
        registry_feed.mirror_captain_projection(
            {
                "event_type": "factory_lifecycle",
                "job_id": "job-1",
                "capability_id": "support_triage",
                "phase": "capability_promoted",
                "status": "succeeded",
                "attempt": 1,
                "subject_version": 1,
                "lease_id": "must-not-project",
                "evidence_refs": ["must-not-project"],
            }
        )
    )

    assert calls[0]["team_key"] == "support_triage"
    assert calls[0]["run_id"] == "job-1"
    assert "lease_id" not in calls[0]
    assert "evidence_refs" not in calls[0]


def test_factory_promotion_builds_redacted_v2_projection_with_correlation() -> None:
    event = registry_feed.factory_promotion_projection(
        {
            "event_id": "10000000-0000-4000-8000-000000000001",
            "job_id": "20000000-0000-4000-8000-000000000001",
            "phase": "capability_promoted",
            "status": "succeeded",
            "subject_version": 7,
            "occurred_at": datetime(2026, 7, 20, tzinfo=timezone.utc).isoformat(),
            "lease_id": "private-lease",
            "evidence_refs": ["private-evidence"],
        },
        {
            "correlation_id": "30000000-0000-4000-8000-000000000001",
            "required_capability": "support_triage",
            "input_ref": "private-input",
        },
    )

    assert event.schema_name == "captain.minibook-projection.v2"
    assert event.event_type == "capability.promoted"
    assert event.event_id == UUID("10000000-0000-4000-8000-000000000001")
    assert event.correlation_id == UUID("30000000-0000-4000-8000-000000000001")
    assert event.subject_id == "subject:20000000-0000-4000-8000-000000000001"
    assert event.subject_version == 7
    assert event.payload.model_dump(exclude_none=True) == {
        "view": "validation",
        "template_id": "factory_capability_ready_to_use",
        "status_id": "ready_to_use",
        "actor_role_id": "captain_gateway",
    }
    serialized = event.model_dump_json(by_alias=True)
    assert "support_triage" not in serialized
    assert "private" not in serialized


def test_projection_feed_is_captain_authenticated_and_paginated(monkeypatch) -> None:
    block = {
        "event_id": "10000000-0000-4000-8000-000000000001",
        "job_id": "20000000-0000-4000-8000-000000000001",
        "phase": "capability_promoted",
        "status": "succeeded",
        "subject_version": 7,
        "occurred_at": datetime(2026, 7, 20, tzinfo=timezone.utc).isoformat(),
    }
    job = {
        "correlation_id": "30000000-0000-4000-8000-000000000001",
        "required_capability": "support_triage",
    }

    class Store:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def factory_projection_feed(self, *, after_index: int, limit: int):
            assert after_index == 4
            assert limit == 1
            return [(5, block, job)], True

    class Mirror:
        def enqueue_nowait(self, _payload) -> None:
            pass

    monkeypatch.setattr("gateway.app.GatewayStore", Store)
    app = create_app(
        storage=object(),
        mirror=Mirror(),
        settings=GatewaySettings(
            ledger_dsn=SecretStr("mysql://unused/captain_test"),
            captain_gateway_token=SecretStr("captain-token"),
            worker_gateway_token=SecretStr("worker-token"),
        ),
    )
    with TestClient(app) as client:
        assert client.get(
            "/api/v1/projections/minibook/events?cursor=4&limit=1",
            headers={"Authorization": "Bearer worker-token"},
        ).status_code == 403
        response = client.get(
            "/api/v1/projections/minibook/events?cursor=4&limit=1",
            headers={"Authorization": "Bearer captain-token"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "events": [
            registry_feed.factory_promotion_projection(block, job).model_dump(
                mode="json", by_alias=True
            )
        ],
        "cursor": "5",
        "has_more": True,
    }
