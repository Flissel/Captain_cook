"""Independent acceptance checks and change detection inside the sandbox.

Note: The judge still runs in the agent-modified image; changes outside workdir
(e.g. site-packages) are not detected — a fresh judge image is required in M1b.
"""
from __future__ import annotations

import posixpath
import re
import shlex
from collections.abc import Sequence
from dataclasses import dataclass

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


# Symlink records: "L", "./path", target kind (find %Y), link text, each NUL-terminated.
_LINK_TAG = "L"
_LINK_PREFIX = "symlink:"
# find %Y: type of what the link points at (N = dangling, L = loop, ? = error).
_LINK_KINDS = frozenset("bcdpfslDLN?")


@dataclass(frozen=True)
class SymlinkEntry:
    """A workspace symlink, recorded by its link text and never followed."""

    path: str
    target: str
    target_kind: str


@dataclass(frozen=True)
class WorkspaceChanges:
    """Changed workspace paths (caches excluded) and the changed symlinks of the after image."""

    paths: tuple[str, ...]
    symlinks: tuple[SymlinkEntry, ...]


@dataclass(frozen=True)
class _Manifest:
    values: dict[str, str]
    links: dict[str, SymlinkEntry]


def _manifest_command(workdir: str) -> str:
    # Fixed command with pipefail; .git excluded; the sandbox shell is bash (GNU find/coreutils).
    # Regular files: NUL-terminated "sha256  ./path" records (sha256sum follows links, so it never sees one).
    # Symlinks: NUL-separated "L ./path kind target" records from the link text; never followed.
    return (
        f"set -o pipefail; cd {shlex.quote(workdir)} && "
        "find . -path ./.git -prune -o -type f -print0 "
        "| sort -z | xargs -0 -r sha256sum -z -- && "
        "find . -path ./.git -prune -o -type l -printf 'L\\0%p\\0%Y\\0%l\\0'"
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
        # The newline before ")" keeps a trailing "#" comment from swallowing the closing parenthesis.
        purged_shell = f"{_acceptance_purge_prefix(workdir)} && ( {command}\n)"
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


def _clean_manifest_path(path: str) -> str:
    if not path.startswith("./"):
        raise SandboxProtocolError("workspace manifest record is malformed")
    # Remove "./" prefix and validate non-empty and no ".." segments
    clean_path = path[2:]
    if not clean_path or ".." in clean_path.split("/"):
        raise SandboxProtocolError("workspace manifest record is malformed")
    return clean_path


def _parse_manifest(stdout: str) -> _Manifest:
    """Strictly parse file and symlink records; any deviation is a protocol error."""
    if stdout and not stdout.endswith("\0"):
        raise SandboxProtocolError("workspace manifest record is malformed")
    fields = stdout.split("\0")[:-1] if stdout else []
    values: dict[str, str] = {}
    links: dict[str, SymlinkEntry] = {}
    index = 0
    while index < len(fields):
        field = fields[index]
        if field == _LINK_TAG:
            if index + 3 >= len(fields):
                raise SandboxProtocolError("workspace manifest record is malformed")
            path = _clean_manifest_path(fields[index + 1])
            kind, target = fields[index + 2], fields[index + 3]
            if len(kind) != 1 or kind not in _LINK_KINDS or not target or path in values:
                raise SandboxProtocolError("workspace manifest record is malformed")
            values[path] = _LINK_PREFIX + target
            links[path] = SymlinkEntry(path=path, target=target, target_kind=kind)
            index += 4
            continue
        digest, sep, raw_path = field.partition("  ")
        # Validate digest: must be exactly 64 hex characters (fullmatch ensures no trailing chars)
        if not sep or not HEX_SHA256_PATTERN.fullmatch(digest):
            raise SandboxProtocolError("workspace manifest record is malformed")
        path = _clean_manifest_path(raw_path)
        if path in values:
            raise SandboxProtocolError("workspace manifest record is malformed")
        values[path] = digest
        index += 1
    return _Manifest(values=values, links=links)


async def _read_manifest(sandbox: SandboxPort, *, image_id: str, workdir: str) -> _Manifest:
    result = await sandbox.run(image_id, SandboxCommand(shell=_manifest_command(workdir), timeout_seconds=MANIFEST_TIMEOUT_SECONDS))
    if result.exit_code != 0 or result.timed_out:
        raise SandboxProtocolError("workspace manifest could not be computed")
    return _parse_manifest(result.stdout)


async def workspace_manifest(sandbox: SandboxPort, *, image_id: str, workdir: str) -> dict[str, str]:
    """Map every workspace entry (relative path, no leading './') to its sha256 or "symlink:<link text>"."""
    return (await _read_manifest(sandbox, image_id=image_id, workdir=workdir)).values


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


async def workspace_changes(
    sandbox: SandboxPort, *, before_image_id: str, after_image_id: str, workdir: str
) -> WorkspaceChanges:
    """Added, removed, modified or retargeted entries between two images.

    `paths` excludes caches; `symlinks` lists every changed path that is a symlink in the
    after image (caches included, so an unsafe link cannot hide in a cache directory).
    """
    before = await _read_manifest(sandbox, image_id=before_image_id, workdir=workdir)
    after = await _read_manifest(sandbox, image_id=after_image_id, workdir=workdir)
    changed = sorted(
        p for p in before.values.keys() | after.values.keys() if before.values.get(p) != after.values.get(p)
    )
    return WorkspaceChanges(
        paths=tuple(p for p in changed if not _is_cache(p)),
        symlinks=tuple(after.links[p] for p in changed if p in after.links),
    )


async def changed_paths(sandbox: SandboxPort, *, before_image_id: str, after_image_id: str, workdir: str) -> tuple[str, ...]:
    """Added, removed or modified workspace entries between two images (caches excluded)."""
    changes = await workspace_changes(
        sandbox, before_image_id=before_image_id, after_image_id=after_image_id, workdir=workdir
    )
    return changes.paths


def unsafe_symlinks(links: Sequence[SymlinkEntry]) -> tuple[str, ...]:
    """Symlinks that can reach outside the tracked workspace.

    A link is unsafe when its target is absolute, resolves (lexically, relative to the
    link's directory) outside the workdir or into the untracked .git directory, or points
    at a directory (writes through it land in a tree the manifest does not follow).
    """

    def unsafe(link: SymlinkEntry) -> bool:
        if link.target_kind == "d" or link.target.startswith("/"):
            return True
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(link.path), link.target))
        if resolved == ".." or resolved.startswith("../"):
            return True
        return resolved == ".git" or resolved.startswith(".git/")

    return tuple(link.path for link in links if unsafe(link))


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
