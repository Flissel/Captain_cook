"""Trivial worker with no external dependencies: echoes the description back
after a short artificial delay. Exists purely so integration/e2e tests
(unit U11) can exercise the full assign -> execute -> complete pipeline
without needing Selenium or a live LLM.
"""
import asyncio
from typing import Any, Dict

from .base import WorkerAgent

DEFAULT_ECHO_DELAY_SECONDS = 0.05


class EchoWorker(WorkerAgent):
    agent_type = "echo_worker"
    capability_tags = ["echo", "test"]

    def __init__(self, *args: Any, echo_delay_seconds: float = DEFAULT_ECHO_DELAY_SECONDS, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.echo_delay_seconds = echo_delay_seconds

    async def execute(self, subproblem_id: str, description: str) -> Dict[str, Any]:
        await asyncio.sleep(self.echo_delay_seconds)
        return {"echo": description}
