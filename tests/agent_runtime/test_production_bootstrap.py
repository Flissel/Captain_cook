from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenten.agent_runtime import runtime_entrypoint
from agenten.agent_runtime.production_bootstrap import (
    RuntimeAdapterManifestError,
    RuntimeBootstrap,
    load_runtime_adapters,
    load_runtime_adapters_from_env,
)
from agenten.agent_runtime.runtime_entrypoint import RuntimeEntrypointSettings


VALID_ADAPTER_MODULE = """
from agenten.agent_runtime.production_bootstrap import RuntimeAdapterBinding

class Hermes:
    async def plan(self, command, grant):
        raise NotImplementedError

    async def design_agent(self, command, grant):
        raise NotImplementedError

class Codex:
    async def start(self, command, grant):
        raise NotImplementedError

    async def resume(self, command, grant):
        raise NotImplementedError

    async def status(self, command, grant):
        raise NotImplementedError

    async def cancel(self, command, grant):
        raise NotImplementedError

    async def heartbeat(self, command, grant):
        raise NotImplementedError

class Artifacts:
    async def require(self, reference):
        raise NotImplementedError

def create_runtime_adapters(context):
    return RuntimeAdapterBinding(
        hermes=Hermes(),
        codex=Codex(),
        artifacts=Artifacts(),
    )
"""


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _write_adapter(root: Path, source: str = VALID_ADAPTER_MODULE) -> Path:
    module_path = root / "adapter_module.py"
    module_path.write_text(source, encoding="utf-8")
    return module_path


def _write_manifest(
    root: Path,
    *,
    module_path: Path | None = None,
    module_sha256: str | None = None,
    factory_name: str = "create_runtime_adapters",
    schema: str = "captain.runtime-adapters.v1",
) -> tuple[Path, str]:
    module_path = module_path or _write_adapter(root)
    manifest_path = root / "runtime-adapters.json"
    document = {
        "schema": schema,
        "module_path": str(module_path),
        "factory_name": factory_name,
        "module_sha256": module_sha256 or _sha256(module_path.read_bytes()),
    }
    manifest_path.write_text(json.dumps(document), encoding="utf-8")
    return manifest_path, _sha256(manifest_path.read_bytes())


def test_load_runtime_adapters_rejects_a_missing_manifest(tmp_path: Path) -> None:
    with pytest.raises(RuntimeAdapterManifestError, match="manifest.*missing"):
        load_runtime_adapters(
            tmp_path / "missing.json",
            expected_sha256="0" * 64,
            repository_root=tmp_path,
        )


def test_load_runtime_adapters_rejects_an_unsupported_schema(tmp_path: Path) -> None:
    manifest, digest = _write_manifest(
        tmp_path,
        schema="captain.runtime-adapters.v0",
    )

    with pytest.raises(RuntimeAdapterManifestError, match="schema"):
        load_runtime_adapters(
            manifest,
            expected_sha256=digest,
            repository_root=tmp_path,
        )


def test_load_runtime_adapters_rejects_digest_mismatch(tmp_path: Path) -> None:
    manifest, _ = _write_manifest(tmp_path)

    with pytest.raises(RuntimeAdapterManifestError, match="manifest digest"):
        load_runtime_adapters(
            manifest,
            expected_sha256="0" * 64,
            repository_root=tmp_path,
        )


