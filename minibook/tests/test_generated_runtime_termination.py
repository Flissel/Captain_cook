"""Regression coverage for generated runtime and lifecycle contracts."""

import time

import pytest

from minibook.swarm.knowledge import AUTOGEN_PATTERNS, GENERIC_MAIN_PY
from minibook.swarm.pipeline import SwarmPipeline, _safe_output_evaluation_task


def test_generated_runtime_honors_project_message_limit() -> None:
    """A project.yml limit must not be silently expanded by the loader."""
    assert "MaxMessageTermination(term_val)" in GENERIC_MAIN_PY
    assert "max(term_val, 50)" not in GENERIC_MAIN_PY


def test_generated_runtime_supports_offline_package_validation() -> None:
    """Package assembly must validate generated entrypoints without an LLM call."""
    assert 'os.environ.get("CAPTAIN_PACKAGE_VALIDATE") == "1"' in GENERIC_MAIN_PY


def test_generated_tools_do_not_write_during_sandbox_import() -> None:
    """Read-only candidate validation may import tools but must not invoke them."""
    assert "def _output_dir()" in AUTOGEN_PATTERNS
    assert 'OUTPUT_DIR.mkdir(exist_ok=True)' not in AUTOGEN_PATTERNS


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


@pytest.mark.asyncio
async def test_feedback_loop_reexecutes_and_reevaluates_an_improved_candidate(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline = SwarmPipeline({}, "project", "task", interactive=False)
    pipeline.start_time = time.time()
    pipeline.output_path = str(tmp_path)
    pipeline.build_dir = tmp_path
    failed_test_task = "Create the required task-specific output files."
    pipeline.output_eval = {
        "status": "FAIL",
        "score": "4",
        "test_task": failed_test_task,
    }
    calls: list[str] = []
    reevaluation_tasks: list[str | None] = []

    async def no_network(*_args, **_kwargs):
        return None

    async def score(*_args, **_kwargs) -> str:
        return "SCORE: 3\nIMPROVEMENTS: use the exact task requirements"

    async def rebuilt(_session: object) -> None:
        calls.append("builder")
        pipeline.build_result = {"status": "PASS"}

    async def rerun(_session: object) -> None:
        calls.append("executor")
        pipeline.run_result = {"status": "PASS"}

    async def reevaluate(
        _session: object, *, test_task_override: str | None = None
    ) -> None:
        calls.append("output_evaluation")
        reevaluation_tasks.append(test_task_override)
        pipeline.output_eval = {"status": "PASS", "score": "8"}

    async def run_feedback_test(*_args, **_kwargs) -> dict[str, str]:
        return {"logs": "ok"}

    pipeline.post_as = no_network
    pipeline.comment_as = no_network
    pipeline.step_coder = no_network
    pipeline.step_reviewer = no_network
    pipeline.step_validator = no_network
    pipeline.step_builder = rebuilt
    pipeline.step_executor = rerun
    pipeline.step_output_eval = reevaluate
    monkeypatch.setattr("minibook.swarm.pipeline.call_gpt4o", score)
    monkeypatch.setattr(
        "minibook.swarm.pipeline.docker_run_test_with_args",
        run_feedback_test,
    )

    await pipeline.step_feedback_loop(session=object())

    assert calls == ["builder", "executor", "output_evaluation"]
    assert reevaluation_tasks == [failed_test_task]
    assert pipeline.output_eval["status"] == "PASS"


@pytest.mark.asyncio
async def test_feedback_loop_promotes_a_passing_executed_feedback_run(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline = SwarmPipeline({}, "project", "task", interactive=False)
    pipeline.start_time = time.time()
    pipeline.output_path = str(tmp_path)
    pipeline.build_dir = tmp_path
    pipeline.output_eval = {"status": "FAIL", "score": "4"}

    async def no_network(*_args, **_kwargs):
        return None

    async def passing_score(*_args, **_kwargs) -> str:
        return "SCORE: 8\nVERDICT: PASS"

    async def passing_run(*_args, **_kwargs) -> dict[str, object]:
        return {"status": "PASS", "duration": 1.0, "logs": "ok"}

    pipeline.post_as = no_network
    pipeline.comment_as = no_network
    monkeypatch.setattr("minibook.swarm.pipeline.call_gpt4o", passing_score)
    monkeypatch.setattr(
        "minibook.swarm.pipeline.docker_run_test_with_args", passing_run
    )

    await pipeline.step_feedback_loop(session=object())

    assert pipeline.output_eval["status"] == "PASS"
    assert pipeline.output_eval["score"] == "8"
    assert pipeline.output_eval["eval_run_status"] == "PASS"


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
