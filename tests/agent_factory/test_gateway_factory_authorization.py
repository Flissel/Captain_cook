from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any

import pytest
from fastapi import HTTPException
from agenten.agent_factory.contracts import FactoryPhase, FactoryRole
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_factory.state_machine import FactoryProjection, apply_block
from gateway.store import GatewayStore
from tests.agent_factory.test_state_machine import block, job


class _ProjectionCursor:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.sql = ""

    def execute(self, sql: str, _: tuple[str, ...]) -> None:
        self.sql = sql

    def fetchall(self) -> list[dict[str, Any]]:
        if "children" not in self.sql:
            return [{"data": json.dumps(self._payload)}]
        return [
            {
                "index": 1,
                "parent_index": 0,
                "block_type": "agent_factory_block",
                "data": json.dumps(self._payload),
                "status": "succeeded",
                "children": "[]",
                "metadata": "{}",
                "hash": "a" * 64,
                "previous_hash": "0",
            }
        ]


class _ProjectionStorage:
    @staticmethod
    def _decode_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "index": row["index"],
            "parent_index": row["parent_index"],
            "block_type": row["block_type"],
            "data": json.loads(row["data"]),
            "status": row["status"],
            "children": json.loads(row["children"]),
            "metadata": json.loads(row["metadata"]),
            "hash": row["hash"],
            "previous_hash": row["previous_hash"],
        }


def test_tool_integrator_lease_is_authorized_for_build_validation() -> None:
    factory_job = job()
    projection = FactoryProjection.from_job(factory_job)
    for evidence in (
        block(FactoryPhase.FORGE_REQUESTED),
        block(FactoryPhase.BLUEPRINT_CREATED),
        block(FactoryPhase.TOOL_CANDIDATE_TESTED),
        block(FactoryPhase.AGENT_CODE_CREATED),
    ):
        projection = apply_block(projection, evidence)
    lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.TOOL_INTEGRATOR,
        attempt=1,
        workspace_ref="workspace://factory/support-triage",
        now=datetime(2026, 7, 19, 10, tzinfo=timezone.utc),
    )

    GatewayStore._assert_lease_is_next_action(lease, projection)


def test_tool_integrator_lease_is_authorized_before_forge_submission() -> None:
    """The code-producing tool lease is issued before the asynchronous Forge handoff."""

    factory_job = job()
    projection = FactoryProjection.from_job(factory_job)
    for evidence in (
        block(FactoryPhase.FORGE_REQUESTED),
        block(FactoryPhase.BLUEPRINT_CREATED),
        block(FactoryPhase.TOOL_CANDIDATE_TESTED),
    ):
        projection = apply_block(projection, evidence)
    lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.TOOL_INTEGRATOR,
        attempt=1,
        workspace_ref="workspace://factory/support-triage",
        now=datetime(2026, 7, 19, 10, tzinfo=timezone.utc),
    )

    GatewayStore._assert_lease_is_next_action(lease, projection)


def test_factory_projection_loads_complete_ledger_rows() -> None:
    """Factory reads need all ledger fields required by the shared decoder."""

    store = GatewayStore.__new__(GatewayStore)
    store.storage = _ProjectionStorage()
    evidence = block(FactoryPhase.FORGE_REQUESTED)
    cursor = _ProjectionCursor(evidence.model_dump(mode="json", by_alias=True))

    assert store._factory_blocks(cursor, job().job_id) == (evidence,)


def test_factory_lease_projection_loads_complete_ledger_rows() -> None:
    store = GatewayStore.__new__(GatewayStore)
    store.storage = _ProjectionStorage()
    lease = issue_factory_lease(
        job=job(),
        role=FactoryRole.AGENT_ARCHITECT,
        attempt=1,
        workspace_ref="workspace://factory/support-triage",
        now=datetime(2026, 7, 19, 10, tzinfo=timezone.utc),
    )
    cursor = _ProjectionCursor(lease.model_dump(mode="json", by_alias=True))

    assert store._factory_leases(cursor, job().job_id) == (lease,)


def test_hermes_evidence_requires_its_persisted_matching_lease() -> None:
    evidence = block(FactoryPhase.BLUEPRINT_CREATED)

    with pytest.raises(HTTPException, match="matching active factory lease"):
        GatewayStore._assert_evidence_lease(evidence, None)
