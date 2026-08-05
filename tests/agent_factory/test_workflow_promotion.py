from __future__ import annotations

from datetime import datetime, timezone
import json
from uuid import UUID

import httpx

from agenten.agent_factory.contracts import FactoryPhase
from agenten.agent_factory.state_machine import FactoryLifecycleStatus
from tests.agent_factory.test_state_machine import _promotion_snapshot


NOW = datetime(2026, 8, 4, 20, tzinfo=timezone.utc)


def test_promote_release_workflow_posts_captain_block_and_verifies_ready() -> None:
    from agenten.agent_factory.workflow_promotion import promote_release_workflow

    pending, _, _, _ = _promotion_snapshot()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json=pending.model_dump(mode="json", by_alias=True),
            )
        if len(requests) == 2:
            body = json.loads(request.content)
            assert body["phase"] == "capability_promoted"
            assert body["producer"] == "captain"
            promotion_event_id_holder[0] = UUID(body["event_id"])
            return httpx.Response(
                201,
                json={"event_id": body["event_id"], "replayed": False},
            )
        ready = pending.model_copy(
            update={
                "projection": pending.projection.model_copy(
                    update={
                        "status": FactoryLifecycleStatus.READY_TO_USE,
                        "phase": FactoryPhase.CAPABILITY_PROMOTED,
                    }
                )
            }
        )
        return httpx.Response(
            200,
            json=ready.model_dump(mode="json", by_alias=True),
        )

    promotion_event_id_holder = [pending.job.event_id]
    with httpx.Client(
        base_url="http://gateway.test",
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer captain-secret"},
    ) as client:
        result = promote_release_workflow(
            client=client,
            job_id=pending.job.job_id,
            occurred_at=NOW,
        )

    assert [request.method for request in requests] == ["GET", "POST", "GET"]
    assert result.job_id == pending.job.job_id
    assert result.promotion_event_id == promotion_event_id_holder[0]
    assert result.replayed is False
    assert result.status is FactoryLifecycleStatus.READY_TO_USE
    assert result.phase is FactoryPhase.CAPABILITY_PROMOTED


def test_promote_release_workflow_replays_already_ready_projection() -> None:
    from agenten.agent_factory.workflow_promotion import (
        build_release_workflow_promotion,
        promote_release_workflow,
    )

    pending, _, _, _ = _promotion_snapshot()
    promotion = build_release_workflow_promotion(pending, occurred_at=NOW)
    ready = pending.model_copy(
        update={
            "blocks": (*pending.blocks, promotion),
            "projection": pending.projection.model_copy(
                update={
                    "status": FactoryLifecycleStatus.READY_TO_USE,
                    "phase": FactoryPhase.CAPABILITY_PROMOTED,
                    "block_ids": (*pending.projection.block_ids, promotion.event_id),
                }
            ),
        }
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=ready.model_dump(mode="json", by_alias=True),
        )

    with httpx.Client(
        base_url="http://gateway.test",
        transport=httpx.MockTransport(handler),
    ) as client:
        result = promote_release_workflow(
            client=client,
            job_id=pending.job.job_id,
            occurred_at=NOW,
        )

    assert len(requests) == 1
    assert result.promotion_event_id == promotion.event_id
    assert result.replayed is True
    assert result.status is FactoryLifecycleStatus.READY_TO_USE
