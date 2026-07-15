"""Command-line entry points for Captain Cook."""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Sequence

from agenten.demo import run_demo


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Captain Cook demos")
    subparsers = parser.add_subparsers(dest="command", required=True)

    demo_parser = subparsers.add_parser("demo", help="Run the offline audited pipeline demo")
    demo_parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/demo-run.json"),
        help="Path for the generated JSON evidence artifact",
    )
    subparsers.add_parser("legacy", help="Run the API-key-backed legacy prototype")
    return parser.parse_args(argv)


def run_legacy() -> int:
    """Preserve the original exploratory workflow behind an explicit command."""
    from agenten.Captain import CaptainAgent
    from agenten.project_definer import execute_project_definition
    from agenten.subtaskGenerator import SubtaskGenerator
    from config.llm_config import API_KEY, MODEL

    llm_config = {"config_list": [{"model": MODEL, "api_key": API_KEY}]}
    captain = CaptainAgent(name="CaptainAgent", llm_config=llm_config)
    project_description = (
        "Develop a multi-agent system which can craft and execute a whole project "
        "from a given project description."
    )
    project_description = execute_project_definition(project_description, captain)
    project_split, _sections = captain.automate_project_split(project_description)
    captain.build_departments(project_split)
    project_block = captain.add_task_to_blockchain(
        task=project_description,
        assigned_agents=[],
        status="in_progress",
    )
    system_prompt = captain.make_system_prompt(project_description)
    subtasks = SubtaskGenerator(system_prompt=system_prompt).generate_prompts(project_description, captain)
    for subtask in subtasks:
        captain.add_task_to_blockchain(
            task=subtask["title"],
            assigned_agents=["Agent1", "Agent2"],
            status="pending",
            parent_index=project_block.index,
        )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "demo":
        summary = asyncio.run(run_demo(args.output))
        print(f"Demo complete: {summary.done_count} subproblems reached done")
        return 0
    return run_legacy()


if __name__ == "__main__":
    raise SystemExit(main())
