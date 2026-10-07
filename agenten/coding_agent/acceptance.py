"""Independent acceptance checks and change detection inside the sandbox.

Note: The judge still runs in the agent-modified image; changes outside workdir
(e.g. site-packages) are not detected — a fresh judge image is required in M1b.
"""
from __future__ import annotations

import shlex
from collections.abc import Sequence

from agenten.coding_agent.contracts import AcceptanceCheckV1, AcceptanceReportV1
from agenten.coding_agent.sandbox import SandboxCommand, SandboxPort, SandboxProtocolError

OUTPUT_TAIL_CHARS = 2000

MANIFEST_TIMEOUT_SECONDS = 120
CACHE_SEGMENTS = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"})
PROTECTED_NAMES = frozenset({"conftest.py", "pytest.ini", "tox.ini", "sitecustomize.py", "usercustomize.py"})
PROTECTED_SUFFIXES = (".pth",)


def _manifest_command(workdir: str) -> str:
    # Fixed command; NUL-separated "sha256  ./path" records; .git excluded; regular files and symlinks.
    return (
        f"cd {shlex.quote(workdir)} && "
        "find . -path ./.git -prune -o \\( -type f -o -type l \\) -print0 "
        "| sort -z | xargs -0 -r sha256sum -z --"
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
        result = await sandbox.run(
            image_id,
            SandboxCommand(shell=f"cd {shlex.quote(workdir)} && {command}", timeout_seconds=timeout_seconds),
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
        if not sep or len(digest) != 64 or not path.startswith("./"):
            raise SandboxProtocolError("workspace manifest record is malformed")
        manifest[path[2:]] = digest
    return manifest


def _is_cache(path: str) -> bool:
    return any(part in CACHE_SEGMENTS for part in path.split("/")) or path.endswith(".pyc")


async def changed_paths(sandbox: SandboxPort, *, before_image_id: str, after_image_id: str, workdir: str) -> tuple[str, ...]:
    """Added, removed or modified workspace files between two images (caches excluded)."""
    before = await workspace_manifest(sandbox, image_id=before_image_id, workdir=workdir)
    after = await workspace_manifest(sandbox, image_id=after_image_id, workdir=workdir)
    changed = {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)}
    return tuple(sorted(p for p in changed if not _is_cache(p)))


def protected_paths(paths: Sequence[str]) -> tuple[str, ...]:
    """Changed paths that can rig the judge regardless of allowed_paths."""
    def protected(path: str) -> bool:
        name = path.rsplit("/", 1)[-1]
        return name in PROTECTED_NAMES or name.endswith(PROTECTED_SUFFIXES)
    return tuple(p for p in paths if protected(p))
