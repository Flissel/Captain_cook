"""Render Claude Code prompts from goal contracts using versioned templates."""
from __future__ import annotations

from functools import cache
from pathlib import Path
from string import Template

from agenten.coding_agent.contracts import AcceptanceReportV1, AgentGoalV1

_PROMPTS = Path(__file__).with_name("prompts")


@cache
def _template(name: str) -> Template:
    return Template((_PROMPTS / name).read_text(encoding="utf-8"))


def _bullets(items: tuple[str, ...], *, code: bool) -> str:
    if not items:
        return "- (none)"
    return "\n".join(f"- `{item}`" if code else f"- {item}" for item in items)


def render_goal_prompt(goal: AgentGoalV1, *, workdir: str) -> str:
    return _template("claude_goal_v1.md").substitute(
        workdir=workdir,
        objective=goal.objective,
        acceptance=_bullets(goal.acceptance_commands, code=True),
        constraints=_bullets(goal.constraints, code=False),
        allowed_paths=_bullets(goal.allowed_paths, code=True),
    )


def render_resume_prompt(report: AcceptanceReportV1) -> str:
    blocks: list[str] = []
    for check in report.failing():
        headline = (
            f"- `{check.command}` timed out"
            if check.timed_out
            else f"- `{check.command}` exited {check.exit_code}"
        )
        blocks.append(f"{headline}\n\n```\n{check.output_tail}\n```")
    return _template("claude_resume_v1.md").substitute(failing="\n\n".join(blocks))
