from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest

from agenten.agent_factory.capability_live_adapters import (
    CaptainCapabilityReleaseReceipt,
    CaptainEvidenceIssuerAdapter,
    CapabilityCreationPreparation,
    CapabilityReleaseObservation,
    ContentAddressedArtifactStore,
    HermesCapabilityCreationAdapter,
    generate_capability_adapter_manifest,
    write_capability_adapter_manifest,
)
from agenten.agent_factory.contracts import (
    AgentFactoryJobV2,
    FactoryPhase,
)
from agenten.agent_factory.forge_contracts import (
    CreationJobV1,
    CreationResultV1,
    CreationSubmissionReceipt,
)
from agenten.agent_factory.outcome_contracts import (
    CapabilityAssertionResult,
    ForgeCapabilityPackageCandidateV1,
    PrivateHoldoutEvidence,
)
from agenten.agent_runtime.contracts import ArtifactRef


NOW = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)


def _ref(name: str, digest: str, media_type: str = "application/json") -> ArtifactRef:
    return ArtifactRef(
        uri=f"artifact://test/{name}/{digest}",
        sha256=digest,
        media_type=media_type,
    )


def _job() -> AgentFactoryJobV2:
    return AgentFactoryJobV2.model_validate_json(
        Path("tests/fixtures/agent_factory/agent_factory_job.v2.json").read_text(
            encoding="utf-8"
        )
    )


def _creation_job(job: AgentFactoryJobV2) -> CreationJobV1:
    fixture = CreationJobV1.model_validate_json(
        Path("tests/fixtures/contracts/minibook_creation_job.v1.json").read_text(
            encoding="utf-8"
        )
    )
    return fixture.model_copy(
        update={
            "factory_job_id": job.job_id,
            "correlation_id": job.correlation_id,
            "causation_id": job.event_id,
            "subject_version": job.subject_version,
            "input_ref": job.input_ref,
            "compiled_spec_ref": job.compiled_spec_ref,
            "dependency_graph_ref": job.dependency_graph_ref,
            "public_assertion_ids": job.acceptance_assertion_ids,
            "deadline_at": job.deadline_at,
        }
    )


def _creation_result(job: AgentFactoryJobV2, creation: CreationJobV1) -> CreationResultV1:
    fixture = CreationResultV1.model_validate_json(
        Path("tests/fixtures/contracts/minibook_creation_result.v1.json").read_text(
            encoding="utf-8"
        )
    )
    return fixture.model_copy(
        update={
            "creation_job_id": creation.creation_job_id,
            "correlation_id": job.correlation_id,
            "subject_version": job.subject_version,
            "attempt": creation.attempt,
        }
    )


