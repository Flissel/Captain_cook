import asyncio
from datetime import datetime, timezone
import json
import sqlite3
from uuid import UUID

from fastapi.testclient import TestClient
import pytest
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


@pytest.mark.parametrize("operation", ("codex.run", "codex.resume"))
def test_runtime_result_builds_stable_redacted_projection_with_command_causation(
    operation: str,
) -> None:
    projection = getattr(registry_feed, "runtime_result_projection", None)
    assert callable(projection), "runtime result projection is not implemented"
    result = {
        "schema": "captain.agent-runtime-result.v1",
        "event_id": "40000000-0000-4000-8000-000000000001",
        "command_id": "50000000-0000-4000-8000-000000000001",
        "correlation_id": "60000000-0000-4000-8000-000000000001",
        "occurred_at": datetime(2026, 7, 20, 12, 1, tzinfo=timezone.utc).isoformat(),
        "producer": "agent-runtime",
        "subject_id": "private-workspace-subtask",
        "subject_version": 3,
        "grant_id": "private-grant",
        "operation": operation,
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
    assert first is not None
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


@pytest.mark.parametrize(
    ("producer", "operation", "status", "error"),
    (
        ("agent-runtime", "codex.run", "failed", "private provider failure"),
        (
            "agent-runtime",
            "codex.run",
            "infrastructure_failed",
            "private infrastructure failure",
        ),
        ("agent-runtime", "codex.run", "policy_failed", "private policy failure"),
        ("agent-runtime", "codex.run", "cancelled", None),
        ("agent-runtime", "codex.status", "succeeded", None),
        ("hermes-runtime", "codex.run", "succeeded", None),
        ("hermes-runtime", "hermes.plan", "succeeded", None),
    ),
)
def test_non_successful_or_non_codex_build_runtime_results_are_not_projected_as_built(
    producer: str,
    operation: str,
    status: str,
    error: str | None,
) -> None:
    result = {
        "schema": "captain.agent-runtime-result.v1",
        "event_id": "40000000-0000-4000-8000-000000000009",
        "command_id": "50000000-0000-4000-8000-000000000009",
        "correlation_id": "60000000-0000-4000-8000-000000000009",
        "occurred_at": "2026-07-20T12:01:00Z",
        "producer": producer,
        "subject_id": "private-subtask",
        "subject_version": 3,
        "grant_id": "private-grant",
        "operation": operation,
        "status": status,
        "session_id": None,
        "artifact_refs": [],
        "evidence_refs": [],
        "error": error,
    }

    assert registry_feed.runtime_result_projection(result) is None


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
    failed_runtime_result = {
        **runtime_result,
        "event_id": "40000000-0000-4000-8000-000000000002",
        "status": "failed",
        "error": "provider text must not project",
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
                (8, "agent_runtime_result", failed_runtime_result, None),
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
        "cursor": "8",
        "has_more": True,
    }


def test_store_pages_admitted_mixed_records_by_one_global_ledger_cursor() -> None:
    promotion_job = {
        "correlation_id": "30000000-0000-4000-8000-000000000001",
    }
    database = sqlite3.connect(":memory:")
    database.row_factory = sqlite3.Row
    database.create_function("JSON_UNQUOTE", 1, lambda value: value)
    database.execute(
        """
        CREATE TABLE blocks (
            `index` INTEGER PRIMARY KEY,
            parent_index INTEGER,
            block_type TEXT NOT NULL,
            data TEXT NOT NULL,
            status TEXT,
            children TEXT,
            metadata TEXT,
            hash TEXT,
            previous_hash TEXT
        )
        """
    )

    def insert(
        index: int,
        block_type: str,
        data: dict[str, object],
        *,
        parent_index: int | None = None,
    ) -> None:
        database.execute(
            "INSERT INTO blocks(`index`, parent_index, block_type, data) VALUES (?, ?, ?, ?)",
            (index, parent_index, block_type, json.dumps(data)),
        )

    insert(0, "agent_factory_job", promotion_job)
    insert(
        1,
        "agent_factory_block",
        {"phase": "capability_promoted", "status": "failed"},
        parent_index=0,
    )
    insert(
        2,
        "agent_factory_block",
        {"phase": "capability_promoted", "status": "succeeded"},
        parent_index=0,
    )
    insert(3, "unrelated_block", {"secret": "not-admitted"})
    insert(4, "agent_runtime_result", {"schema": "captain.agent-runtime-result.v1"})
    insert(
        5,
        "agent_factory_block",
        {"phase": "capability_promoted", "status": "succeeded"},
        parent_index=0,
    )
    insert(
        6,
        "agent_factory_block",
        {"phase": "candidate_built", "status": "succeeded"},
        parent_index=0,
    )
    database.commit()

    class Cursor:
        def __init__(self) -> None:
            self.delegate = database.cursor()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.delegate.close()

        def execute(self, sql: str, params: tuple[int, int]) -> None:
            self.delegate.execute(sql.replace("%s", "?"), params)

        def fetchall(self):
            return self.delegate.fetchall()

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
        def _decode_row(row: sqlite3.Row) -> dict[str, object]:
            decoded = dict(row)
            decoded["data"] = json.loads(str(decoded["data"]))
            return decoded

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
    database.close()
