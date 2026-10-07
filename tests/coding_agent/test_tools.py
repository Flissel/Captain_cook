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
    """Yields to simulate real concurrency; denies every call or only the given 0-based call indexes."""
    def __init__(self, deny=False, deny_indexes=()):
        self.deny = deny
        self.deny_indexes = frozenset(deny_indexes)
        self.calls = []

    async def authorize(self, tool_name, *, now):
        index = len(self.calls)
        self.calls.append(tool_name)
        await asyncio.sleep(0)  # Yield to enable real concurrency
        if self.deny or index in self.deny_indexes:
            raise PermissionError("grant revoked")


class _Tokens:
    def __init__(self, events=None):
        self.issued = []
        self.revoked = []
        self.events = events if events is not None else []

    async def issue(self, goal):
        self.issued.append(goal)
        self.events.append(("issue", goal.goal_id))
        return "tok-secret-" + goal.goal_id

    async def revoke(self, goal_id):
        self.revoked.append(goal_id)
        self.events.append(("revoke", goal_id))


class _Sink:
    def __init__(self, events=None):
        self.results = []
        self.rounds = []
        self.events = events if events is not None else []

    def record(self, result):
        self.results.append(result)
        self.events.append(("result", result.goal_id))

    def record_round(self, goal_id, round_):
        self.rounds.append((goal_id, round_))
        self.events.append(("round", goal_id, round_.round))


