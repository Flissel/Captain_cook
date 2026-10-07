"""Drive Claude Code CLI in a sandbox until a goal's acceptance commands pass."""
from __future__ import annotations

import re
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from agenten.coding_agent.acceptance import changed_paths, protected_paths, run_acceptance
from agenten.coding_agent.claude_stream import summarize_claude_stream
from agenten.coding_agent.contracts import (
    AcceptanceReportV1,
    AgentGoalResultV1,
    AgentGoalRoundV1,
    AgentGoalV1,
    GoalStatus,
    path_allowed,
)
from agenten.coding_agent.goal_prompt import render_goal_prompt, render_resume_prompt
from agenten.coding_agent.sandbox import SandboxCommand, SandboxPort

_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_SESSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})


class BudgetGuardPort(Protocol):
    def exhausted(self) -> bool: ...


@dataclass(frozen=True)
class ClaudeRunnerSettings:
    relay_url: str
    model: str
    small_model: str
    workdir: str = "/work"
    allowed_tools: tuple[str, ...] = ("Bash", "Read", "Edit", "Write", "Glob", "Grep")
    acceptance_timeout_seconds: int = 600

    def __post_init__(self) -> None:
        parsed = urlsplit(self.relay_url)
        secure = parsed.scheme == "https" and bool(parsed.hostname)
        local = parsed.scheme == "http" and parsed.hostname in _LOCAL_HOSTS
        if not (secure or local) or parsed.username or parsed.password or parsed.query or "#" in self.relay_url:
            raise ValueError("relay_url must be https (or http on localhost)")
        for value in (self.model, self.small_model):
            if _MODEL.fullmatch(value) is None:
                raise ValueError("model ids must be plain identifiers")
        if not self.workdir.startswith("/") or any(ch.isspace() for ch in self.workdir):
            raise ValueError("workdir must be an absolute path without whitespace")


def build_claude_shell(settings: ClaudeRunnerSettings, *, max_turns: int, resume_session: str | None) -> str:
    parts = [
        "claude", "-p", "--output-format", "stream-json", "--verbose",
        "--model", settings.model, "--max-turns", str(max_turns),
        "--dangerously-skip-permissions", "--allowedTools", ",".join(settings.allowed_tools),
        "--setting-sources", "", "--strict-mcp-config",
    ]
    if resume_session is not None:
        if _SESSION.fullmatch(resume_session) is None:
            raise ValueError("session id is not a plain identifier")
        parts += ["--resume", resume_session]
    return f"cd {shlex.quote(settings.workdir)} && " + " ".join(shlex.quote(part) for part in parts)


def build_claude_env(settings: ClaudeRunnerSettings, run_token: str) -> dict[str, str]:
    return {
        "ANTHROPIC_BASE_URL": settings.relay_url,
        "ANTHROPIC_AUTH_TOKEN": run_token,
        "ANTHROPIC_MODEL": settings.model,
        "ANTHROPIC_SMALL_FAST_MODEL": settings.small_model,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_TELEMETRY": "1",
    }


_REDACTED = "<redacted>"


def _redactor(token: str) -> Callable[[str], str]:
    """Replace the run token in agent-controlled text; an empty token redacts nothing."""

    def redact(text: str) -> str:
        return text.replace(token, _REDACTED) if token else text

    return redact


def _redact_report(report: AcceptanceReportV1, redact: Callable[[str], str]) -> AcceptanceReportV1:
    return AcceptanceReportV1(
        checks=tuple(
            check.model_copy(update={"command": redact(check.command), "output_tail": redact(check.output_tail)})
            for check in report.checks
        )
    )


