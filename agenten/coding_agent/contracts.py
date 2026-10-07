"""Goal contracts for coding agents that run in sandboxes.

An `AgentGoalV1` is what a team agent hands to the coding tool: an objective,
the commands that judge it, and hard limits. The runner never trusts the
coding agent's own claim of success; the acceptance commands decide.
"""
from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
_SHA256 = r"^[0-9a-f]{64}$"

GoalStatus = Literal[
    "achieved",
    "exhausted",
    "timeout",
    "budget_exhausted",
    "policy_violation",
    "failed",
]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


def _single_line(value: str, *, limit: int) -> str:
    if not value.strip() or len(value) > limit or any(ch in value for ch in "\x00\r\n"):
        raise ValueError("must be a non-empty single line")
    return value


def _safe_relative(value: str) -> str:
    if "\\" in value or ":" in value:
        raise ValueError("path must use forward slashes and no drive")
    path = PurePosixPath(value)
    if value != "." and (path.is_absolute() or ".." in path.parts or not value.strip()):
        raise ValueError("path must be a safe relative path")
    return value


def path_allowed(path: str, allowed: Sequence[str]) -> bool:
    """True when `path` equals an allowed file or lies under an allowed directory."""
    for entry in allowed:
        if entry == ".":
            return True
        base = entry.rstrip("/")
        if path == base or path.startswith(base + "/"):
            return True
    return False


class AgentGoalV1(_Frozen):
    schema_name: Literal["captain.agent-goal.v1"] = Field(
        default="captain.agent-goal.v1", alias="schema"
    )
    goal_id: str = Field(pattern=ID_PATTERN)
    parent_goal_ref: str | None = Field(default=None, pattern=ID_PATTERN)
    objective: str = Field(min_length=1, max_length=8000)
    acceptance_commands: tuple[str, ...] = Field(min_length=1, max_length=20)
    constraints: tuple[str, ...] = Field(default=(), max_length=30)
    allowed_paths: tuple[str, ...] = Field(min_length=1, max_length=50)
    max_rounds: int = Field(ge=1, le=10, strict=True)
    max_turns_per_round: int = Field(ge=1, le=200, strict=True)
    budget_usd: Decimal = Field(gt=Decimal("0"), le=Decimal("50"))
    timeout_seconds: int = Field(ge=60, le=7200, strict=True)

    @field_validator("acceptance_commands")
    @classmethod
    def _commands(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_single_line(item, limit=2000) for item in value)

    @field_validator("constraints")
    @classmethod
    def _constraints(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_single_line(item, limit=500) for item in value)

    @field_validator("allowed_paths")
    @classmethod
    def _paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_safe_relative(item) for item in value)


class AcceptanceCheckV1(_Frozen):
    command: str = Field(min_length=1, max_length=2000)
    exit_code: int = Field(strict=True)
    timed_out: bool
    passed: bool
    output_tail: str = Field(max_length=4000)

    @model_validator(mode="after")
    def _consistent(self) -> "AcceptanceCheckV1":
        if self.passed != (self.exit_code == 0 and not self.timed_out):
            raise ValueError("passed must equal exit_code == 0 and not timed_out")
        return self


class AcceptanceReportV1(_Frozen):
    checks: tuple[AcceptanceCheckV1, ...] = Field(min_length=1)

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def failing(self) -> tuple[AcceptanceCheckV1, ...]:
        return tuple(check for check in self.checks if not check.passed)


class AgentGoalRoundV1(_Frozen):
    round: int = Field(ge=1, strict=True)
    input_image_id: str = Field(min_length=1)
    output_image_id: str = Field(min_length=1)
    session_id: str | None = None
    result_subtype: str | None = None
    num_turns: int = Field(ge=0, strict=True)
    tool_call_count: int = Field(ge=0, strict=True)
    exit_code: int = Field(strict=True)
    timed_out: bool
    journal_sha256: str = Field(pattern=_SHA256)
    changed_paths: tuple[str, ...] = ()
    policy_violations: tuple[str, ...] = ()
    acceptance: AcceptanceReportV1 | None = None


class AgentGoalResultV1(_Frozen):
    schema_name: Literal["captain.agent-goal-result.v1"] = Field(
        default="captain.agent-goal-result.v1", alias="schema"
    )
    goal_id: str = Field(pattern=ID_PATTERN)
    status: GoalStatus
    rounds: tuple[AgentGoalRoundV1, ...]
    final_image_id: str = Field(min_length=1)
    failure_reason: str | None = Field(default=None, max_length=300)
