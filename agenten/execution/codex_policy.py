"""Local authorization boundary for Codex process launches."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field

from agenten.execution.codex_supervisor import CodexRunRequest


class CodexPolicyViolation(ValueError):
    """Raised when a requested Codex run cannot be launched safely."""


class FrozenEnvironment(Mapping[str, str]):
    """Immutable environment values with a secret-safe representation."""

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, str]) -> None:
        object.__setattr__(self, "_values", MappingProxyType(dict(values)))

    def __setattr__(self, name: str, value: object) -> None:
        raise TypeError("authorized environment is immutable")

    def __getitem__(self, name: str) -> str:
        return self._values[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return f"FrozenEnvironment(keys={tuple(self._values)!r})"

    def copy_for_child(self) -> dict[str, str]:
        """Return a fresh mutable mapping suitable for a child process."""
        return dict(self._values)


class AuthorizedCodexRun(BaseModel):
    """Sanitized launch data, safe to retain as non-secret process metadata."""

    model_config = ConfigDict(
        arbitrary_types_allowed=True, extra="forbid", frozen=True, strict=True
    )

    workspace: Path
    command: tuple[str, ...]
    environment: FrozenEnvironment = Field(exclude=True, repr=False)

    def child_environment(self) -> dict[str, str]:
        """Return an isolated environment copy for the authorized child process."""
        return self.environment.copy_for_child()


class CodexExecutionPolicy:
    """Authorize only fenced Codex commands with a complete delivery trace."""

    _ALLOWED_ENVIRONMENT_NAMES = frozenset(
        {"PATH", "HOME", "USERPROFILE", "LANG", "LC_ALL", "TERM", "NO_COLOR"}
    )
    _ALLOWED_COMMAND_PREFIX = ("codex", "exec", "--json")
    _FORBIDDEN_SECRET_PATH_PATTERNS = (
        re.compile(
            r"(?i)(?<![\w])\.env(?:\.[a-z0-9_-]+)?(?=$|[\\/\s'\"`),:;])"
        ),
        re.compile(
            r"(?i)(?<![\w])id_(?:rsa|ed25519)(?:\.pub)?"
            r"(?=$|[\\/\s'\"`),:;])"
        ),
        re.compile(
            r"(?i)(?:[\\/]private[-_]key(?:\.(?:pem|key|p8|p12))?|"
            r"(?<![\w])private[-_]key\.(?:pem|key|p8|p12))"
            r"(?=$|[\\/\s'\"`),:;])"
        ),
    )

    def __init__(
        self,
        *,
        workspace_root: Path,
        environment: Mapping[str, str],
    ) -> None:
        self._workspace_root = workspace_root.resolve()
        self._environment = FrozenEnvironment(
            {
                name: value
                for name, value in environment.items()
                if name in self._ALLOWED_ENVIRONMENT_NAMES
            }
        )

    def authorize(self, request: CodexRunRequest) -> AuthorizedCodexRun:
        """Return a launch-safe request or reject it before any process starts."""
        self._require_complete_trace_context(request)
        project_root = self._resolve_project_root(request)
        workspace = request.workspace.resolve()
        if not self._is_within(workspace, project_root):
            raise CodexPolicyViolation("workspace is outside the claimed project")
        if not self._is_within(project_root, self._workspace_root):
            raise CodexPolicyViolation("project root is outside the approved root")
        self._require_allowed_command(request.command)
        self._reject_secret_paths(request.command)
        self._reject_dirty_project(project_root)
        return AuthorizedCodexRun(
            workspace=workspace,
            command=request.command,
            environment=self._environment,
        )

    @staticmethod
    def _require_complete_trace_context(request: CodexRunRequest) -> None:
        required_values = (
            request.project_id,
            request.run_id,
            request.trace_id,
            request.batch_id,
            request.worker_id,
            request.claim_id,
            request.fencing_token,
        )
        if any(value is None for value in required_values):
            raise CodexPolicyViolation("complete delivery trace context is required")

    @staticmethod
    def _resolve_project_root(request: CodexRunRequest) -> Path:
        if request.project_root is None:
            raise CodexPolicyViolation("complete delivery trace context is required")
        return request.project_root.resolve()

    @classmethod
    def _require_allowed_command(cls, command: tuple[str, ...]) -> None:
        if (
            len(command) != len(cls._ALLOWED_COMMAND_PREFIX) + 1
            or command[: len(cls._ALLOWED_COMMAND_PREFIX)] != cls._ALLOWED_COMMAND_PREFIX
            or command[-1].lstrip().startswith("-")
        ):
            raise CodexPolicyViolation("command is not in the Codex allowlist")

    @classmethod
    def _reject_secret_paths(cls, command: tuple[str, ...]) -> None:
        for argument in command:
            if any(
                pattern.search(argument)
                for pattern in cls._FORBIDDEN_SECRET_PATH_PATTERNS
            ):
                raise CodexPolicyViolation("command references a forbidden secret path")

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        return path == root or root in path.parents

    @staticmethod
    def _reject_dirty_project(project_root: Path) -> None:
        try:
            result = subprocess.run(
                ("git", "-C", str(project_root), "status", "--porcelain"),
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as error:
            raise CodexPolicyViolation("Git worktree could not be inspected") from error
        if result.stdout:
            raise CodexPolicyViolation("dirty Git worktree cannot be mutated")