def _candidate(
    job: AgentFactoryJobV2,
    creation: CreationJobV1,
    result: CreationResultV1,
) -> ForgeCapabilityPackageCandidateV1:
    fixture = ForgeCapabilityPackageCandidateV1.model_validate_json(
        Path("tests/fixtures/contracts/forge_capability_package_candidate.v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert result.package_manifest_ref is not None
    return fixture.model_copy(
        update={
            "capability_id": job.required_capability,
            "factory_job_id": job.job_id,
            "creation_job_id": creation.creation_job_id,
            "correlation_id": job.correlation_id,
            "subject_version": job.subject_version,
            "attempt": creation.attempt,
        }
    )


@pytest.mark.asyncio
async def test_content_store_is_content_addressed_and_rejects_changed_binding(
    tmp_path: Path,
) -> None:
    store = ContentAddressedArtifactStore(tmp_path / "artifacts")
    content = b'{"status":"passed"}'

    first = store.put(content, "application/json", namespace="release-evidence")
    second = store.put(content, "application/json", namespace="release-evidence")
    store.bind("release-run", "job-1/run-1", first)

    assert first == second
    assert first.sha256 == hashlib.sha256(content).hexdigest()
    assert await store.read(first) == content
    assert store.binding("release-run", "job-1/run-1") == first
    with pytest.raises(ValueError, match="immutable artifact binding changed"):
        store.bind(
            "release-run",
            "job-1/run-1",
            store.put(b"changed", "application/json", namespace="release-evidence"),
        )


def test_manifest_generator_is_deterministic_and_separate_from_runtime_graph(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    module = workspace / "adapters" / "capability.py"
    module.parent.mkdir(parents=True)
    module.write_text("def build_entrypoint(config):\n    return config\n", encoding="utf-8")

    first = generate_capability_adapter_manifest(
        workspace_root=workspace,
        module_path=module,
        factory_symbol="build_entrypoint",
    )
    second = generate_capability_adapter_manifest(
        workspace_root=workspace,
        module_path=module,
        factory_symbol="build_entrypoint",
    )

    assert first == second
    payload = json.loads(first.content)
    assert payload == {
        "schema": "captain.capability-factory-adapter-manifest.v2",
        "module_path": "adapters/capability.py",
        "module_sha256": hashlib.sha256(module.read_bytes()).hexdigest(),
        "factory_symbol": "build_entrypoint",
    }
    assert "factory-live-runtime-graph" not in first.content.decode("utf-8")
    assert first.sha256 == hashlib.sha256(first.content).hexdigest()

    written = write_capability_adapter_manifest(
        workspace_root=workspace,
        module_path=module,
        factory_symbol="build_entrypoint",
        output_directory=workspace / "artifacts" / "capability-factory" / "manifests",
    )
    assert written.path.name == f"{first.sha256}.json"
    assert written.path.read_bytes() == first.content


def test_manifest_generator_cli_prints_only_redaction_safe_metadata(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    module = workspace / "adapters" / "capability.py"
    module.parent.mkdir(parents=True)
    module.write_text("def build_entrypoint(config):\n    return config\n", encoding="utf-8")

    completed = subprocess.run(
        (
            sys.executable,
            "scripts/generate-capability-adapter-manifest.py",
            "--workspace-root",
            str(workspace),
            "--module-path",
            str(module),
            "--factory-symbol",
            "build_entrypoint",
            "--output-directory",
            str(workspace / "artifacts" / "capability-factory" / "manifests"),
        ),
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert set(report) == {
        "manifest_path",
        "manifest_sha256",
        "module_sha256",
        "schema",
    }
    assert report["schema"] == "captain.capability-factory-adapter-manifest.v2"
    assert "token" not in completed.stdout.casefold()


@dataclass
class _CreationBackend:
    preparation: CapabilityCreationPreparation
    result_value: CreationResultV1
    submissions: list[CreationJobV1] = field(default_factory=list)

    async def prepare(
        self,
        _job: AgentFactoryJobV2,
        _creation_job: CreationJobV1,
    ) -> CapabilityCreationPreparation:
        return self.preparation

    async def submit(self, creation_job: CreationJobV1) -> CreationSubmissionReceipt:
        replayed = bool(self.submissions)
        self.submissions.append(creation_job)
        return CreationSubmissionReceipt(
            creation_job_id=creation_job.creation_job_id,
            status="queued",
            subject_version=creation_job.subject_version,
            replayed=replayed,
        )

    async def result(self, _creation_job_id: UUID) -> CreationResultV1:
        return self.result_value


@pytest.mark.asyncio
async def test_creation_adapter_binds_hermes_blocks_and_durable_result(
    tmp_path: Path,
) -> None:
    job = _job()
    creation = _creation_job(job)
    result = _creation_result(job, creation)
    preparation = CapabilityCreationPreparation(
        factory_job_id=job.job_id,
        creation_job_id=creation.creation_job_id,
        correlation_id=job.correlation_id,
        subject_version=job.subject_version,
        attempt=creation.attempt,
        occurred_at=NOW + timedelta(seconds=1),
        blueprint_lease_id="lease-architect-1",
        blueprint_evidence_ref=_ref("blueprint", "1" * 64),
        tool_lease_id="lease-tool-1",
        tool_evidence_ref=_ref("tool", "2" * 64),
        code_lease_id="lease-code-1",
    )
    backend = _CreationBackend(preparation=preparation, result_value=result)
    adapter = HermesCapabilityCreationAdapter(
        backend=backend,
        artifact_store=ContentAddressedArtifactStore(tmp_path / "artifacts"),
        clock=lambda: NOW + timedelta(seconds=2),
    )

    blocks = await adapter.preparation_blocks(job, creation)
    first = await adapter.submit(creation)
    replay = await adapter.submit(creation)
    resolved = await adapter.result(creation.creation_job_id)
    completion = await adapter.completion_block(job, resolved)

    assert tuple(block.phase for block in blocks) == (
        FactoryPhase.BLUEPRINT_CREATED,
        FactoryPhase.TOOL_CANDIDATE_TESTED,
    )
    assert all(block.correlation_id == job.correlation_id for block in (*blocks, completion))
    assert all(block.causation_id == job.event_id for block in (*blocks, completion))
    assert first.replayed is False
    assert replay.replayed is True
    assert resolved == result
    assert completion.phase is FactoryPhase.AGENT_CODE_CREATED
    assert completion.evidence_refs[0].sha256 == result.package_manifest_ref.sha256


@dataclass
class _EvidenceExecutor:
    store: ContentAddressedArtifactStore
    calls: list[int] = field(default_factory=list)

    async def execute(
        self,
        job: AgentFactoryJobV2,
        _creation_result: CreationResultV1,
        candidate: ForgeCapabilityPackageCandidateV1,
        run_number: int,
    ) -> CapabilityReleaseObservation | None:
        self.calls.append(run_number)
        assertion_ref = self.store.put(
            json.dumps(
                {
                    "schema": "captain.capability-assertion-evidence.v1",
                    "run_number": run_number,
                    "status": "passed",
                },
                separators=(",", ":"),
            ).encode("utf-8"),
            "application/json",
            namespace="assertion-evidence",
        )
        assertions = tuple(
            CapabilityAssertionResult(
                assertion_id=assertion_id,
                status="passed",
                integration_intent="none",
                evidence_refs=(assertion_ref,),
            )
            for assertion_id in job.acceptance_assertion_ids
        )
        is_recovery = run_number == 1
        return CapabilityReleaseObservation(
            run_id=(
                "controlled-recovery-01"
                if is_recovery
                else f"normal-e2e-{run_number - 1:02d}"
            ),
            capability_version=candidate.capability_version,
            extracted_tree_sha256="e" * 64,
            kind="recovery" if is_recovery else "normal",
            outcome="expected_failure_recovered" if is_recovery else "succeeded",
            assertion_results=assertions,
            recovery_id="controlled-recovery-01" if is_recovery else None,
            recovery_assertion_id=(
                job.acceptance_assertion_ids[0] if is_recovery else None
            ),
            private_holdout_evidence=(
                (
                    PrivateHoldoutEvidence(
                        holdout_id=job.private_holdout_refs[0].holdout_id,
                        assertion_id=job.acceptance_assertion_ids[0],
                        status="passed",
                        evidence_ref=self.store.put(
                            b'{"status":"passed"}',
                            "application/json",
                            namespace="holdout-evidence",
                        ),
                    ),
                )
                if is_recovery
                else ()
            ),
            build_lease_id="lease-build-1",
            tester_lease_id="lease-tester-1",
            quality_lease_id="lease-quality-1",
            occurred_at=NOW + timedelta(seconds=run_number),
        )


@pytest.mark.asyncio
async def test_evidence_issuer_records_recovery_then_three_distinct_successes(
    tmp_path: Path,
) -> None:
    job = _job()
    creation = _creation_job(job)
    result = _creation_result(job, creation)
    candidate = _candidate(job, creation, result)
    store = ContentAddressedArtifactStore(tmp_path / "artifacts")
    executor = _EvidenceExecutor(store=store)
    issuer = CaptainEvidenceIssuerAdapter(executor=executor, artifact_store=store)

    receipts = tuple(
        [
            await issuer.run(job, result, candidate, run_number)
            for run_number in range(1, 5)
        ]
    )
    assert all(isinstance(item, CaptainCapabilityReleaseReceipt) for item in receipts)
    accepted = tuple(item for item in receipts if item is not None)
    lifecycle = await issuer.lifecycle_blocks(job, accepted)

    assert accepted[0].record.kind == "recovery"
    assert accepted[0].record.outcome == "expected_failure_recovered"
    assert tuple(item.record.kind for item in accepted[1:]) == ("normal",) * 3
    assert len({item.record.run_id for item in accepted}) == 4
    assert len({item.reference.sha256 for item in accepted}) == 4
    assert all(item.record.correlation_id == job.correlation_id for item in accepted)
    assert all(item.record.factory_job_id == job.job_id for item in accepted)
    assert all(
        store.read_bytes(item.reference)
        == item.record.model_dump_json(by_alias=True).encode("utf-8")
        for item in accepted
    )
    assert tuple(block.phase for block in lifecycle) == (
        FactoryPhase.BUILD_PASSED,
        FactoryPhase.REAL_CASE_EVIDENCE,
        FactoryPhase.QUALITY_REVIEWED,
    )
    assert all(block.correlation_id == job.correlation_id for block in lifecycle)
    assert all(block.causation_id == job.event_id for block in lifecycle)

    restarted = CaptainEvidenceIssuerAdapter(executor=executor, artifact_store=store)
    replayed = await restarted.run(job, result, candidate, 4)
    assert replayed == accepted[3]
    assert executor.calls == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_evidence_issuer_rejects_unresolved_observation_artifacts(
    tmp_path: Path,
) -> None:
    job = _job()
    creation = _creation_job(job)
    result = _creation_result(job, creation)
    candidate = _candidate(job, creation, result)
    issuer_store = ContentAddressedArtifactStore(tmp_path / "issuer")
    foreign_store = ContentAddressedArtifactStore(tmp_path / "foreign")
    issuer = CaptainEvidenceIssuerAdapter(
        executor=_EvidenceExecutor(store=foreign_store),
        artifact_store=issuer_store,
    )

    with pytest.raises(ValueError, match="artifact content is unavailable"):
        await issuer.run(job, result, candidate, 1)
