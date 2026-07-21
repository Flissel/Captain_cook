from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenten.agent_factory.capability_factory_entrypoint import CapabilityFactoryEntrypoint
from agenten.agent_factory.capability_factory_production import AdapterManifestKind
from agenten.agent_factory.capability_factory_production import (
    MinibookSwarmCreationHttpPort,
    RuntimeCaptainEvidenceHttpPort,
)
from agenten.agent_factory.production_adapter_bundle import (
    ContentAddressedRuntimeArtifactPort,
    ProductionCapabilityFactoryEntrypoint,
    ProductionToolRequired,
    build_capability_factory_entrypoint,
    build_factory_live_runtime,
    production_manifest_commands,
)
from gateway.factory_live_runtime import FactoryLiveExternalRuntimeGraph


def test_production_bundle_exports_both_attested_factory_symbols() -> None:
    commands = production_manifest_commands(Path("C:/workspace"))

    assert set(commands) == {
        AdapterManifestKind.ENTRYPOINT,
        AdapterManifestKind.FACTORY_LIVE_RUNTIME,
    }
    assert "build_capability_factory_entrypoint" in commands[AdapterManifestKind.ENTRYPOINT]
    assert "build_factory_live_runtime" in commands[AdapterManifestKind.FACTORY_LIVE_RUNTIME]


def test_factory_live_runtime_is_typed_and_fails_closed_before_provider_effect() -> None:
    graph = build_factory_live_runtime(SimpleNamespace())

    assert isinstance(graph, FactoryLiveExternalRuntimeGraph)
    with pytest.raises(
        ProductionToolRequired,
        match="TODO_TOOL:factory_live_prepared_dispatch_bridge",
    ):
        graph.prepared_dispatch.prepare(
            job=None,
            action=None,
            expected_skill_digests={},
            projection=None,
            workflow_artifacts=(),
        )


def test_runtime_artifact_port_requires_exact_content_address(tmp_path: Path) -> None:
    content = b"strict runtime prompt"
    digest = hashlib.sha256(content).hexdigest()
    target = tmp_path / "content" / "sha256" / digest[:2] / digest
    target.parent.mkdir(parents=True)
    target.write_bytes(content)
    port = ContentAddressedRuntimeArtifactPort(tmp_path)
    reference = SimpleNamespace(
        uri=f"artifact://runtime-prompt/{digest}",
        sha256=digest,
    )

    asyncio.run(port.require(reference))
    target.write_bytes(b"changed")
    with pytest.raises(ValueError, match="digest"):
        asyncio.run(port.require(reference))


def test_capability_entrypoint_factory_is_real_but_blocks_unimplemented_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Construction is local-only. The first provider operation remains an explicit
    # TODO_TOOL until the Gateway claim-aware result endpoint exists.
    config = SimpleNamespace(
        workspace_root=Path(__file__).resolve().parents[2],
        checkpoint_dir=tmp_path / "checkpoints",
        artifact_dir=tmp_path / "artifacts",
        gateway_url="http://127.0.0.1:18090",
        runtime_url="http://127.0.0.1:8091",
        minibook_url="http://127.0.0.1:3456",
        sandbox_image=("captain-capability-sandbox@sha256:" + "a" * 64),
        gateway_token=SimpleNamespace(get_secret_value=lambda: "gateway-secret"),
        runtime_token=SimpleNamespace(get_secret_value=lambda: "runtime-secret"),
        minibook_projection_api_key=SimpleNamespace(
            get_secret_value=lambda: "projection-secret"
        ),
    )
    monkeypatch.setenv("TEST_MARIADB_DSN", "mariadb://u:p@127.0.0.1:33306/captain_test")
    monkeypatch.setenv("MINIBOOK_API_KEY", "minibook-secret")
    monkeypatch.setenv("CAPTAIN_RUNTIME_ARTIFACT_ROOT", str(tmp_path / "artifacts"))

    entrypoint = build_capability_factory_entrypoint(config)

    assert isinstance(entrypoint, CapabilityFactoryEntrypoint)
    assert isinstance(entrypoint, ProductionCapabilityFactoryEntrypoint)
    assert isinstance(entrypoint._creation, MinibookSwarmCreationHttpPort)
    assert isinstance(entrypoint._evidence_issuer, RuntimeCaptainEvidenceHttpPort)


