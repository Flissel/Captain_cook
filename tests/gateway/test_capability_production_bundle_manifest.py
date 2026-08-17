from __future__ import annotations

import json
import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from gateway.capability_assembly_bootstrap import (
    CapabilityAssemblyBootstrapConfigurationError,
    load_capability_production_bundle_manifest,
)
from gateway.capability_process_operator import operate, service_argv
import gateway.capability_process_operator as process_operator
from gateway.settings import GatewaySettings
from agenten.agent_runtime.runtime_entrypoint import RuntimeEntrypointSettings
from agenten.provider_proxy.entrypoint import require_distinct_authority_secrets


def _environment() -> dict[str, str]:
    return {
        "CAPTAIN_GATEWAY_URL": "http://127.0.0.1:8090",
        "CAPTAIN_GATEWAY_TOKEN": "gateway-token-value",
        "CAPTAIN_CAPABILITY_BENCHMARK_URL": "http://127.0.0.1:8092/v1/execute",
        "CAPTAIN_CAPABILITY_BENCHMARK_TOKEN": "benchmark-token-value",
        "CAPTAIN_RUNTIME_URL": "http://127.0.0.1:8091",
        "CAPTAIN_RUNTIME_TOKEN": "runtime-token-value",
        "CAPTAIN_PROVIDER_PROXY_URL": "http://127.0.0.1:18091",
        "CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN": "provider-token-value",
        "CAPTAIN_PROVIDER_RECEIPT_GATEWAY_TOKEN": "receipt-token-value",
        "CAPTAIN_PROVIDER_POLICY_SHA256": "a" * 64,
        "CAPTAIN_PROVIDER_PRICE_CARD_SHA256": "b" * 64,
        "CAPTAIN_PROVIDER_CONTEXT_SHA256": "c" * 64,
        "CAPTAIN_PROVIDER_PROXY_EXECUTABLE_SHA256": "d" * 64,
        "CAPTAIN_PROVIDER_PROXY_CODE_SHA256": "e" * 64,
        "CAPTAIN_PROVIDER_PAID_COMMAND_SHA256": "f" * 64,
        "CAPTAIN_PROVIDER_HARD_CEILING_ENFORCED": "true",
        "MINIBOOK_BACKEND_URL": "http://127.0.0.1:8080",
        "MINIBOOK_API_KEY": "minibook-token-value",
        "MINIBOOK_PROJECTION_API_KEY": "projection-token-value",
        "CAPTAIN_N8N_URL": "http://127.0.0.1:5679",
        "CAPTAIN_N8N_API_KEY": "n8n-token-value",
    }


