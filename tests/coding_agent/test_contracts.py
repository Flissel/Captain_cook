from decimal import Decimal

import pytest
from pydantic import ValidationError

from agenten.coding_agent.contracts import (
    AcceptanceCheckV1,
    AcceptanceReportV1,
    AgentGoalResultV1,
    AgentGoalRoundV1,
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


# Tests for path_allowed hardening (Issue 1)
@pytest.mark.parametrize("path", [
    "src/../etc/x",      # traversal
    "/etc/passwd",       # absolute
    "src/a\\b",          # backslash
    "src/a\x00b",        # NUL
    "src/a\nb",          # newline
    "src/a\rb",          # CR
])
def test_path_allowed_rejects_unsafe_paths(path):
    assert not path_allowed(path, ("src/",)), f"Should reject unsafe path: {repr(path)}"


# Tests for _safe_relative hardening (Issue 2)
@pytest.mark.parametrize("path", [
    "src//x",            # double slash
    "src/./x",           # dot segment
    "~/x",               # tilde at start
    "src/a\x00b",        # NUL
    "src/a\nb",          # newline
    "src/a\rb",          # CR
])
def test_safe_relative_rejects_unsafe_entries(path):
    with pytest.raises(ValidationError):
        _goal(allowed_paths=(path,))


def test_safe_relative_normalizes_leading_dotslash():
    # "./src" should be normalized to "src" and match "src/a.py"
    goal = _goal(allowed_paths=("./src",))
    assert goal.allowed_paths == ("src",), "Leading ./ should be normalized away"
    assert path_allowed("src/a.py", goal.allowed_paths)


def _make_round(passed: bool) -> "AgentGoalRoundV1":
    """Helper to create a round with acceptance."""
    ok_check = AcceptanceCheckV1(command="test", exit_code=0 if passed else 1, timed_out=False, passed=passed, output_tail="")
    report = AcceptanceReportV1(checks=(ok_check,))
    return AgentGoalRoundV1(
        round=1,
        input_image_id="img-in",
        output_image_id="img-out",
        num_turns=5,
        tool_call_count=3,
        exit_code=0,
        timed_out=False,
        journal_sha256="a" * 64,
        acceptance=report,
    )


# Tests for AgentGoalResultV1 achieved invariant (Issue 3)
def test_achieved_requires_non_empty_rounds():
    with pytest.raises(ValidationError):
        AgentGoalResultV1(
            goal_id="g-1",
            status="achieved",
            rounds=(),  # empty rounds
            final_image_id="img",
            failure_reason=None,
        )


def test_achieved_requires_passing_last_acceptance():
    failing_round = _make_round(passed=False)
    with pytest.raises(ValidationError):
        AgentGoalResultV1(
            goal_id="g-1",
            status="achieved",
            rounds=(failing_round,),
            final_image_id="img",
            failure_reason=None,
        )


def test_achieved_requires_no_failure_reason():
    passing_round = _make_round(passed=True)
    with pytest.raises(ValidationError):
        AgentGoalResultV1(
            goal_id="g-1",
            status="achieved",
            rounds=(passing_round,),
            final_image_id="img",
            failure_reason="some reason",  # should be None
        )


def test_achieved_requires_acceptance_not_none():
    # Create a round without acceptance
    round_no_accept = AgentGoalRoundV1(
        round=1,
        input_image_id="img-in",
        output_image_id="img-out",
        num_turns=5,
        tool_call_count=3,
        exit_code=0,
        timed_out=False,
        journal_sha256="a" * 64,
        acceptance=None,  # No acceptance
    )
    with pytest.raises(ValidationError):
        AgentGoalResultV1(
            goal_id="g-1",
            status="achieved",
            rounds=(round_no_accept,),
            final_image_id="img",
            failure_reason=None,
        )


def test_achieved_with_passing_round_is_valid():
    passing_round = _make_round(passed=True)
    result = AgentGoalResultV1(
        goal_id="g-1",
        status="achieved",
        rounds=(passing_round,),
        final_image_id="img",
        failure_reason=None,
    )
    assert result.status == "achieved"
    assert result.rounds[0].acceptance is not None
    assert result.rounds[0].acceptance.passed


def test_other_statuses_ignore_achieved_constraints():
    # Non-achieved statuses should allow empty rounds, failing acceptance, etc.
    failing_round = _make_round(passed=False)

    # This should NOT raise
    result = AgentGoalResultV1(
        goal_id="g-1",
        status="exhausted",
        rounds=(failing_round,),
        final_image_id="img",
        failure_reason="exhausted budget",
    )
    assert result.status == "exhausted"


# Tests for path entry length limit (Issue 2 - Finding 2)
def test_safe_relative_rejects_entries_longer_than_500_chars():
    # Entry with 501 characters should be rejected
    long_path = "a" * 501
    with pytest.raises(ValidationError):
        _goal(allowed_paths=(long_path,))


def test_safe_relative_accepts_exactly_500_char_entry():
    # Entry with exactly 500 characters should be accepted
    path_500 = "a" * 500
    goal = _goal(allowed_paths=(path_500,))
    assert goal.allowed_paths == (path_500,)
