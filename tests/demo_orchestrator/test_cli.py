from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agenten.demo_orchestrator.cli import DefaultDemoActions, main
from agenten.demo_orchestrator.client import DemoTimeout
from agenten.demo_orchestrator.processes import DemoProcessError, DemoProcessTimeout


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


def test_default_actions_executes_all_six_exact_d11_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = ("gateway", "runtime", "provider_proxy", "benchmark", "minibook", "n8n")
    controls = tuple(
        SimpleNamespace(
            service=service,
            start_argv=("python", "-m", "gateway.capability_process_operator", "--service", service, "start"),
            stop_argv=("python", "-m", "gateway.capability_process_operator", "--service", service, "stop"),
            restart_argv=("python", "-m", "gateway.capability_process_operator", "--service", service, "restart"),
        )
        for service in services
    )
    configuration = SimpleNamespace(
        environment={"CAPTAIN_PROCESS_OPERATOR_BACKEND": "sentinel"},
        require_operator_process_bootstrap=lambda: SimpleNamespace(services=controls),
    )
    monkeypatch.setattr(
        "agenten.demo_orchestrator.cli.load_demo_configuration", lambda _root: configuration
    )
    calls: list[tuple[str, ...]] = []

    def run(argv, **_kwargs):
        command = tuple(argv)
        calls.append(command)
        service = command[-2]
        action = command[-1]
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "service": service, "status": "running", "operation": action,
                "identity_kind": "sentinel", "identity_value": f"{service}-{action}",
            }),
        )

    monkeypatch.setattr("agenten.demo_orchestrator.cli.subprocess.run", run)

    result = DefaultDemoActions(tmp_path).start(3)

    assert result["services"] == list(services)
    assert tuple(command[-2] for command in calls) == services
    assert all(command[-1] == "start" for command in calls)


def test_default_status_revalidates_all_six_authenticated_health_contracts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = ("gateway", "runtime", "provider_proxy", "benchmark", "minibook", "n8n")
    state_root = tmp_path / "state"
    state_root.mkdir()
    controls = tuple(
        SimpleNamespace(
            service=service,
            authenticated_health_url=f"http://127.0.0.1/{service}/authenticated-health",
            authenticated_health_credential_env=f"{service.upper()}_TOKEN",
            authenticated_health_header=("X-N8N-API-KEY" if service == "n8n" else "Authorization"),
        )
        for service in services
    )
    for service in services:
        (state_root / f"{service}.json").write_text(
            json.dumps({
                "service": service, "status": "running",
                "identity_kind": "docker_container" if service == "n8n" else "os_process",
                "identity_value": f"verified-{service}",
            }),
            encoding="utf-8",
        )
    configuration = SimpleNamespace(
        environment={"CAPTAIN_PROCESS_STATE_ROOT": str(state_root)},
        require_operator_process_bootstrap=lambda: SimpleNamespace(services=controls),
        authority_token=lambda name: f"secret-for-{name}",
    )
    calls: list[tuple[str, str, str]] = []

    class Health:
        def request_json(self, _method, url, *, token, auth_header, **_kwargs):
            calls.append((url, token, auth_header))
            return {} if "n8n" in url else {"status": "ready"}

    monkeypatch.setattr("agenten.demo_orchestrator.cli.load_demo_configuration", lambda _root: configuration)
    monkeypatch.setattr("agenten.demo_orchestrator.cli.UrllibJsonHttp", Health)

    result = DefaultDemoActions(tmp_path).status(3)

    assert result["services"] == list(services)
    assert calls == [
        (
            control.authenticated_health_url,
            f"secret-for-{control.authenticated_health_credential_env}",
            control.authenticated_health_header,
        )
        for control in controls
    ]


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


def test_cli_preserves_service_health_timeout_exit_124(tmp_path: Path) -> None:
    class TimeoutActions(FakeActions):
        def start(self, timeout_seconds: float) -> dict[str, object]:
            raise DemoProcessTimeout("authenticated health timed out")

    exit_code = main(
        ["start", "--repository-root", str(tmp_path)],
        actions_factory=lambda _: TimeoutActions(),
    )

    assert exit_code == 124


def test_cli_stop_failure_never_prints_stopped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class FailedStopActions(FakeActions):
        def stop(self) -> dict[str, object]:
            raise DemoProcessError(
                "managed process termination could not be verified"
            )

    exit_code = main(
        ["stop", "--repository-root", str(tmp_path)],
        actions_factory=lambda _: FailedStopActions(),
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "stopped" not in captured.out
    assert "termination could not be verified" in captured.err


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
