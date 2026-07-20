"""One-shot evidence coordinator across Factory, runtime Gateway, and projection feed."""

from __future__ import annotations

from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agenten.agent_factory.live_demo_runtime_chain import (
    LiveDemoRuntimeChain,
    LiveDemoRuntimeRelease,
)
from agenten.agent_runtime.contracts import RuntimeStatus
from agenten.agent_runtime.gateway_client import RuntimeOperationProjection
from agenten.delivery.minibook_events import MinibookProjectionEvent


class RuntimeOperationPort(Protocol):
    async def get_runtime_operation(
        self, operation_id: UUID
    ) -> RuntimeOperationProjection: ...


class ProjectionFeedPort(Protocol):
    async def events_for_correlation(
        self, correlation_id: UUID
    ) -> tuple[MinibookProjectionEvent, ...]: ...


class LiveDemoEvidenceError(RuntimeError):
    """Authoritative state or projection evidence diverged from the release."""


class LiveDemoEvidenceSummary(BaseModel):
    """Redacted machine-readable result consumed by the demo runner."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_name: Literal["captain.live-demo-evidence.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    correlation_id: UUID
    command_id: UUID
    factory_job_id: UUID
    hermes_evidence_id: UUID
    factory_dispatch_correlated: bool
    hermes_evidence_correlated: bool
    autogen_delivery_count: int = Field(ge=1)
    gateway_runtime_persisted: bool
    runtime_status: RuntimeStatus
    minibook_projection_event_count: int = Field(ge=1)
    minibook_projection_event_ids: tuple[UUID, ...]


class LiveDemoOneShot:
    """Run the real relay, then require Gateway and projection-feed evidence."""

    def __init__(
        self,
        *,
        chain: LiveDemoRuntimeChain,
        runtime_state: RuntimeOperationPort,
        projection_feed: ProjectionFeedPort,
    ) -> None:
        self._chain = chain
        self._runtime_state = runtime_state
        self._projection_feed = projection_feed

    async def run(self, release: LiveDemoRuntimeRelease) -> LiveDemoEvidenceSummary:
        chain_result = await self._chain.run(release)
        operation = await self._runtime_state.get_runtime_operation(
            chain_result.command.event_id
        )
        if operation.command != chain_result.command:
            raise LiveDemoEvidenceError("Gateway command does not match Factory release")
        if operation.result != chain_result.runtime_result:
            raise LiveDemoEvidenceError("Gateway result does not match AutoGen runtime result")
        events = await self._projection_feed.events_for_correlation(
            chain_result.correlation_id
        )
        if not events:
            raise LiveDemoEvidenceError("Gateway projection feed has no correlated event")
        if any(event.correlation_id != chain_result.correlation_id for event in events):
            raise LiveDemoEvidenceError("projection feed returned a foreign correlation")
        caused_results = tuple(
            event
            for event in events
            if event.event_type == "codex.result"
            and event.causation_id == chain_result.runtime_result.event_id
        )
        if not caused_results:
            raise LiveDemoEvidenceError(
                "projection feed event was not caused by the runtime result"
            )

        dispatch = release.dispatch
        return LiveDemoEvidenceSummary(
            schema_name="captain.live-demo-evidence.v1",
            correlation_id=chain_result.correlation_id,
            command_id=chain_result.command.event_id,
            factory_job_id=dispatch.job.job_id,
            hermes_evidence_id=chain_result.hermes_evidence.event_id,
            factory_dispatch_correlated=(
                dispatch.job.correlation_id == chain_result.correlation_id
            ),
            hermes_evidence_correlated=(
                chain_result.hermes_evidence.correlation_id
                == chain_result.correlation_id
            ),
            autogen_delivery_count=chain_result.autogen_delivery_count,
            gateway_runtime_persisted=True,
            runtime_status=chain_result.runtime_result.status,
            minibook_projection_event_count=len(caused_results),
            minibook_projection_event_ids=tuple(
                event.event_id for event in caused_results
            ),
        )
