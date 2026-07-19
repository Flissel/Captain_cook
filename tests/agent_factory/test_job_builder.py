from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from agenten.agent_factory.input_document import load_factory_input
from agenten.agent_factory.job_builder import build_factory_job


def test_factory_job_traces_every_required_output_and_quality_gate() -> None:
    document = load_factory_input(Path(__file__).parents[2] / "input.md")
    correlation_id = UUID("00000000-0000-0000-0000-000000000099")

    job = build_factory_job(
        document,
        correlation_id=correlation_id,
        now=datetime(2026, 7, 19, 12, tzinfo=timezone.utc),
    )

    assert job.correlation_id == correlation_id
    assert job.required_capability == "autogen_agent_factory"
    assert job.acceptance_assertion_ids == (
        "output-01", "output-02", "output-03", "output-04", "output-05", "output-06",
        "quality-gate-01", "quality-gate-02", "quality-gate-03", "quality-gate-04", "quality-gate-05", "quality-gate-06",
    )