class ClaudeGoalRunner:
    def __init__(
        self,
        *,
        sandbox: SandboxPort,
        settings: ClaudeRunnerSettings,
        budget: BudgetGuardPort,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sandbox = sandbox
        self._settings = settings
        self._budget = budget
        self._clock = clock

    async def run(self, goal: AgentGoalV1, *, workspace_image_id: str, run_token: str) -> AgentGoalResultV1:
        started = self._clock()
        image = workspace_image_id
        session: str | None = None
        report: AcceptanceReportV1 | None = None
        rounds: list[AgentGoalRoundV1] = []
        env = build_claude_env(self._settings, run_token)
        redact = _redactor(run_token)
        workdir = self._settings.workdir

        def finish(status: GoalStatus, reason: str | None) -> AgentGoalResultV1:
            return AgentGoalResultV1(
                goal_id=goal.goal_id, status=status, rounds=tuple(rounds),
                final_image_id=image, failure_reason=reason,
            )

        for number in range(1, goal.max_rounds + 1):
            if self._budget.exhausted():
                return finish("budget_exhausted", "provider budget exhausted before round")
            remaining = goal.timeout_seconds - (self._clock() - started)
            if remaining <= 0:
                return finish("timeout", "goal timeout reached")
            prompt = render_goal_prompt(goal, workdir=workdir) if report is None else render_resume_prompt(report)
            run = await self._sandbox.run(
                image,
                SandboxCommand(
                    shell=build_claude_shell(self._settings, max_turns=goal.max_turns_per_round, resume_session=session),
                    timeout_seconds=max(1, int(remaining)),
                    env=env,
                    stdin=prompt,
                ),
            )
            summary = summarize_claude_stream(run.stdout)
            reported_session = summary.session_id
            leaked_session = reported_session is not None and bool(run_token) and run_token in reported_session
            bad_session = reported_session is not None and _SESSION.fullmatch(reported_session) is None
            if reported_session is not None and not leaked_session and not bad_session:
                session = reported_session
            input_image, image = image, run.output_image_id
            paths = await changed_paths(
                self._sandbox, before_image_id=input_image, after_image_id=image, workdir=workdir
            )
            outside = {path for path in paths if not path_allowed(path, goal.allowed_paths)}
            judge = set(protected_paths(paths, explicitly_allowed=goal.allowed_paths))
            exfiltrating = {path for path in paths if run_token and run_token in path}
            violations = tuple(redact(path) for path in sorted(outside | judge | exfiltrating))
            paths = tuple(redact(path) for path in paths)
            session_usable = reported_session is not None and not leaked_session and not bad_session
            acceptance = None
            if not run.timed_out and not violations and session_usable:
                acceptance = _redact_report(
                    await run_acceptance(
                        self._sandbox, image_id=image, commands=goal.acceptance_commands,
                        workdir=workdir, timeout_seconds=self._settings.acceptance_timeout_seconds,
                    ),
                    redact,
                )
            rounds.append(
                AgentGoalRoundV1(
                    round=number, input_image_id=input_image, output_image_id=image,
                    session_id=redact(reported_session) if reported_session is not None else None,
                    result_subtype=redact(summary.result_subtype) if summary.result_subtype is not None else None,
                    num_turns=summary.num_turns, tool_call_count=summary.tool_call_count,
                    exit_code=run.exit_code, timed_out=run.timed_out,
                    journal_sha256=summary.journal_sha256, changed_paths=paths,
                    policy_violations=violations, acceptance=acceptance,
                )
            )
            if run.timed_out:
                return finish("timeout", "claude round timed out")
            if leaked_session:
                return finish("policy_violation", "claude session id carried the run token")
            if reported_session is None:
                return finish("failed", "claude stream carried no session")
            if bad_session:
                return finish("failed", "claude session id is not a plain identifier")
            if violations:
                return finish("policy_violation", f"disallowed changed path: {violations[0]}"[:300])
            if acceptance is None:  # unreachable: guarded by the conditions above
                return finish("failed", "acceptance did not run")
            if acceptance.passed:
                return finish("achieved", None)
            report = acceptance
        return finish("exhausted", "acceptance commands still failing after max_rounds")
