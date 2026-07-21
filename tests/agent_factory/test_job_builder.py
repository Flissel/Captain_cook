from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from agenten.agent_factory.holdout_store import InMemoryPrivateHoldoutStore
from agenten.agent_factory.input_compiler import FactoryInputCompiler
from agenten.agent_factory.input_document import load_factory_input
from agenten.agent_factory.job_builder import build_factory_job


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