def test_operator_manifest_is_digest_bound_and_contains_real_process_contracts() -> None:
    environment = _environment()

    manifest = load_capability_production_bundle_manifest(environment=environment)

    assert manifest.schema_name == "captain.capability-production-bundle-manifest.v1"
    assert manifest.provider_supervisor_mode == "dynamic_gateway_context_v1"
    assert manifest.provider_context_reference_header == "X-Captain-Provider-Context-Ref"
    assert (
        manifest.provider_policy_sha256,
        manifest.provider_price_card_sha256,
        manifest.provider_context_sha256,
        manifest.provider_proxy_executable_sha256,
        manifest.provider_proxy_code_sha256,
        manifest.provider_paid_command_sha256,
        manifest.provider_hard_ceiling_enforced,
    ) == ("a" * 64, "b" * 64, "c" * 64, "d" * 64, "e" * 64, "f" * 64, True)
    assert manifest.gateway.health_url == (
        "http://127.0.0.1:8090/v1/capability-assemblies/health"
    )
    assert manifest.benchmark.execute_url == "http://127.0.0.1:8092/v1/execute"
    assert manifest.benchmark.health_url == "http://127.0.0.1:8092/healthz"
    assert manifest.provider_proxy.health_url == "http://127.0.0.1:18091/v1/healthz"
    assert manifest.runtime.health_url == "http://127.0.0.1:8091/health"
    assert manifest.minibook.health_url == "http://127.0.0.1:8080/health"
    assert manifest.gateway.credential_env == "CAPTAIN_GATEWAY_TOKEN"
    assert manifest.benchmark.credential_env == "CAPTAIN_CAPABILITY_BENCHMARK_TOKEN"
    assert manifest.runtime.credential_env == "CAPTAIN_RUNTIME_TOKEN"
    assert manifest.provider_proxy.credential_env == "CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN"
    assert manifest.minibook.credential_env == "MINIBOOK_API_KEY"
    assert manifest.minibook.authenticated_health_url == (
        "http://127.0.0.1:8080/api/v1/projection-health"
    )
    assert manifest.minibook.authenticated_health_credential_env == (
        "MINIBOOK_PROJECTION_API_KEY"
    )
    assert manifest.minibook_evidence_url_template.endswith(
        "/api/v1/projection-assemblies/{assembly_id}/evidence"
    )
    assert manifest.n8n.credential_env == "CAPTAIN_N8N_API_KEY"
    assert manifest.n8n.authenticated_health_url.endswith(
        "/api/v1/workflows?limit=1"
    )
    assert manifest.n8n.authenticated_health_header == "X-N8N-API-KEY"
    assert manifest.n8n.authentication == "x-n8n-api-key"
    assert all(
        binding.authenticated_health_header == "Authorization"
        for binding in (
            manifest.gateway, manifest.runtime, manifest.provider_proxy,
            manifest.benchmark, manifest.minibook,
        )
    )
    for binding in (
        manifest.gateway, manifest.provider_proxy, manifest.runtime,
        manifest.benchmark, manifest.minibook, manifest.n8n,
    ):
        assert binding.start_argv
        assert binding.stop_argv
        assert binding.restart_argv
        assert binding.start_argv[-1] == "start"
        assert binding.stop_argv[-1] == "stop"
        assert binding.restart_argv[-1] == "restart"
        assert binding.start_argv[:-1] == binding.stop_argv[:-1]
        assert binding.start_argv[:-1] == binding.restart_argv[:-1]
    assert len(manifest.manifest_sha256) == 64
    assert manifest.manifest_sha256 == load_capability_production_bundle_manifest(
        environment=environment
    ).manifest_sha256
    rendered = json.dumps(manifest.model_dump(mode="json"), sort_keys=True)
    assert all(value not in rendered for name, value in environment.items() if "TOKEN" in name or "KEY" in name)


def test_operator_executes_all_six_semantic_lifecycles_with_new_restart_identity(
    tmp_path: Path,
) -> None:
    environment = {
        **_environment(),
        "CAPTAIN_PROCESS_OPERATOR_BACKEND": "sentinel",
        "CAPTAIN_PROCESS_STATE_ROOT": "state",
        "CAPTAIN_PROVIDER_PRICE_CARD_PATH": "contracts/price.json",
        "CAPTAIN_PROVIDER_POLICY_PATH": "contracts/policy.json",
        "CAPTAIN_PROVIDER_STATE_ROOT": "state/provider",
    }
    expected_modules = {
        "gateway": "gateway.app",
        "runtime": "agenten.agent_runtime.runtime_entrypoint",
        "provider_proxy": "agenten.provider_proxy.entrypoint",
        "benchmark": "gateway.capability_benchmark_process_app",
        "minibook": "minibook.src.main",
    }
    for service in (
        "gateway", "runtime", "provider_proxy", "benchmark", "minibook", "n8n"
    ):
        command = service_argv(
            service, environment=environment, repository_root=tmp_path
        )
        if service == "n8n":
            assert command[-2:] == (
                str((tmp_path / "scripts" / "captain-n8n.ps1").resolve()), "start"
            )
        else:
            assert command[1:3] == ("-m", expected_modules[service])
        started = operate(
            service, "start", environment=environment, repository_root=tmp_path
        )
        replayed = operate(
            service, "start", environment=environment, repository_root=tmp_path
        )
        restarted = operate(
            service, "restart", environment=environment, repository_root=tmp_path
        )
        stopped = operate(
            service, "stop", environment=environment, repository_root=tmp_path
        )
        assert replayed["process_identity"] == started["process_identity"]
        assert started["identity_kind"] == "sentinel"
        assert started["identity_value"] == started["process_identity"]
        assert restarted["process_identity"] != started["process_identity"]
        assert restarted["operation"] == "restart"
        assert stopped["status"] == "stopped"


