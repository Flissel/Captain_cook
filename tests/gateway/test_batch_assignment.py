"""Captain pins a batch to one agent; only that agent may claim it."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from agenten.delivery.gateway_client import GatewayBatchProjection
from gateway.contracts import (
    BATCH_ASSIGNED_BLOCK_TYPE,
    BatchAssignRequest,
    project_batch,
)
from gateway.store import require_generic_block_type

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


def work_batch() -> dict[str, Any]:
    return {
        "index": 10,
        "parent_index": None,
        "block_type": "work_batch",
        "data": {"batch_id": "batch-1"},
        "status": "pending",
    }


def child(index: int, block_type: str, **data: Any) -> dict[str, Any]:
    return {
        "index": index,
        "parent_index": 10,
        "block_type": block_type,
        "data": {"batch_id": "batch-1", **data},
        "status": "recorded",
    }


def assigned(index: int, agent_id: str = "openclaw") -> dict[str, Any]:
    return child(index, BATCH_ASSIGNED_BLOCK_TYPE, agent_id=agent_id)


def claimed(index: int, agent_id: str | None = "openclaw") -> dict[str, Any]:
    data: dict[str, Any] = {
        "claim_id": f"claim-{index}",
        "fencing_token": 1,
        "claim_token_sha256": hashlib.sha256(b"token").hexdigest(),
        "claim_expires_at": (NOW + timedelta(minutes=30)).isoformat(),
    }
    if agent_id is not None:
        data["agent_id"] = agent_id
    return child(index, "batch_claimed", **data)


def test_assign_request_needs_a_valid_agent_id() -> None:
    assert BatchAssignRequest(agent_id="openclaw").agent_id == "openclaw"
    with pytest.raises(ValidationError):
        BatchAssignRequest.model_validate({"agent_id": "Not Valid"})


def test_assignment_is_gateway_owned() -> None:
    with pytest.raises(HTTPException) as refused:
        require_generic_block_type(BATCH_ASSIGNED_BLOCK_TYPE)
    assert refused.value.status_code == 422


def test_latest_assignment_is_projected_and_kept_through_the_claim() -> None:
    projection = project_batch(
        [work_batch(), assigned(11, "other"), assigned(12), claimed(13)], "batch-1", now=NOW
    )

    assert projection.status == "claimed"
    assert projection.assigned_agent_id == "openclaw"
    assert projection.claim_agent_id == "openclaw"


@pytest.mark.parametrize("claimer", ["other", None])
def test_claim_by_anyone_but_the_assigned_agent_is_invalid(claimer: str | None) -> None:
    with pytest.raises(ValueError, match="assigned agent"):
        project_batch([work_batch(), assigned(11), claimed(12, claimer)], "batch-1", now=NOW)


def test_batch_cannot_be_reassigned_while_claimed() -> None:
    with pytest.raises(ValueError, match="while claimed"):
        project_batch([work_batch(), assigned(11), claimed(12), assigned(13, "other")], "batch-1", now=NOW)


def test_delivery_client_reads_the_assignment() -> None:
    projection = project_batch([work_batch(), assigned(11)], "batch-1", now=NOW)

    parsed = GatewayBatchProjection.model_validate(projection.model_dump(mode="json"))

    assert parsed.assigned_agent_id == "openclaw"
