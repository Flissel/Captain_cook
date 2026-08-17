from __future__ import annotations

import json
from pathlib import Path

import pytest

from gateway.capability_assembly_bootstrap import (
    CapabilityAssemblyBootstrapConfigurationError,
    load_capability_production_bundle_manifest,
)
from gateway.capability_process_operator import operate, service_argv
import gateway.capability_process_operator as process_operator


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
