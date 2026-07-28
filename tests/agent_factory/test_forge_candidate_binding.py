from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from agenten.agent_factory.business_benchmark_production_ports import (
    BusinessBenchmarkContentAddressedArtifactStore,
)
from agenten.agent_factory.candidate_evaluation import (
    CandidateEvaluationFactory,
    FactoryCandidateManifest,
    GatewayForgeCandidateProvider,
)
from agenten.agent_factory.contracts import FactoryPhase, FactoryRole
from agenten.agent_factory.evidence_store import FilesystemFactoryEvidenceStore
from agenten.agent_factory.forge_contracts import (
    CreationResultV1,
)
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_factory.n8n_tools import TypedN8nTool
from agenten.agent_factory.orchestration import FactoryDispatch, FactoryDispatchError
from agenten.agent_factory.state_machine import FactoryAction, FactoryActionKind
from minibook.swarm.contracts import CreationJobV1 as MinibookCreationJobV1
from minibook.swarm.pipeline_adapter import (
    ContentAddressedCreationArtifactPublisher,
    CreationExportBundle,
)
from tests.agent_factory.test_candidate_evaluation import _write_candidate_archive
from tests.agent_factory.test_state_machine import job_v3


class Blocks:
    def __init__(self) -> None:
        self.items = ()

    def blocks(self, job_id):
        assert not self.items or self.items[0].job_id == job_id
        return self.items


def _sealed_result(tmp_path: Path, factory_job):
    archive_path = tmp_path / "candidate.zip"
    team_ref, workflow_ref, input_ref, output_ref, original_source_ref = _write_candidate_archive(
        archive_path
    )
    store = BusinessBenchmarkContentAddressedArtifactStore(
        tmp_path / ".captain-cook" / "forge-cas"
    )
    candidate_template = FactoryCandidateManifest(
        candidate_id="support_triage_v1",
        source_archive_ref=original_source_ref,
        team_manifest={"reference": team_ref, "relative_path": "team_manifest.json"},
        workflow_artifacts=(
            {
                "reference": workflow_ref,
                "relative_path": "workflows/support_triage.json",
            },
        ),
        tool_schema_artifacts=(
            {
                "reference": input_ref,
                "relative_path": "schemas/support_triage.input.json",
            },
            {
                "reference": output_ref,
                "relative_path": "schemas/support_triage.output.json",
            },
        ),
        n8n_tools=(
            TypedN8nTool(
                name="support_triage",
                description="Route a support request.",
                input_schema_ref=input_ref.uri,
                output_schema_ref=output_ref.uri,
            ),
        ),
        build_command=("python", "-m", "compileall", "-q", "."),
        real_case_command=("python", "run_case.py"),
        timeout_seconds=10,
    )
    candidate_payload = candidate_template.model_dump(mode="json", by_alias=True)
    candidate_payload.pop("source_archive_ref")
    creation_job_id = uuid4()
    creation_job = MinibookCreationJobV1(
        creation_job_id=creation_job_id,
        factory_job_id=factory_job.job_id,
        correlation_id=factory_job.correlation_id,
        causation_id=factory_job.event_id,
        subject_version=factory_job.subject_version,
        attempt=1,
        idempotency_key="0" * 64,
        input_ref=factory_job.input_ref.model_dump(mode="json"),
        compiled_spec_ref=factory_job.compiled_spec_ref.model_dump(mode="json"),
        dependency_graph_ref=factory_job.dependency_graph_ref.model_dump(mode="json"),
        released_skill={
            "skill_id": "captain-factory-brief-codex",
            "version": 1,
            "content_ref": factory_job.input_ref.model_dump(mode="json"),
            "content_sha256": factory_job.input_ref.sha256,
        },
        public_assertion_ids=factory_job.acceptance_assertion_ids,
        deadline_at=factory_job.deadline_at,
    )
    receipt = ContentAddressedCreationArtifactPublisher(store).publish(
        creation_job,
        CreationExportBundle(
            source_archive=archive_path.read_bytes(),
            candidate_manifest=candidate_payload,
            skill_usage_receipt=b'{"schema":"hermes.skill-usage-receipt.v1"}',
        ),
    )
    candidate_ref = receipt.candidate_manifest_ref
    source_ref = receipt.source_archive_ref
    candidate = FactoryCandidateManifest.model_validate_json(
        store.read_bytes(
            type(factory_job.input_ref).model_validate(
                candidate_ref.model_dump(mode="json")
            )
        )
    )
    result = CreationResultV1(
        creation_job_id=creation_job_id,
        correlation_id=factory_job.correlation_id,
        subject_version=factory_job.subject_version,
        attempt=1,
        status="succeeded",
        package_manifest_ref=receipt.package_manifest_ref.model_dump(mode="json"),
        artifact_refs=(
            candidate_ref.model_dump(mode="json"),
            source_ref.model_dump(mode="json"),
        ),
        skill_usage_receipt_ref=receipt.skill_usage_receipt_ref.model_dump(mode="json"),
    )
    return store, candidate, result, source_ref


