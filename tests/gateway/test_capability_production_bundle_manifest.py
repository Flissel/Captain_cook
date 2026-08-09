from __future__ import annotations

import json

import pytest

from gateway.capability_assembly_bootstrap import (
    CapabilityAssemblyBootstrapConfigurationError,
    load_capability_production_bundle_manifest,
)


def _environment() -> dict[str, str]:
    return {
        "CAPTAIN_GATEWAY_URL": "http://127.0.0.1:8090",
        "CAPTAIN_GATEWAY_TOKEN": "gateway-token-value",
        "CAPTAIN_CAPABILITY_BENCHMARK_URL": "http://127.0.0.1:8092/v1/execute",
        "CAPTAIN_CAPABILITY_BENCHMARK_TOKEN": "benchmark-token-value",
        "CAPTAIN_RUNTIME_URL": "http://127.0.0.1:8091",
        "CAPTAIN_RUNTIME_TOKEN": "runtime-token-value",
        "CAPTAIN_PROVIDER_PROXY_URL": "http://127.0.0.1:18091/v1",
        "CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN": "provider-token-value",
        "CAPTAIN_PROVIDER_POLICY_SHA256": "a" * 64,
        "CAPTAIN_PROVIDER_PRICE_CARD_SHA256": "b" * 64,
        "MINIBOOK_BACKEND_URL": "http://127.0.0.1:8080",
        "MINIBOOK_API_KEY": "minibook-token-value",
        "MINIBOOK_PROJECTION_API_KEY": "projection-token-value",
    }


def test_operator_manifest_is_digest_bound_and_contains_real_process_contracts() -> None:
    environment = _environment()

    manifest = load_capability_production_bundle_manifest(environment=environment)

    assert manifest.schema_name == "captain.capability-production-bundle-manifest.v1"
    assert manifest.provider_supervisor_mode == "dynamic_gateway_context_v1"
    assert manifest.provider_context_reference_header == "X-Captain-Provider-Context-Ref"
    assert manifest.gateway.health_url == "http://127.0.0.1:8090/healthz"
    assert manifest.benchmark.execute_url == "http://127.0.0.1:8092/v1/execute"
    assert manifest.benchmark.health_url == "http://127.0.0.1:8092/healthz"
    assert manifest.provider_proxy.health_url == "http://127.0.0.1:18091/v1/healthz"
    assert manifest.runtime.health_url == "http://127.0.0.1:8091/healthz"
    assert manifest.minibook.health_url == "http://127.0.0.1:8080/healthz"
    assert len(manifest.manifest_sha256) == 64
    assert manifest.manifest_sha256 == load_capability_production_bundle_manifest(
        environment=environment
    ).manifest_sha256
    rendered = json.dumps(manifest.model_dump(mode="json"), sort_keys=True)
    assert all(value not in rendered for name, value in environment.items() if "TOKEN" in name or "KEY" in name)


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

