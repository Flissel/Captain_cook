from __future__ import annotations

import hashlib
import io
import json
import stat
import zipfile
import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

import pytest

from agenten.agent_factory.contracts import AgentFactoryJobV2
from agenten.agent_factory.forge_contracts import CreationResultV1
from agenten.agent_factory.outcome_contracts import (
    CapabilityAssertionResult,
    CapabilityPackageManifestV1,
    CapabilityReleaseEvidenceV1,
    ForgeCapabilityPackageCandidateV1,
)
from agenten.agent_factory.outcome_validation import (
    CapabilitySandboxRequest,
    CapabilitySandboxResult,
    CapabilitySandboxTermination,
    CapabilityPackageValidationError,
    CapabilityPackageValidator,
)
from agenten.agent_factory.skill_evaluation import HermesSkillUsageReceipt
from agenten.agent_runtime.contracts import ArtifactRef, IntegrationIntent


FIXTURES = Path(__file__).parents[1] / "fixtures"


def _fixture(path: str) -> dict[str, object]:
    return json.loads((FIXTURES / path).read_text(encoding="utf-8"))


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return output.getvalue()


def _ref(content: bytes, media_type: str) -> dict[str, str]:
    digest = hashlib.sha256(content).hexdigest()
    return {
        "uri": f"artifact://validator-test/{digest}",
        "sha256": digest,
        "media_type": media_type,
    }


@dataclass
class RecordingContentStore:
    content_by_uri: dict[str, bytes]
    reads: list[str] = field(default_factory=list)

    async def read(self, reference: object) -> bytes:
        uri = str(getattr(reference, "uri"))
        self.reads.append(uri)
        return self.content_by_uri[uri]


@dataclass
class RecordingTrustedSandboxRunner:
    requests: list[CapabilitySandboxRequest] = field(default_factory=list)
    result_updates: dict[str, object] = field(default_factory=dict)
    termination_updates: dict[str, object] = field(default_factory=dict)
    cancelled_execution_ids: list[UUID] = field(default_factory=list)
    termination_waits: list[UUID] = field(default_factory=list)

    async def validate(self, request: CapabilitySandboxRequest) -> CapabilitySandboxResult:
        self.requests.append(request)
        result = CapabilitySandboxResult(
            execution_id=request.execution_id,
            request_digest=request.request_digest,
            status="passed",
            imported_modules=request.module_names,
            executed_test_paths=request.test_paths,
            sandbox_identity="sandbox://validator-test/isolated-python",
            process_identity=f"sandbox-handle://{request.execution_id}",
            process_identity_verified=True,
            extracted_tree_sha256=request.extracted_tree_sha256,
            workspace_was_read_only=True,
            network_was_disabled=True,
            resource_limits_were_enforced=True,
            process_tree_termination_capable=True,
        )
        return result.model_copy(update=self.result_updates)

    async def cancel(self, execution_id: UUID) -> None:
        self.cancelled_execution_ids.append(execution_id)

    async def await_termination(
        self,
        execution_id: UUID,
    ) -> CapabilitySandboxTermination:
        self.termination_waits.append(execution_id)
        request = next(
            item for item in self.requests if item.execution_id == execution_id
        )
        termination = CapabilitySandboxTermination(
            execution_id=execution_id,
            request_digest=request.request_digest,
            sandbox_identity="sandbox://validator-test/isolated-python",
            process_identity=f"sandbox-handle://{execution_id}",
            process_identity_verified=True,
            extracted_tree_sha256=request.extracted_tree_sha256,
            terminated=True,
            process_tree_terminated=True,
        )
        return termination.model_copy(update=self.termination_updates)


@dataclass
class CancellationResistantTrustedSandboxRunner(RecordingTrustedSandboxRunner):
    stopped: asyncio.Event = field(default_factory=asyncio.Event)
    local_cancellations: int = 0

    async def validate(self, request: CapabilitySandboxRequest) -> CapabilitySandboxResult:
        self.requests.append(request)
        while not self.stopped.is_set():
            try:
                await self.stopped.wait()
            except asyncio.CancelledError:
                self.local_cancellations += 1
        return await super().validate(request)

    async def cancel(self, execution_id: UUID) -> None:
        await super().cancel(execution_id)
        self.stopped.set()


@dataclass
class MutatingTrustedSandboxRunner(RecordingTrustedSandboxRunner):
    async def validate(self, request: CapabilitySandboxRequest) -> CapabilitySandboxResult:
        (request.workspace / "autogen" / "team.py").write_bytes(b"MUTATED = True\n")
        return await super().validate(request)


@dataclass(frozen=True)
class ValidationCase:
    job: AgentFactoryJobV2
    creation_result: CreationResultV1
    candidate: ForgeCapabilityPackageCandidateV1
    release_evidence_refs: tuple[ArtifactRef, ...]
    store: RecordingContentStore
    runner: RecordingTrustedSandboxRunner
    release_evidence: tuple[CapabilityReleaseEvidenceV1, ...]
    skill_receipt: HermesSkillUsageReceipt
    files: dict[str, bytes]


