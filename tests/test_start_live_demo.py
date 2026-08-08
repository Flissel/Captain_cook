import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
START = ROOT / "scripts" / "start-live-demo.ps1"


def _write_child(path: Path, name: str) -> None:
    path.write_text(
        "\n".join(
            [
                "[CmdletBinding()]",
                "param(",
                "  [Parameter(Position = 0)][string]$Action,",
                "  [Parameter(ValueFromRemainingArguments = $true)][string[]]$Remaining",
                ")",
                "$ErrorActionPreference = 'Stop'",
                f"if ($env:LIVE_DEMO_FAIL_CHILD -eq ('{name}:' + $Action)) {{ throw 'forced {name} failure' }}",
                "if ($Action -notin @('start', 'status')) { throw 'unexpected action' }",
                f"Write-Output ('child-called {name} action=' + $Action + ' identity={name}-stable-id endpoint=http://127.0.0.1/{'7011' if name == 'service' else '7012'} status=ready')",
            ]
            + (
                [
                    "Write-Output ('capability-cost=' + $Remaining[$Remaining.IndexOf('-MaxCostUsd') + 1])"
                    if name == "capability"
                    else ""
                ]
                if name == "capability"
                else []
            )
        ),
        encoding="utf-8",
    )


@pytest.fixture
def start_script(tmp_path: Path) -> Path:
    script_dir = tmp_path / "scripts"
    script_dir.mkdir()
    copied = script_dir / START.name
    shutil.copy2(START, copied)
    return copied


@pytest.fixture
def child_commands(tmp_path: Path) -> tuple[Path, Path, Path]:
    commands = tuple(tmp_path / f"{name}.ps1" for name in ("service", "minibook", "capability"))
    for path, name in zip(commands, ("service", "minibook", "capability"), strict=True):
        _write_child(path, name)
    return commands


def invoke_start_script(
    start_script: Path,
    child_commands: tuple[Path, Path, Path],
    *,
    run_demo: bool = False,
    confirm: bool = False,
    budget: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    service, minibook, capability = child_commands
    command = [
        "pwsh",
        "-NoProfile",
        "-File",
        str(start_script),
        "-ServiceCommand",
        str(service),
        "-MinibookCommand",
        str(minibook),
        "-CapabilityCommand",
        str(capability),
    ]
    if run_demo:
        command.append("-RunDemo")
    if confirm:
        command.append("-ConfirmProviderCost")
    if budget is not None:
        command.extend(("-MaxCostUsd", budget))
    process_env = os.environ.copy()
    process_env.update(env or {})
    return subprocess.run(
        command,
        cwd=tmp_path_for(start_script),
        env=process_env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def tmp_path_for(start_script: Path) -> Path:
    return start_script.parent.parent


def test_default_start_uses_only_start_and_status_without_capability(
    start_script: Path, child_commands: tuple[Path, Path, Path]
) -> None:
    result = invoke_start_script(start_script, child_commands)

    assert result.returncode == 0, result.stdout + result.stderr
    output = result.stdout + result.stderr
    assert "child-called service action=start" in output
    assert "child-called service action=status" in output
    assert "child-called minibook action=start" in output
    assert "child-called minibook action=status" in output
    assert "child-called capability" not in output
    assert "[ready] Live demo infrastructure is healthy" in output


def test_repeated_default_start_reuses_child_identities(
    start_script: Path, child_commands: tuple[Path, Path, Path]
) -> None:
    first = invoke_start_script(start_script, child_commands)
    second = invoke_start_script(start_script, child_commands)

    assert first.returncode == second.returncode == 0
    for identity in ("service-stable-id", "minibook-stable-id"):
        assert identity in first.stdout
        assert identity in second.stdout


@pytest.mark.parametrize("budget", [None, "0", "-1", "NaN", "Infinity", "6,00"])
def test_paid_demo_rejects_invalid_budget_before_children(
    start_script: Path, child_commands: tuple[Path, Path, Path], budget: str | None
) -> None:
    result = invoke_start_script(
        start_script, child_commands, run_demo=True, confirm=True, budget=budget
    )

    assert result.returncode != 0
    assert "child-called" not in result.stdout


def test_paid_demo_requires_confirmation_before_children(
    start_script: Path, child_commands: tuple[Path, Path, Path]
) -> None:
    result = invoke_start_script(start_script, child_commands, run_demo=True, budget="6.00")

    assert result.returncode != 0
    assert "child-called" not in result.stdout


def test_paid_demo_runs_after_health_checks_with_one_total_ceiling(
    start_script: Path, child_commands: tuple[Path, Path, Path]
) -> None:
    result = invoke_start_script(
        start_script, child_commands, run_demo=True, confirm=True, budget="6.00"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    output = result.stdout + result.stderr
    assert output.index("child-called service action=status") < output.index(
        "child-called minibook action=start"
    )
    assert output.index("child-called minibook action=status") < output.index(
        "child-called capability action=start"
    )
    assert output.count("capability-cost=6.00") == 1


def test_child_failure_never_prints_green_summary(
    start_script: Path, child_commands: tuple[Path, Path, Path]
) -> None:
    result = invoke_start_script(
        start_script,
        child_commands,
        env={"LIVE_DEMO_FAIL_CHILD": "minibook:status"},
    )

    assert result.returncode != 0
    assert "[ready] Live demo infrastructure is healthy" not in result.stdout
