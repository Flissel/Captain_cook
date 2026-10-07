"""Captain-authorized AutoGen tools that let team agents use Claude Code in a sandbox."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from autogen_core import CancellationToken
from autogen_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agenten.coding_agent.acceptance import run_acceptance
from agenten.coding_agent.contracts import AcceptanceReportV1, AgentGoalResultV1, AgentGoalV1, GoalStatus
from agenten.coding_agent.sandbox import SandboxPort

_ADVANCING: frozenset[GoalStatus] = frozenset({"achieved", "exhausted"})


class WorkspaceCursor:
    """The current workspace image of one team run, shared by its coding tools."""

    def __init__(self, image_id: str) -> None:
        self.image_id = image_id


class CodingToolAuthorityPort(Protocol):
    async def authorize(self, tool_name: str, *, now: datetime) -> None: ...


class RunTokenIssuerPort(Protocol):
    async def issue(self, goal_id: str) -> str: ...


class GoalEvidenceSinkPort(Protocol):
    def record(self, result: AgentGoalResultV1) -> None: ...


class _GoalRunnerPort(Protocol):
    async def run(self, goal: AgentGoalV1, *, workspace_image_id: str, run_token: str) -> AgentGoalResultV1: ...


@dataclass(frozen=True)
class CodingToolLimits:
    max_calls: int
    max_rounds: int
    max_turns_per_round: int
    budget_usd: Decimal
    timeout_seconds: int


def _single_lines(values: list[str]) -> list[str]:
    for value in values:
        if not value.strip() or any(ch in value for ch in "\x00\r\n"):
            raise ValueError("commands must be non-empty single lines")
    return values


class ClaudeCodeBuildArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective: str = Field(min_length=1, max_length=8000, description="What to build or change.")
    acceptance_commands: list[str] = Field(min_length=1, max_length=20, description="Shell commands that must all exit 0.")
    constraints: list[str] = Field(default_factory=list, max_length=30)
    allowed_paths: list[str] = Field(default_factory=lambda: ["."], min_length=1, max_length=50)

    @field_validator("acceptance_commands")
    @classmethod
    def _commands(cls, value: list[str]) -> list[str]:
        return _single_lines(value)


class ClaudeCodeBuildResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: GoalStatus
    rounds: int
    final_image_id: str
    failing_checks: list[str]
    failure_reason: str | None


class ClaudeCodeBuildTool(BaseTool[ClaudeCodeBuildArgs, ClaudeCodeBuildResult]):
    TOOL_NAME = "claude_code_build"

    def __init__(
        self,
        *,
        runner: _GoalRunnerPort,
        cursor: WorkspaceCursor,
        authority: CodingToolAuthorityPort,
        tokens: RunTokenIssuerPort,
        evidence: GoalEvidenceSinkPort,
        limits: CodingToolLimits,
        clock: Callable[[], datetime],
        goal_prefix: str,
    ) -> None:
        super().__init__(
            ClaudeCodeBuildArgs,
            ClaudeCodeBuildResult,
            self.TOOL_NAME,
            "Build or change code in the sandboxed workspace with Claude Code until the acceptance commands pass.",
        )
        self._runner = runner
        self._cursor = cursor
        self._authority = authority
        self._tokens = tokens
        self._evidence = evidence
        self._limits = limits
        self._clock = clock
        self._goal_prefix = goal_prefix
        self._calls = 0

    async def run(self, args: ClaudeCodeBuildArgs, cancellation_token: CancellationToken) -> ClaudeCodeBuildResult:
        if self._calls >= self._limits.max_calls:
            raise RuntimeError("claude_code_build call ceiling reached")
        await self._authority.authorize(self.TOOL_NAME, now=self._clock())
        self._calls += 1
        goal = AgentGoalV1(
            goal_id=f"{self._goal_prefix}-build-{self._calls}",
            objective=args.objective,
            acceptance_commands=tuple(args.acceptance_commands),
            constraints=tuple(args.constraints),
            allowed_paths=tuple(args.allowed_paths),
            max_rounds=self._limits.max_rounds,
            max_turns_per_round=self._limits.max_turns_per_round,
            budget_usd=self._limits.budget_usd,
            timeout_seconds=self._limits.timeout_seconds,
        )
        token = await self._tokens.issue(goal.goal_id)
        result = await self._runner.run(goal, workspace_image_id=self._cursor.image_id, run_token=token)
        self._evidence.record(result)
        if result.status in _ADVANCING:
            self._cursor.image_id = result.final_image_id
        last = result.rounds[-1].acceptance if result.rounds else None
        return ClaudeCodeBuildResult(
            status=result.status,
            rounds=len(result.rounds),
            final_image_id=result.final_image_id,
            failing_checks=[check.command for check in last.failing()] if last is not None else [],
            failure_reason=result.failure_reason,
        )


class SandboxRunTestsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    commands: list[str] = Field(min_length=1, max_length=20, description="Shell commands to run in the workspace.")

    @field_validator("commands")
    @classmethod
    def _commands(cls, value: list[str]) -> list[str]:
        return _single_lines(value)


class SandboxRunTestsTool(BaseTool[SandboxRunTestsArgs, AcceptanceReportV1]):
    TOOL_NAME = "sandbox_run_tests"

    def __init__(
        self,
        *,
        sandbox: SandboxPort,
        cursor: WorkspaceCursor,
        authority: CodingToolAuthorityPort,
        clock: Callable[[], datetime],
        workdir: str,
        timeout_seconds: int,
        max_calls: int,
    ) -> None:
        super().__init__(
            SandboxRunTestsArgs,
            AcceptanceReportV1,
            self.TOOL_NAME,
            "Run test commands against the current sandboxed workspace without changing it.",
        )
        self._sandbox = sandbox
        self._cursor = cursor
        self._authority = authority
        self._clock = clock
        self._workdir = workdir
        self._timeout_seconds = timeout_seconds
        self._max_calls = max_calls
        self._calls = 0

    async def run(self, args: SandboxRunTestsArgs, cancellation_token: CancellationToken) -> AcceptanceReportV1:
        if self._calls >= self._max_calls:
            raise RuntimeError("sandbox_run_tests call ceiling reached")
        await self._authority.authorize(self.TOOL_NAME, now=self._clock())
        self._calls += 1
        return await run_acceptance(
            self._sandbox,
            image_id=self._cursor.image_id,
            commands=args.commands,
            workdir=self._workdir,
            timeout_seconds=self._timeout_seconds,
        )
