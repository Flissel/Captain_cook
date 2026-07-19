"""Deterministic Captain factory-job creation from the canonical input."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid5

from agenten.agent_factory.contracts import AgentFactoryJob
from agenten.agent_factory.input_document import FactoryInputDocument


_JOB_NAMESPACE = UUID("9a5cf3fe-053b-4bf9-a1b1-aa5fc6dcd42e")


def build_factory_job(
    document: FactoryInputDocument,
    *,
    correlation_id: UUID,
    now: datetime,
    subject_version: int = 1,
) -> AgentFactoryJob:
    """Create one reproducible job whose assertions cover every input requirement."""

    if now.tzinfo is None or now.utcoffset() != timezone.utc.utcoffset(now):
        raise ValueError("factory job clock must be UTC")
    digest = document.input_ref.sha256
    job_id = uuid5(_JOB_NAMESPACE, f"factory-job|{digest}|{subject_version}")
    event_id = uuid5(correlation_id, f"factory-job|{digest}|{subject_version}")
    return AgentFactoryJob(
        schema_name="captain.agent-factory-job.v1",
        event_id=event_id,
        correlation_id=correlation_id,
        occurred_at=now,
        producer="captain",
        job_id=job_id,
        subject_version=subject_version,
        input_ref=document.input_ref,
        required_capability="autogen_agent_factory",
        acceptance_assertion_ids=tuple(
            [f"output-{index:02d}" for index, _ in enumerate(document.required_outputs, start=1)]
            + [f"quality-gate-{index:02d}" for index, _ in enumerate(document.quality_gates, start=1)]
        ),
        max_behavioral_iterations=5,
    )