@pytest.mark.asyncio
async def test_gateway_forge_candidate_is_available_only_after_authoritative_code_block(
    tmp_path: Path,
) -> None:
    factory_job = job_v3(mode="demo")
    store, candidate, result, source_ref = _sealed_result(tmp_path, factory_job)
    blocks = Blocks()
    provider = GatewayForgeCandidateProvider(repository=blocks, artifacts=store)
    validator = CandidateEvaluationFactory(
        provider=provider,
        evidence_store=FilesystemFactoryEvidenceStore(tmp_path / "evidence"),
    )
    lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.TOOL_INTEGRATOR,
        attempt=1,
        workspace_ref="workspace://factory/support-triage",
        now=factory_job.occurred_at,
    )
    request = FactoryDispatch(
        job=factory_job,
        action=FactoryAction(
            kind=FactoryActionKind.EMIT_AGENT_CODE_EVIDENCE,
            attempt=1,
        ),
        role=FactoryRole.TOOL_INTEGRATOR,
        lease=lease,
    )

    with pytest.raises(FileNotFoundError, match="authoritative"):
        provider.candidate_for(factory_job)

    block = await validator.record_creation_result(request, result)
    assert block.phase is FactoryPhase.AGENT_CODE_CREATED
    assert provider.current_candidate_ref(factory_job, 1) is None

    blocks.items = (block,)
    resolved = provider.candidate_for(factory_job)

    assert resolved.candidate == candidate
    assert resolved.source_archive == store.local_path(
        type(factory_job.input_ref).model_validate(source_ref.model_dump(mode="json"))
    )
    assert provider.current_candidate_ref(factory_job, 1) == type(
        factory_job.input_ref
    ).model_validate(source_ref.model_dump(mode="json"))


@pytest.mark.asyncio
async def test_gateway_rejects_creation_result_when_package_bytes_are_unavailable(
    tmp_path: Path,
) -> None:
    factory_job = job_v3(mode="demo")
    store, _, result, _ = _sealed_result(tmp_path, factory_job)
    unavailable = result.package_manifest_ref.model_copy(
        update={"uri": "artifact://business-benchmark-production/missing/" + "0" * 64}
    )
    forged = result.model_copy(update={"package_manifest_ref": unavailable})
    provider = GatewayForgeCandidateProvider(repository=Blocks(), artifacts=store)
    validator = CandidateEvaluationFactory(
        provider=provider,
        evidence_store=FilesystemFactoryEvidenceStore(tmp_path / "evidence"),
    )
    lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.TOOL_INTEGRATOR,
        attempt=1,
        workspace_ref="workspace://factory/support-triage",
        now=factory_job.occurred_at,
    )

    with pytest.raises(FactoryDispatchError, match="package manifest"):
        await validator.record_creation_result(
            FactoryDispatch(
                job=factory_job,
                action=FactoryAction(
                    kind=FactoryActionKind.EMIT_AGENT_CODE_EVIDENCE,
                    attempt=1,
                ),
                role=FactoryRole.TOOL_INTEGRATOR,
                lease=lease,
            ),
            forged,
        )


def test_gateway_rejects_local_source_path_that_differs_from_verified_cas_bytes(
    tmp_path: Path,
) -> None:
    factory_job = job_v3(mode="demo")
    store, _, result, _ = _sealed_result(tmp_path, factory_job)
    divergent_path = tmp_path / "divergent-candidate.zip"
    divergent_path.write_bytes(b"not-the-verified-source-archive")

    class DivergentArtifactStore:
        def read_bytes(self, reference):
            return store.read_bytes(reference)

        def local_path(self, _reference):
            return divergent_path

    provider = GatewayForgeCandidateProvider(
        repository=Blocks(),
        artifacts=DivergentArtifactStore(),
    )

    with pytest.raises(FactoryDispatchError, match="local source archive"):
        provider.accept_creation_result(factory_job, result)
