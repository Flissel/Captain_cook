from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from agenten.agent_factory.execution_policy import FactoryExecutionPolicyV1
from agenten.agent_factory.holdout_store import InMemoryPrivateHoldoutStore
from agenten.agent_factory.input_compiler import FactoryInputCompiler
from agenten.agent_factory.input_document import load_factory_input
from agenten.agent_factory.job_builder import build_factory_job, build_factory_job_v3


FIXTURE = Path(__file__).parents[1] / "fixtures" / "agent_factory" / "TO_BE_BUILT.valid.md"


def compiled(tmp_path: Path):
    path = tmp_path / "TO_BE_BUILT.md"
    path.write_bytes(FIXTURE.read_bytes())
    return FactoryInputCompiler(holdout_store=InMemoryPrivateHoldoutStore()).compile(load_factory_input(path), 1)


def test_factory_job_v2_binds_compiled_artifacts_and_replays_identically(tmp_path: Path) -> None:
    specification = compiled(tmp_path)
    correlation_id = UUID("00000000-0000-0000-0000-000000000099")
    now = datetime(2026, 7, 21, 12, tzinfo=timezone.utc)
    first = build_factory_job(specification, correlation_id=correlation_id, now=now, wall_clock_budget_seconds=900)
    second = build_factory_job(specification, correlation_id=correlation_id, now=now, wall_clock_budget_seconds=900)

    assert first == second
    assert first.schema_name == "captain.agent-factory-job.v2"
    assert first.input_ref == specification.source_ref
    assert first.required_capability == specification.capability_key
    assert first.acceptance_assertion_ids == specification.assertion_ids
    assert first.private_holdout_refs == specification.private_holdout_refs
    assert first.deadline_at == datetime(2026, 7, 21, 12, 15, tzinfo=timezone.utc)
    assert first.compiled_spec_ref.sha256 == specification.compilation_digest
    assert first.dependency_graph_ref.sha256 != first.compiled_spec_ref.sha256


def release_policy(*, max_cost_usd: str = "5.00") -> FactoryExecutionPolicyV1:
    return FactoryExecutionPolicyV1.model_validate(
        {
            "schema": "captain.factory-execution-policy.v1",
            "mode": "release",
            "live_execution": True,
            "max_cost_usd": max_cost_usd,
            "max_runtime_seconds": 900,
            "required_live_runs": 3,
            "allowed_models": ["approved-model-id"],
            "live_capabilities": ["model.invoke"],
            "sandbox_mode": "workspace_write",
        }
    )


def test_factory_job_v3_has_byte_stable_policy_bound_identity(tmp_path: Path) -> None:
    specification = compiled(tmp_path)
    correlation_id = UUID("00000000-0000-0000-0000-000000000099")
    now = datetime(2026, 7, 21, 12, tzinfo=timezone.utc)
    policy = release_policy()

    first = build_factory_job_v3(
        specification,
        correlation_id=correlation_id,
        now=now,
        execution_policy=policy,
    )
    second = build_factory_job_v3(
        specification,
        correlation_id=correlation_id,
        now=now,
        execution_policy=policy,
    )

    first_bytes = json.dumps(
        first.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    second_bytes = json.dumps(
        second.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert first_bytes == second_bytes
    assert first.schema_name == "captain.agent-factory-job.v3"
    assert first.deadline_at == now.replace(minute=15)
    assert first.execution_policy == policy


def test_factory_job_v3_identity_changes_with_execution_policy(tmp_path: Path) -> None:
    specification = compiled(tmp_path)
    correlation_id = UUID("00000000-0000-0000-0000-000000000099")
    now = datetime(2026, 7, 21, 12, tzinfo=timezone.utc)

    first = build_factory_job_v3(
        specification,
        correlation_id=correlation_id,
        now=now,
        execution_policy=release_policy(max_cost_usd="5.00"),
    )
    changed = build_factory_job_v3(
        specification,
        correlation_id=correlation_id,
        now=now,
        execution_policy=release_policy(max_cost_usd="6.00"),
    )

    assert first.job_id != changed.job_id
    assert first.event_id != changed.event_id
