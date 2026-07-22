"""Regression coverage for generated runtime and lifecycle contracts."""

import time

import pytest

from minibook.swarm.knowledge import GENERIC_MAIN_PY
from minibook.swarm.pipeline import SwarmPipeline, _safe_output_evaluation_task


def test_generated_runtime_honors_project_message_limit() -> None:
    """A project.yml limit must not be silently expanded by the loader."""
    assert "MaxMessageTermination(term_val)" in GENERIC_MAIN_PY
    assert "max(term_val, 50)" not in GENERIC_MAIN_PY


def test_generated_runtime_supports_offline_package_validation() -> None:
    """Package assembly must validate generated entrypoints without an LLM call."""
    assert 'os.environ.get("CAPTAIN_PACKAGE_VALIDATE") == "1"' in GENERIC_MAIN_PY


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


@pytest.mark.asyncio
async def test_feedback_loop_reuses_passing_output_evaluation() -> None:
    """A passed evaluation is valid evidence; it must not trigger duplicate live runs."""
    pipeline = SwarmPipeline({}, "project", "task", interactive=False)
    pipeline.start_time = time.time()
    pipeline.output_eval = {"status": "PASS", "score": "8"}

    await pipeline.step_feedback_loop(session=object())

    assert "FeedbackAgent" in pipeline.completed_steps


def test_output_evaluation_task_replaces_remote_fixture_with_self_contained_work() -> None:
    """A provider-created evaluation must not depend on an invented remote API."""
    task = _safe_output_evaluation_task(
        'Fetch https://example.com/data.csv, analyze it, and write a report.',
        "Enterprise sales pipeline briefing team",
    )

    assert "https://" not in task
    assert "Enterprise sales pipeline briefing team" in task
    assert "self-contained" in task.lower()


def test_output_evaluation_task_replaces_unbound_api_endpoint_work() -> None:
    """An API mentioned without supplied data is as unavailable as a remote URL."""
    task = _safe_output_evaluation_task(
        "Collect data from the provided API endpoint and analyze it.",
        "Renewal orchestration team",
    )

    assert "api endpoint" not in task.lower()
    assert "Renewal orchestration team" in task
