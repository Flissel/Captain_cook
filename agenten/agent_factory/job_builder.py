"""Deterministic Captain Factory-job creation from compiled input."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid5

from agenten.agent_factory.contracts import AgentFactoryJobV2, AgentFactoryJobV3
from agenten.agent_factory.execution_policy import FactoryExecutionPolicyV1
from agenten.agent_factory.input_compiler import CompiledFactorySpecification
from agenten.agent_runtime.contracts import ArtifactRef


_JOB_NAMESPACE = UUID("9a5cf3fe-053b-4bf9-a1b1-aa5fc6dcd42e")


def build_factory_job(
    compiled: CompiledFactorySpecification,
    *,
    correlation_id: UUID,
    now: datetime,
    wall_clock_budget_seconds: int,
) -> AgentFactoryJobV2:
    if now.tzinfo is None or now.utcoffset() != timezone.utc.utcoffset(now):
        raise ValueError("factory job clock must be UTC")
    if isinstance(wall_clock_budget_seconds, bool) or wall_clock_budget_seconds < 1:
        raise ValueError("wall_clock_budget_seconds must be positive")
    source_digest = compiled.source_ref.sha256
    identity = f"factory-job|{correlation_id}|{source_digest}|{compiled.compilation_digest}|{compiled.subject_version}"
    job_id = uuid5(_JOB_NAMESPACE, identity)
    event_id = uuid5(correlation_id, identity)
    graph_payload = {
        "schema": "captain.factory-work-graph.v1",
        "source_sha256": source_digest,
        "nodes": [node.model_dump(mode="json") for node in compiled.work_nodes],
        "dependency_order": compiled.dependency_order,
    }
    graph_digest = hashlib.sha256(_canonical_json(graph_payload).encode()).hexdigest()
    return AgentFactoryJobV2(
        schema_name="captain.agent-factory-job.v2",
        event_id=event_id,
        correlation_id=correlation_id,
        occurred_at=now,
        producer="captain",
        job_id=job_id,
        subject_version=compiled.subject_version,
        input_ref=compiled.source_ref,
        compiled_spec_ref=ArtifactRef(uri=f"artifact://compiled-factory-spec/{compiled.compilation_digest}", sha256=compiled.compilation_digest, media_type="application/json"),
        dependency_graph_ref=ArtifactRef(uri=f"artifact://factory-work-graph/{graph_digest}", sha256=graph_digest, media_type="application/json"),
        required_capability=compiled.capability_key,
        acceptance_assertion_ids=compiled.assertion_ids,
        private_holdout_refs=compiled.private_holdout_refs,
        max_behavioral_iterations=5,
        deadline_at=now + timedelta(seconds=wall_clock_budget_seconds),
    )


def build_factory_job_v3(
    compiled: CompiledFactorySpecification,
    *,
    correlation_id: UUID,
    now: datetime,
    execution_policy: FactoryExecutionPolicyV1,
) -> AgentFactoryJobV3:
    v2 = build_factory_job(
        compiled,
        correlation_id=correlation_id,
        now=now,
        wall_clock_budget_seconds=execution_policy.max_runtime_seconds,
    )
    policy_payload = execution_policy.model_dump(mode="json", by_alias=True)
    policy_payload["max_cost_usd"] = _canonical_decimal(
        execution_policy.max_cost_usd
    )
    policy_json = _canonical_json(policy_payload)
    policy_digest = hashlib.sha256(policy_json.encode("utf-8")).hexdigest()
    identity = f"factory-job-v3|{v2.job_id}|{policy_digest}"
    payload = v2.model_dump(mode="json", by_alias=True)
    payload["schema"] = "captain.agent-factory-job.v3"
    payload["job_id"] = str(uuid5(_JOB_NAMESPACE, identity))
    payload["event_id"] = str(uuid5(correlation_id, identity))
    payload["execution_policy"] = policy_payload
    return AgentFactoryJobV3.model_validate(payload)


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _canonical_decimal(value: Decimal) -> str:
    if value == 0:
        return "0"
    rendered = format(value, "f")
    if "." in rendered:
        return rendered.rstrip("0").rstrip(".")
    return rendered