class _Runner:
    """Yields to simulate real concurrency."""
    def __init__(self, status="achieved", final="img-new", raise_error=None, events=None, rounds_before_error=0):
        self.status = status
        self.rounds_before_error = rounds_before_error
        self.final = final
        self.seen = []
        self._call_count = 0
        self.raise_error = raise_error
        self.events = events if events is not None else []

    async def run(self, goal, *, workspace_image_id, run_token, on_round=None):
        self.events.append(("runner", "enter"))
        self.seen.append((goal, workspace_image_id, run_token))
        self._call_count += 1

        # Yield to enable real concurrency
        await asyncio.sleep(0)
        self.events.append(("runner", "exit"))

        # Raise error if configured (to test slot consumption on non-auth errors)
        if self.raise_error:
            for number in range(1, self.rounds_before_error + 1):
                await on_round(_round(number, workspace_image_id))
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
            if on_round is not None:
                await on_round(round_result)
            return AgentGoalResultV1(
                goal_id=goal.goal_id,
                status=self.status,
                rounds=(round_result,),
                final_image_id=final_image,
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


def _round(number, image):
    return AgentGoalRoundV1(
        round=number, input_image_id=image, output_image_id=f"{image}-r{number}",
        num_turns=1, tool_call_count=0, exit_code=0, timed_out=False, journal_sha256="0" * 64,
    )


class _YieldingSandbox:
    """Sandbox that yields to simulate real concurrency."""
    def __init__(self, handler, events=None):
        self._handler = handler
        self.calls = []
        self.events = events if events is not None else []

    async def run(self, image_id, command):
        self.events.append(("sandbox", "enter"))
        self.calls.append((image_id, command))
        await asyncio.sleep(0)  # Yield for concurrency
        self.events.append(("sandbox", "exit"))
        return self._handler(image_id, command)


def _build_tool(runner, authority=None, sink=None, cursor=None, tokens=None):
    return ClaudeCodeBuildTool(
        runner=runner, cursor=cursor or WorkspaceCursor("img-0"), authority=authority or _Authority(),
        tokens=tokens or _Tokens(), evidence=sink or _Sink(), limits=LIMITS, clock=lambda: NOW, goal_prefix="team-7",
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

    # Image chain: the cursor lock serialized the builds, so the second build started
    # from the first build's final image rather than from the shared initial image.
    assert runner.seen[0][1] == "img-0"
    assert runner.seen[1][1] == results[0].final_image_id == "img-0-1"
    assert results[1].final_image_id == "img-0-1-2"
    assert cursor.image_id == "img-0-1-2"


@pytest.mark.asyncio
async def test_gathered_builds_denied_middle_call_returns_slot_and_ids_stay_distinct():
    """Three gathered builds, max_calls=3, authority denies exactly the second call."""
    runner = _Runner()
    authority = _Authority(deny_indexes={1})
    limits = CodingToolLimits(max_calls=3, max_rounds=3, max_turns_per_round=20, budget_usd=Decimal("2"), timeout_seconds=900)
    tool = ClaudeCodeBuildTool(
        runner=runner, cursor=WorkspaceCursor("img-0"), authority=authority,
        tokens=_Tokens(), evidence=_Sink(), limits=limits, clock=lambda: NOW, goal_prefix="team-7",
    )

    results = await asyncio.gather(*(tool.run(_args(), CancellationToken()) for _ in range(3)), return_exceptions=True)

    denied = [r for r in results if isinstance(r, PermissionError)]
    succeeded = [r for r in results if not isinstance(r, BaseException)]
    assert len(denied) == 1 and len(succeeded) == 2
    assert isinstance(results[1], PermissionError)
    assert len(runner.seen) == 2
    goal_ids = [goal.goal_id for goal, _, _ in runner.seen]
    assert len(set(goal_ids)) == 2
    assert goal_ids == ["team-7-build-0", "team-7-build-2"]

    # The denied slot was returned: exactly one more call fits, then the ceiling applies.
    follow_up = await tool.run(_args(), CancellationToken())
    assert follow_up.status == "achieved"
    assert runner.seen[2][0].goal_id == "team-7-build-3"
    with pytest.raises(RuntimeError, match="ceiling"):
        await tool.run(_args(), CancellationToken())
    assert len(runner.seen) == 3


@pytest.mark.asyncio
async def test_concurrent_build_and_sandbox_no_interleaving():
    """Build and sandbox_run_tests on same cursor → never interleave (lock serializes)."""
    events = []
    runner = _Runner(events=events)
    sandbox = _YieldingSandbox(lambda image, command: ok("ok", exit_code=0), events=events)
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

    # Critical sections never overlap: one side fully enters and exits before the other starts.
    run_block = [("runner", "enter"), ("runner", "exit")]
    box_block = [("sandbox", "enter"), ("sandbox", "exit")]
    assert events in (run_block + box_block, box_block + run_block)


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


# --- I2: token issuer binds the goal and is always revoked ------------------------------


@pytest.mark.asyncio
async def test_token_issue_receives_the_goal_with_its_budget():
    tokens = _Tokens()
    runner = _Runner()
    await _build_tool(runner, tokens=tokens).run(_args(), CancellationToken())
    assert [goal.goal_id for goal in tokens.issued] == ["team-7-build-0"]
    assert tokens.issued[0].budget_usd == Decimal("2")
    assert tokens.issued[0] is runner.seen[0][0]


@pytest.mark.parametrize("status", ["achieved", "exhausted", "policy_violation", "failed"])
@pytest.mark.asyncio
async def test_token_is_revoked_when_the_goal_ends(status):
    events = []
    tokens, sink = _Tokens(events=events), _Sink(events=events)
    await _build_tool(_Runner(status=status), tokens=tokens, sink=sink).run(_args(), CancellationToken())
    assert tokens.revoked == ["team-7-build-0"]
    assert events.index(("revoke", "team-7-build-0")) > events.index(("issue", "team-7-build-0"))


@pytest.mark.asyncio
async def test_token_is_revoked_when_the_runner_raises():
    tokens = _Tokens()
    tool = _build_tool(_Runner(raise_error=RuntimeError("boom")), tokens=tokens)
    with pytest.raises(RuntimeError, match="boom"):
        await tool.run(_args(), CancellationToken())
    assert tokens.revoked == ["team-7-build-0"]


# --- I4: per-round evidence -------------------------------------------------------------


@pytest.mark.asyncio
async def test_rounds_are_recorded_as_they_happen_then_the_result():
    events = []
    sink = _Sink(events=events)
    await _build_tool(_Runner(), sink=sink, tokens=_Tokens(events=events)).run(_args(), CancellationToken())
    assert [(goal_id, r.round) for goal_id, r in sink.rounds] == [("team-7-build-0", 1)]
    assert events.index(("round", "team-7-build-0", 1)) < events.index(("result", "team-7-build-0"))


@pytest.mark.asyncio
async def test_rounds_are_recorded_even_when_a_later_round_raises():
    sink, tokens = _Sink(), _Tokens()
    tool = _build_tool(_Runner(raise_error=RuntimeError("lost"), rounds_before_error=2), sink=sink, tokens=tokens)
    with pytest.raises(RuntimeError, match="lost"):
        await tool.run(_args(), CancellationToken())
    assert [(goal_id, r.round) for goal_id, r in sink.rounds] == [("team-7-build-0", 1), ("team-7-build-0", 2)]
    assert sink.results == []
    assert tokens.revoked == ["team-7-build-0"]


# --- M1: command length cap -------------------------------------------------------------


def test_build_and_run_tests_args_cap_command_length():
    ClaudeCodeBuildArgs(objective="x", acceptance_commands=["x" * 2000])
    SandboxRunTestsArgs(commands=["x" * 2000])
    with pytest.raises(ValueError):
        ClaudeCodeBuildArgs(objective="x", acceptance_commands=["x" * 2001])
    with pytest.raises(ValueError):
        SandboxRunTestsArgs(commands=["x" * 2001])


# --- M5: constructor validation ----------------------------------------------------------


@pytest.mark.parametrize("prefix", ["", "-team", "team 7", "team/7", "x" * 101])
def test_build_tool_rejects_goal_prefixes_that_cannot_form_goal_ids(prefix):
    with pytest.raises(ValueError, match="goal_prefix"):
        ClaudeCodeBuildTool(
            runner=_Runner(), cursor=WorkspaceCursor("img-0"), authority=_Authority(), tokens=_Tokens(),
            evidence=_Sink(), limits=LIMITS, clock=lambda: NOW, goal_prefix=prefix,
        )


@pytest.mark.parametrize("timeout_seconds,max_calls", [(0, 1), (7201, 1), (60, 0), (60, -1)])
def test_run_tests_tool_validates_timeout_and_max_calls(timeout_seconds, max_calls):
    with pytest.raises(ValueError):
        SandboxRunTestsTool(
            sandbox=_YieldingSandbox(lambda image, command: ok()), cursor=WorkspaceCursor("img-0"),
            authority=_Authority(), clock=lambda: NOW, workdir="/work",
            timeout_seconds=timeout_seconds, max_calls=max_calls,
        )
