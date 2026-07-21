import asyncio
from datetime import datetime, timezone
from uuid import UUID

from fastapi.testclient import TestClient
from pydantic import SecretStr

from gateway import registry_feed
from gateway.app import create_app
from gateway.settings import GatewaySettings
from gateway.store import GatewayStore


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


def test_runtime_result_builds_stable_redacted_projection_with_command_causation() -> None:
    projection = getattr(registry_feed, "runtime_result_projection", None)
    assert callable(projection), "runtime result projection is not implemented"
    result = {
        "schema": "captain.agent-runtime-result.v1",
        "event_id": "40000000-0000-4000-8000-000000000001",
        "command_id": "50000000-0000-4000-8000-000000000001",
        "correlation_id": "60000000-0000-4000-8000-000000000001",
        "occurred_at": datetime(2026, 7, 20, 12, 1, tzinfo=timezone.utc).isoformat(),
        "producer": "hermes-runtime",
        "subject_id": "private-workspace-subtask",
        "subject_version": 3,
        "grant_id": "private-grant",
        "operation": "codex.run",
        "status": "succeeded",
        "session_id": "private-session",
        "artifact_refs": [
            {
                "uri": "artifact://private/workspace/result.json",
                "sha256": "a" * 64,
                "media_type": "application/json",
            }
        ],
        "evidence_refs": [
            {
                "uri": "artifact://private/provider/transcript.json",
                "sha256": "b" * 64,
                "media_type": "application/json",
            }
        ],
        "error": None,
    }

    first = projection(result)
    replay = projection(result)

    assert first == replay
    assert first.event_id == UUID(result["event_id"])
    assert first.causation_id == UUID(result["command_id"])
    assert first.correlation_id == UUID(result["correlation_id"])
    assert first.event_type == "codex.result"
    assert first.payload.model_dump(exclude_none=True) == {
        "view": "build",
        "template_id": "runtime_build_recorded",
        "status_id": "built",
        "actor_role_id": "codex_worker",
        "artifact_digest": f"sha256:{'a' * 64}",
    }
    serialized = first.model_dump_json(by_alias=True)
    for secret in (
        "private-workspace-subtask",
        "private-grant",
        "private-session",
        "artifact://private",
        "provider",
    ):
        assert secret not in serialized


def test_projection_feed_is_captain_authenticated_and_globally_paginated(monkeypatch) -> None:
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
    runtime_result = {
        "schema": "captain.agent-runtime-result.v1",
        "event_id": "40000000-0000-4000-8000-000000000001",
        "command_id": "50000000-0000-4000-8000-000000000001",
        "correlation_id": "30000000-0000-4000-8000-000000000001",
        "occurred_at": datetime(2026, 7, 20, 12, 1, tzinfo=timezone.utc).isoformat(),
        "producer": "agent-runtime",
        "subject_id": "subtask-private",
        "subject_version": 8,
        "grant_id": "grant-private",
        "operation": "codex.run",
        "status": "succeeded",
        "session_id": "session-private",
        "artifact_refs": [],
        "evidence_refs": [],
        "error": None,
    }

    class Store:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def minibook_projection_feed(self, *, after_index: int, limit: int):
            assert after_index == 4
            assert limit == 2
            return [
                (5, "agent_factory_block", block, job),
                (7, "agent_runtime_result", runtime_result, None),
            ], True

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
            "/api/v1/projections/minibook/events?cursor=4&limit=2",
            headers={"Authorization": "Bearer worker-token"},
        ).status_code == 403
        response = client.get(
            "/api/v1/projections/minibook/events?cursor=4&limit=2",
            headers={"Authorization": "Bearer captain-token"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "events": [
            registry_feed.factory_promotion_projection(block, job).model_dump(
                mode="json", by_alias=True
            ),
            registry_feed.runtime_result_projection(runtime_result).model_dump(
                mode="json", by_alias=True
            ),
        ],
        "cursor": "7",
        "has_more": True,
    }


def test_store_pages_admitted_mixed_records_by_one_global_ledger_cursor() -> None:
    promotion_job = {
        "correlation_id": "30000000-0000-4000-8000-000000000001",
    }
    ledger = [
        {
            "index": 2,
            "block_type": "agent_factory_block",
            "data": {"phase": "capability_promoted", "status": "succeeded"},
            "parent_data": promotion_job,
        },
        {
            "index": 3,
            "block_type": "unrelated_block",
            "data": {"secret": "not-admitted"},
            "parent_data": None,
        },
        {
            "index": 4,
            "block_type": "agent_runtime_result",
            "data": {"schema": "captain.agent-runtime-result.v1"},
            "parent_data": None,
        },
        {
            "index": 5,
            "block_type": "agent_factory_block",
            "data": {"phase": "capability_promoted", "status": "succeeded"},
            "parent_data": promotion_job,
        },
    ]
    executed: list[tuple[str, tuple[int, int]]] = []

    class Cursor:
        rows: list[dict[str, object]] = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, sql: str, params: tuple[int, int]) -> None:
            executed.append((sql, params))
            after_index, fetch_limit = params
            admitted = [
                row
                for row in ledger
                if int(row["index"]) > after_index
                and row["block_type"] in {
                    "agent_factory_block",
                    "agent_runtime_result",
                }
            ]
            self.rows = admitted[:fetch_limit]

        def fetchall(self):
            return self.rows

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def cursor(self) -> Cursor:
            return Cursor()

    class Storage:
        def transaction(self) -> Connection:
            return Connection()

        @staticmethod
        def _decode_row(row: dict[str, object]) -> dict[str, object]:
            return row

    feed = getattr(GatewayStore, "minibook_projection_feed", None)
    assert callable(feed), "one globally ordered Minibook feed query is not implemented"
    store = object.__new__(GatewayStore)
    store.storage = Storage()
    cursor = -1
    observed: list[tuple[int, str]] = []
    while True:
        records, has_more = store.minibook_projection_feed(
            after_index=cursor,
            limit=1,
        )
        assert len(records) == 1
        index, block_type, _, _ = records[0]
        assert index > cursor
        cursor = index
        observed.append((index, block_type))
        if not has_more:
            break

    assert observed == [
        (2, "agent_factory_block"),
        (4, "agent_runtime_result"),
        (5, "agent_factory_block"),
    ]
    assert all(params[1] == 2 for _, params in executed)
    assert all("ORDER BY event.`index`" in sql for sql, _ in executed)