def test_operator_refuses_to_stop_a_reused_process_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        process_operator,
        "_observed_process_identity",
        lambda _pid: ("C:/other/python.exe", "later-start"),
    )
    with pytest.raises(RuntimeError, match="refusing to stop PID"):
        process_operator._require_same_process(
            {
                "process_id": 4242,
                "executable": "C:/expected/python.exe",
                "os_start_identity": "original-start",
            }
        )


def test_each_process_receives_only_its_allowlisted_environment() -> None:
    environment = {
        **_environment(),
        "WORKER_GATEWAY_TOKEN": "worker-token-value",
        "CAPTAIN_PROVIDER_RECEIPT_GATEWAY_TOKEN": "receipt-token-value",
        "LEDGER_DSN": "mysql://captain:redacted@127.0.0.1/captain",
        "OPENAI_API_KEY": "provider-only-key-value",
        "UNRELATED_SECRET": "must-not-cross-process-boundary",
        "MINIBOOK_CREATION_DB": ".captain-cook/minibook/creation.sqlite3",
        "MINIBOOK_PROJECTION_PROJECT_ID": "captain-runtime-projection-v2",
        "MINIBOOK_PROJECTION_SERVICE_AGENT_ID": "captain-projection-service-v2",
    }

    for service in ("gateway", "runtime", "provider_proxy", "benchmark", "minibook", "n8n"):
        child = process_operator._service_environment(service, environment)
        assert "UNRELATED_SECRET" not in child
        assert ("OPENAI_API_KEY" in child) is (service == "provider_proxy")
        assert ("CAPTAIN_PROVIDER_RECEIPT_GATEWAY_TOKEN" in child) is (
            service in {"gateway", "provider_proxy"}
        )
    minibook = process_operator._service_environment("minibook", environment)
    assert minibook["MINIBOOK_CREATION_DB"].endswith("creation.sqlite3")
    assert "CAPTAIN_GATEWAY_TOKEN" not in minibook


def test_health_rejects_attacker_url_before_reading_token_or_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[object] = []
    monkeypatch.setattr(
        process_operator.urllib.request, "urlopen",
        lambda *_args, **_kwargs: requested.append(object()),
    )

    with pytest.raises(ValueError, match="health endpoint is invalid"):
        process_operator._wait_authenticated_health(
            "gateway",
            environment={
                "CAPTAIN_GATEWAY_URL": "https://attacker.invalid",
                "CAPTAIN_GATEWAY_TOKEN": "must-not-be-attached",
            },
            timeout_seconds=0.1,
        )

    assert requested == []


def test_filtered_environments_satisfy_real_entrypoint_preflights(tmp_path: Path) -> None:
    executor = Path("gateway/capability_benchmark_process_executor.py")
    executor_digest = hashlib.sha256(executor.read_bytes()).hexdigest()
    environment = {
        **_environment(),
        "WORKER_GATEWAY_TOKEN": "worker-token-value",
        "CAPTAIN_PROVIDER_RECEIPT_GATEWAY_TOKEN": "receipt-token-value",
        "LEDGER_DSN": "mysql://captain:redacted@127.0.0.1/captain",
        "OPENAI_API_KEY": "provider-only-key-value",
        "CAPTAIN_CAPABILITY_BENCHMARK_AUTHORITY_ID": str(uuid4()),
        "CAPTAIN_CAPABILITY_BENCHMARK_EXECUTOR": executor.as_posix(),
        "CAPTAIN_CAPABILITY_BENCHMARK_EXECUTOR_SHA256": executor_digest,
        "CAPTAIN_PROVIDER_PRICE_CARD_PATH": "contracts/price.json",
        "CAPTAIN_PROVIDER_POLICY_PATH": "contracts/policy.json",
        "CAPTAIN_PROVIDER_STATE_ROOT": "state/provider",
    }

    GatewaySettings.from_env(process_operator._service_environment("gateway", environment))
    RuntimeEntrypointSettings.from_env(
        process_operator._service_environment("runtime", environment)
    )
    require_distinct_authority_secrets(
        process_operator._service_environment("provider_proxy", environment)
    )
    benchmark = process_operator._service_environment("benchmark", environment)
    assert {
        "CAPTAIN_CAPABILITY_BENCHMARK_TOKEN",
        "CAPTAIN_CAPABILITY_BENCHMARK_AUTHORITY_ID",
        "CAPTAIN_CAPABILITY_BENCHMARK_EXECUTOR",
        "CAPTAIN_CAPABILITY_BENCHMARK_EXECUTOR_SHA256",
    } <= set(benchmark)