def _validation_case(
    *,
    autogen_source: bytes = (
        b"from autogen.helpers import HELPER\n"
        b"CAPABILITY_ID = 'customer_support_triage'\n"
    ),
    package_test: bytes = (
        b"from autogen.team import CAPABILITY_ID\n\n"
        b"def test_generated_team_identity():\n"
        b"    assert CAPABILITY_ID == 'customer_support_triage'\n"
    ),
    team_manifest_updates: dict[str, object] | None = None,
) -> ValidationCase:
    job_payload = _fixture("agent_factory/agent_factory_job.v2.json")
    job = AgentFactoryJobV2.model_validate(job_payload)

    team_manifest: dict[str, object] = {
        "schema": "autogen-team.v1",
        "capability_id": job.required_capability,
        "capability_version": 1,
        "autogen_modules": [
            "autogen/__init__.py",
            "autogen/helpers.py",
            "autogen/team.py",
        ],
        "test_paths": ["tests/test_support_triage.py"],
    }
    team_manifest.update(team_manifest_updates or {})
    files = {
        "team-manifest.json": json.dumps(
            team_manifest, sort_keys=True, separators=(",", ":")
        ).encode("utf-8"),
        "autogen/__init__.py": b"from .team import CAPABILITY_ID\n",
        "autogen/helpers.py": b"HELPER = 'sealed'\n",
        "autogen/team.py": autogen_source,
        "skills/support_triage/SKILL.md": b"# Support triage skill\n",
        "tests/test_support_triage.py": package_test,
        "evidence/summary.json": b'{"status":"candidate"}\n',
        "RUNBOOK.md": b"# Runbook\n",
    }
    archive = _zip_bytes(files)
    source_ref = _ref(archive, "application/zip")

    kind_by_path = {
        "team-manifest.json": "team_manifest",
        "autogen/__init__.py": "autogen_source",
        "autogen/helpers.py": "autogen_source",
        "autogen/team.py": "autogen_source",
        "skills/support_triage/SKILL.md": "skill",
        "tests/test_support_triage.py": "test",
        "evidence/summary.json": "evidence",
        "RUNBOOK.md": "runbook",
    }
    media_type_by_path = {
        "team-manifest.json": "application/json",
        "autogen/__init__.py": "text/x-python",
        "autogen/helpers.py": "text/x-python",
        "autogen/team.py": "text/x-python",
        "skills/support_triage/SKILL.md": "text/markdown",
        "tests/test_support_triage.py": "text/x-python",
        "evidence/summary.json": "application/json",
        "RUNBOOK.md": "text/markdown",
    }
    artifacts = [
        {
            "path": path,
            "kind": kind_by_path[path],
            "reference": _ref(content, media_type_by_path[path]),
        }
        for path, content in files.items()
    ]
    artifact_ref_by_path = {
        str(item["path"]): item["reference"] for item in artifacts
    }

    skill_detail = b'{"check":"released-skill-use","status":"passed"}'
    skill_detail_ref = _ref(skill_detail, "application/json")
    released_skill_ref = artifact_ref_by_path["skills/support_triage/SKILL.md"]
    skill_receipt = HermesSkillUsageReceipt.model_validate(
        {
            "schema": "hermes.skill-usage-receipt.v1",
            "receipt_id": "20000000-0000-4000-8000-000000000001",
            "request_id": "20000000-0000-4000-8000-000000000002",
            "job_id": str(job.job_id),
            "correlation_id": str(job.correlation_id),
            "lease_id": "factory-validation-lease-01",
            "occurred_at": "2026-07-21T12:05:00Z",
            "producer": "hermes",
            "released_skill": {
                "schema": "captain.released-hermes-skill.v1",
                "skill_id": "autogen-agent-factory",
                "version": 1,
                "capability": job.required_capability,
                "content_ref": released_skill_ref,
                "content_sha256": released_skill_ref["sha256"],
                "status": "released",
                "released_at": "2026-07-21T11:55:00Z",
                "producer": "captain",
            },
            "used_skill_id": "autogen-agent-factory",
            "used_skill_version": 1,
            "used_skill_sha256": released_skill_ref["sha256"],
            "commands": [
                {"command_id": "build", "max_seconds": 60},
                {"command_id": "test", "max_seconds": 60},
            ],
            "evidence_refs": [skill_detail_ref],
            "assertion_ids": list(job.acceptance_assertion_ids),
            "outcome": "passed",
        }
    )
    skill_receipt_bytes = skill_receipt.model_dump_json(by_alias=True).encode("utf-8")
    skill_receipt_ref = _ref(skill_receipt_bytes, "application/json")
    candidate_payload = {
        "schema": "forge.capability-package-candidate.v1",
        "capability_id": job.required_capability,
        "capability_version": 1,
        "factory_job_id": str(job.job_id),
        "creation_job_id": "11111111-1111-4111-8111-111111111111",
        "correlation_id": str(job.correlation_id),
        "subject_version": job.subject_version,
        "attempt": 1,
        "source_ref": source_ref,
        "team_manifest_ref": artifact_ref_by_path["team-manifest.json"],
        "artifacts": artifacts,
        "skill_usage_receipt_ref": skill_receipt_ref,
        "tool_gaps": [],
        "runbook_ref": artifact_ref_by_path["RUNBOOK.md"],
    }
    candidate = ForgeCapabilityPackageCandidateV1.model_validate(candidate_payload)
    candidate_bytes = candidate.model_dump_json(by_alias=True).encode("utf-8")
    candidate_ref = _ref(candidate_bytes, "application/json")
    tree_entries = sorted(
        (path, hashlib.sha256(content).hexdigest(), len(content))
        for path, content in files.items()
    )
    tree_sha256 = hashlib.sha256(
        json.dumps(tree_entries, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assertion_evidence_contents = tuple(
        json.dumps(
            {
                "schema": "captain.capability-assertion-evidence.v1",
                "run_id": (
                    "controlled-recovery-run-01"
                    if run_number == 1
                    else f"normal-e2e-run-{run_number - 1}"
                ),
                "assertion_id": job.acceptance_assertion_ids[0],
                "status": "passed",
                "producer": "captain",
            },
            separators=(",", ":"),
        ).encode("utf-8")
        for run_number in range(1, 5)
    )
    assertion_evidence_refs = tuple(
        _ref(content, "application/json") for content in assertion_evidence_contents
    )
    holdout_evidence_content = b'{"holdout":"private","status":"passed"}'
    holdout_evidence_ref = _ref(holdout_evidence_content, "application/json")
    release_evidence = tuple(
        CapabilityReleaseEvidenceV1.model_validate(
            {
                "schema": "captain.capability-release-evidence.v1",
                "run_id": (
                    "controlled-recovery-run-01"
                    if run_number == 1
                    else f"normal-e2e-run-{run_number - 1}"
                ),
                "run_number": run_number,
                "factory_job_id": str(job.job_id),
                "creation_job_id": str(candidate.creation_job_id),
                "correlation_id": str(job.correlation_id),
                "subject_version": job.subject_version,
                "attempt": candidate.attempt,
                "capability_id": candidate.capability_id,
                "capability_version": candidate.capability_version,
                "candidate_manifest_sha256": candidate_ref["sha256"],
                "package_archive_sha256": source_ref["sha256"],
                "extracted_tree_sha256": tree_sha256,
                "kind": "recovery" if run_number == 1 else "normal",
                "outcome": (
                    "expected_failure_recovered"
                    if run_number == 1
                    else "succeeded"
                ),
                "producer": "captain",
                "assertion_results": [
                    {
                        "assertion_id": job.acceptance_assertion_ids[0],
                        "status": "passed",
                        "integration_intent": "none",
                        "evidence_refs": [assertion_evidence_refs[run_number - 1]],
                    }
                ],
                "recovery_id": (
                    "controlled-recovery-01" if run_number == 1 else None
                ),
                "recovery_assertion_id": (
                    job.acceptance_assertion_ids[0] if run_number == 1 else None
                ),
                "private_holdout_evidence": (
                    [
                        {
                            "holdout_id": job.private_holdout_refs[0].holdout_id,
                            "assertion_id": job.acceptance_assertion_ids[0],
                            "status": "passed",
                            "evidence_ref": holdout_evidence_ref,
                        }
                    ]
                    if run_number == 1
                    else []
                ),
            }
        )
        for run_number in range(1, 5)
    )
    release_evidence_bytes = tuple(
        item.model_dump_json(by_alias=True).encode("utf-8")
        for item in release_evidence
    )
    release_evidence_refs = tuple(
        ArtifactRef.model_validate(_ref(content, "application/json"))
        for content in release_evidence_bytes
    )

    creation_payload = _fixture("contracts/minibook_creation_result.v1.json")
    creation_payload.update(
        {
            "creation_job_id": str(candidate.creation_job_id),
            "correlation_id": str(job.correlation_id),
            "subject_version": job.subject_version,
            "attempt": candidate.attempt,
            "status": "succeeded",
            "package_manifest_ref": candidate_ref,
            "artifact_refs": [source_ref],
            "evidence_refs": [],
            "tool_gaps": [],
            "skill_usage_receipt_ref": skill_receipt_ref,
            "failure": None,
        }
    )
    creation_result = CreationResultV1.model_validate(creation_payload)

    all_content: dict[str, bytes] = {
        str(source_ref["uri"]): archive,
        str(candidate_ref["uri"]): candidate_bytes,
        str(skill_receipt_ref["uri"]): skill_receipt_bytes,
        str(skill_detail_ref["uri"]): skill_detail,
        str(holdout_evidence_ref["uri"]): holdout_evidence_content,
    }
    all_content.update(
        {
            str(item["reference"]["uri"]): files[str(item["path"])]
            for item in artifacts
        }
    )
    all_content.update(
        {
            reference.uri: content
            for reference, content in zip(
                release_evidence_refs,
                release_evidence_bytes,
                strict=True,
            )
        }
    )
    all_content.update(
        {
            str(reference["uri"]): content
            for reference, content in zip(
                assertion_evidence_refs,
                assertion_evidence_contents,
                strict=True,
            )
        }
    )
    return ValidationCase(
        job=job,
        creation_result=creation_result,
        candidate=candidate,
        release_evidence_refs=release_evidence_refs,
        store=RecordingContentStore(all_content),
        runner=RecordingTrustedSandboxRunner(),
        release_evidence=release_evidence,
        skill_receipt=skill_receipt,
        files=files,
    )


def _validator(case: ValidationCase) -> CapabilityPackageValidator:
    return CapabilityPackageValidator(
        content_store=case.store,
        sandbox_runner=case.runner,
    )


@pytest.mark.asyncio
async def test_validator_independently_accepts_a_bound_digest_verified_package() -> None:
    case = _validation_case()

    validated = await _validator(case).validate(
        job=case.job,
        creation_result=case.creation_result,
        candidate=case.candidate,
        release_evidence_refs=case.release_evidence_refs,
    )

    assert isinstance(validated, CapabilityPackageManifestV1)
    assert validated is not case.candidate
    assert validated.schema_name == "captain.capability-package.v1"
    assert validated.creation_job_id == case.candidate.creation_job_id
    assert validated.release_evidence_refs == case.release_evidence_refs
    expected_uris = {
        reference.uri
        for reference in (
            case.creation_result.package_manifest_ref,
            *case.creation_result.artifact_refs,
            *case.creation_result.evidence_refs,
            case.creation_result.skill_usage_receipt_ref,
            case.candidate.source_ref,
            case.candidate.team_manifest_ref,
            *(item.reference for item in case.candidate.artifacts),
            *case.release_evidence_refs,
            case.candidate.skill_usage_receipt_ref,
            *case.skill_receipt.evidence_refs,
            *(
                reference
                for evidence in case.release_evidence
                for result in evidence.assertion_results
                for reference in result.evidence_refs
            ),
            *(
                holdout.evidence_ref
                for evidence in case.release_evidence
                for holdout in evidence.private_holdout_evidence
            ),
            case.candidate.runbook_ref,
        )
        if reference is not None
    }
    assert set(case.store.reads) == expected_uris
    request = case.runner.requests[0]
    assert request.python_path_root == request.workspace
    assert request.module_names == (
        "autogen",
        "autogen.helpers",
        "autogen.team",
    )
    assert request.test_paths == ("tests/test_support_triage.py",)
    assert request.workspace_access == "read_only"
    assert request.network_access == "disabled"
    assert request.package_archive_sha256 == case.candidate.source_ref.sha256
    assert request.extracted_tree_sha256 == case.release_evidence[0].extracted_tree_sha256
    assert request.max_processes >= 1
    assert request.max_memory_bytes >= 1
    assert request.kill_process_tree_on_cancel is True
    assert request.require_process_identity is True
    canonical_workspace = str(request.workspace.resolve()).replace("\\", "/").casefold()
    canonical_python_root = (
        str(request.python_path_root.resolve()).replace("\\", "/").casefold()
    )
    digest_payload = {
        "execution_id": str(request.execution_id),
        "process_identity": request.process_identity,
        "correlation_id": str(request.correlation_id),
        "workspace": canonical_workspace,
        "python_path_root": canonical_python_root,
        "package_sha256": request.package_archive_sha256,
        "tree_sha256": request.extracted_tree_sha256,
        "module_names": request.module_names,
        "test_paths": request.test_paths,
        "timeout_seconds": request.timeout_seconds,
        "workspace_access": request.workspace_access,
        "network_access": request.network_access,
        "max_memory_bytes": request.max_memory_bytes,
        "max_processes": request.max_processes,
        "kill_process_tree_on_cancel": request.kill_process_tree_on_cancel,
        "require_process_identity": request.require_process_identity,
    }
    assert request.request_digest == hashlib.sha256(
        json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


@pytest.mark.asyncio
async def test_validator_propagates_n8n_intent_and_requires_n8n_package_root() -> None:
    case = _validation_case()
    for index, evidence in enumerate(case.release_evidence):
        result = evidence.assertion_results[0]
        _replace_release_record(
            case,
            index,
            {
                "assertion_results": (
                    result.model_copy(
                        update={"integration_intent": IntegrationIntent.N8N}
                    ),
                )
            },
        )

    with pytest.raises(CapabilityPackageValidationError, match="n8n/"):
        await _validator(case).validate(
            job=case.job,
            creation_result=case.creation_result,
            candidate=case.candidate,
            release_evidence_refs=case.release_evidence_refs,
        )


@pytest.mark.asyncio
async def test_validator_fails_closed_without_an_explicit_trusted_sandbox() -> None:
    case = _validation_case()

    with pytest.raises(CapabilityPackageValidationError, match="trusted sandbox"):
        await CapabilityPackageValidator(content_store=case.store).validate(
            job=case.job,
            creation_result=case.creation_result,
        candidate=case.candidate,
        release_evidence_refs=case.release_evidence_refs,
        )

    assert case.store.reads == []


@pytest.mark.asyncio
async def test_validator_requires_trusted_runner_to_attest_the_exact_request() -> None:
    case = _validation_case()
    case.runner.result_updates = {"request_digest": "f" * 64}

    with pytest.raises(CapabilityPackageValidationError, match="request digest"):
        await _validator(case).validate(
            job=case.job,
            creation_result=case.creation_result,
        candidate=case.candidate,
        release_evidence_refs=case.release_evidence_refs,
        )


@pytest.mark.asyncio
async def test_validator_rejects_missing_sandbox_isolation_attestations() -> None:
    case = _validation_case()
    case.runner.result_updates = {"network_was_disabled": False}

    with pytest.raises(CapabilityPackageValidationError, match="isolation"):
        await _validator(case).validate(
            job=case.job,
            creation_result=case.creation_result,
        candidate=case.candidate,
        release_evidence_refs=case.release_evidence_refs,
        )


@pytest.mark.asyncio
async def test_validator_requires_verified_matching_process_identity() -> None:
    case = _validation_case()
    case.runner.result_updates = {"process_identity_verified": False}

    with pytest.raises(CapabilityPackageValidationError, match="process identity"):
        await _validator(case).validate(
            job=case.job,
            creation_result=case.creation_result,
            candidate=case.candidate,
            release_evidence_refs=case.release_evidence_refs,
        )


@pytest.mark.asyncio
async def test_timeout_rejects_termination_for_a_different_process_identity() -> None:
    case = _validation_case()
    runner = CancellationResistantTrustedSandboxRunner(
        termination_updates={"process_identity": "sandbox-handle://foreign"}
    )

    with pytest.raises(CapabilityPackageValidationError, match="cancellation"):
        await CapabilityPackageValidator(
            content_store=case.store,
            sandbox_runner=runner,
            command_timeout_seconds=1,
            termination_timeout_seconds=0.25,
        ).validate(
            job=case.job,
            creation_result=case.creation_result,
            candidate=case.candidate,
            release_evidence_refs=case.release_evidence_refs,
        )


@pytest.mark.asyncio
async def test_validator_bounds_and_cancels_a_hung_trusted_runner() -> None:
    case = _validation_case()
    runner = CancellationResistantTrustedSandboxRunner()
    validator = CapabilityPackageValidator(
        content_store=case.store,
        sandbox_runner=runner,
        command_timeout_seconds=1,
        termination_timeout_seconds=0.25,
    )

    with pytest.raises(CapabilityPackageValidationError, match="timed out"):
        await validator.validate(
            job=case.job,
            creation_result=case.creation_result,
        candidate=case.candidate,
        release_evidence_refs=case.release_evidence_refs,
        )

    execution_id = runner.requests[0].execution_id
    assert runner.cancelled_execution_ids == [execution_id]
    assert runner.termination_waits == [execution_id]


@pytest.mark.asyncio
async def test_validator_rehashes_the_effective_tree_after_sandbox_validation() -> None:
    case = _validation_case()
    runner = MutatingTrustedSandboxRunner()

    with pytest.raises(CapabilityPackageValidationError, match="effective extracted tree"):
        await CapabilityPackageValidator(
            content_store=case.store,
            sandbox_runner=runner,
        ).validate(
            job=case.job,
            creation_result=case.creation_result,
        candidate=case.candidate,
        release_evidence_refs=case.release_evidence_refs,
        )


@pytest.mark.asyncio
async def test_validator_does_not_trust_forge_succeeded_when_content_digest_is_wrong() -> None:
    case = _validation_case()
    case.store.content_by_uri[case.candidate.source_ref.uri] += b"tampered"

    with pytest.raises(CapabilityPackageValidationError, match="digest"):
        await _validator(case).validate(
            job=case.job,
            creation_result=case.creation_result,
        candidate=case.candidate,
        release_evidence_refs=case.release_evidence_refs,
        )


@pytest.mark.asyncio
async def test_validator_rejects_nested_evidence_uri_with_conflicting_digest() -> None:
    case = _validation_case()
    result = case.release_evidence[2].assertion_results[0]
    nested_ref = result.evidence_refs[0]
    conflicting_ref = nested_ref.model_copy(
        update={"uri": case.candidate.source_ref.uri}
    )
    _replace_release_record(
        case,
        2,
        {
            "assertion_results": (
                result.model_copy(update={"evidence_refs": (conflicting_ref,)}),
            )
        },
    )

    with pytest.raises(CapabilityPackageValidationError, match="conflicting"):
        await _validator(case).validate(
            job=case.job,
            creation_result=case.creation_result,
        candidate=case.candidate,
        release_evidence_refs=case.release_evidence_refs,
        )


@pytest.mark.asyncio
async def test_validator_rejects_semanticless_release_evidence_blobs() -> None:
    case = _validation_case()
    _replace_release_blob(case, 1, b'{"status":"succeeded"}')

    with pytest.raises(CapabilityPackageValidationError, match="typed release evidence"):
        await _validator(case).validate(
            job=case.job,
            creation_result=case.creation_result,
        candidate=case.candidate,
        release_evidence_refs=case.release_evidence_refs,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("updates", "message"),
    (
        (
            {"factory_job_id": UUID("99999999-9999-4999-8999-999999999999")},
            "release evidence factory job",
        ),
        (
            {"correlation_id": UUID("99999999-9999-4999-8999-999999999999")},
            "release evidence correlation",
        ),
        ({"subject_version": 2}, "release evidence subject version"),
        (
            {
                "assertion_results": (
                    CapabilityAssertionResult(
                        assertion_id="foreign-assertion",
                        status="passed",
                        integration_intent=IntegrationIntent.NONE,
                        evidence_refs=(
                            ArtifactRef(
                                uri="artifact://validator-test/foreign-assertion",
                                sha256="f" * 64,
                                media_type="application/json",
                            ),
                        ),
                    ),
                )
            },
            "release evidence assertions",
        ),
    ),
)
async def test_validator_binds_typed_release_evidence_to_captain_authority(
    updates: dict[str, object],
    message: str,
) -> None:
    case = _validation_case()
    _replace_release_record(case, 1, updates)

    with pytest.raises(CapabilityPackageValidationError, match=message):
        await _validator(case).validate(
            job=case.job,
            creation_result=case.creation_result,
        candidate=case.candidate,
        release_evidence_refs=case.release_evidence_refs,
        )


@pytest.mark.asyncio
async def test_validator_requires_exactly_three_distinct_successes_after_recovery() -> None:
    case = _validation_case()
    _append_release_record(case)

    with pytest.raises(CapabilityPackageValidationError, match="exactly three"):
        await _validator(case).validate(
            job=case.job,
            creation_result=case.creation_result,
        candidate=case.candidate,
        release_evidence_refs=case.release_evidence_refs,
        )

    case = _validation_case()
    _replace_release_record(
        case,
        2,
        {"run_id": case.release_evidence[1].run_id},
    )
    with pytest.raises(CapabilityPackageValidationError, match="run IDs"):
        await _validator(case).validate(
            job=case.job,
            creation_result=case.creation_result,
        candidate=case.candidate,
        release_evidence_refs=case.release_evidence_refs,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("updates", "message"),
    (
        ({"private_holdout_evidence": ()}, "private holdout"),
        ({"outcome": "failed"}, "succeeded"),
    ),
)
async def test_validator_binds_recovery_holdout_and_success_semantics(
    updates: dict[str, object],
    message: str,
) -> None:
    case = _validation_case()
    index = 0 if "outcome" not in updates else 1
    _replace_release_record(case, index, updates)

    with pytest.raises(CapabilityPackageValidationError, match=message):
        await _validator(case).validate(
            job=case.job,
            creation_result=case.creation_result,
        candidate=case.candidate,
        release_evidence_refs=case.release_evidence_refs,
        )


@pytest.mark.asyncio
async def test_validator_never_derives_a_passed_holdout_or_reuses_recovery_ref() -> None:
    case = _validation_case()
    holdout = case.release_evidence[0].private_holdout_evidence[0]
    _replace_release_record(
        case,
        0,
        {
            "private_holdout_evidence": (
                holdout.model_copy(update={"status": "failed"}),
            )
        },
    )

    with pytest.raises(CapabilityPackageValidationError, match="holdout.*passed"):
        await _validator(case).validate(
            job=case.job,
            creation_result=case.creation_result,
            candidate=case.candidate,
            release_evidence_refs=case.release_evidence_refs,
        )


@pytest.mark.asyncio
async def test_validator_rejects_assertion_evidence_swapped_between_runs() -> None:
    case = _validation_case()
    first_result = case.release_evidence[0].assertion_results[0]
    second_result = case.release_evidence[1].assertion_results[0]
    _replace_release_record(
        case,
        0,
        {
            "assertion_results": (
                first_result.model_copy(
                    update={"evidence_refs": second_result.evidence_refs}
                ),
            )
        },
    )
    _replace_release_record(
        case,
        1,
        {
            "assertion_results": (
                second_result.model_copy(
                    update={"evidence_refs": first_result.evidence_refs}
                ),
            )
        },
    )

    with pytest.raises(CapabilityPackageValidationError, match="assertion evidence"):
        await _validator(case).validate(
            job=case.job,
            creation_result=case.creation_result,
            candidate=case.candidate,
            release_evidence_refs=case.release_evidence_refs,
        )


@pytest.mark.asyncio
async def test_validator_parses_and_binds_the_skill_usage_receipt() -> None:
    case = _validation_case()
    _replace_skill_receipt(
        case,
        case.skill_receipt.model_copy(
            update={"job_id": UUID("99999999-9999-4999-8999-999999999999")}
        ),
    )

    with pytest.raises(CapabilityPackageValidationError, match="skill receipt job"):
        await _validator(case).validate(
            job=case.job,
            creation_result=case.creation_result,
        candidate=case.candidate,
        release_evidence_refs=case.release_evidence_refs,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "updates", "message"),
    (
        ("candidate", {"factory_job_id": "99999999-9999-4999-8999-999999999999"}, "factory job"),
        ("candidate", {"correlation_id": "99999999-9999-4999-8999-999999999999"}, "correlation"),
        ("candidate", {"subject_version": 2}, "subject version"),
        ("candidate", {"capability_id": "foreign_capability"}, "capability"),
        ("candidate", {"attempt": 2}, "attempt"),
        ("creation", {"creation_job_id": "99999999-9999-4999-8999-999999999999"}, "creation job"),
        ("creation", {"correlation_id": "99999999-9999-4999-8999-999999999999"}, "correlation"),
        ("creation", {"subject_version": 2}, "subject version"),
    ),
)
async def test_validator_rejects_foreign_job_relationships(
    target: str,
    updates: dict[str, object],
    message: str,
) -> None:
    case = _validation_case()
    candidate = case.candidate
    creation_result = case.creation_result
    if target == "candidate":
        candidate = candidate.model_copy(update=updates)
    else:
        creation_result = creation_result.model_copy(update=updates)

    with pytest.raises(CapabilityPackageValidationError, match=message):
        await _validator(case).validate(
            job=case.job,
            creation_result=creation_result,
            candidate=candidate,
            release_evidence_refs=case.release_evidence_refs,
        )


@pytest.mark.asyncio
async def test_validator_binds_release_evidence_to_attempt_and_candidate_digest() -> None:
    case = _validation_case()
    _replace_release_record(case, 1, {"attempt": 2})

    with pytest.raises(CapabilityPackageValidationError, match="attempt"):
        await _validator(case).validate(
            job=case.job,
            creation_result=case.creation_result,
            candidate=case.candidate,
            release_evidence_refs=case.release_evidence_refs,
        )

    case = _validation_case()
    _replace_release_record(case, 1, {"candidate_manifest_sha256": "f" * 64})
    with pytest.raises(CapabilityPackageValidationError, match="candidate digest"):
        await _validator(case).validate(
            job=case.job,
            creation_result=case.creation_result,
            candidate=case.candidate,
            release_evidence_refs=case.release_evidence_refs,
        )


@pytest.mark.asyncio
async def test_validator_requires_creation_skill_receipt_and_source_archive_bindings() -> None:
    case = _validation_case()
    creation_result = case.creation_result.model_copy(
        update={"artifact_refs": (), "skill_usage_receipt_ref": None}
    )

    with pytest.raises(CapabilityPackageValidationError, match="source archive|skill usage"):
        await _validator(case).validate(
            job=case.job,
            creation_result=creation_result,
        candidate=case.candidate,
        release_evidence_refs=case.release_evidence_refs,
        )


@pytest.mark.asyncio
async def test_validator_rejects_manifest_bytes_that_do_not_describe_the_package() -> None:
    case = _validation_case()
    manifest_ref = case.creation_result.package_manifest_ref
    assert manifest_ref is not None
    mismatched = case.candidate.model_copy(update={"capability_version": 2})
    mismatched_bytes = mismatched.model_dump_json(by_alias=True).encode("utf-8")
    mismatched_ref = manifest_ref.model_copy(
        update={"sha256": hashlib.sha256(mismatched_bytes).hexdigest()}
    )
    case.store.content_by_uri[mismatched_ref.uri] = mismatched_bytes
    creation_result = case.creation_result.model_copy(
        update={"package_manifest_ref": mismatched_ref}
    )

    with pytest.raises(CapabilityPackageValidationError, match="manifest.*candidate"):
        await _validator(case).validate(
            job=case.job,
            creation_result=creation_result,
        candidate=case.candidate,
        release_evidence_refs=case.release_evidence_refs,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe_entry",
    (
        "../escaped.py",
        "/absolute.py",
        "C:/escaped.py",
        "autogen/team.py:payload",
        "CON.py",
        "autogen/aux.txt",
        "autogen/trailing-dot.",
        "autogen/trailing-space ",
    ),
)
async def test_validator_rejects_archive_path_traversal(unsafe_entry: str) -> None:
    case = _validation_case()
    unsafe_files = {**case.files, unsafe_entry: b"ESCAPED = True\n"}
    await _replace_source_archive(case, _zip_bytes(unsafe_files))

    with pytest.raises(CapabilityPackageValidationError, match="unsafe path"):
        await _validator(case).validate(
            job=case.job,
            creation_result=case.creation_result,
        candidate=case.candidate,
        release_evidence_refs=case.release_evidence_refs,
        )


@pytest.mark.asyncio
async def test_validator_rejects_archive_symlinks() -> None:
    case = _validation_case()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for path, content in case.files.items():
            archive.writestr(path, content)
        link = zipfile.ZipInfo("autogen/escape.py")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "../../outside.py")
    await _replace_source_archive(case, output.getvalue())

    with pytest.raises(CapabilityPackageValidationError, match="symlink"):
        await _validator(case).validate(
            job=case.job,
            creation_result=case.creation_result,
        candidate=case.candidate,
        release_evidence_refs=case.release_evidence_refs,
        )


@pytest.mark.asyncio
async def test_validator_rejects_undeclared_archive_members() -> None:
    case = _validation_case()
    archive = _zip_bytes({**case.files, "autogen/undeclared.py": b"UNSEALED = True\n"})
    await _replace_source_archive(case, archive)

    with pytest.raises(CapabilityPackageValidationError, match="declared artifacts"):
        await _validator(case).validate(
            job=case.job,
            creation_result=case.creation_result,
        candidate=case.candidate,
        release_evidence_refs=case.release_evidence_refs,
        )


@pytest.mark.asyncio
async def test_validator_rejects_case_insensitive_archive_collisions() -> None:
    case = _validation_case()
    archive = _zip_bytes(
        {**case.files, "AUTOGEN/TEAM.PY": b"COLLISION = True\n"}
    )
    await _replace_source_archive(case, archive)

    with pytest.raises(CapabilityPackageValidationError, match="case-insensitive"):
        await _validator(case).validate(
            job=case.job,
            creation_result=case.creation_result,
        candidate=case.candidate,
        release_evidence_refs=case.release_evidence_refs,
        )


@pytest.mark.asyncio
async def test_validator_rejects_team_manifest_schema_or_capability_mismatch() -> None:
    case = _validation_case(team_manifest_updates={"capability_id": "foreign_capability"})

    with pytest.raises(CapabilityPackageValidationError, match="team manifest capability"):
        await _validator(case).validate(
            job=case.job,
            creation_result=case.creation_result,
        candidate=case.candidate,
        release_evidence_refs=case.release_evidence_refs,
        )


@pytest.mark.asyncio
async def test_validator_imports_autogen_code_in_the_fresh_workspace() -> None:
    case = _validation_case()
    case.runner.result_updates = {"status": "failed", "failure_stage": "import"}

    with pytest.raises(CapabilityPackageValidationError, match="sandbox import"):
        await _validator(case).validate(
            job=case.job,
            creation_result=case.creation_result,
        candidate=case.candidate,
        release_evidence_refs=case.release_evidence_refs,
        )


@pytest.mark.asyncio
async def test_validator_runs_only_manifest_allowlisted_package_tests() -> None:
    case = _validation_case()
    case.runner.result_updates = {"status": "failed", "failure_stage": "test"}

    with pytest.raises(CapabilityPackageValidationError, match="sandbox test"):
        await _validator(case).validate(
            job=case.job,
            creation_result=case.creation_result,
        candidate=case.candidate,
        release_evidence_refs=case.release_evidence_refs,
        )


@pytest.mark.asyncio
async def test_validator_rejects_test_paths_not_exactly_declared_as_test_artifacts() -> None:
    case = _validation_case(
        team_manifest_updates={"test_paths": ["tests/not-sealed.py"]}
    )

    with pytest.raises(CapabilityPackageValidationError, match="allowlisted test paths"):
        await _validator(case).validate(
            job=case.job,
            creation_result=case.creation_result,
        candidate=case.candidate,
        release_evidence_refs=case.release_evidence_refs,
        )


def _replace_release_record(
    case: ValidationCase,
    index: int,
    updates: dict[str, object],
) -> None:
    evidence = case.release_evidence[index].model_copy(update=updates)
    content = evidence.model_dump_json(by_alias=True).encode("utf-8")
    old_ref = case.release_evidence_refs[index]
    reference = old_ref.__class__.model_validate(_ref(content, "application/json"))
    case.store.content_by_uri[reference.uri] = content
    release_refs = list(case.release_evidence_refs)
    release_refs[index] = reference
    object.__setattr__(case, "release_evidence_refs", tuple(release_refs))
    records = list(case.release_evidence)
    records[index] = evidence
    object.__setattr__(case, "release_evidence", tuple(records))


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ("whitespace", "key_order", "escaped_z"))
async def test_validator_rejects_noncanonical_release_evidence_json(variant: str) -> None:
    case = _validation_case()
    payload = case.release_evidence[0].model_dump(mode="json", by_alias=True)
    if variant == "whitespace":
        content = json.dumps(payload, indent=2).encode("utf-8")
    elif variant == "key_order":
        content = json.dumps(
            dict(reversed(tuple(payload.items()))),
            separators=(",", ":"),
        ).encode("utf-8")
    else:
        payload["run_id"] = f'{payload["run_id"]}-Z'
        content = json.dumps(payload, separators=(",", ":")).replace(
            "-Z",
            r"-\u005a",
            1,
        ).encode("utf-8")
    _replace_release_blob(case, 0, content)

    with pytest.raises(CapabilityPackageValidationError, match="canonical"):
        await _validator(case).validate(
            job=case.job,
            creation_result=case.creation_result,
            candidate=case.candidate,
            release_evidence_refs=case.release_evidence_refs,
        )


