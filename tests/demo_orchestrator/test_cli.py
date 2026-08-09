from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agenten.demo_orchestrator.cli import main
from agenten.demo_orchestrator.client import DemoTimeout


ROOT = Path(__file__).resolve().parents[2]
WRAPPER = ROOT / "scripts/capability-demo.ps1"


class FakeActions:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def init(self) -> dict[str, object]:
        self.calls.append(("init", None))
        return {"status": "initialized"}

    def start(self, timeout_seconds: float) -> dict[str, object]:
        self.calls.append(("start", timeout_seconds))
        return {"status": "ready"}

    def status(self, timeout_seconds: float) -> dict[str, object]:
        self.calls.append(("status", timeout_seconds))
        return {"status": "ready"}

    def run(
        self, *, allow_paid_run: bool, maximum_cost_usd: str, timeout_seconds: float
    ) -> dict[str, object]:
        self.calls.append(("run", (allow_paid_run, maximum_cost_usd, timeout_seconds)))
        return {"status": "ready_to_use"}

    def resume(
        self, *, allow_paid_run: bool, maximum_cost_usd: str, timeout_seconds: float
    ) -> dict[str, object]:
        self.calls.append(("resume", (allow_paid_run, maximum_cost_usd, timeout_seconds)))
        return {"status": "ready_to_use"}

    def stop(self) -> dict[str, object]:
        self.calls.append(("stop", None))
        return {"status": "stopped"}


@pytest.mark.parametrize("action", ("init", "start", "status", "stop"))
def test_cli_exposes_non_paid_operator_actions(
    tmp_path: Path, action: str, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeActions()

    exit_code = main(
        [action, "--repository-root", str(tmp_path), "--timeout-seconds", "9"],
        actions_factory=lambda _: fake,
    )

    assert exit_code == 0
    assert fake.calls[0][0] == action
    output = json.loads(capsys.readouterr().out)
    assert "status" in output


@pytest.mark.parametrize("action", ("run", "resume"))
def test_cli_paid_actions_forward_explicit_six_dollar_authority(
    tmp_path: Path, action: str
) -> None:
    fake = FakeActions()

    exit_code = main(
        [
            action,
            "--repository-root",
            str(tmp_path),
            "--allow-paid-run",
            "--max-cost-usd",
            "6.00",
            "--timeout-seconds",
            "30",
        ],
        actions_factory=lambda _: fake,
    )

    assert exit_code == 0
    assert fake.calls == [(action, (True, "6.00", 30.0))]


def test_cli_rejects_unconfirmed_paid_action_before_action_factory(tmp_path: Path) -> None:
    created = False

    def factory(_: Path) -> FakeActions:
        nonlocal created
        created = True
        return FakeActions()

    exit_code = main(
        ["run", "--repository-root", str(tmp_path)],
        actions_factory=factory,
    )

    assert exit_code == 2
    assert created is False


def test_cli_preserves_timeout_exit_124(tmp_path: Path) -> None:
    class TimeoutActions(FakeActions):
        def resume(
            self, *, allow_paid_run: bool, maximum_cost_usd: str, timeout_seconds: float
        ) -> dict[str, object]:
            raise DemoTimeout("bounded timeout")

    exit_code = main(
        [
            "resume",
            "--repository-root",
            str(tmp_path),
            "--allow-paid-run",
            "--max-cost-usd",
            "6.00",
        ],
        actions_factory=lambda _: TimeoutActions(),
    )

    assert exit_code == 124


def test_powershell_wrapper_init_creates_only_gitignored_private_environment(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    root_env = repository / ".env"
    root_env.write_text("OPENAI_API_KEY=operator-owned\n", encoding="utf-8")

    result = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-File",
            str(WRAPPER),
            "init",
            "-RepositoryRoot",
            str(repository),
            "-PythonExecutable",
            sys.executable,
        ],
        cwd=repository,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=os.environ.copy(),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert root_env.read_text(encoding="utf-8") == "OPENAI_API_KEY=operator-owned\n"
    private_env = repository / ".captain-cook/live-demo/private/demo.env"
    assert private_env.is_file()
    assert "OPENAI_API_KEY" not in private_env.read_text(encoding="utf-8")
    assert "operator-owned" not in result.stdout + result.stderr


def test_powershell_wrapper_refuses_paid_run_without_switch_before_bundle_load(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()

    result = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-File",
            str(WRAPPER),
            "run",
            "-RepositoryRoot",
            str(repository),
            "-PythonExecutable",
            sys.executable,
        ],
        cwd=repository,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 2
    assert "paid-run authorization" in result.stderr
    assert "D11" not in result.stdout + result.stderr
