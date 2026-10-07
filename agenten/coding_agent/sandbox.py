"""Port for sandboxes that run one shell command on an image and return a new image."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol


class SandboxProtocolError(RuntimeError):
    """The sandbox or workspace violated an expectation of the coding runner."""


@dataclass(frozen=True)
class SandboxCommand:
    shell: str
    timeout_seconds: int
    env: Mapping[str, str] = field(default_factory=dict, repr=False)
    stdin: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class SandboxRunResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    output_image_id: str


class SandboxPort(Protocol):
    async def run(self, image_id: str, command: SandboxCommand) -> SandboxRunResult: ...