def _replace_release_blob(case: ValidationCase, index: int, content: bytes) -> None:
    old_ref = case.release_evidence_refs[index]
    reference = old_ref.__class__.model_validate(_ref(content, "application/json"))
    case.store.content_by_uri[reference.uri] = content
    release_refs = list(case.release_evidence_refs)
    release_refs[index] = reference
    object.__setattr__(case, "release_evidence_refs", tuple(release_refs))


def _append_release_record(case: ValidationCase) -> None:
    evidence = case.release_evidence[-1].model_copy(
        update={"run_id": "normal-e2e-run-04", "run_number": 5}
    )
    content = evidence.model_dump_json(by_alias=True).encode("utf-8")
    reference = case.release_evidence_refs[-1].__class__.model_validate(
        _ref(content, "application/json")
    )
    case.store.content_by_uri[reference.uri] = content
    object.__setattr__(
        case,
        "release_evidence_refs",
        (*case.release_evidence_refs, reference),
    )
    object.__setattr__(
        case,
        "release_evidence",
        (*case.release_evidence, evidence),
    )


def _replace_skill_receipt(
    case: ValidationCase,
    receipt: HermesSkillUsageReceipt,
) -> None:
    content = receipt.model_dump_json(by_alias=True).encode("utf-8")
    reference = case.candidate.skill_usage_receipt_ref.__class__.model_validate(
        _ref(content, "application/json")
    )
    case.store.content_by_uri[reference.uri] = content
    object.__setattr__(
        case,
        "candidate",
        case.candidate.model_copy(update={"skill_usage_receipt_ref": reference}),
    )
    creation_reference = case.creation_result.skill_usage_receipt_ref
    assert creation_reference is not None
    object.__setattr__(
        case,
        "creation_result",
        case.creation_result.model_copy(
            update={
                "skill_usage_receipt_ref": creation_reference.__class__.model_validate(
                    _ref(content, "application/json")
                )
            }
        ),
    )
    object.__setattr__(case, "skill_receipt", receipt)
    _refresh_sealed_candidate(case)
    _rebind_release_digests(case)


