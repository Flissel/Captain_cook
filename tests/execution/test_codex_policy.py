from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from agenten.execution.codex_policy import CodexExecutionPolicy, CodexPolicyViolation
from agenten.execution.codex_supervisor import CodexRunRequest


def _git(project: Path, *arguments: str) -> None:
    subprocess.run(
        ("git", "-C", str(project), *arguments),
        check=True,
        capture_output=True,
        text=True,
    )


def _clean_project(tmp_path: Path) -> tuple[Path, Path, Path]:
    approved_root = tmp_path / "approved"
    project = approved_root / "project"
    workspace = project / "attempts" / "attempt-1"
    workspace.mkdir(parents=True)
    _git(project, "init", "-q")
    _git(project, "config", "user.email", "tests@example.invalid")
    _git(project, "config", "user.name", "Captain Tests")
    (project / ".gitignore").write_text("/attempts/\n", encoding="utf-8")
    (project / "tracked.txt").write_text("clean\n", encoding="utf-8")
    _git(project, "add", ".gitignore", "tracked.txt")
    _git(project, "commit", "-q", "-m", "test fixture")
    return approved_root, project, workspace


def _request(project: Path, workspace: Path, **changes: object) -> CodexRunRequest:
    values: dict[str, object] = {
        "project_id": "project-1",
        "run_id": "run-1",
        "trace_id": "trace-1",
        "batch_id": "batch-1",
        "worker_id": "worker-1",
        "claim_id": "claim-1",
        "fencing_token": 7,
        "session_id": "session-1",
        "claim_token": "claim-credential",
        "iteration": 1,
        "command": ("codex", "exec", "--json", "build the approved batch"),
        "project_root": project,
        "workspace": workspace,
    }
    values.update(changes)
    return CodexRunRequest.model_validate(values)


def test_authorize_returns_resolved_run_with_allowlisted_nonserialized_environment(
    tmp_path: Path,
) -> None:
    approved_root, project, workspace = _clean_project(tmp_path)
    policy = CodexExecutionPolicy(
        workspace_root=approved_root,
        environment={"PATH": "safe-path", "API_TOKEN": "sensitive-value"},
    )

    authorized = policy.authorize(_request(project, workspace))

    assert authorized.workspace == workspace.resolve()
    assert authorized.command[:3] == ("codex", "exec", "--json")
    assert authorized.environment == {"PATH": "safe-path"}
    serialized = authorized.model_dump_json()
    assert "safe-path" not in serialized
    assert "sensitive-value" not in serialized
    assert "safe-path" not in repr(authorized)


@pytest.mark.parametrize(
    "field",
    [
        "project_id",
        "run_id",
        "trace_id",
        "batch_id",
        "worker_id",
        "claim_id",
        "fencing_token",
        "project_root",
    ],
)
def test_authorize_requires_complete_delivery_trace(
    tmp_path: Path, field: str
) -> None:
    approved_root, project, workspace = _clean_project(tmp_path)
    values = _request(project, workspace).model_dump()
    values.pop(field)

    with pytest.raises(CodexPolicyViolation, match="trace context"):
        CodexExecutionPolicy(
            workspace_root=approved_root, environment={}
        ).authorize(CodexRunRequest.model_validate(values))


def test_traversal_outside_claimed_project_is_rejected(tmp_path: Path) -> None:
    approved_root, project, _ = _clean_project(tmp_path)
    traversal = project / "attempts" / ".." / ".." / "outside"
    traversal.mkdir()

    with pytest.raises(CodexPolicyViolation, match="workspace"):
        CodexExecutionPolicy(workspace_root=approved_root, environment={}).authorize(
            _request(project, traversal)
        )


def test_symlink_escape_is_rejected_using_resolved_filesystem_paths(tmp_path: Path) -> None:
    approved_root, project, workspace = _clean_project(tmp_path)
    outside = approved_root / "outside"
    outside.mkdir()
    workspace.rmdir()
    try:
        os.symlink(outside, workspace, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlinks are unavailable in this environment: {error}")

    with pytest.raises(CodexPolicyViolation, match="workspace"):
        CodexExecutionPolicy(workspace_root=approved_root, environment={}).authorize(
            _request(project, workspace)
        )


def test_forbidden_secret_path_is_rejected(tmp_path: Path) -> None:
    approved_root, project, workspace = _clean_project(tmp_path)

    with pytest.raises(CodexPolicyViolation, match="secret path"):
        CodexExecutionPolicy(workspace_root=approved_root, environment={}).authorize(
            _request(
                project,
                workspace,
                command=("codex", "exec", "--json", str(project / ".env")),
            )
        )


def test_dirty_git_project_is_rejected_before_mutating_codex_run(tmp_path: Path) -> None:
    approved_root, project, workspace = _clean_project(tmp_path)
    (project / "tracked.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(CodexPolicyViolation, match="dirty"):
        CodexExecutionPolicy(workspace_root=approved_root, environment={}).authorize(
            _request(project, workspace)
        )


def test_command_outside_allowlist_is_rejected(tmp_path: Path) -> None:
    approved_root, project, workspace = _clean_project(tmp_path)

    with pytest.raises(CodexPolicyViolation, match="allowlist"):
        CodexExecutionPolicy(workspace_root=approved_root, environment={}).authorize(
            _request(project, workspace, command=("powershell", "-Command", "Get-ChildItem"))
        )


def test_codex_command_without_the_jsonl_allowlist_is_rejected(tmp_path: Path) -> None:
    approved_root, project, workspace = _clean_project(tmp_path)

    with pytest.raises(CodexPolicyViolation, match="allowlist"):
        CodexExecutionPolicy(workspace_root=approved_root, environment={}).authorize(
            _request(project, workspace, command=("codex", "exec", "build"))
        )
