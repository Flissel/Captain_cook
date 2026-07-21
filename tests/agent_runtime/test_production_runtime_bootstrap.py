from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

from agenten.agent_factory.production_adapter_bundle import (
    ProductionToolRequired,
    build_runtime_app_from_environment,
)
from agenten.agent_runtime.runtime_entrypoint import RuntimeEntrypointSettings


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