def _refresh_sealed_candidate(case: ValidationCase) -> None:
    package_bytes = case.candidate.model_dump_json(by_alias=True).encode("utf-8")
    manifest_ref = case.creation_result.package_manifest_ref
    assert manifest_ref is not None
    updated_manifest_ref = manifest_ref.__class__.model_validate(
        _ref(package_bytes, "application/json")
    )
    object.__setattr__(
        case,
        "creation_result",
        case.creation_result.model_copy(
            update={"package_manifest_ref": updated_manifest_ref}
        ),
    )
    case.store.content_by_uri[updated_manifest_ref.uri] = package_bytes


async def _replace_source_archive(case: ValidationCase, archive: bytes) -> None:
    """Mutate only test models/store to preserve a valid digest binding."""

    source_ref = case.candidate.source_ref.model_copy(
        update={"sha256": hashlib.sha256(archive).hexdigest()}
    )
    object.__setattr__(
        case,
        "candidate",
        case.candidate.model_copy(update={"source_ref": source_ref}),
    )
    creation_source_ref = case.creation_result.artifact_refs[0].model_copy(
        update={"sha256": source_ref.sha256}
    )
    object.__setattr__(
        case,
        "creation_result",
        case.creation_result.model_copy(update={"artifact_refs": (creation_source_ref,)}),
    )
    case.store.content_by_uri[source_ref.uri] = archive
    _refresh_sealed_candidate(case)
    _rebind_release_digests(case)


def _rebind_release_digests(case: ValidationCase) -> None:
    manifest_ref = case.creation_result.package_manifest_ref
    assert manifest_ref is not None
    for index in range(len(case.release_evidence)):
        _replace_release_record(
            case,
            index,
            {
                "candidate_manifest_sha256": manifest_ref.sha256,
                "package_archive_sha256": case.candidate.source_ref.sha256,
            },
        )