def test_start_reuse_requires_exact_identity_and_authenticated_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    state = {
        "schema": "captain.production-process-state.v1", "service": "gateway",
        "status": "running", "operation": "start", "process_id": 4242,
        "process_identity": "opaque", "identity_kind": "os_process",
        "identity_value": "C:/python.exe:start-1", "executable": "C:/python.exe",
        "os_start_identity": "start-1", "argv": ["python", "-m", "gateway.app"],
    }
    (state_root / "gateway.json").write_text(json.dumps(state), encoding="utf-8")
    health: list[str] = []
    monkeypatch.setattr(process_operator, "_observed_process_identity", lambda _pid: ("C:/python.exe", "start-1"))
    monkeypatch.setattr(process_operator, "_wait_authenticated_health", lambda service, **_kwargs: health.append(service))
    monkeypatch.setattr(process_operator.subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("reuse spawned a process"))
    environment = {
        "CAPTAIN_PROCESS_STATE_ROOT": str(state_root),
        "CAPTAIN_GATEWAY_TOKEN": "gateway-token", "WORKER_GATEWAY_TOKEN": "worker-token",
        "CAPTAIN_GATEWAY_URL": "http://127.0.0.1:8090",
    }

    reused = operate("gateway", "start", environment=environment, repository_root=tmp_path)

    assert reused == state
    assert health == ["gateway"]


def test_n8n_reuse_uses_container_identity_without_pid_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    state = {
        "schema": "captain.production-process-state.v1", "service": "n8n",
        "status": "running", "operation": "start", "process_id": None,
        "process_identity": "opaque", "identity_kind": "docker_container",
        "identity_value": "container-id:started-at", "argv": ["pwsh"],
    }
    (state_root / "n8n.json").write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(process_operator, "_observed_n8n_container_identity", lambda _environment: ("container-id", "started-at"))
    monkeypatch.setattr(process_operator, "_wait_authenticated_health", lambda *_args, **_kwargs: None)

    reused = operate("n8n", "start", environment={
        "CAPTAIN_PROCESS_STATE_ROOT": str(state_root), "CAPTAIN_N8N_API_KEY": "n8n-key",
        "CAPTAIN_N8N_URL": "http://127.0.0.1:5679",
    }, repository_root=tmp_path)

    assert reused["process_id"] is None
    assert reused["identity_kind"] == "docker_container"


def test_failed_health_retains_ambiguous_spawn_identity_after_bounded_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Process:
        pid = 4343

    monkeypatch.setattr(process_operator.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(process_operator, "_observed_process_identity", lambda _pid: ("C:/python.exe", "start-4343"))
    monkeypatch.setattr(process_operator, "_wait_authenticated_health", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("not ready")))
    monkeypatch.setattr(process_operator, "_wait_tracked_tree_gone", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        process_operator, "_collect_exact_process_tree",
        lambda _state: ((4343, "C:/python.exe", "start-4343"),),
    )
    signals: list[object] = []
    cleanup_commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(process_operator.os, "kill", lambda _pid, sent: signals.append(sent))
    monkeypatch.setattr(
        process_operator.subprocess,
        "run",
        lambda argv, **_kwargs: cleanup_commands.append(tuple(argv)),
    )
    environment = {
        **_environment(),
        "CAPTAIN_PROCESS_STATE_ROOT": "state",
        "WORKER_GATEWAY_TOKEN": "worker-token",
        "LEDGER_DSN": "redacted-dsn",
    }

    with pytest.raises(RuntimeError, match="authenticated start failed"):
        operate("gateway", "start", environment=environment, repository_root=tmp_path)

    persisted = json.loads((tmp_path / "state/gateway.json").read_text(encoding="utf-8"))
    assert persisted["status"] == "starting"
    assert persisted["process_id"] == 4343
    assert signals == [process_operator.signal.SIGTERM]
    assert cleanup_commands == [("taskkill", "/PID", "4343", "/T", "/F")]


