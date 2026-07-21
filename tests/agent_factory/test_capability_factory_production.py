from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from agenten.agent_factory.capability_factory_production import (
    AdapterManifestKind,
    RuntimeCaptainEvidenceHttpPort,
    create_capability_factory_runtime_app,
    generate_adapter_manifest,
)
from agenten.agent_factory.capability_factory_entrypoint import (
    CapabilityFactoryConfigurationError,
    CapabilityReleaseRunReceipt,
    parse_capability_factory_args,
)
from agenten.agent_factory.forge_contracts import CreationResultV1
from agenten.agent_factory.outcome_contracts import ForgeCapabilityPackageCandidateV1
from agenten.agent_runtime.contracts import ArtifactRef
from gateway.factory_live_runtime import FactoryLiveBootstrapError, load_factory_live_environment
from tests.agent_factory.test_release_gate import accepted_manifest, capability_e2e
from tests.agent_factory.test_state_machine import v2_job


def _module(path: Path, symbol: str) -> Path:
    path.write_text(f"def {symbol}(context):\n    return context\n", encoding="utf-8")
    return path


def test_manifest_generator_uses_distinct_attested_contracts(tmp_path: Path) -> None:
    entry_module = _module(tmp_path / "entry.py", "build_entrypoint")
    runtime_module = _module(tmp_path / "runtime.py", "build_runtime")

    entry = generate_adapter_manifest(
        workspace_root=tmp_path,
        module_path=entry_module,
        factory_symbol="build_entrypoint",
        target_path=tmp_path / "entry.manifest.json",
        kind=AdapterManifestKind.ENTRYPOINT,
    )
    runtime = generate_adapter_manifest(
        workspace_root=tmp_path,
        module_path=runtime_module,
        factory_symbol="build_runtime",
        target_path=tmp_path / "runtime.manifest.json",
        kind=AdapterManifestKind.FACTORY_LIVE_RUNTIME,
    )

    entry_payload = json.loads(entry.path.read_text(encoding="utf-8"))
    runtime_payload = json.loads(runtime.path.read_text(encoding="utf-8"))
    assert entry_payload["schema"] == "captain.capability-factory-entrypoint-adapter-manifest.v1"
    assert runtime_payload["schema"] == "captain.factory-live-runtime-adapter-manifest.v1"
    assert entry.sha256 == hashlib.sha256(entry.path.read_bytes()).hexdigest()
    assert runtime.sha256 == hashlib.sha256(runtime.path.read_bytes()).hexdigest()


def test_old_shared_manifest_alias_is_rejected_by_both_bootstraps(tmp_path: Path) -> None:
    input_path = tmp_path / "TO_BE_BUILT.md"
    input_path.write_text("# build", encoding="utf-8")
    old = {
        "CAPABILITY_FACTORY_ADAPTER_MANIFEST": "legacy.json",
        "CAPABILITY_FACTORY_ADAPTER_SHA256": "0" * 64,
        "CAPTAIN_GATEWAY_TOKEN": "gateway-secret",
        "CAPTAIN_RUNTIME_TOKEN": "runtime-secret",
        "MINIBOOK_PROJECTION_API_KEY": "projection-secret",
    }
    with pytest.raises(CapabilityFactoryConfigurationError, match="ENTRYPOINT"):
        parse_capability_factory_args(
            ["--correlation-id", "00000000-0000-0000-0000-000000000001"],
            environ=old,
            workspace_root=tmp_path,
        )
    with pytest.raises(FactoryLiveBootstrapError, match="incomplete"):
        load_factory_live_environment(
            old | {"CAPTAIN_FACTORY_JOB_ID": "00000000-0000-0000-0000-000000000002", "CAPTAIN_RUNTIME_URL": "http://127.0.0.1:8091"},
            workspace_root=tmp_path,
        )


@pytest.mark.asyncio
async def test_8091_evidence_port_is_authenticated_and_preserves_canonical_receipt() -> None:
    job = v2_job()
    manifest = accepted_manifest()
    record = capability_e2e(manifest=manifest)[0]
    content = record.model_dump_json(by_alias=True).encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    receipt = CapabilityReleaseRunReceipt(
        record=record,
        reference=ArtifactRef(
            uri=f"artifact://runtime-evidence/{digest}",
            sha256=digest,
            media_type="application/json",
        ),
    )

    class Backend:
        async def run(self, request):
            assert request.run_number == 1
            return receipt

        async def lifecycle_blocks(self, request):
            raise AssertionError("not called")

    class Executor:
        async def execute(self, command):
            raise AssertionError("not called")

    app = create_capability_factory_runtime_app(
        runtime_executor=Executor(),
        backend=Backend(),
        token=SecretStr("runtime-secret-value"),
    )
    fixture_root = Path(__file__).parents[1] / "fixtures" / "contracts"
    result = CreationResultV1.model_validate_json(
        (fixture_root / "minibook_creation_result.v1.json").read_text(encoding="utf-8")
    )
    candidate = ForgeCapabilityPackageCandidateV1.model_validate_json(
        (fixture_root / "forge_capability_package_candidate.v1.json").read_text(encoding="utf-8")
    ).model_copy(
        update={
            "factory_job_id": job.job_id,
            "creation_job_id": result.creation_job_id,
            "correlation_id": job.correlation_id,
            "subject_version": job.subject_version,
            "capability_id": job.required_capability,
        }
    )
    result = result.model_copy(
        update={
            "correlation_id": job.correlation_id,
            "package_manifest_ref": candidate.team_manifest_ref,
            "artifact_refs": (candidate.source_ref,),
            "evidence_refs": (candidate.runbook_ref,),
            "skill_usage_receipt_ref": candidate.skill_usage_receipt_ref,
        }
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1:8091",
    ) as client:
        unauthorized = await client.post(
            "/v1/capability-factory/evidence-runs",
            json={
                "job": job.model_dump(mode="json", by_alias=True),
                "creation_result": result.model_dump(mode="json", by_alias=True),
                "candidate": candidate.model_dump(mode="json", by_alias=True),
                "run_number": 1,
            },
        )
        assert unauthorized.status_code == 401
        port = RuntimeCaptainEvidenceHttpPort(
            "http://127.0.0.1:8091",
            SecretStr("runtime-secret-value"),
            client,
        )
        observed = await port.run(job, result, candidate, 1)

    assert observed == receipt
