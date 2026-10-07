from decimal import Decimal

import pytest
from pydantic import ValidationError

from agenten.coding_agent.contracts import (
    AcceptanceCheckV1,
    AcceptanceReportV1,
    AgentGoalResultV1,
    AgentGoalV1,
    path_allowed,
)


def _goal(**overrides):
    values = {
        "goal_id": "team-1-build-1",
        "objective": "Add a hello() function returning 'hi'.",
        "acceptance_commands": ("pytest -q tests/test_hello.py",),
        "allowed_paths": ("src/", "tests/test_hello.py"),
        "max_rounds": 3,
        "max_turns_per_round": 30,
        "budget_usd": Decimal("1.50"),
        "timeout_seconds": 900,
    }
    values.update(overrides)
    return AgentGoalV1(**values)


def test_goal_round_trips_with_schema_alias():
    goal = _goal()
    dumped = goal.model_dump(mode="json", by_alias=True)
    assert dumped["schema"] == "captain.agent-goal.v1"
    assert AgentGoalV1.model_validate(dumped) == goal


@pytest.mark.parametrize("command", ["", "  ", "pytest\nrm -rf /", "a\x00b"])
def test_acceptance_commands_must_be_single_non_empty_lines(command):
    with pytest.raises(ValidationError):
        _goal(acceptance_commands=(command,))


@pytest.mark.parametrize("path", ["/etc/passwd", "../outside", "src/../../x", "C:\\x"])
def test_allowed_paths_must_be_safe_relative(path):
    with pytest.raises(ValidationError):
        _goal(allowed_paths=(path,))


@pytest.mark.parametrize(
    "field,value",
    [("max_rounds", 0), ("max_rounds", 11), ("max_turns_per_round", 0), ("timeout_seconds", 59), ("budget_usd", Decimal("0"))],
)
def test_limits_are_bounded(field, value):
    with pytest.raises(ValidationError):
        _goal(**{field: value})


def test_goal_is_frozen():
    with pytest.raises(ValidationError):
        _goal().objective = "other"  # type: ignore[misc]


def test_path_allowed_matches_files_directories_and_dot():
    assert path_allowed("src/app.py", ("src/",))
    assert path_allowed("src/app.py", ("src",))
    assert path_allowed("tests/test_hello.py", ("tests/test_hello.py",))
    assert not path_allowed("srcx/app.py", ("src",))
    assert not path_allowed("tests/test_other.py", ("tests/test_hello.py",))
    assert path_allowed("anything/at/all.txt", (".",))


def test_report_passed_and_failing():
    ok = AcceptanceCheckV1(command="a", exit_code=0, timed_out=False, passed=True, output_tail="")
    bad = AcceptanceCheckV1(command="b", exit_code=1, timed_out=False, passed=False, output_tail="E")
    report = AcceptanceReportV1(checks=(ok, bad))
    assert report.passed is False
    assert report.failing() == (bad,)
    assert AcceptanceReportV1(checks=(ok,)).passed is True


def test_check_passed_must_match_exit_code():
    with pytest.raises(ValidationError):
        AcceptanceCheckV1(command="a", exit_code=1, timed_out=False, passed=True, output_tail="")


def test_result_requires_known_status():
    with pytest.raises(ValidationError):
        AgentGoalResultV1(goal_id="g-1", status="done", rounds=(), final_image_id="img", failure_reason=None)
