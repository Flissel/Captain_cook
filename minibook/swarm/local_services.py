"""Local Minibook service prerequisites with explicit, reproducible recovery."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path


def default_npm_command() -> str:
    return "npm.cmd" if os.name == "nt" else "npm"


def _next_exists(frontend_directory: Path) -> bool:
    binary_directory = frontend_directory / "node_modules" / ".bin"
    return any((binary_directory / name).is_file() for name in ("next", "next.cmd"))


def ensure_frontend_dependencies(
    frontend_directory: Path,
    *,
    npm_command: str | None = None,
    runner: Callable[..., int] | None = None,
) -> bool:
    """Install locked frontend dependencies once when the Next executable is absent."""
    if _next_exists(frontend_directory):
        return True
    command = npm_command or default_npm_command()
    if runner is None:
        def runner(arguments: list[str], *, cwd: Path) -> int:
            return subprocess.run(arguments, cwd=cwd, check=False).returncode
    exit_code = runner([command, "ci", "--no-audit", "--no-fund"], cwd=frontend_directory)
    return exit_code == 0 and _next_exists(frontend_directory)
