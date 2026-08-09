from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenten.demo_orchestrator.configuration import DemoConfiguration, DemoConfigurationError
from agenten.demo_orchestrator.contracts import CANONICAL_INPUT_PATHS
from agenten.demo_orchestrator.processes import (
    DemoProcessError,
    DemoProcessTimeout,
    ManagedProcessSupervisor,
    ManagedProcessPlan,
    ManagedServicePlan,
    SpawnedProcess,
    WindowsProcessBackend,
    stop_managed_processes,
)


def _configuration(tmp_path: Path) -> DemoConfiguration:
    repository = tmp_path / "repository"
    repository.mkdir()
    state_root = repository / ".captain-cook/live-demo"
    state_root.mkdir(parents=True)
    input_digests: dict[str, str] = {}
    for index, relative in enumerate(CANONICAL_INPUT_PATHS):
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"input {index}\n", encoding="utf-8")
        input_digests[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    (repository / "service.py").write_text("# service\n", encoding="utf-8")
    compose = repository / "docker-compose.test.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    environment = {
        "CAPTAIN_GATEWAY_TOKEN": "gateway-secret",
        "CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN": "proxy-secret",
        "CAPTAIN_RUNTIME_TOKEN": "runtime-secret",
        "CAPTAIN_CAPABILITY_BENCHMARK_TOKEN": "benchmark-secret",
        "MINIBOOK_API_KEY": "minibook-api-secret",
        "MINIBOOK_PROJECTION_API_KEY": "minibook-secret",
        "OPENAI_API_KEY": "provider-secret",
    }
    return DemoConfiguration(
        repository_root=repository,
        state_root=state_root,
        production_manifest=SimpleNamespace(
            manifest_sha256="c" * 64,
            gateway=SimpleNamespace(base_url="http://127.0.0.1:19000"),
            provider_context_reference_header="X-Captain-Provider-Context-Ref",
        ),
        repository_revision="b" * 40,
        canonical_input_digests=input_digests,
        environment=environment,
    )


def _plan(configuration: DemoConfiguration) -> ManagedProcessPlan:
    tokens = {
        "gateway": "CAPTAIN_GATEWAY_TOKEN",
        "provider_proxy": "CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN",
        "runtime": "CAPTAIN_RUNTIME_TOKEN",
        "benchmark": "CAPTAIN_CAPABILITY_BENCHMARK_TOKEN",
    }
    services = tuple(
        ManagedServicePlan(
            name=name,
            argv=("{python}", "-m", f"demo_{name}"),
            working_directory=".",
            port=19000 + offset,
            health_url=f"http://127.0.0.1:{19000 + offset}/healthz",
            health_token_env=token,
            environment=(token,),
            artifact_paths=("service.py",),
        )
        for offset, (name, token) in enumerate(tokens.items())
    )
    return ManagedProcessPlan(
        services=services,
        compose_files=("docker-compose.test.yml",),
        compose_project="captain-capability-demo",
    )


