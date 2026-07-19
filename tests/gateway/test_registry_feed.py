import asyncio

from gateway import registry_feed


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
