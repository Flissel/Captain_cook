from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[2]
HERMES = ROOT / "hermes-agent"
READINESS_VERIFIER = ROOT / "scripts" / "verify_hermes_readiness.ps1"


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def gitlink_commit(root: Path, path: str | None = None) -> str:
    if path is None:
        return _git("rev-parse", "HEAD", cwd=root)
    entry = _git("ls-tree", "HEAD", "--", path, cwd=root)
    mode, object_type, commit, _ = entry.split(maxsplit=3)
    assert mode == "160000"
    assert object_type == "commit"
    return commit


def pinned_parent_gitlink(root: Path, path: str) -> str:
    return gitlink_commit(root, path)


def test_pinned_hermes_runtime_exposes_required_surfaces() -> None:
    assert (HERMES / "hermes_cli" / "captain_planner.py").is_file()
    assert (HERMES / "hermes_cli" / "mcp_config.py").is_file()
    assert (HERMES / "tests" / "fixtures" / "captain_work_package_released.v1.json").is_file()
    assert gitlink_commit(HERMES) == pinned_parent_gitlink(ROOT, "hermes-agent")
    assert READINESS_VERIFIER.is_file()


def test_readiness_verifier_emits_only_redacted_readiness_fields() -> None:
    result = subprocess.run(
        [
            "powershell",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(READINESS_VERIFIER),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.splitlines() == [
        "hermes_commit=a5199779876455ece6aa7c1220de70bf3f62ece2",
        "entrypoints=hermes_cli/captain_planner.py,hermes_cli/mcp_config.py",
        "tests=passed",
        "n8n_server=n8n-mcp",
    ]
    assert "N8N_API_KEY" not in result.stdout
    assert "N8N_MCP_TOKEN" not in result.stdout