class FakeBackend:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.listeners: dict[int, int] = {}
        self.processes: dict[int, str] = {}
        self.next_pid = 500
        self.health_result = True
        self.health_callback = None
        self.leave_process_alive = False

    def validate_compose(self, files: tuple[Path, ...], project: str) -> None:
        assert files
        self.events.append(f"preflight:compose:{project}")

    def start_compose(self, files: tuple[Path, ...], project: str) -> None:
        self.events.append(f"mutate:compose:{project}")

    def listener_pid(self, port: int) -> int | None:
        self.events.append(f"preflight:listener:{port}")
        return self.listeners.get(port)

    def is_alive(self, pid: int, start_identity: str) -> bool:
        return self.processes.get(pid) == start_identity

    def spawn(
        self,
        argv: tuple[str, ...],
        cwd: Path,
        environment: dict[str, str],
        stdout_path: Path,
    ) -> SpawnedProcess:
        name = argv[-1].removeprefix("demo_")
        self.events.append(f"mutate:spawn:{name}")
        pid = self.next_pid
        self.next_pid += 1
        identity = f"start-{pid}"
        self.processes[pid] = identity
        port = 19000 + ("gateway", "provider_proxy", "runtime", "benchmark").index(name)
        self.listeners[port] = pid
        return SpawnedProcess(pid=pid, start_identity=identity)

    def wait_authenticated_health(self, url: str, token: str, timeout_seconds: float) -> bool:
        assert token.endswith("secret")
        self.events.append(f"health:{url}")
        if self.health_callback is not None:
            self.health_callback()
        return self.health_result

    def terminate(self, pid: int, start_identity: str) -> bool:
        self.events.append(f"terminate:{pid}:{start_identity}")
        if self.processes.get(pid) != start_identity:
            return False
        if not self.leave_process_alive:
            del self.processes[pid]
            self.listeners = {port: owner for port, owner in self.listeners.items() if owner != pid}
        return True

    def kill(self, pid: int, start_identity: str) -> bool:
        self.events.append(f"kill:{pid}:{start_identity}")
        if self.processes.get(pid) != start_identity:
            return False
        if not self.leave_process_alive:
            del self.processes[pid]
            self.listeners = {
                port: owner for port, owner in self.listeners.items() if owner != pid
            }
        return True

    def wait_until_gone(
        self, pid: int, start_identity: str, timeout_seconds: float
    ) -> bool:
        self.events.append(f"wait-gone:{pid}:{start_identity}")
        return not self.is_alive(pid, start_identity)


def test_start_preflights_every_service_before_dependency_ordered_mutation(
    tmp_path: Path,
) -> None:
    configuration = _configuration(tmp_path)
    backend = FakeBackend()
    supervisor = ManagedProcessSupervisor(configuration, plan=_plan(configuration), backend=backend)

    result = supervisor.start_all(timeout_seconds=1)

    first_mutation = next(index for index, event in enumerate(backend.events) if event.startswith("mutate:"))
    assert all(event.startswith("preflight:") for event in backend.events[:first_mutation])
    assert [event for event in backend.events if event.startswith("mutate:spawn:")] == [
        "mutate:spawn:gateway",
        "mutate:spawn:provider_proxy",
        "mutate:spawn:runtime",
        "mutate:spawn:benchmark",
    ]
    assert tuple(item.service for item in result) == (
        "gateway",
        "provider_proxy",
        "runtime",
        "benchmark",
    )
    state = json.loads((configuration.state_root / "managed-processes.json").read_text(encoding="utf-8"))
    assert [item["service"] for item in state["processes"]] == [
        "gateway",
        "provider_proxy",
        "runtime",
        "benchmark",
    ]
    assert "secret" not in json.dumps(state)


