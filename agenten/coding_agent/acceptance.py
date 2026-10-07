"""Independent acceptance checks and change detection inside the sandbox.

Note: The judge still runs in the agent-modified image; changes outside workdir
(e.g. site-packages) are not detected — a fresh judge image is required in M1b.
"""
from __future__ import annotations

import re
import shlex
from collections.abc import Sequence

from agenten.coding_agent.contracts import AcceptanceCheckV1, AcceptanceReportV1
from agenten.coding_agent.sandbox import SandboxCommand, SandboxPort, SandboxProtocolError

OUTPUT_TAIL_CHARS = 2000

MANIFEST_TIMEOUT_SECONDS = 120
# Top-level cache directories and suffixes only
TOPLEVEL_CACHE_DIRS = frozenset({".pytest_cache", ".mypy_cache", ".ruff_cache"})
PROTECTED_NAMES = frozenset({"conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", ".coveragerc", "sitecustomize.py", "usercustomize.py"})
PROTECTED_SUFFIXES = (".pth",)

# Hex digest pattern: exactly 64 hex digits
HEX_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _manifest_command(workdir: str) -> str:
    # Fixed command with pipefail; NUL-separated "sha256  ./path" records; .git excluded; regular files and symlinks.
    # Note: The sandbox shell is bash.
    return (
        f"set -o pipefail; cd {shlex.quote(workdir)} && "
        "find . -path ./.git -prune -o \\( -type f -o -type l \\) -print0 "
        "| sort -z | xargs -0 -r sha256sum -z --"
    )


def _acceptance_purge_prefix(workdir: str) -> str:
    """Cache purge command: remove .git-excluded caches and prevent bytecode writing."""
    return (
        f"cd {shlex.quote(workdir)} && "
        "find . -path ./.git -prune -o \\( -name __pycache__ -o -name .pytest_cache "
        "-o -name .mypy_cache -o -name .ruff_cache \\) -prune -exec rm -rf {} + && "
        "export PYTHONDONTWRITEBYTECODE=1"
    )


async def run_acceptance(
    sandbox: SandboxPort,
    *,
    image_id: str,
    commands: Sequence[str],
    workdir: str,
    timeout_seconds: int,
) -> AcceptanceReportV1:
    checks: list[AcceptanceCheckV1] = []
    for command in commands:
        # Purge caches and prevent bytecode writing to isolate judge from agent modifications.
        # Wrap user command in subshell to prevent shell operators (;, ||, etc.) from running after purge failure.
        purged_shell = f"{_acceptance_purge_prefix(workdir)} && ( {command} )"
        result = await sandbox.run(
            image_id,
            SandboxCommand(shell=purged_shell, timeout_seconds=timeout_seconds),
        )
        checks.append(
            AcceptanceCheckV1(
                command=command,
                exit_code=result.exit_code,
                timed_out=result.timed_out,
                passed=result.exit_code == 0 and not result.timed_out,
                output_tail=(result.stdout + result.stderr)[-OUTPUT_TAIL_CHARS:],
            )
        )
    return AcceptanceReportV1(checks=tuple(checks))


async def workspace_manifest(sandbox: SandboxPort, *, image_id: str, workdir: str) -> dict[str, str]:
    """Map every workspace file (relative path, no leading './') to its sha256."""
    result = await sandbox.run(image_id, SandboxCommand(shell=_manifest_command(workdir), timeout_seconds=MANIFEST_TIMEOUT_SECONDS))
    if result.exit_code != 0 or result.timed_out:
        raise SandboxProtocolError("workspace manifest could not be computed")
    manifest: dict[str, str] = {}
    for record in result.stdout.split("\0"):
        if not record:
            continue
        digest, sep, path = record.partition("  ")
        if not sep or not path.startswith("./"):
            raise SandboxProtocolError("workspace manifest record is malformed")
        # Validate digest: must be exactly 64 hex characters (fullmatch ensures no trailing chars)
        if not HEX_SHA256_PATTERN.fullmatch(digest):
            raise SandboxProtocolError("workspace manifest record is malformed")
        # Remove "./" prefix and validate non-empty and no ".." segments
        clean_path = path[2:]
        if not clean_path or ".." in clean_path.split("/"):
            raise SandboxProtocolError("workspace manifest record is malformed")
        manifest[clean_path] = digest
    return manifest


def _is_cache(path: str) -> bool:
    """Check if path is a cache file (narrow rules).

    Cache files:
    - .pyc files whose immediate parent is __pycache__ (e.g. src/pkg/__pycache__/m.cpython-311.pyc)
    - Anything under top-level cache directories (.pytest_cache/, .mypy_cache/, .ruff_cache/)
    """
    parts = path.split("/")

    # Check for .pyc with __pycache__ parent
    if path.endswith(".pyc") and len(parts) >= 2 and parts[-2] == "__pycache__":
        return True

    # Check for top-level cache directory (first segment only)
    if parts and parts[0] in TOPLEVEL_CACHE_DIRS:
        return True

    return False


async def changed_paths(sandbox: SandboxPort, *, before_image_id: str, after_image_id: str, workdir: str) -> tuple[str, ...]:
    """Added, removed or modified workspace files between two images (caches excluded)."""
    before = await workspace_manifest(sandbox, image_id=before_image_id, workdir=workdir)
    after = await workspace_manifest(sandbox, image_id=after_image_id, workdir=workdir)
    changed = {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)}
    return tuple(sorted(p for p in changed if not _is_cache(p)))


def protected_paths(paths: Sequence[str], *, explicitly_allowed: Sequence[str] = ()) -> tuple[str, ...]:
    """Changed paths that can rig the judge regardless of allowed_paths.

    A path is protected if:
    - Its basename is in PROTECTED_NAMES (conftest.py, pytest.ini, setup.cfg, pyproject.toml, .coveragerc, etc.)
    - Its basename ends with a PROTECTED_SUFFIX (.pth)
    - It is a .pyc file not in __pycache__ (sourceless, can be placed on sys.path)

    A protected path is exempt only if it appears verbatim (exact string) in explicitly_allowed.
    Directory entries like ("src/",) or (".",) do not exempt nested files.
    """
    allowed_set = set(explicitly_allowed)

    def protected(path: str) -> bool:
        # Check exact match in allowed list first
        if path in allowed_set:
            return False

        name = path.rsplit("/", 1)[-1]

        # Check basename-based protection
        if name in PROTECTED_NAMES or name.endswith(PROTECTED_SUFFIXES):
            return True

        # Check for sourceless .pyc (not in __pycache__)
        if path.endswith(".pyc"):
            parts = path.split("/")
            # Must not have __pycache__ as immediate parent
            if len(parts) < 2 or parts[-2] != "__pycache__":
                return True

        return False

    return tuple(p for p in paths if protected(p))
