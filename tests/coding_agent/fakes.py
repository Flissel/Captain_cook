"""Deterministic sandbox fake for coding-agent tests."""
from __future__ import annotations

from collections.abc import Callable

from agenten.coding_agent.sandbox import SandboxCommand, SandboxRunResult


class FakeSandbox:
    def __init__(self, handler: Callable[[str, SandboxCommand], SandboxRunResult]) -> None:
        self._handler = handler
        self.calls: list[tuple[str, SandboxCommand]] = []

    async def run(self, image_id: str, command: SandboxCommand) -> SandboxRunResult:
        self.calls.append((image_id, command))
        return self._handler(image_id, command)


def ok(stdout: str = "", *, image: str = "img-out", exit_code: int = 0, timed_out: bool = False) -> SandboxRunResult:
    return SandboxRunResult(exit_code=exit_code, stdout=stdout, stderr="", timed_out=timed_out, output_image_id=image)
