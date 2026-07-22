"""Regression coverage for generated runtime and lifecycle contracts."""

import time

import pytest

from minibook.swarm.knowledge import GENERIC_MAIN_PY
from minibook.swarm.pipeline import SwarmPipeline


def test_generated_runtime_honors_project_message_limit() -> None:
    """A project.yml limit must not be silently expanded by the loader."""
    assert "MaxMessageTermination(term_val)" in GENERIC_MAIN_PY
    assert "max(term_val, 50)" not in GENERIC_MAIN_PY


@pytest.mark.asyncio
async def test_todo_implementation_completes_when_no_todo_tools_exist(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A no-op TODO scan is a valid completed lifecycle checkpoint."""
    pipeline = SwarmPipeline({}, "project", "task", interactive=False)
    pipeline.start_time = time.time()
    pipeline.build_dir = tmp_path
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "tools.py").write_text("async def ready():\n    return 'ok'\n")

    async def no_todos(_: str) -> list[object]:
        return []

    monkeypatch.setattr("minibook.swarm.pipeline.scan_todo_tools", no_todos)

    await pipeline.step_todo_implement(session=object())

    assert "TodoImplementer" in pipeline.completed_steps
