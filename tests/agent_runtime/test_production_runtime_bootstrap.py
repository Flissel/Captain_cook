from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx

from agenten.agent_factory.production_adapter_bundle import (
    ProductionToolRequired,
    _ensure_factory_n8n_batch,
    build_production_runtime_app_from_environment,
    build_runtime_app_from_environment,
    build_runtime_app_with_v3_evidence,
)
from agenten.agent_factory.capability_v3_evidence_bridge import (
    CapabilityV3BridgeConfigurationError,
)
from agenten.agent_runtime.runtime_entrypoint import (
    RuntimeEntrypointSettings,
    preflight_runtime,
)


class InjectedEvidenceBackend:
    async def run(self, request):
        del request
        return None

    async def lifecycle_blocks(self, request):
        del request
        return ()


def test_runtime_bootstrap_builds_authenticated_8091_app_without_provider_call(
    tmp_path: Path,
) -> None:
    settings = RuntimeEntrypointSettings.from_env(
        {
            "CAPTAIN_RUNTIME_TOKEN": "runtime-secret",
            "CAPTAIN_GATEWAY_TOKEN": "gateway-secret",
            "CAPTAIN_GATEWAY_URL": "http://127.0.0.1:8090",
        }
    )
    app = build_runtime_app_from_environment(
        settings,
        {
            "HERMES_EXECUTABLE": sys.executable,
            "CODEX_EXECUTABLE": sys.executable,
            "CAPTAIN_RUNTIME_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
        },
    )

    async def verify() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8091",
        ) as client:
            assert (await client.get("/health")).status_code == 200
            assert (await client.get("/healthz")).status_code == 200
            denied = await client.post(
                "/v1/capability-factory/evidence-runs",
                json={},
            )
            assert denied.status_code == 401

    asyncio.run(verify())


def test_runtime_bootstrap_fails_closed_for_missing_cli(tmp_path: Path) -> None:
    settings = RuntimeEntrypointSettings.from_env(
        {
            "CAPTAIN_RUNTIME_TOKEN": "runtime-secret",
            "CAPTAIN_GATEWAY_TOKEN": "gateway-secret",
            "CAPTAIN_GATEWAY_URL": "http://127.0.0.1:8090",
        }
    )
    try:
        build_runtime_app_from_environment(
            settings,
            {
                "HERMES_EXECUTABLE": str(tmp_path / "missing-hermes.exe"),
                "CODEX_EXECUTABLE": sys.executable,
                "CAPTAIN_RUNTIME_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
            },
        )
    except ProductionToolRequired as exc:
        assert str(exc) == "TODO_TOOL:runtime_executable:HERMES_EXECUTABLE"
    else:
        raise AssertionError("missing Hermes executable was accepted")


def test_runtime_bootstrap_accepts_explicit_production_evidence_bridge(
    tmp_path: Path,
) -> None:
    settings = RuntimeEntrypointSettings.from_env(
        {
            "CAPTAIN_RUNTIME_TOKEN": "runtime-secret",
            "CAPTAIN_GATEWAY_TOKEN": "gateway-secret",
            "CAPTAIN_GATEWAY_URL": "http://127.0.0.1:8090",
        }
    )
    backend = InjectedEvidenceBackend()

    app = build_runtime_app_from_environment(
        settings,
        {
            "HERMES_EXECUTABLE": sys.executable,
            "CODEX_EXECUTABLE": sys.executable,
            "CAPTAIN_RUNTIME_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
        },
        evidence_backend=backend,
    )

    assert app.state.capability_evidence_backend is backend


def test_runtime_v3_builder_imports_bridge_and_preserves_recovery_todo() -> None:
    settings = RuntimeEntrypointSettings.from_env(
        {
            "CAPTAIN_RUNTIME_TOKEN": "runtime-secret",
            "CAPTAIN_GATEWAY_TOKEN": "gateway-secret",
            "CAPTAIN_GATEWAY_URL": "http://127.0.0.1:8090",
        }
    )
    context = SimpleNamespace(controlled_recovery=None)

    try:
        build_runtime_app_with_v3_evidence(settings, {}, context=context)
    except CapabilityV3BridgeConfigurationError as exc:
        assert "TODO_TOOL.v1 required capability=controlled_provider_recovery" in str(exc)
    else:
        raise AssertionError("missing controlled recovery port was accepted")


