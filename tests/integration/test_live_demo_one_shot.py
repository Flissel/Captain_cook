from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from agenten.agent_factory.live_demo_one_shot import LiveDemoOneShot
from agenten.agent_factory.live_demo_runtime_chain import LiveDemoRuntimeChain
from agenten.agent_runtime.contracts import AgentRuntimeCommand, AgentRuntimeResult
from agenten.agent_runtime.gateway_client import RuntimeOperationProjection
from agenten.delivery.minibook_events import MinibookProjectionEvent
from tests.integration.test_live_demo_runtime_chain import (
    CORRELATION_ID,
    GatewayBackedRuntime,
    HermesWorker,
    NOW,
    _release,
)


class PersistingRuntime(GatewayBackedRuntime):
    result: AgentRuntimeResult | None = None

    async def execute(self, command: AgentRuntimeCommand) -> AgentRuntimeResult:
        self.result = await super().execute(command)
        return self.result


@dataclass
class GatewayState:
    runtime: PersistingRuntime

    async def get_runtime_operation(self, operation_id: UUID) -> RuntimeOperationProjection:
        command = self.runtime.commands[0]
        assert self.runtime.result is not None
        return RuntimeOperationProjection(
            operation_id=operation_id,
            command=command,
            result=self.runtime.result,
        )


@dataclass
class ProjectionFeed:
    runtime: PersistingRuntime

    async def events_for_correlation(
        self, correlation_id: UUID
    ) -> tuple[MinibookProjectionEvent, ...]:
        assert self.runtime.result is not None
        return (
            MinibookProjectionEvent.model_validate(
                {
                    "schema": "captain.minibook-projection.v2",
                    "event_id": str(uuid4()),
                    "correlation_id": str(correlation_id),
                    "causation_id": str(self.runtime.result.event_id),
                    "occurred_at": datetime.now(timezone.utc),
                    "producer": "captain-gateway",
                    "subject_id": f"subject:{uuid4()}",
                    "subject_version": 1,
                    "event_type": "codex.result",
                    "payload": {
                        "view": "build",
                        "template_id": "runtime_build_recorded",
                        "status_id": "built",
                        "actor_role_id": "captain_gateway",
                    },
                }
            ),
        )


@pytest.mark.asyncio
async def test_one_shot_proves_one_correlation_through_gateway_and_projection() -> None:
    runtime = PersistingRuntime()
    runner = LiveDemoOneShot(
        chain=LiveDemoRuntimeChain(
            hermes=HermesWorker(), runtime_service=runtime, clock=lambda: NOW
        ),
        runtime_state=GatewayState(runtime),
        projection_feed=ProjectionFeed(runtime),
    )

    summary = await runner.run(_release())

    assert summary.schema_name == "captain.live-demo-evidence.v1"
    assert summary.correlation_id == CORRELATION_ID
    assert summary.factory_dispatch_correlated is True
    assert summary.hermes_evidence_correlated is True
    assert summary.autogen_delivery_count == 1
    assert summary.gateway_runtime_persisted is True
    assert summary.minibook_projection_event_count == 1
    assert summary.runtime_status == "succeeded"
    assert "token" not in summary.model_dump_json().lower()


@pytest.mark.asyncio
async def test_one_shot_rejects_projection_not_caused_by_runtime_result() -> None:
    runtime = PersistingRuntime()

    class UnrelatedProjectionFeed(ProjectionFeed):
        async def events_for_correlation(
            self, correlation_id: UUID
        ) -> tuple[MinibookProjectionEvent, ...]:
            events = await super().events_for_correlation(correlation_id)
            return (events[0].model_copy(update={"causation_id": uuid4()}),)

    runner = LiveDemoOneShot(
        chain=LiveDemoRuntimeChain(
            hermes=HermesWorker(), runtime_service=runtime, clock=lambda: NOW
        ),
        runtime_state=GatewayState(runtime),
        projection_feed=UnrelatedProjectionFeed(runtime),
    )

    with pytest.raises(Exception, match="caused by the runtime result"):
        await runner.run(_release())
