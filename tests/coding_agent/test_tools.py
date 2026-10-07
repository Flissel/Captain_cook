from datetime import datetime, timezone
from decimal import Decimal

import pytest
from autogen_core import CancellationToken

from agenten.coding_agent.contracts import AgentGoalResultV1, AcceptanceCheckV1, AcceptanceReportV1, AgentGoalRoundV1
from agenten.coding_agent.tools import (
    ClaudeCodeBuildArgs,
    ClaudeCodeBuildTool,
    CodingToolLimits,
    SandboxRunTestsArgs,
    SandboxRunTestsTool,
    WorkspaceCursor,
)
from tests.coding_agent.fakes import FakeSandbox, ok

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
LIMITS = CodingToolLimits(max_calls=2, max_rounds=3, max_turns_per_round=20, budget_usd=Decimal("2"), timeout_seconds=900)


class _Authority:
    def __init__(self, deny=False):
        self.deny = deny
        self.calls = []

    async def authorize(self, tool_name, *, now):
        self.calls.append(tool_name)
        if self.deny:
            raise PermissionError("grant revoked")


class _Tokens:
    async def issue(self, goal_id):
        return "tok-secret-" + goal_id


class _Sink:
    def __init__(self):
        self.results = []

    def record(self, result):
        self.results.append(result)


class _Runner:
    def __init__(self, status="achieved", final="img-new"):
        self.status = status
        self.final = final
        self.seen = []

    async def run(self, goal, *, workspace_image_id, run_token):
        self.seen.append((goal, workspace_image_id, run_token))

        # For "achieved" status, construct proper rounds with passing acceptance
        if self.status == "achieved":
            passing_check = AcceptanceCheckV1(
                command="pytest -q",
                exit_code=0,
                timed_out=False,
                passed=True,
                output_tail=""
            )
            acceptance = AcceptanceReportV1(checks=(passing_check,))
            round_result = AgentGoalRoundV1(
                round=1,
                input_image_id=workspace_image_id,
                output_image_id=self.final,
                session_id=None,
                result_subtype=None,
                num_turns=0,
                tool_call_count=0,
                exit_code=0,
                timed_out=False,
                journal_sha256="0" * 64,
                changed_paths=(),
                policy_violations=(),
                acceptance=acceptance
            )
            return AgentGoalResultV1(
                goal_id=goal.goal_id,
                status=self.status,
                rounds=(round_result,),
                final_image_id=self.final,
                failure_reason=None
            )
        else:
            # Non-achieved status can have empty rounds
            return AgentGoalResultV1(
                goal_id=goal.goal_id,
                status=self.status,
                rounds=(),
                final_image_id=self.final,
                failure_reason=None if self.status == "achieved" else "nope"
            )


def _build_tool(runner, authority=None, sink=None, cursor=None):
    return ClaudeCodeBuildTool(
        runner=runner, cursor=cursor or WorkspaceCursor("img-0"), authority=authority or _Authority(),
        tokens=_Tokens(), evidence=sink or _Sink(), limits=LIMITS, clock=lambda: NOW, goal_prefix="team-7",
    )


def _args():
    return ClaudeCodeBuildArgs(objective="add hello", acceptance_commands=["pytest -q"], allowed_paths=["src/"])


@pytest.mark.asyncio
async def test_build_tool_runs_goal_with_limits_and_advances_cursor():
    runner, sink, cursor = _Runner(), _Sink(), WorkspaceCursor("img-0")
    tool = _build_tool(runner, sink=sink, cursor=cursor)

    result = await tool.run(_args(), CancellationToken())

    goal, image, token = runner.seen[0]
    assert tool.name == "claude_code_build"
    assert goal.goal_id == "team-7-build-1"
    assert (goal.max_rounds, goal.max_turns_per_round, goal.timeout_seconds) == (3, 20, 900)
    assert goal.budget_usd == Decimal("2")
    assert image == "img-0"
    assert cursor.image_id == "img-new"
    assert result.status == "achieved"
    assert sink.results[0].goal_id == "team-7-build-1"
    assert "tok-secret" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_policy_violation_does_not_advance_cursor():
    cursor = WorkspaceCursor("img-0")
    tool = _build_tool(_Runner(status="policy_violation", final="img-bad"), cursor=cursor)
    result = await tool.run(_args(), CancellationToken())
    assert cursor.image_id == "img-0"
    assert result.status == "policy_violation"


@pytest.mark.asyncio
async def test_denied_authority_runs_nothing_and_does_not_count():
    runner = _Runner()
    tool = _build_tool(runner, authority=_Authority(deny=True))
    with pytest.raises(PermissionError):
        await tool.run(_args(), CancellationToken())
    assert runner.seen == []


@pytest.mark.asyncio
async def test_call_ceiling_is_enforced():
    tool = _build_tool(_Runner())
    await tool.run(_args(), CancellationToken())
    await tool.run(_args(), CancellationToken())
    with pytest.raises(RuntimeError, match="ceiling"):
        await tool.run(_args(), CancellationToken())


def test_build_args_reject_multiline_commands():
    with pytest.raises(ValueError):
        ClaudeCodeBuildArgs(objective="x", acceptance_commands=["a\nb"])


@pytest.mark.asyncio
async def test_run_tests_tool_runs_on_cursor_image():
    sandbox = FakeSandbox(lambda image, command: ok("ok", exit_code=0))
    cursor = WorkspaceCursor("img-5")
    authority = _Authority()
    tool = SandboxRunTestsTool(sandbox=sandbox, cursor=cursor, authority=authority, clock=lambda: NOW,
                               workdir="/work", timeout_seconds=120, max_calls=1)

    report = await tool.run(SandboxRunTestsArgs(commands=["pytest -q"]), CancellationToken())

    assert tool.name == "sandbox_run_tests"
    assert report.passed is True
    assert sandbox.calls[0][0] == "img-5"
    assert authority.calls == ["sandbox_run_tests"]
    with pytest.raises(RuntimeError, match="ceiling"):
        await tool.run(SandboxRunTestsArgs(commands=["pytest -q"]), CancellationToken())