def test_production_runtime_builder_injects_real_v3_backend_without_effects(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agenten.agent_factory import production_candidate_ports
    from agenten.agent_factory import production_evidence_composition
    from agenten.agent_factory import production_external_ports

    backend = InjectedEvidenceBackend()
    candidate_ports = SimpleNamespace(
        candidate_provider=object(),
        candidate_attestation=object(),
    )
    external_ports = object()
    evidence_runtime = SimpleNamespace(backend=backend)
    monkeypatch.setattr(
        production_candidate_ports,
        "build_production_candidate_ports",
        lambda **kwargs: candidate_ports,
    )
    monkeypatch.setattr(
        production_external_ports,
        "build_production_v3_external_ports",
        lambda *args, **kwargs: external_ports,
    )
    monkeypatch.setattr(
        production_evidence_composition,
        "build_production_v3_evidence_backend_from_environment",
        lambda *args, **kwargs: evidence_runtime,
    )
    settings = RuntimeEntrypointSettings.from_env(
        {
            "CAPTAIN_RUNTIME_TOKEN": "runtime-secret",
            "CAPTAIN_GATEWAY_TOKEN": "gateway-secret",
            "CAPTAIN_GATEWAY_URL": "http://127.0.0.1:8090",
        }
    )
    environ = {
        "CAPTAIN_RUNTIME_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
        "CAPTAIN_CAPABILITY_SANDBOX_IMAGE": (
            "captain-capability-sandbox@sha256:" + "a" * 64
        ),
        "CAPTAIN_GATEWAY_URL": "http://127.0.0.1:8090",
        "CAPTAIN_GATEWAY_TOKEN": "gateway-secret",
        "HERMES_EXECUTABLE": sys.executable,
        "CODEX_EXECUTABLE": sys.executable,
    }

    app = build_production_runtime_app_from_environment(settings, environ)

    assert app.state.capability_evidence_backend is backend
    assert app.state.production_v3_evidence_runtime is evidence_runtime
    assert app.state.production_v3_external_ports is external_ports


def test_runtime_preflight_selects_production_v3_mode(monkeypatch) -> None:
    from agenten.agent_factory import production_adapter_bundle

    sentinel = object()
    monkeypatch.setenv("CAPTAIN_RUNTIME_TOKEN", "runtime-secret")
    monkeypatch.setenv("CAPTAIN_GATEWAY_TOKEN", "gateway-secret")
    monkeypatch.setenv("CAPTAIN_GATEWAY_URL", "http://127.0.0.1:8090")
    monkeypatch.setenv("CAPTAIN_RUNTIME_EVIDENCE_MODE", "production-v3")
    monkeypatch.setattr(
        production_adapter_bundle,
        "build_production_runtime_app_from_environment",
        lambda settings, environ: sentinel,
    )

    assert preflight_runtime() is sentinel


def test_runtime_releases_exact_candidate_n8n_tools_before_leasing() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"index": 1})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        _ensure_factory_n8n_batch(
            environ={"CAPTAIN_N8N_BATCH_ID": "factory-live-demo-n8n"},
            gateway_url="http://127.0.0.1:8090",
            gateway_token="captain-secret",
            client=client,
            tool_names=("crm_read", "calendar_read"),
        )

    payload = json.loads(requests[0].content)
    assert requests[0].url.path == "/blocks"
    assert payload["data"]["subtask_ids"] == ["crm_read", "calendar_read"]
    assert payload["data"]["capability_tags"] == ["n8n-builder"]
    assert requests[0].headers["authorization"] == "Bearer captain-secret"
