"""Small, dependency-free runtime controls for one-shot Swarm runs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class RuntimeOptions:
    interactive: bool = True
    max_runtime_seconds: float | None = None


def parse_runtime_options(argv: Sequence[str]) -> RuntimeOptions:
    """Read only the runtime controls while leaving mode-specific parsing intact."""
    interactive = "--non-interactive" not in argv
    max_runtime_seconds: float | None = None
    if "--max-runtime-seconds" in argv:
        index = argv.index("--max-runtime-seconds")
        if index + 1 >= len(argv):
            raise ValueError("--max-runtime-seconds requires a positive number")
        try:
            max_runtime_seconds = float(argv[index + 1])
        except ValueError as exc:
            raise ValueError("--max-runtime-seconds requires a positive number") from exc
        if max_runtime_seconds <= 0:
            raise ValueError("--max-runtime-seconds requires a positive number")
    return RuntimeOptions(interactive=interactive, max_runtime_seconds=max_runtime_seconds)