def test_load_runtime_adapters_rejects_manifest_outside_repository_root(
    tmp_path: Path,
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    manifest, digest = _write_manifest(tmp_path)

    with pytest.raises(RuntimeAdapterManifestError, match="outside"):
        load_runtime_adapters(
            manifest,
            expected_sha256=digest,
            repository_root=repository_root,
        )


def test_load_runtime_adapters_rejects_module_outside_allowed_roots(
    tmp_path: Path,
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    outside_module = _write_adapter(tmp_path)
    manifest, digest = _write_manifest(
        repository_root,
        module_path=outside_module,
    )

    with pytest.raises(RuntimeAdapterManifestError, match="module.*outside"):
        load_runtime_adapters(
            manifest,
            expected_sha256=digest,
            repository_root=repository_root,
        )


def test_load_runtime_adapters_rejects_module_digest_mismatch(tmp_path: Path) -> None:
    manifest, digest = _write_manifest(tmp_path, module_sha256="0" * 64)

    with pytest.raises(RuntimeAdapterManifestError, match="module digest"):
        load_runtime_adapters(
            manifest,
            expected_sha256=digest,
            repository_root=tmp_path,
        )


def test_load_runtime_adapters_rejects_a_missing_factory(tmp_path: Path) -> None:
    manifest, digest = _write_manifest(tmp_path, factory_name="missing_factory")

    with pytest.raises(RuntimeAdapterManifestError, match="factory.*missing"):
        load_runtime_adapters(
            manifest,
            expected_sha256=digest,
            repository_root=tmp_path,
        )


def test_load_runtime_adapters_redacts_factory_exceptions(tmp_path: Path) -> None:
    module = _write_adapter(
        tmp_path,
        "def create_runtime_adapters(context):\n"
        "    raise RuntimeError('Bearer secret-factory-marker')\n",
    )
    manifest, digest = _write_manifest(tmp_path, module_path=module)

    with pytest.raises(RuntimeAdapterManifestError, match="factory failed") as failure:
        load_runtime_adapters(
            manifest,
            expected_sha256=digest,
            repository_root=tmp_path,
        )

    assert "secret-factory-marker" not in str(failure.value)


def test_load_runtime_adapters_rejects_an_incomplete_binding(tmp_path: Path) -> None:
    module = _write_adapter(
        tmp_path,
        "from agenten.agent_runtime.production_bootstrap import RuntimeAdapterBinding\n"
        "class IncompleteHermes:\n"
        "    async def plan(self, command, grant): pass\n"
        "class IncompleteCodex: pass\n"
        "class IncompleteArtifacts: pass\n"
        "def create_runtime_adapters(context):\n"
        "    return RuntimeAdapterBinding(IncompleteHermes(), IncompleteCodex(), IncompleteArtifacts())\n",
    )
    manifest, digest = _write_manifest(tmp_path, module_path=module)

    with pytest.raises(RuntimeAdapterManifestError, match="HermesPlannerPort"):
        load_runtime_adapters(
            manifest,
            expected_sha256=digest,
            repository_root=tmp_path,
        )


def test_load_runtime_adapters_returns_a_complete_structural_binding(
    tmp_path: Path,
) -> None:
    manifest, digest = _write_manifest(tmp_path)

    binding = load_runtime_adapters(
        manifest,
        expected_sha256=digest,
        repository_root=tmp_path,
    )

    assert type(binding.hermes).__name__ == "Hermes"
    assert type(binding.codex).__name__ == "Codex"
    assert type(binding.artifacts).__name__ == "Artifacts"


def test_load_runtime_adapters_from_env_requires_both_manifest_settings(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeAdapterManifestError, match="missing required"):
        load_runtime_adapters_from_env(repository_root=tmp_path, environ={})


def test_main_composes_the_verified_binding_before_starting_the_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = RuntimeEntrypointSettings.from_env(
        {
            "CAPTAIN_RUNTIME_TOKEN": "runtime-test-token",
            "CAPTAIN_GATEWAY_TOKEN": "gateway-test-token",
            "CAPTAIN_GATEWAY_URL": "http://127.0.0.1:8090",
        }
    )
    binding = SimpleNamespace(
        hermes=object(),
        codex=object(),
        artifacts=object(),
    )
    bootstrap = RuntimeBootstrap(settings=settings, binding=binding)
    client = object()
    app = object()
    events: list[tuple[str, object]] = []

    monkeypatch.setattr(runtime_entrypoint, "preflight_runtime", lambda: bootstrap)
    monkeypatch.setattr(runtime_entrypoint.httpx, "AsyncClient", lambda: client)

    def compose(**kwargs: object) -> object:
        events.append(("compose", kwargs))
        return app

    def run(observed_app: object, **kwargs: object) -> None:
        events.append(("listen", (observed_app, kwargs)))

    monkeypatch.setattr(runtime_entrypoint, "compose_gateway_backed_runtime_app", compose)
    monkeypatch.setattr(runtime_entrypoint.uvicorn, "run", run)

    runtime_entrypoint.main()

    assert events == [
        (
            "compose",
            {
                "settings": settings,
                "client": client,
                "hermes": binding.hermes,
                "codex": binding.codex,
                "artifacts": binding.artifacts,
            },
        ),
        ("listen", (app, {"host": "127.0.0.1", "port": 8091, "workers": 1})),
    ]
