"""`POST /blocks` accepts only an explicit allow-list of block types.

Every other ledger block type (delivery events, runtime commands/grants,
factory evidence, ...) has a dedicated route with its own validation. A
deny-list let a worker holding a live claim forge those types generically.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from gateway.store import (
    CAPTAIN_BLOCK_TYPES,
    GATEWAY_OWNED_EVENT_TYPES,
    WORKER_EVIDENCE_BLOCK_TYPES,
    require_generic_block_type,
)


@pytest.mark.parametrize(
    "block_type",
    sorted(CAPTAIN_BLOCK_TYPES | WORKER_EVIDENCE_BLOCK_TYPES),
)
def test_allow_listed_block_types_pass(block_type: str) -> None:
    require_generic_block_type(block_type)


def test_worker_evidence_types_are_exactly_the_delivery_worker_outputs() -> None:
    assert WORKER_EVIDENCE_BLOCK_TYPES == {
        "codex_session",
        "codex_process",
        "reasoning_slice",
        "validation_run",
        "batch_done",
    }


@pytest.mark.parametrize(
    "block_type",
    [
        "delivery_event",
        "agent_runtime_command",
        "agent_runtime_grant",
        "agent_runtime_grant_revocation",
        "agent_runtime_result",
        "agent_factory_block",
        "validated_capability",
        "made_up_type",
    ],
)
def test_types_with_dedicated_routes_or_unknown_are_rejected(block_type: str) -> None:
    with pytest.raises(HTTPException) as excinfo:
        require_generic_block_type(block_type)

    assert excinfo.value.status_code == 422
    assert excinfo.value.detail == f"{block_type} is not writable through /blocks"


@pytest.mark.parametrize("block_type", sorted(GATEWAY_OWNED_EVENT_TYPES))
def test_gateway_owned_types_keep_their_dedicated_route_message(block_type: str) -> None:
    with pytest.raises(HTTPException) as excinfo:
        require_generic_block_type(block_type)

    assert excinfo.value.status_code == 422
    assert excinfo.value.detail == f"{block_type} must use its dedicated gateway route"
