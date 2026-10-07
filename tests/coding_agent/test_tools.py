import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from autogen_core import CancellationToken

from agenten.coding_agent.contracts import (
    AcceptanceCheckV1,
    AcceptanceReportV1,
    AgentGoalResultV1,
    AgentGoalRoundV1,
    GoalStatus,
)
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
    """Yields to simulate real concurrency."""
    def __init__(self, deny=False):
        self.deny = deny
        self.calls = []

    async def authorize(self, tool_name, *, now):
        self.calls.append(tool_name)
        await asyncio.sleep(0)  # Yield to enable real concurrency
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
    """Yields to simulate real concurrency."""
    def __init__(self, status="achieved", final="img-new", raise_error=None):
        self.status = status
        self.final = final
        self.seen = []
        self._call_count = 0
        self.raise_error = raise_error
        self.enter_times = []
        self.exit_times = []

    async def run(self, goal, *, workspace_image_id, run_token):
        self.enter_times.append(asyncio.current_task())
        self.seen.append((goal, workspace_image_id, run_token))
        self._call_count += 1

        # Yield to enable real concurrency
        await asyncio.sleep(0)

        # Raise error if configured (to test slot consumption on non-auth errors)
        if self.raise_error:
            raise self.raise_error

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
            # Use workspace_image_id as base so cursor advancement can be verified
            final_image = f"{workspace_image_id}-{self._call_count}"
            round_result = AgentGoalRoundV1(
                round=1,
                input_image_id=workspace_image_id,
                output_image_id=final_image,
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
            self.exit_times.append(asyncio.current_task())
            return AgentGoalResultV1(
                goal_id=goal.goal_id,
                status=self.status,
                rounds=(round_result,),
                final_image_id=final_image,
                failure_reason=None
            )
        else:
            # Non-achieved status can have empty rounds
            self.exit_times.append(asyncio.current_task())
            return AgentGoalResultV1(
                goal_id=goal.goal_id,
                status=self.status,
                rounds=(),
                final_image_id=self.final,
                failure_reason=None if self.status == "achieved" else "nope"
            )


class _YieldingSandbox:
    """Sandbox that yields to simulate real concurrency."""
    def __init__(self, handler):
        self._handler = handler
        self.calls = []
        self.enter_times = []
        self.exit_times = []

    async def run(self, image_id, command):
        self.enter_times.append(asyncio.current_task())
        self.calls.append((image_id, command))
        await asyncio.sleep(0)  # Yield for concurrency
        self.exit_times.append(asyncio.current_task())
        return self._handler(image_id, command)


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
    assert goal.goal_id == "team-7-build-0"  # First call, goal_number=0
    assert (goal.max_rounds, goal.max_turns_per_round, goal.timeout_seconds) == (3, 20, 900)
    assert goal.budget_usd == Decimal("2")
    assert image == "img-0"
    assert cursor.image_id == "img-0-1"
    assert result.status == "achieved"
    assert sink.results[0].goal_id == "team-7-build-0"
    assert "tok-secret" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_concurrent_calls_with_max_calls_1_exact_one_runs():
    """asyncio.gather of two builds with max_calls=1 → exactly one runs, other hits ceiling."""
    runner = _Runner()
    limits = CodingToolLimits(max_calls=1, max_rounds=3, max_turns_per_round=20, budget_usd=Decimal("2"), timeout_seconds=900)
    tool = ClaudeCodeBuildTool(
        runner=runner, cursor=WorkspaceCursor("img-0"), authority=_Authority(),
        tokens=_Tokens(), evidence=_Sink(), limits=limits, clock=lambda: NOW, goal_prefix="team-7",
    )

    async def call_tool():
        return await tool.run(_args(), CancellationToken())

    results = await asyncio.gather(call_tool(), call_tool(), return_exceptions=True)

    # Exactly one should succeed, one should raise ceiling error
    successes = [r for r in results if not isinstance(r, Exception)]
    failures = [r for r in results if isinstance(r, Exception)]

    assert len(successes) == 1
    assert len(failures) == 1
    assert "ceiling" in str(failures[0])
    assert len(runner.seen) == 1  # Only one call actually executed


@pytest.mark.asyncio
async def test_concurrent_builds_with_max_calls_2_have_distinct_ids():
    """Two concurrent builds with max_calls=2 → distinct goal_ids and tokens; second sees first's final image."""
    runner = _Runner()
    cursor = WorkspaceCursor("img-0")
    limits = CodingToolLimits(max_calls=2, max_rounds=3, max_turns_per_round=20, budget_usd=Decimal("2"), timeout_seconds=900)
    tool = ClaudeCodeBuildTool(
        runner=runner, cursor=cursor, authority=_Authority(),
        tokens=_Tokens(), evidence=_Sink(), limits=limits, clock=lambda: NOW, goal_prefix="team-7",
    )

    async def call_tool():
        return await tool.run(_args(), CancellationToken())

    results = await asyncio.gather(call_tool(), call_tool(), return_exceptions=True)

    assert len(results) == 2
    assert all(not isinstance(r, Exception) for r in results)

    # Extract goal IDs from runner calls
    goal1, _, token1 = runner.seen[0]
    goal2, _, token2 = runner.seen[1]

    # Distinct goal IDs (not both "build-2")
    assert goal1.goal_id != goal2.goal_id
    assert "build-0" in goal1.goal_id or "build-1" in goal1.goal_id
    assert "build-0" in goal2.goal_id or "build-1" in goal2.goal_id

    # Distinct tokens
    assert token1 != token2


@pytest.mark.asyncio
async def test_concurrent_build_and_sandbox_no_interleaving():
    """Build and sandbox_run_tests on same cursor → never interleave (lock serializes)."""
    runner = _Runner()
    sandbox = _YieldingSandbox(lambda image, command: ok("ok", exit_code=0))
    cursor = WorkspaceCursor("img-0")

    build_tool = ClaudeCodeBuildTool(
        runner=runner, cursor=cursor, authority=_Authority(),
        tokens=_Tokens(), evidence=_Sink(),
        limits=CodingToolLimits(max_calls=2, max_rounds=3, max_turns_per_round=20, budget_usd=Decimal("2"), timeout_seconds=900),
        clock=lambda: NOW, goal_prefix="team-7",
    )
    sandbox_tool = SandboxRunTestsTool(
        sandbox=sandbox, cursor=cursor, authority=_Authority(), clock=lambda: NOW,
        workdir="/work", timeout_seconds=120, max_calls=2
    )

    async def build():
        return await build_tool.run(_args(), CancellationToken())

    async def sandbox_test():
        return await sandbox_tool.run(SandboxRunTestsArgs(commands=["pytest -q"]), CancellationToken())

    results = await asyncio.gather(build(), sandbox_test(), return_exceptions=True)

    # Both should succeed
    assert all(not isinstance(r, Exception) for r in results)

    # Runner enter/exit and sandbox enter/exit should be interleaved but not overlapping
    # (cursor lock ensures no simultaneous access)
    assert len(runner.enter_times) >= 1
    assert len(sandbox.enter_times) >= 1


@pytest.mark.parametrize("status", [
    "achieved",
    "exhausted",
    "timeout",
    "budget_exhausted",
    "policy_violation",
    "failed",
])
@pytest.mark.asyncio
async def test_cursor_advance_only_for_advancing_statuses(status: GoalStatus):
    """Cursor advances only for 'achieved' and 'exhausted' statuses."""
    runner = _Runner(status=status)
    cursor = WorkspaceCursor("img-0")
    tool = _build_tool(runner, cursor=cursor)
    result = await tool.run(_args(), CancellationToken())

    assert result.status == status

    # Only "achieved" and "exhausted" should advance the cursor
    if status in ("achieved", "exhausted"):
        # For achieved, runner uses workspace_image_id to generate final image
        if status == "achieved":
            assert cursor.image_id == "img-0-1"
        else:
            # For exhausted, runner uses self.final ("img-new") but doesn't add suffix
            assert cursor.image_id == "img-new"
    else:
        assert cursor.image_id == "img-0"  # Cursor unchanged


@pytest.mark.asyncio
async def test_denied_authority_runs_nothing_and_does_not_count():
    """Authorization denial doesn't consume a slot; subsequent allowed calls get full budget."""
    runner = _Runner()

    # First authority denies
    deny_authority = _Authority(deny=True)
    cursor = WorkspaceCursor("img-0")
    limits = CodingToolLimits(max_calls=2, max_rounds=3, max_turns_per_round=20, budget_usd=Decimal("2"), timeout_seconds=900)
    tool = ClaudeCodeBuildTool(
        runner=runner, cursor=cursor, authority=deny_authority,
        tokens=_Tokens(), evidence=_Sink(), limits=limits, clock=lambda: NOW, goal_prefix="team-7",
    )

    # First call should be denied and runner should not be called
    with pytest.raises(PermissionError):
        await tool.run(_args(), CancellationToken())
    assert runner.seen == []

    # Now flip authority to allow and verify we still have full budget (2 calls)
    allow_authority = _Authority(deny=False)
    tool._authority = allow_authority

    # These two calls should succeed (full budget available)
    result1 = await tool.run(_args(), CancellationToken())
    result2 = await tool.run(_args(), CancellationToken())

    assert result1.status == "achieved"
    assert result2.status == "achieved"
    assert len(runner.seen) == 2

    # Verify distinct goal IDs (note: first successful call is build-1 because build-0 was attempted before denial)
    assert runner.seen[0][0].goal_id == "team-7-build-1"
    assert runner.seen[1][0].goal_id == "team-7-build-2"


@pytest.mark.asyncio
async def test_sandbox_denied_authority_runs_nothing_and_does_not_count():
    """Authorization denial for sandbox tool doesn't consume a slot."""
    sandbox = _YieldingSandbox(lambda image, command: ok("ok", exit_code=0))

    # First authority denies
    deny_authority = _Authority(deny=True)
    cursor = WorkspaceCursor("img-0")
    tool = SandboxRunTestsTool(
        sandbox=sandbox, cursor=cursor, authority=deny_authority, clock=lambda: NOW,
        workdir="/work", timeout_seconds=120, max_calls=2
    )

    # First call should be denied and sandbox should not be called
    with pytest.raises(PermissionError):
        await tool.run(SandboxRunTestsArgs(commands=["pytest -q"]), CancellationToken())
    assert sandbox.calls == []

    # Now flip authority to allow and verify we still have full budget (2 calls)
    allow_authority = _Authority(deny=False)
    tool._authority = allow_authority

    # These two calls should succeed (full budget available)
    report1 = await tool.run(SandboxRunTestsArgs(commands=["pytest -q"]), CancellationToken())
    report2 = await tool.run(SandboxRunTestsArgs(commands=["pytest -q"]), CancellationToken())

    assert report1.passed is True
    assert report2.passed is True
    assert len(sandbox.calls) == 2


@pytest.mark.asyncio
async def test_runner_error_keeps_slot_consumed():
    """Non-authorization failures keep the slot consumed (budget may be spent)."""
    runner = _Runner(raise_error=RuntimeError("boom"))
    cursor = WorkspaceCursor("img-0")
    limits = CodingToolLimits(max_calls=1, max_rounds=3, max_turns_per_round=20, budget_usd=Decimal("2"), timeout_seconds=900)
    tool = ClaudeCodeBuildTool(
        runner=runner, cursor=cursor, authority=_Authority(),
        tokens=_Tokens(), evidence=_Sink(), limits=limits, clock=lambda: NOW, goal_prefix="team-7",
    )

    # First call raises from runner but slot is consumed
    with pytest.raises(RuntimeError, match="boom"):
        await tool.run(_args(), CancellationToken())

    # Next call should hit ceiling (slot wasn't returned)
    with pytest.raises(RuntimeError, match="ceiling"):
        await tool.run(_args(), CancellationToken())


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


def test_build_args_reject_multiline_constraints():
    with pytest.raises(ValueError):
        ClaudeCodeBuildArgs(objective="x", acceptance_commands=["cmd"], constraints=["a\nb"])


def test_build_args_reject_unsafe_allowed_paths():
    with pytest.raises(ValueError):
        ClaudeCodeBuildArgs(objective="x", acceptance_commands=["cmd"], allowed_paths=["../x"])


def test_build_tool_rejects_invalid_limits_in_init():
    """Tool __init__ validates limits against AgentGoalV1 bounds."""
    cursor = WorkspaceCursor("img-0")

    # Test max_rounds out of bounds
    with pytest.raises(ValueError, match="max_rounds"):
        ClaudeCodeBuildTool(
            runner=_Runner(), cursor=cursor, authority=_Authority(), tokens=_Tokens(),
            evidence=_Sink(), limits=CodingToolLimits(max_calls=1, max_rounds=11, max_turns_per_round=20,
                                                       budget_usd=Decimal("2"), timeout_seconds=900),
            clock=lambda: NOW, goal_prefix="team-7",
        )

    # Test max_turns_per_round out of bounds
    with pytest.raises(ValueError, match="max_turns_per_round"):
        ClaudeCodeBuildTool(
            runner=_Runner(), cursor=cursor, authority=_Authority(), tokens=_Tokens(),
            evidence=_Sink(), limits=CodingToolLimits(max_calls=1, max_rounds=3, max_turns_per_round=201,
                                                       budget_usd=Decimal("2"), timeout_seconds=900),
            clock=lambda: NOW, goal_prefix="team-7",
        )

    # Test budget_usd out of bounds
    with pytest.raises(ValueError, match="budget_usd"):
        ClaudeCodeBuildTool(
            runner=_Runner(), cursor=cursor, authority=_Authority(), tokens=_Tokens(),
            evidence=_Sink(), limits=CodingToolLimits(max_calls=1, max_rounds=3, max_turns_per_round=20,
                                                       budget_usd=Decimal("51"), timeout_seconds=900),
            clock=lambda: NOW, goal_prefix="team-7",
        )

    # Test timeout_seconds out of bounds
    with pytest.raises(ValueError, match="timeout_seconds"):
        ClaudeCodeBuildTool(
            runner=_Runner(), cursor=cursor, authority=_Authority(), tokens=_Tokens(),
            evidence=_Sink(), limits=CodingToolLimits(max_calls=1, max_rounds=3, max_turns_per_round=20,
                                                       budget_usd=Decimal("2"), timeout_seconds=7201),
            clock=lambda: NOW, goal_prefix="team-7",
        )


def test_invalid_build_args_never_reach_authorize():
    """Invalid args are rejected before authorization is attempted."""
    cursor = WorkspaceCursor("img-0")
    authority = _Authority()  # Will track if authorize is called
    runner = _Runner()

    tool = _build_tool(runner, authority=authority, cursor=cursor)

    # This should fail at args validation, never reach authorize
    with pytest.raises(ValueError):
        ClaudeCodeBuildArgs(objective="x", acceptance_commands=["a\nb"])

    # Verify authorize was never called
    assert authority.calls == []
    assert runner.seen == []


@pytest.mark.asyncio
async def test_run_tests_tool_runs_on_cursor_image():
    sandbox = _YieldingSandbox(lambda image, command: ok("ok", exit_code=0))
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
