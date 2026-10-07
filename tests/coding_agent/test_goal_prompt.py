from decimal import Decimal

from agenten.coding_agent.contracts import AcceptanceCheckV1, AcceptanceReportV1, AgentGoalV1
from agenten.coding_agent.goal_prompt import render_goal_prompt, render_resume_prompt


def _goal():
    return AgentGoalV1(
        goal_id="g-1",
        objective="Add hello() returning 'hi' and cost $5 docs",
        acceptance_commands=("pytest -q tests/test_hello.py", "ruff check src"),
        constraints=("Keep public API stable",),
        allowed_paths=("src/", "tests/test_hello.py"),
        max_rounds=2,
        max_turns_per_round=20,
        budget_usd=Decimal("1"),
        timeout_seconds=600,
    )


def test_goal_prompt_lists_objective_commands_constraints_paths():
    text = render_goal_prompt(_goal(), workdir="/work")
    assert "Add hello() returning 'hi' and cost $5 docs" in text
    assert "- `pytest -q tests/test_hello.py`" in text
    assert "- `ruff check src`" in text
    assert "- Keep public API stable" in text
    assert "- `src/`" in text
    assert "/work" in text
    assert "until every acceptance command passes" in text


def test_goal_prompt_without_constraints_says_none():
    goal = _goal().model_copy(update={"constraints": ()})
    assert "- (none)" in render_goal_prompt(goal, workdir="/work")


def test_resume_prompt_lists_only_failing_checks_with_tails():
    report = AcceptanceReportV1(checks=(
        AcceptanceCheckV1(command="pytest -q a", exit_code=0, timed_out=False, passed=True, output_tail="fine"),
        AcceptanceCheckV1(command="ruff check src", exit_code=1, timed_out=False, passed=False, output_tail="E501 line too long"),
        AcceptanceCheckV1(command="pytest -q slow", exit_code=0, timed_out=True, passed=False, output_tail=""),
    ))
    text = render_resume_prompt(report)
    assert "pytest -q a" not in text
    assert "`ruff check src` exited 1" in text
    assert "E501 line too long" in text
    assert "`pytest -q slow` timed out" in text