def test_starting_state_is_durable_before_process_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[dict[str, object]] = []

    class Process:
        pid = 5151

    def spawn(*_args, **_kwargs):
        observed.append(json.loads((tmp_path / "state/gateway.json").read_text()))
        raise RuntimeError("controlled spawn stop")

    monkeypatch.setattr(process_operator.subprocess, "Popen", spawn)
    environment = {
        **_environment(), "WORKER_GATEWAY_TOKEN": "worker-token",
        "LEDGER_DSN": "redacted-dsn", "CAPTAIN_PROCESS_STATE_ROOT": "state",
    }

    with pytest.raises(RuntimeError, match="controlled spawn stop"):
        operate("gateway", "start", environment=environment, repository_root=tmp_path)

    assert observed[0]["status"] == "starting"
    assert observed[0]["process_id"] is None


def test_missing_spawn_identity_retains_exact_starting_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Process:
        pid = 5252
        def terminate(self): pass
        def wait(self, *, timeout): return None

    monkeypatch.setattr(process_operator.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(process_operator, "_observed_process_identity", lambda _pid: None)
    environment = {
        **_environment(), "WORKER_GATEWAY_TOKEN": "worker-token",
        "LEDGER_DSN": "redacted-dsn", "CAPTAIN_PROCESS_STATE_ROOT": "state",
    }

    with pytest.raises(RuntimeError, match="identity is unavailable"):
        operate("gateway", "start", environment=environment, repository_root=tmp_path)

    state = json.loads((tmp_path / "state/gateway.json").read_text())
    assert state["status"] == "starting"
    assert state["process_id"] == 5252


def test_cleanup_tracks_and_kills_an_exact_orphan_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identities = {
        6000: ("C:/python.exe", "parent-start"),
        6001: ("C:/worker.exe", "child-start"),
    }
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        process_operator, "_collect_exact_process_tree",
        lambda _state: (
            (6000, "C:/python.exe", "parent-start"),
            (6001, "C:/worker.exe", "child-start"),
        ),
    )
    monkeypatch.setattr(process_operator, "_observed_process_identity", identities.get)
    monkeypatch.setattr(process_operator.os, "kill", lambda *_args: None)
    monkeypatch.setattr(process_operator, "_wait_tracked_tree_gone", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        process_operator.subprocess, "run",
        lambda argv, **_kwargs: commands.append(tuple(argv)),
    )

    assert process_operator._terminate_exact_process_tree({
        "process_id": 6000, "executable": "C:/python.exe",
        "os_start_identity": "parent-start",
    }) is False
    assert commands == [
        ("taskkill", "/PID", "6001", "/T", "/F"),
        ("taskkill", "/PID", "6000", "/T", "/F"),
    ]


def test_n8n_start_timeout_retains_starting_without_pid_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    starts: list[str] = []
    def timeout(*_args, **_kwargs):
        starts.append("start")
        return (_ for _ in ()).throw(
            process_operator.subprocess.TimeoutExpired("pwsh", 30)
        )
    monkeypatch.setattr(process_operator.subprocess, "run", timeout)
    environment = {
        "CAPTAIN_PROCESS_STATE_ROOT": "state",
        "CAPTAIN_N8N_API_KEY": "n8n-token-value",
        "CAPTAIN_N8N_URL": "http://127.0.0.1:5679",
    }

    with pytest.raises(process_operator.subprocess.TimeoutExpired):
        operate("n8n", "start", environment=environment, repository_root=tmp_path)

    state = json.loads((tmp_path / "state/n8n.json").read_text())
    assert state["status"] == "starting"
    assert state["process_id"] is None
    assert state["identity_kind"] == "docker_container"
    with pytest.raises(RuntimeError, match="n8n start is ambiguous"):
        operate("n8n", "start", environment=environment, repository_root=tmp_path)
    assert starts == ["start"]


