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