def test_capability_entrypoint_rejects_split_runtime_and_creation_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = SimpleNamespace(
        workspace_root=Path(__file__).resolve().parents[2],
        checkpoint_dir=tmp_path / "checkpoints",
        artifact_dir=tmp_path / "factory-artifacts",
        gateway_url="http://127.0.0.1:18090",
        runtime_url="http://127.0.0.1:8091",
        minibook_url="http://127.0.0.1:3456",
        sandbox_image=("captain-capability-sandbox@sha256:" + "a" * 64),
        gateway_token=SimpleNamespace(get_secret_value=lambda: "gateway-secret"),
        runtime_token=SimpleNamespace(get_secret_value=lambda: "runtime-secret"),
        minibook_projection_api_key=SimpleNamespace(
            get_secret_value=lambda: "projection-secret"
        ),
    )
    monkeypatch.setenv("TEST_MARIADB_DSN", "mariadb://u:p@127.0.0.1:33306/captain_test")
    monkeypatch.setenv("MINIBOOK_API_KEY", "minibook-secret")
    monkeypatch.setenv("CAPTAIN_RUNTIME_ARTIFACT_ROOT", str(tmp_path / "runtime-artifacts"))

    with pytest.raises(
        ProductionToolRequired,
        match="TODO_TOOL:configuration:shared_capability_artifact_root",
    ):
        build_capability_factory_entrypoint(config)


def test_production_entrypoint_seeds_exact_canonical_input_into_shared_cas(
    tmp_path: Path,
) -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "demo_inputs"
        / "agent_factory"
        / "sales_pipeline_brief"
        / "TO_BE_BUILT.md"
    )
    entrypoint = object.__new__(ProductionCapabilityFactoryEntrypoint)
    from agenten.agent_factory.capability_live_adapters import (
        ContentAddressedArtifactStore,
    )

    entrypoint._production_artifacts = ContentAddressedArtifactStore(tmp_path)

    reference = entrypoint.seed_input(source)

    target = tmp_path / "content" / "sha256" / reference.sha256[:2] / reference.sha256
    assert reference.uri == f"artifact://factory-input/{reference.sha256}"
    assert target.read_bytes() == source.read_bytes()


def test_live_scripts_forward_provider_and_both_manifest_contracts() -> None:
    root = Path(__file__).resolve().parents[2]
    services = (root / "scripts" / "live-demo-services.ps1").read_text(encoding="utf-8")
    capability = (root / "scripts" / "run-capability-factory-live.ps1").read_text(
        encoding="utf-8"
    )
    required = {
        "OPENAI_API_KEY",
        "CONTEXT7_API_KEY",
        "HERMES_EXECUTABLE",
        "CODEX_EXECUTABLE",
        "CAPTAIN_RUNTIME_ARTIFACT_ROOT",
        "CAPABILITY_FACTORY_ENTRYPOINT_ADAPTER_MANIFEST",
        "CAPABILITY_FACTORY_ENTRYPOINT_ADAPTER_SHA256",
        "FACTORY_LIVE_RUNTIME_ADAPTER_MANIFEST",
        "FACTORY_LIVE_RUNTIME_ADAPTER_SHA256",
        "CAPTAIN_FACTORY_JOB_ID",
    }

    for name in required:
        assert f"'{name}'" in services
    for name in required:
        assert f"'{name}'" in capability


def test_hermes_preflight_does_not_parse_truncated_human_skill_table() -> None:
    root = Path(__file__).resolve().parents[2]
    source = (
        root / "agenten" / "agent_factory" / "factory_live_entrypoint.py"
    ).read_text(encoding="utf-8")

    assert '("hermes", "skills", "list", "--enabled-only")' not in source
    assert "_require_installed_factory_skill_directories" in source
