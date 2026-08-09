from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agenten.demo_orchestrator.configuration import DemoConfiguration
from agenten.demo_orchestrator.contracts import CANONICAL_INPUT_PATHS, DemoBundleV1
from agenten.demo_orchestrator.processes import (
    DemoProcessError,
    ManagedProcessSupervisor,
    SpawnedProcess,
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
    artifact = repository / "service.py"
    artifact.write_text("# service\n", encoding="utf-8")
    compose = repository / "docker-compose.test.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    catalog = state_root / "private/catalog.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text("{}\n", encoding="utf-8")
    sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    tokens = {
        "gateway": "CAPTAIN_GATEWAY_TOKEN",
        "provider_proxy": "CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN",
        "runtime": "CAPTAIN_RUNTIME_TOKEN",
        "benchmark": "CAPTAIN_BENCHMARK_TOKEN",
    }
    services = []
    for offset, name in enumerate(tokens):
        environment = [tokens[name]]
        if name == "provider_proxy":
            environment.append("OPENAI_API_KEY")
        services.append(
            {
                "name": name,
                "argv": ["{python}", "-m", f"demo_{name}"],
                "working_directory": ".",
                "port": 19000 + offset,
                "health_url": (
                    "http://127.0.0.1:19000/v1/capability-assemblies/health"
                    if name == "gateway"
                    else f"http://127.0.0.1:{19000 + offset}/health"
                ),
                "health_token_env": tokens[name],
                "environment": environment,
                "artifact_paths": ["service.py"],
            }
        )
    bundle_payload = {
        "schema": "captain.capability-demo-bundle.v1",
        "repository_revision": "b" * 40,
        "canonical_inputs": input_digests,
        "artifact_digests": {"service.py": sha(artifact)},
        "compose_files": {"docker-compose.test.yml": sha(compose)},
        "compose_project": "captain-capability-demo",
        "benchmark_catalog_path": ".captain-cook/live-demo/private/catalog.json",
        "benchmark_catalog_sha256": sha(catalog),
        "gateway_base_url": "http://127.0.0.1:19000",
        "minibook_evidence_url_template": "http://127.0.0.1:19004/v1/projections/{assembly_id}",
        "minibook_token_env": "MINIBOOK_PROJECTION_API_KEY",
        "services": services,
    }
    bundle = DemoBundleV1.model_validate_json(json.dumps(bundle_payload))
    environment = {
        "CAPTAIN_GATEWAY_TOKEN": "gateway-secret",
        "CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN": "proxy-secret",
        "CAPTAIN_RUNTIME_TOKEN": "runtime-secret",
        "CAPTAIN_BENCHMARK_TOKEN": "benchmark-secret",
        "MINIBOOK_PROJECTION_API_KEY": "minibook-secret",
        "OPENAI_API_KEY": "provider-secret",
    }
    return DemoConfiguration(
        repository_root=repository,
        state_root=state_root,
        bundle=bundle,
        bundle_path=state_root / "private/bundle.json",
        bundle_sha256="c" * 64,
        environment=environment,
    )


class FakeBackend:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.listeners: dict[int, int] = {}
        self.processes: dict[int, str] = {}
        self.next_pid = 500

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
        return True

    def terminate(self, pid: int, start_identity: str) -> bool:
        self.events.append(f"terminate:{pid}:{start_identity}")
        if self.processes.get(pid) != start_identity:
            return False
        del self.processes[pid]
        self.listeners = {port: owner for port, owner in self.listeners.items() if owner != pid}
        return True


def test_start_preflights_every_service_before_dependency_ordered_mutation(
    tmp_path: Path,
) -> None:
    configuration = _configuration(tmp_path)
    backend = FakeBackend()
    supervisor = ManagedProcessSupervisor(configuration, backend=backend)

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
    supervisor = ManagedProcessSupervisor(configuration, backend=backend)
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
    supervisor = ManagedProcessSupervisor(configuration, backend=backend)

    with pytest.raises(DemoProcessError, match="foreign listener.*19002"):
        supervisor.start_all(timeout_seconds=1)

    assert not any(event.startswith("mutate:") for event in backend.events)


def test_stop_terminates_only_persisted_pid_and_start_identities(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    backend = FakeBackend()
    supervisor = ManagedProcessSupervisor(configuration, backend=backend)
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
    supervisor = ManagedProcessSupervisor(configuration, backend=backend)
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
    ManagedProcessSupervisor(configuration, backend=backend).start_all(timeout_seconds=1)
    changed = replace(configuration, bundle_sha256="f" * 64)

    healthy = ManagedProcessSupervisor(changed, backend=backend).status(timeout_seconds=1)

    assert healthy == ()


def test_stop_uses_persisted_identity_even_when_d11_bundle_is_unavailable(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    backend = FakeBackend()
    records = ManagedProcessSupervisor(configuration, backend=backend).start_all(timeout_seconds=1)
    backend.events.clear()

    stopped = stop_managed_processes(configuration.repository_root, backend=backend)

    assert tuple(item.service for item in stopped) == tuple(
        item.service for item in reversed(records)
    )
    assert not backend.processes


def test_missing_service_executable_fails_before_compose_or_process_mutation(
    tmp_path: Path,
) -> None:
    configuration = _configuration(tmp_path)
    gateway = configuration.bundle.services[0].model_copy(
        update={"argv": ("Z:\\captain-missing\\gateway.exe", "gateway")}
    )
    changed_bundle = configuration.bundle.model_copy(
        update={"services": (gateway, *configuration.bundle.services[1:])}
    )
    changed = replace(configuration, bundle=changed_bundle)
    backend = FakeBackend()

    with pytest.raises(DemoProcessError, match="service executable is unavailable"):
        ManagedProcessSupervisor(changed, backend=backend).start_all(timeout_seconds=1)

    assert not any(event.startswith("mutate:") for event in backend.events)