def test_existing_ambiguous_starting_claim_never_spawns_second_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    (state_root / "gateway.json").write_text(json.dumps({
        "schema": "captain.production-process-state.v1", "service": "gateway",
        "status": "starting", "operation": "start", "process_id": 7000,
        "process_identity": "claim", "identity_kind": "os_process",
        "identity_value": None, "argv": ["python", "-m", "gateway.app"],
    }), encoding="utf-8")
    monkeypatch.setattr(
        process_operator.subprocess, "Popen",
        lambda *_args, **_kwargs: pytest.fail("exclusive starting claim respawned"),
    )
    environment = {
        **_environment(), "WORKER_GATEWAY_TOKEN": "worker-token",
        "LEDGER_DSN": "redacted-dsn", "CAPTAIN_PROCESS_STATE_ROOT": str(state_root),
    }

    with pytest.raises(RuntimeError, match="start is ambiguous"):
        operate("gateway", "start", environment=environment, repository_root=tmp_path)

    retained = json.loads((state_root / "gateway.json").read_text())
    assert retained["status"] == "starting"
    assert retained["process_id"] == 7000


def test_existing_exact_healthy_starting_claim_becomes_running_without_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    (state_root / "gateway.json").write_text(json.dumps({
        "schema": "captain.production-process-state.v1", "service": "gateway",
        "status": "starting", "operation": "start", "process_id": 7100,
        "process_identity": "claim", "identity_kind": "os_process",
        "identity_value": "C:/python.exe:start-7100", "executable": "C:/python.exe",
        "os_start_identity": "start-7100", "argv": ["python", "-m", "gateway.app"],
    }), encoding="utf-8")
    monkeypatch.setattr(process_operator, "_observed_process_identity", lambda _pid: ("C:/python.exe", "start-7100"))
    monkeypatch.setattr(process_operator, "_wait_authenticated_health", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        process_operator.subprocess, "Popen",
        lambda *_args, **_kwargs: pytest.fail("healthy starting claim respawned"),
    )
    environment = {
        **_environment(), "WORKER_GATEWAY_TOKEN": "worker-token",
        "LEDGER_DSN": "redacted-dsn", "CAPTAIN_PROCESS_STATE_ROOT": str(state_root),
    }

    reconciled = operate("gateway", "start", environment=environment, repository_root=tmp_path)

    assert reconciled["status"] == "running"
    assert reconciled["process_id"] == 7100


def test_starting_cleanup_retains_claim_when_tree_enumeration_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    state = {
        "schema": "captain.production-process-state.v1", "service": "gateway",
        "status": "starting", "operation": "start", "process_id": 7200,
        "process_identity": "claim", "identity_kind": "os_process",
        "identity_value": "C:/python.exe:start-7200", "executable": "C:/python.exe",
        "os_start_identity": "start-7200", "argv": ["python", "-m", "gateway.app"],
    }
    (state_root / "gateway.json").write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(
        process_operator, "_observed_process_identity",
        lambda _pid: ("C:/python.exe", "start-7200"),
    )
    monkeypatch.setattr(
        process_operator, "_wait_authenticated_health",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("unhealthy")),
    )
    monkeypatch.setattr(
        process_operator.subprocess, "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            process_operator.subprocess.TimeoutExpired("pwsh", 10)
        ),
    )
    environment = {
        **_environment(), "WORKER_GATEWAY_TOKEN": "worker-token",
        "LEDGER_DSN": "redacted-dsn", "CAPTAIN_PROCESS_STATE_ROOT": str(state_root),
    }

    with pytest.raises(RuntimeError, match="process tree is unavailable"):
        operate("gateway", "start", environment=environment, repository_root=tmp_path)

    retained = json.loads((state_root / "gateway.json").read_text())
    assert retained["status"] == "starting"


def test_operator_manifest_fails_closed_before_probe_on_missing_or_reused_credentials() -> None:
    environment = _environment()
    environment["CAPTAIN_RUNTIME_TOKEN"] = environment["CAPTAIN_GATEWAY_TOKEN"]

    with pytest.raises(
        CapabilityAssemblyBootstrapConfigurationError,
        match="credentials must be distinct",
    ):
        load_capability_production_bundle_manifest(environment=environment)

    del environment["CAPTAIN_RUNTIME_URL"]
    with pytest.raises(
        CapabilityAssemblyBootstrapConfigurationError,
        match="configuration is incomplete",
    ):
        load_capability_production_bundle_manifest(environment=environment)
