from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/run-capability-live-demo.ps1"


@pytest.fixture
def compatibility_wrapper(tmp_path: Path) -> tuple[Path, Path, Path]:
    repository = tmp_path / "repository"
    scripts = repository / "scripts"
    scripts.mkdir(parents=True)
    wrapper = scripts / SCRIPT.name
    shutil.copy2(SCRIPT, wrapper)
    lifecycle = scripts / "fake-capability-demo.ps1"
    lifecycle.write_text(
        r"""
param(
    [Parameter(Mandatory, Position=0)][string]$Action,
    [switch]$AllowPaidRun,
    [string]$MaxCostUsd,
    [double]$TimeoutSeconds,
    [string]$RepositoryRoot,
    [string]$PythonExecutable
)
[ordered]@{
    action=$Action
    allow_paid_run=$AllowPaidRun.IsPresent
    max_cost_usd=$MaxCostUsd
    timeout_seconds=$TimeoutSeconds
    repository_root=$RepositoryRoot
} | ConvertTo-Json -Compress | Set-Content -LiteralPath $env:CAPTAIN_TEST_CALL_LOG -Encoding utf8NoBOM
exit [int]$env:CAPTAIN_TEST_EXIT_CODE
""".strip(),
        encoding="utf-8",
    )
    return repository, wrapper, lifecycle


def _invoke(
    fixture: tuple[Path, Path, Path],
    *arguments: str,
    exit_code: int = 0,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    repository, wrapper, lifecycle = fixture
    log = repository / "call.json"
    environment = os.environ.copy()
    environment["CAPTAIN_TEST_CALL_LOG"] = str(log)
    environment["CAPTAIN_TEST_EXIT_CODE"] = str(exit_code)
    result = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-File",
            str(wrapper),
            *arguments,
            "-RepositoryRoot",
            str(repository),
            "-PythonExecutable",
            str(Path(os.environ.get("ComSpec", r"C:\Windows\System32\cmd.exe"))),
            "-LifecycleScript",
            str(lifecycle),
        ],
        cwd=repository,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=environment,
    )
    return result, log


@pytest.mark.parametrize(
    "arguments",
    (
        ("start", "-MaxCostUsd", "6.00"),
        ("start", "-ConfirmProviderCost"),
        ("start", "-ConfirmProviderCost", "-MaxCostUsd", "6.01"),
    ),
)
def test_compatibility_start_rejects_missing_or_excess_paid_authority_before_delegate(
    compatibility_wrapper: tuple[Path, Path, Path],
    arguments: tuple[str, ...],
) -> None:
    result, log = _invoke(compatibility_wrapper, *arguments)

    assert result.returncode != 0
    assert not log.exists()


def test_compatibility_start_delegates_to_canonical_run_without_patterns_or_paths(
    compatibility_wrapper: tuple[Path, Path, Path],
) -> None:
    repository, _, _ = compatibility_wrapper

    result, log = _invoke(
        compatibility_wrapper,
        "start",
        "-ConfirmProviderCost",
        "-MaxCostUsd",
        "6.00",
        "-TimeoutSeconds",
        "42",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    call = json.loads(log.read_text(encoding="utf-8-sig"))
    assert call == {
        "action": "run",
        "allow_paid_run": True,
        "max_cost_usd": "6.00",
        "timeout_seconds": 42.0,
        "repository_root": str(repository.resolve()),
    }


def test_compatibility_verify_delegates_to_authenticated_status(
    compatibility_wrapper: tuple[Path, Path, Path],
) -> None:
    result, log = _invoke(compatibility_wrapper, "verify")

    assert result.returncode == 0, result.stdout + result.stderr
    call = json.loads(log.read_text(encoding="utf-8-sig"))
    assert call["action"] == "status"
    assert call["allow_paid_run"] is False


def test_compatibility_wrapper_preserves_terminal_timeout_124(
    compatibility_wrapper: tuple[Path, Path, Path],
) -> None:
    result, log = _invoke(
        compatibility_wrapper,
        "start",
        "-ConfirmProviderCost",
        "-MaxCostUsd",
        "6.00",
        exit_code=124,
    )

    assert log.exists()
    assert result.returncode == 124