def test_start_safely_reuses_only_exact_healthy_managed_configuration(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    backend = FakeBackend()
    supervisor = ManagedProcessSupervisor(configuration, plan=_plan(configuration), backend=backend)
    first = supervisor.start_all(timeout_seconds=1)
    backend.events.clear()

    second = supervisor.start_all(timeout_seconds=1)

    assert [(item.pid, item.config_sha256) for item in second] == [
        (item.pid, item.config_sha256) for item in first
    ]
    assert not any(event.startswith("mutate:") for event in backend.events)
    assert sum(event.startswith("health:") for event in backend.events) == 4


def test_foreign_listener_refuses_start_before_any_mutation(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    backend = FakeBackend()
    backend.listeners[19002] = 999
    supervisor = ManagedProcessSupervisor(configuration, plan=_plan(configuration), backend=backend)

    with pytest.raises(DemoProcessError, match="foreign listener.*19002"):
        supervisor.start_all(timeout_seconds=1)

    assert not any(event.startswith("mutate:") for event in backend.events)


def test_stop_terminates_only_persisted_pid_and_start_identities(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    backend = FakeBackend()
    supervisor = ManagedProcessSupervisor(configuration, plan=_plan(configuration), backend=backend)
    records = supervisor.start_all(timeout_seconds=1)
    backend.processes[999] = "foreign-start"
    backend.events.clear()

    stopped = supervisor.stop()

    assert [item.service for item in stopped] == [
        "benchmark",
        "runtime",
        "provider_proxy",
        "gateway",
    ]
    assert backend.processes == {999: "foreign-start"}
    assert [event for event in backend.events if event.startswith("terminate:")] == [
        f"terminate:{item.pid}:{item.start_identity}" for item in reversed(records)
    ]
    assert not any("compose" in event for event in backend.events)


def test_runtime_restart_keeps_exact_config_and_replaces_only_runtime(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    backend = FakeBackend()
    supervisor = ManagedProcessSupervisor(configuration, plan=_plan(configuration), backend=backend)
    first = supervisor.start_all(timeout_seconds=1)
    backend.events.clear()

    restarted = supervisor.restart_runtime(timeout_seconds=1)

    original = next(item for item in first if item.service == "runtime")
    assert restarted.pid != original.pid
    assert restarted.config_sha256 == original.config_sha256
    assert backend.events[0] == "preflight:listener:19002"
    assert f"terminate:{original.pid}:{original.start_identity}" in backend.events
    assert "mutate:spawn:runtime" in backend.events


def test_status_rejects_healthy_pid_when_sealed_configuration_changed(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    backend = FakeBackend()
    ManagedProcessSupervisor(configuration, plan=_plan(configuration), backend=backend).start_all(timeout_seconds=1)
    changed = replace(
        configuration,
        production_manifest=SimpleNamespace(
            manifest_sha256="f" * 64,
            gateway=configuration.production_manifest.gateway,
            provider_context_reference_header="X-Captain-Provider-Context-Ref",
        ),
    )

    healthy = ManagedProcessSupervisor(changed, plan=_plan(changed), backend=backend).status(timeout_seconds=1)

    assert healthy == ()


def test_stop_uses_persisted_identity_even_when_d11_bundle_is_unavailable(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    backend = FakeBackend()
    records = ManagedProcessSupervisor(configuration, plan=_plan(configuration), backend=backend).start_all(timeout_seconds=1)
    backend.events.clear()

    stopped = stop_managed_processes(configuration.repository_root, backend=backend)

    assert tuple(item.service for item in stopped) == tuple(
        item.service for item in reversed(records)
    )
    assert not backend.processes


def test_stop_fails_when_exact_managed_process_remains_alive(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    backend = FakeBackend()
    ManagedProcessSupervisor(
        configuration, plan=_plan(configuration), backend=backend
    ).start_all(timeout_seconds=1)
    backend.leave_process_alive = True

    with pytest.raises(
        DemoProcessError,
        match="managed process termination could not be verified",
    ):
        stop_managed_processes(configuration.repository_root, backend=backend)

    assert backend.processes


def test_supervisor_stop_retains_and_raises_for_still_alive_exact_process(
    tmp_path: Path,
) -> None:
    configuration = _configuration(tmp_path)
    backend = FakeBackend()
    supervisor = ManagedProcessSupervisor(
        configuration, plan=_plan(configuration), backend=backend
    )
    records = supervisor.start_all(timeout_seconds=1)
    backend.leave_process_alive = True

    with pytest.raises(
        DemoProcessError,
        match="managed process termination could not be verified",
    ):
        supervisor.stop()

    persisted = json.loads(
        (configuration.state_root / "managed-processes.json").read_text(
            encoding="utf-8"
        )
    )
    assert {item["pid"] for item in persisted["processes"]} == {
        item.pid for item in records
    }


def test_missing_service_executable_fails_before_compose_or_process_mutation(
    tmp_path: Path,
) -> None:
    configuration = _configuration(tmp_path)
    plan = _plan(configuration)
    gateway = replace(
        plan.services[0], argv=("Z:\\captain-missing\\gateway.exe", "gateway")
    )
    changed_plan = replace(plan, services=(gateway, *plan.services[1:]))
    backend = FakeBackend()

    with pytest.raises(DemoProcessError, match="service executable is unavailable"):
        ManagedProcessSupervisor(configuration, plan=changed_plan, backend=backend).start_all(timeout_seconds=1)

    assert not any(event.startswith("mutate:") for event in backend.events)


def test_start_without_d11_process_plan_fails_before_any_backend_effect(
    tmp_path: Path,
) -> None:
    configuration = _configuration(tmp_path)
    backend = FakeBackend()

    with pytest.raises(DemoConfigurationError, match="D11 operator process bootstrap is unavailable"):
        ManagedProcessSupervisor(configuration, backend=backend).start_all(timeout_seconds=1)

    assert backend.events == []


def test_spawn_identity_is_persisted_before_health_and_reaped_on_timeout(
    tmp_path: Path,
) -> None:
    configuration = _configuration(tmp_path)
    backend = FakeBackend()
    backend.health_result = False
    observed: list[dict[str, object]] = []

    def observe_state() -> None:
        observed.append(
            json.loads(
                (configuration.state_root / "managed-processes.json").read_text(
                    encoding="utf-8"
                )
            )
        )

    backend.health_callback = observe_state

    with pytest.raises(DemoProcessTimeout, match="authenticated health timed out"):
        ManagedProcessSupervisor(configuration, plan=_plan(configuration), backend=backend).start_all(
            timeout_seconds=1
        )

    assert observed[0]["processes"][0]["pid"] == 500
    assert backend.processes == {}
    final_state = json.loads(
        (configuration.state_root / "managed-processes.json").read_text(encoding="utf-8")
    )
    assert final_state["processes"] == []
    assert "terminate:500:start-500" in backend.events
    assert "wait-gone:500:start-500" in backend.events


def test_failed_spawn_cleanup_retains_identity_when_exact_process_cannot_be_reaped(
    tmp_path: Path,
) -> None:
    configuration = _configuration(tmp_path)
    backend = FakeBackend()
    backend.health_result = False
    backend.leave_process_alive = True

    with pytest.raises(DemoProcessError, match="orphaned managed process gateway"):
        ManagedProcessSupervisor(configuration, plan=_plan(configuration), backend=backend).start_all(
            timeout_seconds=1
        )

    state = json.loads(
        (configuration.state_root / "managed-processes.json").read_text(encoding="utf-8")
    )
    assert state["processes"][0]["pid"] == 500
    assert backend.processes == {500: "start-500"}


def test_windows_listener_probe_accepts_duplicate_wildcard_rows_for_one_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Result:
        returncode = 0
        stdout = "321\n321\n"

    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: Result())

    assert WindowsProcessBackend().listener_pid(19000) == 321


def test_windows_listener_probe_rejects_multiple_owning_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Result:
        returncode = 9
        stdout = ""

    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: Result())

    with pytest.raises(DemoProcessError, match="multiple listeners"):
        WindowsProcessBackend().listener_pid(19000)


@pytest.mark.parametrize(
    "result",
    (
        SimpleNamespace(returncode=1, stdout="", stderr="super-secret-stderr"),
        SimpleNamespace(returncode=0, stdout="not-a-process-tick\n", stderr=""),
    ),
)
def test_windows_identity_probe_rejects_ambiguous_or_malformed_result_redacted(
    monkeypatch: pytest.MonkeyPatch, result: object
) -> None:
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: result)

    with pytest.raises(
        DemoProcessError,
        match="could not inspect process identity",
    ) as error:
        WindowsProcessBackend().is_alive(812, "known-start")

    assert "super-secret-stderr" not in str(error.value)


def test_windows_identity_probe_accepts_only_explicit_absent_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = SimpleNamespace(
        returncode=0,
        stdout="__CAPTAIN_PROCESS_ABSENT__\n",
        stderr="",
    )
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: result)

    assert WindowsProcessBackend().is_alive(812, "known-start") is False


def test_windows_identity_probe_redacts_subprocess_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_: object, **__: object) -> object:
        raise OSError("secret powershell failure")

    monkeypatch.setattr("subprocess.run", fail)

    with pytest.raises(
        DemoProcessError,
        match="could not inspect process identity",
    ) as error:
        WindowsProcessBackend().is_alive(812, "known-start")

    assert "secret powershell failure" not in str(error.value)


def test_stop_retains_persisted_record_on_windows_identity_probe_ambiguity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configuration = _configuration(tmp_path)
    fake = FakeBackend()
    records = ManagedProcessSupervisor(
        configuration, plan=_plan(configuration), backend=fake
    ).start_all(timeout_seconds=1)
    result = SimpleNamespace(
        returncode=1,
        stdout="",
        stderr="secret-inspection-detail",
    )
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: result)

    with pytest.raises(
        DemoProcessError,
        match="could not inspect process identity",
    ) as error:
        stop_managed_processes(
            configuration.repository_root,
            backend=WindowsProcessBackend(),
        )

    persisted = json.loads(
        (configuration.state_root / "managed-processes.json").read_text(
            encoding="utf-8"
        )
    )
    assert {item["pid"] for item in persisted["processes"]} == {
        record.pid for record in records
    }
    assert "secret-inspection-detail" not in str(error.value)


def test_status_health_timeout_remains_terminal_124(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    backend = FakeBackend()
    supervisor = ManagedProcessSupervisor(
        configuration, plan=_plan(configuration), backend=backend
    )
    supervisor.start_all(timeout_seconds=1)
    backend.health_result = False

    with pytest.raises(DemoProcessTimeout, match="authenticated health timed out"):
        supervisor.status(timeout_seconds=1)


def test_windows_spawn_reaps_popen_handle_when_identity_capture_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    class FakePopen:
        pid = 812
        returncode: int | None = None

        def terminate(self) -> None:
            events.append("handle:terminate")

        def wait(self, timeout: float) -> int:
            events.append(f"handle:wait:{timeout}")
            if events.count(f"handle:wait:{timeout}") == 1:
                raise subprocess.TimeoutExpired("fake", timeout)
            self.returncode = -9
            return self.returncode

        def kill(self) -> None:
            events.append("handle:kill")

        def poll(self) -> int | None:
            events.append("handle:poll")
            return self.returncode

    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: FakePopen())
    monkeypatch.setattr(
        WindowsProcessBackend,
        "_start_identity",
        staticmethod(lambda _: None),
    )
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    with pytest.raises(DemoProcessError, match="started process identity is unavailable"):
        WindowsProcessBackend().spawn(
            (sys.executable, "-c", "pass"),
            tmp_path,
            {},
            tmp_path / "spawn.log",
        )

    assert events == [
        "handle:terminate",
        "handle:wait:2.0",
        "handle:kill",
        "handle:wait:2.0",
        "handle:poll",
    ]


def test_windows_spawn_verifies_handle_already_exited_after_cleanup_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    class ExitedPopen:
        pid = 813
        returncode = 0

        def terminate(self) -> None:
            events.append("handle:terminate")
            raise OSError("already exited")

        def wait(self, timeout: float) -> int:
            raise AssertionError("wait must not run after failed terminate")

        def kill(self) -> None:
            events.append("handle:kill")
            raise OSError("already exited")

        def poll(self) -> int | None:
            events.append("handle:poll")
            return self.returncode

    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: ExitedPopen())
    monkeypatch.setattr(
        WindowsProcessBackend,
        "_start_identity",
        staticmethod(lambda _: (_ for _ in ()).throw(RuntimeError("identity failed"))),
    )

    with pytest.raises(DemoProcessError, match="started process identity is unavailable"):
        WindowsProcessBackend().spawn(
            (sys.executable, "-c", "pass"),
            tmp_path,
            {},
            tmp_path / "spawn-exited.log",
        )

    assert events[-1] == "handle:poll"
