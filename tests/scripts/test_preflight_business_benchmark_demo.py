from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from agenten.agent_factory.business_benchmark_production import (
    BusinessBenchmarkProductionScopeError,
)


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "preflight-business-benchmark-demo.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("benchmark_demo_preflight", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_preflight_resolves_default_composition_without_calling_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_module()
    closed = False
    settings = object()
    scope = SimpleNamespace(
        job_id=UUID("71000000-0000-0000-0000-000000000001"),
        candidate_id="candidate-a",
        attempt=1,
    )

    class Settings:
        @classmethod
        def from_environment(cls, environment, *, repository_root):
            assert environment == {"SAFE": "value"}
            assert repository_root == tmp_path
            return settings

    class Composition:
        async def preflight(self, actual, environment, *, repository_root):
            assert actual is settings
            assert environment == {"SAFE": "value"}
            assert repository_root == tmp_path
            return (scope,)

        async def run(self, actual):  # pragma: no cover - forbidden effect
            raise AssertionError("provider execution must not run during preflight")

        async def aclose(self):
            nonlocal closed
            closed = True

    monkeypatch.setattr(module, "LiveBusinessBenchmarkSettings", Settings)
    monkeypatch.setattr(
        module,
        "load_production_business_benchmark_composition",
        lambda actual, *, environment: Composition(),
    )

    result = asyncio.run(
        module.preflight({"SAFE": "value"}, repository_root=tmp_path)
    )

    assert result["status"] == "resolvable"
    assert result["production_scope_resolvable"] is True
    assert result["jobs"] == [
        {
            "job_id": "71000000-0000-0000-0000-000000000001",
            "candidate_id": "candidate-a",
            "attempt": 1,
        }
    ]
    assert closed is True


def test_preflight_maps_only_typed_scope_failure_to_factory_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_module()
    closed = False

    class Settings:
        @classmethod
        def from_environment(cls, environment, *, repository_root):
            return object()

    class Composition:
        async def preflight(self, settings, environment, *, repository_root):
            raise BusinessBenchmarkProductionScopeError("private diagnostic")

        async def aclose(self):
            nonlocal closed
            closed = True

    monkeypatch.setattr(module, "LiveBusinessBenchmarkSettings", Settings)
    monkeypatch.setattr(
        module,
        "load_production_business_benchmark_composition",
        lambda settings, *, environment: Composition(),
    )

    result = asyncio.run(module.preflight({}, repository_root=tmp_path))

    assert result == {
        "schema": "captain.business-benchmark-default-preflight.v1",
        "status": "factory_dispatch_required",
        "database": "captain_test",
        "production_scope_resolvable": False,
    }
    assert "private diagnostic" not in str(result)
    assert closed is True
